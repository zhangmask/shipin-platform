"""变体/重跑(variant re-run) — 对标 hypit「同一制作源换 runtime 重执行」。

hypit 的执行模型:一个制作源(runtime profile + dataRoot 素材池)换配置
重新执行,产出独立结果(仓库中不存在 swap-* 包)。本模块等价落点:

    derive_variant(base_id, variant_id, overrides)
        base 的 brief 为制作源;白名单参数覆盖(换 runtime);独立产物目录
        data/projects/<variant_id>(换 dataRoot);阶段状态与产物各自隔离。

- 源数据:brief.json(白名单合并)、storyboard.json(独立拷贝)——变体从源
  重放 text/generate/assemble 三阶段;
- 素材池:manifest.json 深拷贝、媒体路径保持指向基准(dataRoot 共享语义),
  命中缓存即跳过重生成;
- 覆盖白名单:只允许影响创作语义的顶层键,拒绝任意键注入。

安全:variant_id 走严格字符白名单 + resolve 越界二次校验(防路径穿越);
base 只读(派生绝不改写基准文件);不发任何网络请求。
"""
from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path
from typing import Optional

from shipin_platform.orchestration.pipeline_runner import PROJECTS_DIR
from shipin_platform.orchestration.stage_store import ProjectStageStore

# brief 覆盖白名单:仅允许调整创作语义,不允许注入其他字段
VARIANT_OVERRIDE_KEYS = frozenset({
    "category", "duration_sec", "style_anchor", "pacing",
    "brand_name", "slogan", "product_info",
})

# 源 JSON 清单:派生时拷贝/合并(json 形态稳定;媒体始终引用 base dataRoot)
_SOURCE_FILES = ("brief.json", "storyboard.json")


class VariantError(ValueError):
    """变体派生失败(归类错误:参数/白名单/重名/源缺失)。"""


# ---------------------------------------------------------------------------
# id 校验与目录定位
# ---------------------------------------------------------------------------

def _check_variant_id(variant_id: str) -> str:
    v = (variant_id or "").strip()
    if not v:
        raise VariantError("variant_id 不能为空")
    if len(v) > 64:
        raise VariantError(f"variant_id 过长(<=64): {v!r}")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", v):
        raise VariantError(
            "variant_id 只允许字母/数字/中划线/下划线且不能以 - 开头")
    if ".." in v:
        raise VariantError(f"variant_id 含非法字符: {v!r}")
    return v


def _variant_dir(variant_id: str, projects_dir: Path = PROJECTS_DIR) -> Path:
    d = (projects_dir / variant_id).resolve()
    root = projects_dir.resolve()
    try:
        d.relative_to(root)
    except ValueError:
        raise VariantError(f"variant_id 越界 projects 目录: {variant_id!r}")
    return d


# ---------------------------------------------------------------------------
# 参数校验与合并
# ---------------------------------------------------------------------------

def _merge_brief(base_brief: dict, overrides: dict) -> dict:
    """白名单校验 + 类型合理化 + 覆盖合并(基 brief 其余字段原样保留)。"""
    merged = json.loads(json.dumps(base_brief, ensure_ascii=False))
    for k, v in (overrides or {}).items():
        if k not in VARIANT_OVERRIDE_KEYS:
            raise VariantError(
                f"覆盖键 {k!r} 不在白名单: {sorted(VARIANT_OVERRIDE_KEYS)}")
        if k == "duration_sec":
            if not isinstance(v, int) or v <= 0:
                raise VariantError("duration_sec 必须为正整数")
        elif k == "category":
            if not isinstance(v, str):
                raise VariantError("category 必须为字符串")
        elif v is None:
            continue
        elif not isinstance(v, (str, int, float, bool)):
            raise VariantError(
                f"覆盖键 {k!r} 值类型不支持: {type(v).__name__}")
        # category 走模板参数,不进 brief 正文(避免未注册品类改变字段含义)
        if k != "category":
            merged[k] = v
    return merged


# ---------------------------------------------------------------------------
# derive / run / status
# ---------------------------------------------------------------------------

