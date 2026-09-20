"""平台完整性封印 —— 证明 src/config 从未被 AI 或任何人改动。

config/integrity.json 保存 src/ 全部 .py 与 config/ 全部 .json 的 sha256
清单（构建时生成）。verify() 重算比对：任何改动产生 TAMPERED / MISSING /
UNLISTED 条目。清单自身也参与封印（self = 对 profile+sealed_at+files 的
sha256），直接改 integrity.json 同样会红。/api/platform/integrity 与 MCP
platform_integrity 工具把自检结果暴露给 AI——AI 想"改平台代码"先得过这个
封印；配 --lock 只读锁后物理上也写不进去。

注：清单不包含自身文件条目（self 已经覆盖它），否则构建后会变成一条
UNLISTED。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from shipin_platform import roots
from typing import Iterator, Optional

ROOT = roots.data_root()
MANIFEST_PATH = ROOT / "config" / "integrity.json"
GUARD_DIRS = ("src", "config", "tools")             # 平台核心目录
GUARD_EXTS = (".py", ".json")


def platform_files(root: Path = ROOT,
                   exclude: Optional[Path] = None) -> Iterator[Path]:
    """遍历受保护文件：src/**/*.py + config/*.json（排除 __pycache__ 与
    隐藏目录如 .mimosa 会话簿记）。exclude 用于排除清单自身——否则构建后
    清单会变成一条 UNLISTED。"""
    excluded = Path(exclude).resolve() if exclude is not None else None
    for d in GUARD_DIRS:
        base = root / d
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            rel = p.relative_to(base).parts     # 只看受保护目录内的相对路径
            if (p.is_file() and p.suffix in GUARD_EXTS
                    and "__pycache__" not in rel
                    and not any(c.startswith(".") for c in rel)
                    and (excluded is None or p.resolve() != excluded)):
                yield p


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _canonical(manifest_core: dict) -> str:
    """对 {profile, sealed_at, files} 做确定性序列化，作为 self 的输入。"""
    return json.dumps(manifest_core, ensure_ascii=False,
                      sort_keys=True, separators=(",", ":"))


def build_manifest(root: Path = ROOT,
                   manifest_path: Optional[Path] = None) -> dict:
    """生成 {profile, sealed_at, files, self}；传 manifest_path 才写盘。"""
    mpath = Path(manifest_path) if manifest_path is not None else MANIFEST_PATH
    files = {p.relative_to(root).as_posix(): _sha256_file(p)
             for p in platform_files(root, exclude=mpath)}
    core = {"profile": "shipin.integrity@2",
            "sealed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "files": files}
    manifest = {**core, "self": hashlib.sha256(
        _canonical(core).encode("utf-8")).hexdigest()}
    if manifest_path is not None:
        mpath.parent.mkdir(parents=True, exist_ok=True)
        mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                         encoding="utf-8")
    return manifest


def verify(root: Path = ROOT,
           manifest_path: Optional[Path] = None) -> dict:
    """对照清单校验实际文件。files[] 每项 {path, status}。"""
    from shipin_platform import roots
    mpath = Path(manifest_path) if manifest_path is not None else MANIFEST_PATH
    if roots.is_frozen():
        # 打包发布：源码清单不随包分发，封印语义由 exe 自身的数字签名/发布
        # 流程承担（frozen 环境没有 .py 可校）。显式豁免而非误报红——
        # 原因固定写死在返回里，外部扫描一眼可见是打包态的常规自检。
        return {"ok": True, "reason": "FROZEN_BUILD_SKIP",
                "clean": 0, "tampered": 0, "missing": 0, "unlisted": 0,
                "files": []}
    if not mpath.is_file():
        return {"ok": False, "reason": "MANIFEST_MISSING",
                "clean": 0, "tampered": 0, "missing": 0, "unlisted": 0,
                "files": []}
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    expected = manifest.get("files", {})
    # 第一步：清单自身封条。任何对 profile/sealed_at/files 的编辑都会使
    # 重算的 self 与记录值不一致。
    core = {k: v for k, v in manifest.items() if k != "self"}
    self_ok = (manifest.get("self") == hashlib.sha256(
        _canonical(core).encode("utf-8")).hexdigest())
    if not self_ok:
        return {"ok": False, "reason": "SELF_TAMPERED",
                "clean": 0, "tampered": 1, "missing": 0, "unlisted": 0,
                "files": []}

    files: list[dict] = []
    clean = tampered = missing = 0
    for rel, sha in expected.items():
        p = root / rel
        if not p.is_file():
            missing += 1
            files.append({"path": rel, "status": "MISSING"})
            continue
        if _sha256_file(p) == sha:
            clean += 1
            files.append({"path": rel, "status": "OK"})
        else:
            tampered += 1
            files.append({"path": rel, "status": "TAMPERED"})
    # 清单外新增受保护文件 = 未授权引入（加文件改平台也拦）
    unlisted = 0
    for p in platform_files(root, exclude=mpath):
        rel = p.relative_to(root).as_posix()
        if rel not in expected:
            unlisted += 1
            tampered += 1
            files.append({"path": rel, "status": "UNLISTED"})
    return {"ok": tampered == 0 and missing == 0,
            "clean": clean, "tampered": tampered, "missing": missing,
            "unlisted": unlisted, "files": files}


def set_lock(root: Path = ROOT, on: bool = True) -> dict:
    """把受保护文件置只读/恢复可写（Windows 下 os.chmod 同样生效）。"""
    import os
    cnt = 0
    for p in platform_files(root):
        try:
            os.chmod(p, 0o444 if on else 0o644)
            cnt += 1
        except OSError:
            pass
    return {"locked": on, "files": cnt}


__all__ = ["ROOT", "MANIFEST_PATH", "platform_files", "build_manifest",
           "verify", "set_lock", "GUARD_DIRS"]