"""参考视频复刻 API —— 搜索 / 分享链接解析 / 白名单下载 / 反推分析 / 注入画布。

产品闭环（用户要求：AI 从视频站找参考 TVC → 拖下来抄剧本抄分镜 → 反推
提示词 → 灌进画布复刻）：

    POST /api/reference/search      {keyword, source, limit}   → 候选列表
    POST /api/reference/resolve      {text}                     → 干净视频页 URL
    POST /api/reference/fetch       {url, max_duration}        → 下载（异步任务）
    GET  /api/reference/{ref_id}                             → 任务状态/结果
    POST /api/reference/{ref_id}/analyze  {scene_threshold}    → 切镜+ASR+VLM 反推
    GET  /api/reference/{ref_id}/pack  → 反推包（shots/script/brief）
    GET  /api/reference/{ref_id}/frames/{name} → 关键帧图片
    POST /api/reference/{ref_id}/apply {graph_id} → 注入画布

任务状态持久化在 data/references/<ref_id>/state.json，前端轮询即可
（下载/反推都是分钟级任务，轮询比 SSE 简单可靠；画布自身仍走 SSE）。
安全：下载 URL 白名单由 video_fetch 封闭校验；ref_root 由本层围栏；
产物目录只在 data/references/ 之下。
"""
from __future__ import annotations

import json
import re
import threading
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from shipin_platform import roots
from shipin_platform.tools import video_fetch as vf

router = APIRouter(prefix="/api/reference", tags=["reference"])

REFS_ROOT = roots.data_dir() / "references"
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


class SearchReq(BaseModel):
    q: str = Field(default="", max_length=200)
    source: str = "bilibili"           # bilibili / youtube / douyin
    limit: int = Field(default=10, ge=1, le=20)


class ResolveReq(BaseModel):
    text: str = Field(max_length=500)


class FetchReq(BaseModel):
    url: str = Field(max_length=500)
    max_duration: int = Field(default=180, ge=10, le=600)   # 秒


class AnalyzeReq(BaseModel):
    scene_threshold: float = Field(default=0.3, ge=0.1, le=0.8)
    max_shots: int = Field(default=60, ge=5, le=120)
    skip_asr: bool = False


class ApplyReq(BaseModel):
    graph_id: str = Field(default="", max_length=64)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _ref_dir(ref_id: str) -> Path:
    if not _SAFE_NAME.match(ref_id):
        raise HTTPException(status_code=422, detail=f"ref_id 非法: {ref_id!r}")
    d = REFS_ROOT / ref_id
    try:
        d.mkdir(parents=True, exist_ok=True)
        d = d.resolve()
        if not d.is_relative_to(REFS_ROOT.resolve()):
            raise HTTPException(status_code=422, detail="ref 目录越界")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"创建 ref 目录失败: {e}")
    return d


def _state_path(d: Path) -> Path:
    return d / "state.json"


def _save_state(d: Path, state: dict) -> None:
    (_state_path(d)).write_text(json.dumps(state, ensure_ascii=False, indent=1),
                                encoding="utf-8")


def _load_state(d: Path) -> dict:
    p = _state_path(d)
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _read_json(d: Path, name: str) -> dict:
    p = d / name
    if not p.is_file():
        raise HTTPException(status_code=404, detail=f"参考包缺少 {name}")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=422, detail=f"{name} 损坏: {e}")


# ---------------------------------------------------------------------------
# 搜索 / 解析
# ---------------------------------------------------------------------------

@router.post("/search")
def reference_search(req: SearchReq):
    """站内搜索（B站/YouTube/抖音），返回候选视频列表。"""
    try:
        cands = vf.search_videos(req.q, source=req.source, limit=req.limit)
    except vf.VideoFetchError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"ok": True, "items": [
        {"id": c.id, "title": c.title, "url": c.url, "platform": c.platform,
         "duration_sec": c.duration_sec, "thumb": c.thumb} for c in cands]}


@router.post("/resolve")
def reference_resolve(req: ResolveReq):
    """把分享文本/链接解析成白名单内、无 query 的可下载 URL。"""
    try:
        url = vf.safe_share_url(req.text)
    except vf.VideoFetchError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"ok": True, "url": url}


# ---------------------------------------------------------------------------
# 下载（异步任务：fetch → state.json 轮询进度）
# ---------------------------------------------------------------------------

@router.post("/fetch")
def reference_fetch(req: FetchReq):
    """白名单 URL → 后台线程下载到 data/references/<ref_id>/video.mp4。"""
    try:
        page = vf.safe_share_url(req.url)
    except vf.VideoFetchError as e:
        raise HTTPException(status_code=422, detail=str(e))
    ref_id = "ref_" + uuid.uuid4().hex[:10]
    d = _ref_dir(ref_id)
    _save_state(d, {"ref_id": ref_id, "stage": "downloading",
                    "page": page, "error": None})
    import threading
    t = threading.Thread(
        target=_fetch_worker, args=(ref_id, d, page, req.max_duration),
        daemon=True)
    t.start()
    return {"ok": True, "ref_id": ref_id, "page": page,
            "status": "downloading"}


