"""P2 产物版本化：DVC 式「内容哈希即版本」+ n8n 式「每次写全量快照」。

调研结论（2026-09-18）：DVC 的关键是内容哈希作为版本标识 + 元数据指针
（.dvc 文件），版本切换=指向另一份内容；n8n 每次执行保存全量节点数据供
replay/回滚；Litestream 是 SQLite WAL 时间线。本平台产物是 JSON 文件，
用「内容哈希 → 版本号」最贴近三者的共同思想：

- versions.json 是元数据索引（对标 .dvc 指针），快照本体放在
  versions/<stage>/v<n>.json（对标 n8n 全量快照）；
- 同一内容重复写不产生新版本（哈希去重，对标 DVC 缓存复用）；
- 回滚 = 把某版本的 JSON 写回当前产物（对标 dvc checkout），并由
  rewrite 同款语义清闸门 + 失效下游（杜绝"改完旧链条继续花钱"）。
"""
from __future__ import annotations

import hashlib
import json as _json
import os
import threading
import time
import uuid as _uuid
from pathlib import Path
from typing import Optional

TRACKED_STAGES = ("brief", "script", "storyboard",
                  "image_prompt", "video_prompt")

META_NAME = "versions.json"


def _atomic_write(p: Path, text: str) -> None:
    """轮56:tmp+os.replace 原子写——截断式 write_text 在半路被杀
    (磁盘满/进程被杀)时留半截 JSON,load 侧裸抛/静默返 {}。"""
    tmp = p.with_suffix("." + _uuid.uuid4().hex[:8] + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)


def _canonical(data) -> str:
    """内容规范序列化：sort_keys 保证「内容一样 → 哈希一样」。"""
    return _json.dumps(data, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"))


def _content_hash(data) -> str:
    return hashlib.sha256(_canonical(data).encode("utf-8")).hexdigest()


def _versions_dir(project_dir: Path, stage: str) -> Path:
    return project_dir / "versions" / stage


# 轮56(十审 P1-3):快照读-改-写的进程内串行化锁——旧 snapshot 无锁:
# 两个并发快照(同 stage)都读到同一 prev、都算 version=1 → versions.json
# 出现两条 v1、磁盘只剩一个 v1.json;read_version 按 version 字典取最后
# 一条 entry →「entry.hash 说的是一份内容、v1.json 里是另一份」,restore
# 静默回滚到错内容;索引 read-modify-write 还会整份丢条目(实测 10 并发
# 只剩 2 条)。与 costing._LEDGER_LOCK 同范式。
_SNAPSHOT_LOCK = threading.Lock()


def snapshot(project_dir: Path, stage: str, data, caller: str = "auto",
             note: str = "") -> Optional[dict]:
    """写入一个版本快照。内容与最新版一致 → 返回 None（不产生新版本）。
    返回 {version, hash, ts}。项目目录不存在时静默跳过（幂等）。"""
    if stage not in TRACKED_STAGES:
        return None  # 仅可回溯产物参与版本化
    if not project_dir.is_dir():
        return None
    h = _content_hash(data)
    with _SNAPSHOT_LOCK:
        prev = list_versions(project_dir, stage)
        if prev and prev[-1]["hash"] == h:
            return None
        vdir = _versions_dir(project_dir, stage)
        vdir.mkdir(parents=True, exist_ok=True)
        version = (prev[-1]["version"] + 1) if prev else 1
        entry = {"version": version,
                 "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                 "hash": h, "caller": caller or "auto", "note": note or "",
                 "bytes": len(_canonical(data).encode("utf-8"))}
        # 版本文件与索引都走原子写(半路被杀不留半截 JSON)
        _atomic_write(vdir / f"v{version}.json", _canonical(data))
        index = _load_index(project_dir)
        index.setdefault(stage, []).append(entry)
        _save_index(project_dir, index)
    return entry


def list_versions(project_dir: Path, stage: str) -> list[dict]:
    if stage not in TRACKED_STAGES:
        return []
    return list(_load_index(project_dir).get(stage, []))


def all_versions(project_dir: Path) -> dict[str, list[dict]]:
    return _load_index(project_dir)


def read_version(project_dir: Path, stage: str,
                 version) -> tuple[Optional[dict], Optional[dict]]:
    """按版本号取快照：返回 (entry, content)。找不到 → (None, None)。"""
    entries = {str(e["version"]): e
               for e in list_versions(project_dir, stage)}
    entry = entries.get(str(version))
    if entry is None:
        return None, None
    p = _versions_dir(project_dir, stage) / f"v{entry['version']}.json"
    if not p.is_file():
        return entry, None
    return entry, _json.loads(p.read_text(encoding="utf-8"))


def _load_index(project_dir: Path) -> dict:
    return _load_json(project_dir)


def _load_json(project_dir: Path) -> dict:
    p = project_dir / META_NAME
    if not p.is_file():
        return {}
    try:
        data = _json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (_json.JSONDecodeError, OSError):
        return {}


def _save_index(project_dir: Path, index: dict) -> None:
    # 轮56:索引同样原子写(与 _SNAPSHOT_LOCK 内的 RMW 配对)
    _atomic_write(project_dir / META_NAME,
                  _json.dumps(index, ensure_ascii=False, indent=1))