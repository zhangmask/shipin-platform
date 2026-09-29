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
    # 轮71:AI 编排通道——created_by 让前端给 AI 建节点打角标;
    # connect_to 让 AI 建 video_gen 等节点时即连上游(端口不悬空)。
    created_by: str = ""
    connect_to: dict | None = None


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


@router.post("/from-project/{project_id}")
def graph_from_project(project_id: str, aspect: str = "portrait"):
    """轮68:把管线项目摊成画布——每镜一句话/景别/运动/转场全部可改。

    前置:项目已有 script/storyboard(管线跑过 text 阶段)。
    已有首帧/clip 以 reuse 参数挂载(零成本复用),用户只改一句话时
    重跑 tts+assemble 即可,不重生视频。
    """
    from shipin_platform.graph.from_project import graph_from_project as _gfp
    try:
        data = _gfp(project_id, aspect=aspect)
    except (FileNotFoundError, ValueError) as e:
        raise HTTPException(status_code=404, detail=str(e))
    g = engine.new_graph(data["name"])
    g.update({"nodes": data["nodes"], "edges": data["edges"]})
    errs = engine.validate_graph(g)
    if errs:
        raise HTTPException(status_code=422,
                            detail={"code": "INVALID_GRAPH",
                                    "message": "导出图不合法",
                                    "errors": errs[:5]})
    engine.save_graph(g)
    events.publish(g["id"], {"type": "changed", "op": "from-project",
                             "project": project_id})
    return {"id": g["id"], "name": g["name"],
            "nodes": len(data["nodes"]), "edges": len(data["edges"])}


@router.get("/{gid}")
def graph_get(gid: str):
    g = _load_or_404(gid)
    return {"graph": g, "errors": engine.validate_graph(g)}


@router.put("/{gid}")
def graph_save(gid: str, req: GraphSave):
    # 轮55(十审 P0-2):load→改→save 全程持按图锁——两个并发 PUT/add/
    # patch 各拿旧快照互相覆盖(lost update),单进程部署下进程内锁消除
    with engine.graph_write(gid):
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
    # 轮55:run 全程持按图锁——run_node 内部 load→执行→save,两个并发
    # run 会互相覆盖节点状态/产物
    with engine.graph_write(gid):
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
    with engine.graph_write(gid):  # 轮55:RMW 事务锁(整图串行)
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
    """新增一个节点（AI 可用它逐步搭流程）。

    轮71:x/y 不传则由服务端自动排布(旧行为默认 0,0,AI 批量建节点
    全叠左上角);created_by/connect_to 支撑 AI 编排的显示与连线。
    """
    with engine.graph_write(gid):  # 轮55:RMW 事务锁防并发丢节点
        g = _load_or_404(gid)
        try:
            node = engine.add_node(g, req.type, req.x, req.y,
                                   req.params, req.title,
                                   created_by=req.created_by,
                                   connect_to=req.connect_to)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        except KeyError as e:
            # 轮57(真实使用发现,子智能体 B):未知节点类型 get_def 抛
            # KeyError,旧代码只捕 ValueError → 500 无 detail。与 PUT
            # 路径的 INVALID_GRAPH 422 对齐。
            raise HTTPException(status_code=422, detail={
                "code": "UNKNOWN_NODE_TYPE",
                "message": f"未知节点类型: {req.type}"})
    events.publish(gid, {"type": "node", "op": "add", "node": node})
    return {"ok": True, "node": node}


@router.delete("/{gid}/nodes/{node_id}")
def graph_del_node(gid: str, node_id: str):
    with engine.graph_write(gid):  # 轮55:RMW 事务锁
        g = _load_or_404(gid)
        if not engine.remove_node(g, node_id):
            raise HTTPException(status_code=404, detail="node not found")
    events.publish(gid, {"type": "node", "op": "remove",
                         "node_id": node_id})
    return {"ok": True}


@router.post("/{gid}/nodes/{node_id}/params")
def graph_patch_node(gid: str, node_id: str, req: NodePatch):
    """增量更新某节点参数/标题（AI 写入提示词、审核结论等）。"""
    with engine.graph_write(gid):  # 轮55:RMW 事务锁
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
    轮55(十审 P1-2):glob 必须带 `_` 分隔(`n1_*`)——旧的无分隔前缀
    `n1*` 会把 `n10_xxx` 也算进来,字典序下 'n10_x' < 'n1_y'
    ('0'<'_')→ 节点 n1 读到 n10 的产物(串台,正是九审在执行器侧修
    掉的同类 bug 的读侧残留)。带扩展名的查询走精确名优先。"""
    root = engine.artifact_dir(gid)
    # 轮55:查询名清洗保留点(im1.png 形态)——safe_node 会把 '.' 也剥掉
    # 导致带扩展名查询永远 404(docstring 承诺的形态实际不可用);仍剔
    # 除路径分隔符等危险字符,最终 resolve 前缀校验兜底。
    import re as _re
    safe = _re.sub(r"[^A-Za-z0-9_.\-]", "", str(node_id))
    if not safe:
        raise HTTPException(status_code=404, detail="artifact not found")
    # 带扩展名(n1.png/n1.mp3):精确名优先
    exact = root / safe
    if exact.is_file() and not exact.is_dir():
        p = exact.resolve()
    else:
        # 轮57(真实使用发现,子智能体 B):无扩展名形态(n1)的兜底 glob。
        # 产物真实命名是 **{node_id}.{ext}**(无下划线)——轮55 写的
        # {stem}_* 对现有命名体系是死代码,docstring 承诺的「im1 或
        # im1.png 均可」实际只有后者可用。两种形态都兼容:
        #   {stem}.*      → n1.png / n1.mp3(现役命名)
        #   {stem}_*      → n1_<uuid>.mp3(tts 的历史命名,向后兼容)
        # 都不越界到别的节点('n10.png' 不匹配 'n1.*',因为 '.'≠'0')。
        stem = safe.rsplit(".", 1)[0] if "." in safe else safe
        cands = sorted(fp for fp in root.glob(f"{stem}.*")
                       if fp.is_file())
        cands += sorted(fp for fp in root.glob(f"{stem}_*")
                        if fp.is_file())
        if not cands:
            raise HTTPException(status_code=404, detail="artifact not found")
        # 同 stem 多产物(如 n1.png 与 n1.mp3):按 mtime 取最新
        p = max(cands, key=lambda fp: fp.stat().st_mtime).resolve()
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