def derive_variant(
    base_id: str,
    variant_id: str,
    overrides: Optional[dict] = None,
    projects_dir: Path = PROJECTS_DIR,
    force: bool = False,
) -> dict:
    """从 base 派生一个变体(独立 dataRoot + 独立档案)。

    base 必须已具备 brief.json + storyboard.json(完整成片源)。派生只读
    base;变体目录已存在且未 force → VariantError。
    """
    base_id = _check_variant_id(base_id)
    variant_id = _check_variant_id(variant_id)
    if variant_id == base_id:
        raise VariantError(f"变体 id 不能与基准相同: {variant_id!r}")
    base_dir = _variant_dir(base_id, projects_dir)
    if not base_dir.is_dir():
        raise VariantError(f"基准项目不存在: {base_id}")
    for name in _SOURCE_FILES:
        if not (base_dir / name).is_file():
            raise VariantError(
                f"基准项目缺少源文件 {name}(不是完整成片源): {base_id}")

    brief = json.loads(
        (base_dir / "brief.json").read_text(encoding="utf-8"))
    if not isinstance(brief, dict):
        raise VariantError("基准 brief.json 格式非法(应为对象)")
    merged = _merge_brief(brief, overrides or {})

    vd = _variant_dir(variant_id, projects_dir)
    if vd.exists() and not force:
        raise VariantError(f"变体已存在: {variant_id}(force=True 才允许重派生)")
    vd.mkdir(parents=True, exist_ok=True)

    record = {
        "schema": "shipin.variant.v1",
        "base_project_id": base_id,
        "variant_id": variant_id,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "overrides": dict(overrides or {}),
        "phases": ["text", "generate", "assemble"],
        "media_root": str(base_dir),  # dataRoot 引用(base 素材池)
    }
    (vd / "variant.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    (vd / "brief.json").write_text(
        json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
    shutil.copy2(base_dir / "storyboard.json", vd / "storyboard.json")
    # manifest 继承素材池:深拷贝媒体路径引用(不复制字节);align 清空 →
    # generate 阶段重新对齐(时长可能因 overrides 变化而重排)
    manifest = {}
    if (base_dir / "manifest.json").is_file():
        manifest = json.loads(
            (base_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest = json.loads(json.dumps(manifest, ensure_ascii=False))
    manifest.pop("align", None)
    (vd / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")

    return {
        "ok": True,
        "base_project_id": base_id,
        "variant_id": variant_id,
        "overrides": dict(overrides or {}),
        "brief_merged": {k: merged[k] for k in VARIANT_OVERRIDE_KEYS
                         if k in merged},
        "media_root": str(base_dir),
        "directory": str(vd),
        "note": "阶段状态独立;素材引用基准 dataRoot;跑各阶段用 "
                "POST /api/variant/<id>/run",
    }


def load_variant_manifest(variant_id: str,
                          projects_dir: Path = PROJECTS_DIR) -> dict:
    """variant.json(已校验的派生记录),不含任何媒体字节。"""
    vd = _variant_dir(variant_id, projects_dir)
    p = vd / "variant.json"
    if not p.is_file():
        raise VariantError(f"不是已派生的变体: {variant_id}")
    return json.loads(p.read_text(encoding="utf-8"))


def run_variant_phases(
    variant_id: str,
    store: ProjectStageStore,
    phases: str = "all",
    category: Optional[str] = None,
    projects_dir: Path = PROJECTS_DIR,
) -> dict:
    """变体重跑入口:按 phases 依次跑既有管线(text/generate/assemble)。

    复用 run_text_phase / run_generate_phase / run_assemble_phase(公开
    签名不变);category 默认取 variant.json 里的覆盖值。阶段间约束由各
    阶段自身门禁执行(text 未过 → generate 的闸门拒入),这里只串联。
    """
    meta = load_variant_manifest(variant_id, projects_dir)
    vd = _variant_dir(variant_id, projects_dir)
    store.create_project(variant_id)

    from shipin_platform.orchestration.pipeline_runner import (  # noqa: PLC0415
        run_assemble_phase, run_generate_phase, run_text_phase,
    )
    cat = category or (meta.get("overrides") or {}).get("category")
    brief = json.loads((vd / "brief.json").read_text(encoding="utf-8"))
    out: dict = {"variant_id": variant_id, "phases": phases}

    if phases in ("all", "text"):
        out["text"] = run_text_phase(variant_id, brief, store, category=cat)
    if phases in ("all", "generate"):
        out["generate"] = run_generate_phase(variant_id, store)
    if phases in ("all", "assemble"):
        out["assemble"] = run_assemble_phase(variant_id, store)
    return out


def variant_status(variant_id: str, store: ProjectStageStore,
                   projects_dir: Path = PROJECTS_DIR) -> dict:
    """变体完整状态:档案 + 阶段 + 产物清单(文件名级别,不返回字节)。"""
    meta = load_variant_manifest(variant_id, projects_dir)
    vd = _variant_dir(variant_id, projects_dir)
    stages = store.get_project_status(variant_id) \
        if store.project_exists(variant_id) else {}
    files = sorted(p.name for p in vd.iterdir() if p.is_file())
    return {
        "ok": True,
        "variant_id": variant_id,
        "base_project_id": meta["base_project_id"],
        "overrides": meta.get("overrides", {}),
        "created_at": meta["created_at"],
        "stages": stages,
        "files": files,
        "final_exists": (vd / "final.mp4").is_file(),
    }


def list_variants(base_id: str, projects_dir: Path = PROJECTS_DIR) -> list[dict]:
    """列出 base 派生的所有变体(遍历 variant.json 的 base_project_id)。"""
    out = []
    if not (projects_dir / base_id).is_dir():
        return out
    for kid in sorted(projects_dir.iterdir()):
        if not kid.is_dir():
            continue
        vf = kid / "variant.json"
        if not vf.is_file():
            continue
        try:
            meta = json.loads(vf.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if meta.get("base_project_id") == base_id:
            out.append({"variant_id": meta["variant_id"],
                        "created_at": meta.get("created_at", "")})
    return out


__all__ = [
    "VARIANT_OVERRIDE_KEYS", "VariantError", "derive_variant",
    "run_variant_phases", "variant_status", "list_variants",
    "load_variant_manifest",
]