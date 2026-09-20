"""节点画布 REST API：图的 CRUD + 节点执行 + 节点类型定义 + SSE 事件流。

外部 AI（Codex 等）通过「创建节点 / 写参数 / 运行 / 审核」这些端点驱动
平台；每步操作都在 `events` 总线上广播，画布前端用 EventSource 订阅，
节点状态（运行中/成功/失败/新增/改参）即刻反映，无需手动刷新。
"""
from __future__ import annotations

import json
import mimetypes

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from shipin_platform.graph import engine, events
from shipin_platform.graph.node_types import definitions_json

router = APIRouter(prefix="/api/graphs", tags=["graph"])


class GraphCreate(BaseModel):
    name: str = Field(default="新画布", max_length=120)


class GraphSave(BaseModel):
    name: str | None = None
    nodes: list[dict] = []
    edges: list[dict] = []


class RunRequest(BaseModel):
    node_id: str = Field(default="", max_length=64)
    force: bool = False


class NodeCreate(BaseModel):
    type: str
    x: float = 0.0
    y: float = 0.0
    title: str | None = None
    params: dict = {}


class NodePatch(BaseModel):
    """仅更新给定字段，未给的保持原样（AI 增量改参数）。"""
    title: str | None = None
    params: dict | None = None


def _load_or_404(gid: str) -> dict:
    try:
        return engine.load_graph(gid)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("")
def graph_list():
    return {"graphs": engine.list_graphs()}


@router.post("")
def graph_create(req: GraphCreate):
    g = engine.new_graph(req.name)
    return {"id": g["id"], "name": g["name"]}


@router.get("/{gid}")
def graph_get(gid: str):
    g = _load_or_404(gid)
    return {"graph": g, "errors": engine.validate_graph(g)}


@router.put("/{gid}")
def graph_save(gid: str, req: GraphSave):
    g = _load_or_404(gid)
    if req.name is not None:
        g["name"] = req.name
    g["nodes"] = req.nodes
    g["edges"] = req.edges
    errs = engine.validate_graph(g)
    if errs:
        raise HTTPException(status_code=422,
                            detail={"code": "INVALID_GRAPH",
                                    "message": "图表不合法",
                                    "errors": errs})
    engine.save_graph(g)
    events.publish(gid, {"type": "changed", "op": "save"})
    return {"ok": True, "errors": []}


@router.delete("/{gid}")
def graph_delete(gid: str):
    import shutil
    d = engine.gdir_for(gid)
    if not d.exists():
        raise HTTPException(status_code=404, detail="graph not found")
    shutil.rmtree(d)
    return {"ok": True}


@router.post("/{gid}/run")
def graph_run(gid: str, req: RunRequest):
    g = _load_or_404(gid)
    try:
        state = engine.run_node(g, req.node_id, force=req.force)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"ok": bool(state.get("ok")), "node_id": req.node_id,
            "state": state}


@router.post("/{gid}/run-all")
def graph_run_all(gid: str, req: RunRequest):
    """运行全图（ComfyUI Queue Prompt 语义）：拓扑序逐节点执行，
    审核/质检门照常生效，返回每节点结果（含被拦截节点及原因）。"""
    g = _load_or_404(gid)
    try:
        results = engine.run_all(g, force=req.force)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"ok": True, "results": results}


# ---------------------------------------------------------------------------
# 外部 AI 驱动端点：AI 编排平台时，下面每个操作都会向画布广播实时事件
# ---------------------------------------------------------------------------

@router.post("/{gid}/nodes")
def graph_add_node(gid: str, req: NodeCreate):
    """新增一个节点（AI 可用它逐步搭流程）。"""
    g = _load_or_404(gid)
    try:
        node = engine.add_node(g, req.type, req.x, req.y,
                               req.params, req.title)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    events.publish(gid, {"type": "node", "op": "add", "node": node})
    return {"ok": True, "node": node}


@router.delete("/{gid}/nodes/{node_id}")
def graph_del_node(gid: str, node_id: str):
    g = _load_or_404(gid)
    if not engine.remove_node(g, node_id):
        raise HTTPException(status_code=404, detail="node not found")
    events.publish(gid, {"type": "node", "op": "remove",
                         "node_id": node_id})
    return {"ok": True}


@router.post("/{gid}/nodes/{node_id}/params")
def graph_patch_node(gid: str, node_id: str, req: NodePatch):
    """增量更新某节点参数/标题（AI 写入提示词、审核结论等）。"""
    g = _load_or_404(gid)
    try:
        node = engine.patch_node(g, node_id, req.title, req.params)
    except KeyError:
        raise HTTPException(status_code=404, detail="node not found")
    events.publish(gid, {"type": "node", "op": "params",
                         "node_id": node_id, "node": node})
    return {"ok": True, "node": node}


@router.get("/{gid}/events")
async def graph_events(gid: str, last_seq: int = 0):
    """SSE 事件流：画布状态变化实时推送（EventSource 直接消费）。"""
    _load_or_404(gid)

    async def gen():
        async with events.GraphEventStream(gid) as s:
            async for ev in s.iter_events(last_seq=last_seq):
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache",
                 "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"})


@router.get("/{gid}/assets/{node_id}")
def graph_asset(gid: str, node_id: str):
    """暴露产物文件（仅限本图 artifacts 目录内，防穿越）。

    node_id 传 `im1` 或 `im1.png` 均可：按 basename 前缀查文件，
    前端无需手拼扩展名（image/video/audio/report 扩展名不同）。
    """
    root = engine.artifact_dir(gid)
    safe = engine.safe_node(node_id)
    matches = sorted(root.glob(f"{safe}*"))
    if not matches:
        raise HTTPException(status_code=404, detail="artifact not found")
    p = matches[0].resolve()
    if not p.is_relative_to(root.resolve()):
        raise HTTPException(status_code=404, detail="not in artifacts")
    if not p.is_file():
        raise HTTPException(status_code=404, detail="artifact not found")
    mt = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    return FileResponse(p, media_type=mt, filename=p.name)


@router.get("/kit/definitions")
def graph_definitions():
    return {"nodes": definitions_json(),
            "models": {"video": ["agnes-video-2.5-flash",
                                 "agnes-video-v2.0"],
                       "image": ["agnes-image-2.1-flash",
                                 "agnes-image-2.0-flash",
                                 "agnes-image-2.5-flash"]}}