def _fetch_worker(ref_id: str, d: Path, page: str, max_duration: int) -> None:
    try:
        r = vf.download_video(page, d / "source", max_duration=max_duration,
                              ref_root=REFS_ROOT)
        _save_state(d, {"ref_id": ref_id, "stage": "downloaded",
                        "page": page, "file": r["file"],
                        "analyzed": False})
    except Exception as e:
        _save_state(d, {"ref_id": ref_id, "stage": "error",
                        "page": page, "error": str(e)[:400]})


# ---------------------------------------------------------------------------
# 分析（切镜+抽帧+ASR+VLM 反推）
# ---------------------------------------------------------------------------

@router.post("/{ref_id}/analyze")
def reference_analyze(ref_id: str, req: AnalyzeReq):
    d = _ref_dir(ref_id)
    st = _load_state(d)
    if st.get("stage") not in ("downloaded", "error"):
        raise HTTPException(status_code=409, detail="参考包未就绪（先 POST /api/reference/fetch）")
    video = Path(st.get("file") or "")
    if not video.is_file() or not video.is_relative_to(REFS_ROOT.resolve()):
        raise HTTPException(status_code=422, detail="参考视频文件缺失")
    if st.get("analyzed"):
        raise HTTPException(status_code=409, detail="已分析过；如需重跑请先删除参考包")
    _save_state(d, {**st, "stage": "analyzing"})
    import threading
    t = threading.Thread(
        target=_analyze_worker,
        args=(ref_id, d, video, req, st),
        daemon=True)
    t.start()
    return {"ok": True, "ref_id": ref_id, "stage": "analyzing"}


def _analyze_worker(ref_id: str, d: Path, video: Path, req: AnalyzeReq,
                    st: dict) -> None:
    from shipin_platform.analysis.reference_pipeline import run_reference_pipeline
    try:
        res = run_reference_pipeline(
            video, d, scene_threshold=req.scene_threshold,
            max_shots=req.max_shots, skip_asr=req.skip_asr)
        _save_state(d, {**st, "stage": "analyzed",
                        "analyzed": True,
                        "result": {k: res[k] for k in
                                   ("ok", "shot_count", "duration",
                                    "vlm_used", "vlm_error", "title")
                                   if k in res}})
    except Exception as e:
        _save_state(d, {**st, "stage": "error",
                        "error": str(e)[:400]})


# ---------------------------------------------------------------------------
# 读取状态 / 反推包 / 帧 / 注入
# ---------------------------------------------------------------------------

@router.get("/{ref_id}")
def reference_status(ref_id: str):
    d = _ref_dir(ref_id)
    st = _load_state(d)
    if not st:
        raise HTTPException(status_code=404, detail="参考包不存在")
    return st


@router.get("/{ref_id}/pack")
def reference_pack(ref_id: str):
    """反推结果包（剧本/分镜/提示词/简报预填），供前端预览与应用。"""
    d = _ref_dir(ref_id)
    st = _load_state(d)
    if st.get("stage") != "analyzed":
        raise HTTPException(status_code=409, detail="参考包尚未完成反推")
    return {
        "ok": True,
        "script": _read_json(d, "script.json"),
        "shots": _read_json(d, "shots.json"),
        "brief": _read_json(d, "brief.json"),
        "stage": "analyzed",
    }


@router.get("/{ref_id}/frames/{name}")
def reference_frame(ref_id: str, name: str):
    """关键帧文件（限制在 frames/ 目录内，防穿越）。"""
    d = _ref_dir(ref_id)
    if not _SAFE_NAME.match(name or ""):
        raise HTTPException(status_code=422, detail="frame 名非法")
    p = (d / "frames" / name).resolve()
    if not p.is_relative_to((d / "frames").resolve()) or not p.is_file():
        raise HTTPException(status_code=404, detail="frame not found")
    return FileResponse(p, media_type="image/jpeg")


@router.post("/{ref_id}/apply")
def reference_apply(ref_id: str, req: ApplyReq):
    """一键注入画布：参考反推包 → script/storyboard/video_prompt 节点链。"""
    from shipin_platform.graph.reference_apply import (
        ReferenceApplyError, build_reference_graph)

    if not req.graph_id:
        from shipin_platform.graph import engine
        g = engine.new_graph(f"复刻·{ref_id}")
        req.graph_id = g["id"]
    d = _ref_dir(ref_id)
    st = _load_state(d)
    if st.get("stage") != "analyzed":
        raise HTTPException(status_code=409, detail="参考包尚未完成反推")
    try:
        return build_reference_graph(d, req.graph_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ReferenceApplyError as e:
        raise HTTPException(status_code=422, detail=str(e))