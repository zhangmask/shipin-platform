"""图执行引擎：驱动节点图沿「连线」真实执行平台生成能力。

数据流语义：edges 把上游输出端口的值（图片/视频路径或文本）喂给下游
输入端口；类型不符或闭环被 validate_graph 拦截。执行带输入哈希缓存，
参数或上游输出未变化时不重复调用花钱的生成 API。

安全基线：
- 节点 id / 图 id 白名单化（[A-Za-z0-9_-]），全部产物落在
  data/graphs/<gid>/artifacts/ 内，杜绝路径穿越；
- 合成/拼接复用平台已评审的 assembly 模块（内部 argv+shell=False，
  并做 master 边界降级），本模块不直接调用外部命令；
- 传给拼接的输入先经 _owned_artifact 校验必须在图产物目录内，
  外部路径直接拒绝。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .node_types import REGISTRY, get_def
from shipin_platform import roots

GRAPHS_DIR: Path = (roots.data_root() / "data" / "graphs")
_ID_RE = re.compile(r"[^A-Za-z0-9_-]")


def _safe(gid: str) -> str:
    s = _ID_RE.sub("", str(gid))
    if not s:
        raise ValueError("invalid id")
    return s


def _gdir(gid: str) -> Path:
    return GRAPHS_DIR / _safe(gid)


def _artifact_path(gid: str, node_id: str, ext: str) -> Path:
    d = _gdir(gid) / "artifacts"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{_safe(node_id)}.{ext}"


# ------------------------------------------------------------------ 存取
def load_graph(gid: str) -> dict:
    p = _gdir(gid) / "graph.json"
    if not p.exists():
        raise FileNotFoundError(f"graph not found: {gid}")
    return json.loads(p.read_text(encoding="utf-8"))


# 轮55(十审 P0-2/P1-3 同族):并发写图的进程内串行化锁——旧 save_graph
# 是裸 write_text 全量覆写:(a) 磁盘满/进程被杀留半截 JSON,load_graph
# 之后 json.loads 裸抛 500;(b) 两个并发操作各拿旧快照互相覆盖,节点/
# 边静默丢失(实测 8 线程 add_node 只剩 1 个)。单进程部署(start.sh/
# Dockerfile 均无 --workers)下进程内锁足以消除 lost update;原子写
# (tmp+os.replace)消除半截文件。
_GRAPH_SAVE_LOCK = threading.Lock()
_GRAPH_LOCKS: dict[str, threading.Lock] = {}
_GRAPH_LOCKS_GUARD = threading.Lock()


def graph_write(gid: str):
    """轮55(十审 P0-2):按图的 read-modify-write 事务锁。

    save_graph 内的锁只序列化**写**,lost update 发生在 load→改→save
    周期(两个请求各拿旧快照,后保存者覆盖先保存者)。用方:
        with graph_write(gid):
            g = load_graph(gid); ...改...; save_graph(g)
    锁按 gid 细分(不同图互不阻塞);单进程部署下足以消除丢失更新。
    """
    key = _safe(gid)
    with _GRAPH_LOCKS_GUARD:
        lk = _GRAPH_LOCKS.get(key)
        if lk is None:
            lk = threading.Lock()
            _GRAPH_LOCKS[key] = lk
    return lk


def save_graph(g: dict) -> None:
    d = _gdir(gid_of(g))
    d.mkdir(parents=True, exist_ok=True)
    g["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    payload = json.dumps(g, ensure_ascii=False, indent=1)
    p = d / "graph.json"
    with _GRAPH_SAVE_LOCK:
        tmp = p.with_suffix("." + uuid.uuid4().hex[:8] + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, p)


def gid_of(g: dict) -> str:
    return _safe(g.get("id", ""))


def gdir_for(gid: str) -> Path:
    """图目录（已白名单化），不存在时返回不存在的 Path。"""
    return _gdir(gid)


def artifact_dir(gid: str) -> Path:
    d = _gdir(gid) / "artifacts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def safe_node(nid: str) -> str:
    return _safe(nid)


def list_graphs() -> list[dict]:
    out = []
    for doc in GRAPHS_DIR.glob("*/graph.json"):
        try:
            g = json.loads(doc.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append({"id": g["id"], "name": g.get("name", g["id"]),
                    "updated_at": g.get("updated_at", ""),
                    "node_count": len(g.get("nodes", []))})
    out.sort(key=lambda x: x["updated_at"], reverse=True)
    return out


def new_graph(name: str) -> dict:
    # 轮55(十审 P1-1):秒级时间戳 id 在同秒创建时互相覆盖(外部 AI 批量
    # 建画布/前端双击/apply 并发都可达)——第二个图 save_graph 覆盖第一个,
    # 先建画布节点/边/产物静默丢失。追加 uuid8 后缀保唯一(时间戳保留
    # 可读性)。
    from datetime import datetime
    gid = ("g-" + datetime.now().strftime("%Y%m%d%H%M%S")
           + "-" + uuid.uuid4().hex[:8])
    g = {"id": gid, "name": name, "nodes": [], "edges": []}
    save_graph(g)
    return g


# ---------------------------------------------------------------- 校验
def _cycle_members(g: dict) -> list[str]:
    # 轮57:只收有 id 的节点——缺 id 的畸形节点由 validate_graph 报
    # 「缺少 id 字段」,这里不再裸 KeyError(旧行为 500)
    node_ids = [n["id"] for n in g.get("nodes", [])
                if isinstance(n, dict) and n.get("id")]
    indeg = {nid: 0 for nid in node_ids}
    for e in g.get("edges", []):
        t = e.get("to")
        if t in indeg:
            indeg[t] += 1
    q = [nid for nid, d in indeg.items() if d == 0]
    while q:
        u = q.pop()
        for e in g.get("edges", []):
            if e.get("from") == u:
                v = e.get("to")
                if v in indeg and indeg[v] > 0:
                    indeg[v] -= 1
                    if indeg[v] == 0:
                        q.append(v)
    return [nid for nid, d in indeg.items() if d > 0]


def validate_graph(g: dict) -> list[str]:
    errs: list[str] = []
    # 轮57(真实使用发现,子智能体 B):节点缺 id/type 字段时旧代码
    # n["id"]/get_def(f["type"]) 裸 KeyError → 500(PUT 路径应 422
    # INVALID_GRAPH)。这里一律转可读 err,与未知类型同款处理。
    nodes = [n for n in g.get("nodes", []) if isinstance(n, dict)]
    nodemap = {n["id"]: n for n in nodes if n.get("id")}
    seen_ids: set[str] = set()
    seen_edges: set[tuple] = set()
    for idx, n in enumerate(g.get("nodes", [])):
        if not isinstance(n, dict):
            errs.append(f"节点 #{idx} 不是对象: {n!r}")
            continue
        if not n.get("id"):
            errs.append(f"节点 #{idx} 缺少 id 字段")
            continue
        if n["id"] in seen_ids:
            errs.append(f"重复节点 id: {n['id']}")
        seen_ids.add(n["id"])
        if n.get("type") not in REGISTRY:
            errs.append(f"未知节点类型: {n.get('type')}")
    for e in g.get("edges", []):
        key = (e.get("from"), e.get("from_port"), e.get("to"), e.get("to_port"))
        if key in seen_edges:
            errs.append(f"重复边: {key}")
        seen_edges.add(key)
        f, t = nodemap.get(e.get("from")), nodemap.get(e.get("to"))
        if not f or not t:
            errs.append(f"边指向未知节点: {e}")
            continue
        try:
            fdef, tdef = get_def(f["type"]), get_def(t["type"])
        except KeyError:
            errs.append(f"边端点类型未注册: {e}")
            continue
        fport = next((p for p in fdef.outputs if p.name == e.get("from_port")), None)
        tport = next((p for p in tdef.inputs if p.name == e.get("to_port")), None)
        if not fport or not tport:
            errs.append(f"边端口不存在: {e}")
            continue
        if fport.kind != tport.kind:
            errs.append(f"端口类型不匹配: {e['from']}.{e['from_port']}"
                        f"({fport.kind}) != {e['to']}.{e['to_port']}"
                        f"({tport.kind})")
    cyc = _cycle_members(g)
    if cyc:
        errs.append(f"存在环路: {cyc}")
    return errs


# ---------------------------------------------------------------- 数据流
def _nodemap(g: dict) -> dict[str, dict]:
    return {n["id"]: n for n in g.get("nodes", [])}


def _upstream_of(g: dict, node_id: str) -> list[str]:
    """轮57:直接上游节点 id(有边指向 node_id 的源)——run_all 的
    跳过判定用(上游失败则本节点 skipped)。"""
    return [e.get("from") for e in g.get("edges", [])
            if e.get("to") == node_id and e.get("from")]


def _edge_value(graph: dict, node_id: str, port: str,
                nodemap: dict[str, dict]) -> Any:
    for e in graph.get("edges", []):
        src = nodemap.get(e.get("from"))
        if e.get("to") == node_id and e.get("to_port") == port and src:
            v = ((src.get("state") or {}).get("outputs") or {}) \
                .get(e.get("from_port"), {}).get("value")
            if v is not None:
                return v
    return None


def _param_text(v: Any) -> str:
    """轮57(真实使用发现,子智能体 B):节点参数的文本化。

    旧代码对 params 值裸 str():dict/list 被 Python repr 化
    ("{'a': 1}" 单引号形态)静默注入下游提示词/剧本正文,数字也会
    变成 "12345"。这里:str 原样;dict/list → JSON(ensure_ascii=False,
    双引号标准形态,LLM/渲染器都不会误解);其余 → str()。仍不做
    schema 级拒尽(那是 NodeCreate 的 TODO),但消灭 repr 注入。"""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, (dict, list)):
        try:
            return json.dumps(v, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(v)
    return str(v)


def resolved_input(graph: dict, node: dict, port_name: str,
                   nodemap: dict[str, dict]) -> Any:
    v = _edge_value(graph, node["id"], port_name, nodemap)
    if v is not None:
        return v
    return (node.get("params") or {}).get(port_name)


def node_input_hash(graph: dict, node: dict, nodemap: dict[str, dict]) -> str:
    """确定性输入摘要：节点自身参数 + 全部上游节点的参数与已执行输出。
    任一处变化（改文本、重跑上游）都会让缓存失效；节点自身输出（state）
    不进摘要——那是运行结果，不是输入。"""
    h = hashlib.sha256()
    h.update(f"self:{node['id']}:".encode("utf-8"))
    h.update(json.dumps(node.get("params") or {}, sort_keys=True,
                        ensure_ascii=False).encode("utf-8"))
    seen: set[str] = {node["id"]}
    stack = [node["id"]]
    while stack:
        u = stack.pop()
        for e in graph.get("edges", []):
            if e.get("to") == u:
                src = e.get("from")
                if src in seen or src not in nodemap:
                    continue
                seen.add(src)
                cur = nodemap[src]
                h.update(f"up:{src}:".encode("utf-8"))
                h.update(json.dumps(cur.get("params") or {}, sort_keys=True,
                                    ensure_ascii=False).encode("utf-8"))
                st = cur.get("state") or {}
                if st.get("ok"):
                    o = st.get("outputs") or {}
                    for k in sorted(o):
                        h.update(f"{k}=".encode("utf-8"))
                        h.update(str(o[k].get("value", "")).encode("utf-8"))
                stack.append(src)
    return h.hexdigest()[:16]


# ---------------------------------------------------------------- 执行器
def _exec_text(graph: dict, node: dict, nodemap: dict[str, dict]) -> dict:
    txt = resolved_input(graph, node, "text", nodemap)
    # 轮57:走 _param_text(拒绝 repr 注入),见该函数注释
    return {"kind": "text", "value": _param_text(txt)}


# ---- 阶段流水线：内容由外部 AI 写入节点参数，执行 = 物化 + 校验记录 ----
def _exec_script(graph: dict, node: dict, nodemap: dict[str, dict]) -> dict:
    p = node.get("params") or {}
    content = _param_text(p.get("content")).strip()
    if not content:                       # 未写正文 → 透传上游简报
        content = _param_text(resolved_input(graph, node, "brief", nodemap))
    return {"kind": "text", "value": content,
            "meta": {"stage": "script", "chars": len(content)}}


def _exec_storyboard(graph: dict, node: dict, nodemap: dict[str, dict]) -> dict:
    p = node.get("params") or {}
    content = _param_text(p.get("content")).strip()
    if not content:
        content = _param_text(resolved_input(graph, node, "script", nodemap))
    shots = [ln for ln in content.splitlines() if ln.strip()]
    return {"kind": "text", "value": content,
            "meta": {"stage": "storyboard", "shots": len(shots)}}


def _exec_frame_prompts(graph: dict, node: dict, nodemap: dict[str, dict]) -> dict:
    """多输出：首帧提示词 / 尾帧提示词（各成一个输出端口，供两个 image_gen 连线）"""
    p = node.get("params") or {}
    first = _param_text(p.get("first_prompt")).strip()
    last = _param_text(p.get("last_prompt")).strip()
    _board = _param_text(resolved_input(graph, node, "board", nodemap))
    if not first:
        first = f"依据分镜首帧：{_board}"
    if not last:
        # 轮57(真实使用发现,子智能体 B):尾帧提示词回退到 first 的
        # 文案是「依据分镜**首帧**」——尾帧描述写成首帧,两个端口同值
        # 且语义错。回退文本按端口各说各话。
        last = f"依据分镜尾帧：{_board}"
    # 多输出：键 = 输出端口名，engine 按端口名写入 state.outputs
    return {"first_prompt": {"kind": "text", "value": first},
            "last_prompt": {"kind": "text", "value": last}}


def _graph_cost_pid(graph_id: str) -> str:
    """轮50(九审 P1-4):画布车道的成本归属 id——graph-<gid>。

    画布(engine/graph lane)此前的 image/video/tts 执行器直连付费
    供应商但全车道零 record_cost、零预算闸:同一把 key 在 pipeline
    车道被 422、画布车道可无限花钱且 showback 零可见(违反平台不变
    量「每次真实生成都登记」)。用 graph-<gid> 作为台账 project_id
    落在 data/projects/graph-<gid>/cost.json:global_cost_summary
    扫 data/projects/* 自然纳入,预算文件同约定可配。"""
    return f"graph-{graph_id}"


def _record_graph_cost(graph: dict, kind: str, *, model: str = "",
                       units: float = 1.0, note: str = "") -> None:
    """画布节点生成入账(失败不计——调用方只在成功后调)。"""
    try:
        from shipin_platform.services.costing import record_cost
        record_cost(_graph_cost_pid(graph["id"]), kind, model=model,
                    units=units, note=note)
    except Exception:
        # 记账失败绝不阻断生成本身(pipeline 车道同口径:熔断由预算闸
        # 负责,不由记账异常负责)——但也不静默:节点 state 的 meta 由
        # 调用方可见,此处最差情况是 showback 少一行
        pass


def _budget_blocked(graph: dict) -> str:
    """轮50(九审 P1-4):画布车道预算硬闸(每个花钱节点执行前复查)。

    判据与 pipeline 车道同口径:全局月度闸(global_budget_exceeded)
    + 本图(graph-<gid>)预算文件(若配置)。任一超限返回原因串;账本
    损坏视为超限(不能按空账放行)。"""
    try:
        from shipin_platform.services.costing import (
            LedgerCorruptError, cost_summary, global_budget_exceeded)
        over, used, mx = global_budget_exceeded()
        if over:
            return f"全局月度预算超限：已用 ${used:.4f} > 上限 ${mx:.4f}"
        pid = _graph_cost_pid(graph["id"])
        fp = roots.data_dir() / "projects" / pid / "budget.json"
        if not fp.is_file():
            return ""
        import json as _json
        cfg = _json.loads(fp.read_text(encoding="utf-8"))
        mx = cfg.get("max_budget_usd")
        if mx is None:
            return ""
        used = cost_summary(pid)["total_usd"]
        if used > float(mx):
            return f"本图预算超限：已用 ${used:.4f} > 上限 ${float(mx):.4f}"
    except LedgerCorruptError:
        return "成本账本损坏，预算无法核验——拒绝按空账继续"
    except Exception:
        return ""  # 读预算失败不误伤(与 pipeline 的 _read_budget 容错一致)
    return ""


def _exec_image_gen(graph: dict, node: dict, nodemap: dict[str, dict]) -> dict:
    from shipin_platform.generation.generate_assets import (
        generate_image_agnes, generate_image_pil)
    p = node.get("params") or {}
    # 轮68:reuse_image——管线项目导出的画布挂载已有首帧,零成本复用
    # (校验在 prompt 之前:复用节点可能没有上游连线提供的 prompt)
    reuse = str(p.get("reuse_image") or "")
    if reuse and Path(reuse).is_file():
        out = str(_artifact_path(graph["id"], node["id"], "png"))
        shutil.copyfile(reuse, out)
        return {"kind": "image", "value": out,
                "meta": {"reused": reuse, "model": "reuse"}}
    prompt = resolved_input(graph, node, "prompt", nodemap)
    if not prompt:
        raise ValueError("image_gen 需要提示词")
    w, h = int(p.get("width", 1280)), int(p.get("height", 720))
    model = p.get("model", "agnes-image-2.1-flash")
    out = str(_artifact_path(graph["id"], node["id"], "png"))
    if model == "pil":
        generate_image_pil(prompt, w, h, out)
    else:
        r = generate_image_agnes(prompt, w, h, out, model=model)
        if isinstance(r, dict) and not r.get("ok", True):
            raise RuntimeError(f"image_gen 失败: {r.get('error', r)}")
        # 轮50(九审 P1-4):花钱生成必须入账(pil 占位不花钱,不记)
        _record_graph_cost(graph, "image", model=str(model), units=1.0,
                           note=node["id"])
    return {"kind": "image", "value": out}


def _exec_video_gen(graph: dict, node: dict, nodemap: dict[str, dict]) -> dict:
    from shipin_platform.generation.generate_assets import generate_video_agnes
    p = node.get("params") or {}
    prompt = resolved_input(graph, node, "prompt", nodemap)
    first = resolved_input(graph, node, "first_frame", nodemap)
    last = resolved_input(graph, node, "last_frame", nodemap)
    if not first:
        raise ValueError("video_gen 需要首帧连线（image_gen 输出即可）")
    if not prompt:
        raise ValueError("video_gen 需要提示词")
    # 轮68:景别抬头 + 运动子句(画布上手选=实时改分镜景别/运镜)
    _SIZE = {"ecu": "extreme close-up", "cu": "close-up",
             "mcu": "medium close-up", "ms": "medium shot",
             "ls": "long shot"}
    motion = str(p.get("motion") or "").strip()
    full_prompt = (f"{_SIZE.get(str(p.get('shot_size') or ''), 'medium close-up')}. "
                   + (motion + " " if motion else "")
                   + str(prompt)).strip()
    engine = str(p.get("engine") or "agnes-cloud")
    dur = max(1, int(p.get("duration", 5) or 5))
    out = str(_artifact_path(graph["id"], node["id"], "mp4"))
    # 轮68:reuse_clip——管线项目已有验收 clip 时零成本复用(用户只改
    # 一句话/转场时不必重生视频);cleared 后该参数仍在,重跑即重生。
    reuse_clip = str(p.get("reuse_clip") or "")
    if reuse_clip and Path(reuse_clip).is_file() \
            and not str(p.get("force_regen") or "").lower() in ("1", "true"):
        shutil.copyfile(reuse_clip, out)
        _record_graph_cost(graph, "video", model="reuse", units=0.0,
                           note=node["id"])
        return {"kind": "video", "value": out,
                "meta": {"model": "reuse", "anchored": True,
                         "warnings": [], "reused": reuse_clip,
                         "transition": str(p.get("transition") or "softcut"),
                         "transition_duration": float(
                             p.get("transition_duration", 0.4) or 0.4)}}
    if engine in ("h3", "wan22", "wan21-flf2v"):
        # 本地引擎(DGX):H3 快;FLF2V 首尾帧锚定——与管线同底座,
        # 画布上换引擎即换生成质量策略(轮67 AB 定档)
        from shipin_platform.generation.local_media import local_video
        aspect = str(p.get("aspect") or "portrait")
        w, h = (720, 1280) if aspect == "portrait" else (1280, 720)
        # 轮68:32 倍数吸附——H3 拒收非 32 倍数(720x1280 直接 400,
        # 管线车道靠 _gen_dims_for_canvas 吸附官方预设,画布车道同样要)。
        # 上取整到 32(720→736);混合尺寸由 assemble 尺寸预检裁剪填充
        # 归一到首镜尺寸,「比例一刀切」不受影响。
        w = int((w + 31) // 32 * 32)
        h = int((h + 31) // 32 * 32)
        r = local_video(prompt=full_prompt, out=out,
                        first_frame=str(first),
                        last_frame=str(last) if last else "",
                        duration=dur, engine=engine, width=w, height=h)
        if not r.get("ok"):
            raise RuntimeError(f"本地 video_gen 失败({engine}): {r.get('error', r)}")
        model = engine
    else:
        model = str(p.get("model", "agnes-video-2.5-flash"))
        r = generate_video_agnes(
            prompt=full_prompt, model=model, duration=dur,
            first_frame=str(first),
            last_frame=str(last) if last else None,
            output_path=out, work_dir=str(_gdir(graph["id"]) / "artifacts"))
        if not r.get("ok"):
            raise RuntimeError(f"video_gen 失败: {r.get('error', r)}")
    # 轮50(九审 P1-4):视频按秒入账(与 costing 的 video 计价单位一致)
    _record_graph_cost(graph, "video", model=model, units=float(dur),
                       note=node["id"])
    return {"kind": "video", "value": out,
            "meta": {"model": r.get("model"),
                     "anchored": bool(r.get("anchored")),
                     "warnings": r.get("warnings", []),
                     # 轮68:转场参数随节点落账,assemble 据此逐边界选型
                     "transition": str(p.get("transition") or "softcut"),
                     "transition_duration": float(
                         p.get("transition_duration", 0.4) or 0.4)}}


def _exec_tts(graph: dict, node: dict, nodemap: dict[str, dict]) -> dict:
    from shipin_platform.services.tts_service import create_tts_service
    p = node.get("params") or {}
    text = resolved_input(graph, node, "text", nodemap)
    if not text:
        raise ValueError("tts 需要文案")
    art_dir = _gdir(graph["id"]) / "artifacts"
    # 轮57(真实使用发现,子智能体 B):第二参必须传 Path——旧代码传
    # str(roots.data_dir()/"voice_cast.db"),而 TtsService.__init__
    # 不包装 Path,_load_roles 的 self.db_path.exists() 直接
    # AttributeError → 画布 tts 节点 100% 失败(裸 AttributeError
    # 还不可操作)。pipeline/api 两个调用方都传 Path,仅画布车道崩。
    svc = create_tts_service(art_dir, roots.data_dir() / "voice_cast.db")
    seg = svc.build_segment(node["id"], str(text),
                            role_code=str(p.get("role", "biz_female")))
    res = svc.synthesize_segments_sync([seg])[0]
    # 轮50(九审 P1-2):以 synthesize 返回的 output_path 为权威值,失败
    # 显式抛——旧代码丢弃返回值后先找永远不存在的 {node_id}.mp3,再
    # sorted(art_dir.glob(f"{node_id}*"))[0](无 _ 分隔的前缀 glob +
    # 字典序首个):'n10_x.mp3' < 'n1_y.mp3'('0'<'_')→ 节点 n1 串到
    # n10 的音频;改文本重跑还可能选中上一轮旧音频;合成失败时 cands
    # 非空 → 返回旧音频且节点状态 ok。三类错都随 assemble 流出且画布
    # 车道无声画一致性门。与 pipeline 车道轮31「静默失败必须显式」同范式。
    out = str(getattr(res, "output_path", "") or "")
    err = str(getattr(res, "error", "") or "")
    if err or not out or not Path(out).is_file():
        raise RuntimeError(f"tts 合成失败: {err or '无输出文件'}")
    # 轮68:语速倍率(画布手滑)——VoxCPM 原生节奏偏快(实测~6字/s),
    # speed<1 用 atempo 后处理压档,>1 加速;1.0 原样。
    speed = float(p.get("speed", 1.0) or 1.0)
    if abs(speed - 1.0) > 0.01:
        from shipin_platform.generation import local_media as _lm
        fixed = str(Path(out).with_suffix("")) + f"_sp{speed}.mp3"
        r2 = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", out,
             "-filter:a", f"atempo={speed:.3f}",
             "-c:a", "libmp3lame", fixed],
            capture_output=True, text=True)
        if r2.returncode == 0 and Path(fixed).is_file():
            out = fixed
    _record_graph_cost(graph, "tts", model="tts-v1", units=1.0,
                       note=node["id"])
    return {"kind": "audio", "value": out}


def _exec_qc(graph: dict, node: dict, nodemap: dict[str, dict]) -> dict:
    from shipin_platform.review.clip_qc import qc_clip
    p = node.get("params") or {}
    video = resolved_input(graph, node, "video", nodemap)
    if not video:
        raise ValueError("qc 需要视频连线")
    exp = float(p.get("expected_duration", 5) or 5)
    r = qc_clip(str(video), shot_id=node.get("id", ""),
                expected_duration_sec=exp if exp > 0 else None,
                max_internal_cuts=int(p.get("max_internal_cuts", 0)),
                use_vlm=True)  # M5:逐镜同场验证默认开启,无 key 自动优雅降级
    rep = _artifact_path(graph["id"], node["id"], "json")
    rep.write_text(json.dumps(r, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    return {"kind": "report", "value": str(rep),
            "meta": {"verdict": r.get("verdict")}}


def _owned_artifact(gid: str, raw: Any) -> Path:
    """合成输入必须来自本图产物目录（连线上游已执行输出）。
    外部路径一概拒绝——ffmpeg 永远只吃受控文件。"""
    root = _gdir(gid).resolve()
    p = Path(str(raw)).resolve()
    if not p.is_relative_to(root):
        raise ValueError(f"拒绝外部路径作为合成输入: {p}")
    return p


def _exec_assemble(graph: dict, node: dict, nodemap: dict[str, dict]) -> dict:
    from shipin_platform.assembly import build_transition_stitch
    p = node.get("params") or {}
    clips: list[tuple[int, Path]] = []
    clip_srcs: list[dict] = []   # 轮68:每镜的源节点(取其转场参数)
    audios: list[tuple[int, Path]] = []  # 轮68:配音轨(一句话的载体)
    for e in graph.get("edges", []):
        src = nodemap.get(e.get("from"))
        if e.get("to") != node["id"] or not src:
            continue
        outs = ((src.get("state") or {}).get("outputs") or {})
        v = outs.get(e.get("from_port"), {}).get("value")
        if not v:
            continue
        if e.get("to_port") == "clips":
            clips.append((int(e.get("order", len(clips))),
                          _owned_artifact(graph["id"], v)))
            clip_srcs.append(src)
        elif e.get("to_port") == "audio":
            audios.append((int(e.get("order", len(audios))),
                           _owned_artifact(graph["id"], v)))
    if not clips:
        raise ValueError("assemble 至少需要连入一个镜头视频")
    order = sorted(range(len(clips)), key=lambda i: clips[i][0])
    clips = [clips[i] for i in order]
    clip_srcs = [clip_srcs[i] for i in order]
    audios = [a for _, a in sorted(audios, key=lambda x: x[0])]
    n = len(clips)
    paths = [str(c) for _, c in clips]
    windows = []
    for _, pth in clips:
        from shipin_platform.assembly import _ffprobe_duration  # noqa: PLC0415
        windows.append(round(_ffprobe_duration(pth), 2))
    # 轮68:逐边界转场——入镜节点的 transition 参数(画布上手选)优先,
    # 缺失回退 assemble.default_transition,再兜底硬切。
    default_bt = str(p.get("default_transition") or "cut")
    # 轮68:边界数 = 镜头数-1(第 i 个入镜的转场),勿按镜头全长收集
    bts = []
    for src in clip_srcs[1:]:
        sp = (src.get("params") or {})
        st = ((src.get("state") or {}).get("outputs") or {}) \
            .get("video", {}).get("meta") or {}
        bt = str(sp.get("transition") or st.get("transition")
                 or default_bt)
        bts.append(bt if bt == "cut" else bt)
    td = float(p.get("transition_duration", 0.4) or 0.4)
    fps = int(p.get("fps", 24) or 24)
    out = str(_artifact_path(graph["id"], node["id"], "mp4"))
    r = build_transition_stitch(clips=paths, windows=windows, output=out,
                                fps=fps,
                                transition_duration=td,
                                boundary_transitions=bts if n > 1 else None,
                                masters=None)
    if not r.get("ok"):
        raise RuntimeError(f"拼接失败: {r.get('error')}")
    print(f"[graph-engine] stitch ok dur={r.get('duration')} "
          f"expected={r.get('expected_sec')} bts={r.get('transitions')}",
          flush=True)
    result = {"kind": "video", "value": out,
              "meta": {"shots": n, "warnings": r.get("warnings", []),
                       "transitions": r.get("transitions")}}
    # 轮68:配音轨混音(burn_audio)——「一句话」在这里真正进入成片。
    # 复用管线的 master_audio(narration_events 逐镜 adelay,平台已验证
    # 不崩的通道,agent 不自混),无 BGM。
    if p.get("burn_audio") and audios:
        from shipin_platform.assembly import master_audio
        total = float(r.get("duration") or sum(windows))
        events, t = [], 0.0
        for (_, pth), a in zip(clips, audios):
            events.append({"path": str(a), "time": round(t, 3)})
            t += windows[len(events) - 1] if len(events) <= len(windows) else 0
        mixed = str(_artifact_path(graph["id"], node["id"], "mix.wav"))
        ma = master_audio(None, total, mixed,
                          narration_events=events)
        if ma.get("ok") and Path(mixed).is_file():
            final = str(_artifact_path(graph["id"], node["id"], "final.mp4"))
            rr = subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-i", out, "-i", mixed,
                 "-map", "0:v", "-map", "1:a", "-c:v", "copy",
                 "-c:a", "aac", "-shortest", final],
                capture_output=True, text=True)
            if rr.returncode == 0 and Path(final).is_file():
                result["value"] = final
                result["meta"]["audio_mixed"] = len(audios)
    if p.get("color_grade"):
        from shipin_platform.assembly import color_grade_warm
        graded = str(_artifact_path(graph["id"], node["id"], "grade.mp4"))
        color_grade_warm(result["value"], graded)
        result["value"] = graded
    return result
    if p.get("color_grade"):
        from shipin_platform.assembly import color_grade_warm
        graded = str(_artifact_path(graph["id"], node["id"], "grade.mp4"))
        color_grade_warm(out, graded)
        result["value"] = graded
    return result


def _exec_video_prompt(graph: dict, node: dict, nodemap: dict[str, dict]) -> dict:
    p = node.get("params") or {}
    content = str(p.get("content") or "").strip()
    if not content:
        content = str(resolved_input(graph, node, "board", nodemap) or "")
    return {"kind": "text", "value": content,
            "meta": {"stage": "video_prompt", "chars": len(content)}}


def _exec_review(graph: dict, node: dict, nodemap: dict[str, dict]) -> dict:
    """审核门：优先 AI 自动审查内容，其次人工判定；落盘留痕。

    auto_review=auto 时用固定 rubric 调 LLM 审查 target（接入内容），
    按模型 verdict 自动给出 pass/reject 并附发现的问题；无 LLM key 或
    无内容可审时降级人工判定（status 由 params 决定）。结果永远留痕。
    审核状态将作为下游运行闸门：非 pass 的下游执行会被 run_node 拦截。
    """
    p = node.get("params") or {}
    passed = None
    status_note = ""
    suggestion = ""
    auto = str(p.get("auto_review") or "manual").lower() == "auto"
    target = resolved_input(graph, node, "target", nodemap)
    if auto and isinstance(target, str) and target.strip():
        from shipin_platform.review.llm_review import llm_text_review
        try:
            r = llm_text_review(target[:6000])
            if r.get("available"):
                verdict = str(r.get("verdict") or "reject").lower()
                passed = verdict == "pass" or verdict == "approve"
                status = "pass" if passed else "reject"
                note = str(r.get("summary") or "").strip()
                ai_comments = []
                suggestion_parts = []
                for f in (r.get("findings") or [])[:5]:
                    sev = f.get("severity")
                    ai_comments.append(
                        f"[{sev}] {f.get('issue','')}")
                    fix = str(f.get("fix") or "").strip()
                    if fix and (sev == "critical" or len(suggestion_parts) < 3):
                        suggestion_parts.append(f"问题：{f.get('issue','')} → {fix}")
                if ai_comments:
                    note = (note + " " if note else "") \
                        + "；".join(ai_comments)
                if suggestion_parts:
                    suggestion = "；".join(suggestion_parts)[:800]
                note = note.strip(" ;；")[:600]
                status_note = (f"AI 自动审查：{note}" if note
                               else f"AI 自动审查通过（scores={r.get('scores')}）")
                status_note = status_note[:600]
            else:
                status_note = f"自动审查不可用（{r.get('reason','')}），回退人工判定"
        except Exception as ex:
            status_note = f"自动审查失败（{type(ex).__name__}），回退人工判定"
    if passed is None:
        status = str(p.get("status") or "pending")
        passed = status == "pass"
    record = {
        "node": node["id"],
        "display_name": str(p.get("display_name") or "审核"),
        "stage": p.get("stage", "script"),
        "status": status, "passed": passed,
        "comments": str(p.get("comments") or ""),
        "target_excerpt": str(target or "")[:1200],
        "auto_review": auto,
        "note": status_note,
        "reviewed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    rep = _artifact_path(graph["id"], node["id"], "json")
    rep.write_text(json.dumps(record, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    verdict_out = {"kind": "report", "value": str(rep),
                   "meta": {"verdict": status, "passed": passed,
                            "stage": record["stage"],
                            "comments": record["comments"],
                            "note": status_note,
                            "reviewed_at": record["reviewed_at"]}}
    # 通过的内容向下游转发（未通过则下游取不到输入，天然断流）
    content_out = ({"kind": "text", "value": target,
                    "meta": {"verdict": status, "passed": passed}}
                   if passed else {"kind": "text", "value": "",
                                   "meta": {"verdict": status,
                                            "passed": False, "blocked": True}})
    # AI 修改建议独立成端口：通过时也可带（锦上添花），驳回时最有价值
    suggestion_out = {"kind": "text", "value": suggestion,
                      "meta": {"verdict": status, "passed": passed,
                               "src": "auto" if auto else "manual"}}
    return {"verdict": verdict_out, "content": content_out,
            "suggestion": suggestion_out}


def _qc_gate_blocked(graph: dict, node: dict,
                     nodemap: dict[str, dict]) -> str | None:
    """质量门：目标消费的视频若被 QC 断过、或 QC 针对同一视频未通过 → 拦截。

    QC 节点独立于数据流主干（输出 report，不参与拼接），所以不能只查
    上游闭包——还要看：目标上游的视频（video_gen 产物）是否有对应质检
    节点。有 qc 节点把该视频（同一输出端口连线）作为输入、且 verdict
    不是 ok → 拦下；qc 节点自身可以先行运行出结论。
    """
    if node.get("type") in ("review", "qc"):
        return None
    # 1) 收集目标上游闭包内产生的视频（video_gen 或其他产出 video 的节点）
    closed: set[str] = {node["id"]}
    changed = True
    while changed:
        changed = False
        for e in graph.get("edges", []):
            if e.get("to") in closed and e.get("from") not in closed:
                closed.add(e.get("from"))
                changed = True
    video_srcs: set[str] = set()
    vid_port_of: dict[str, str] = {}
    for nid in closed:
        n = nodemap.get(nid)
        if not n:
            continue
        try:
            d = get_def(n["type"])
        except KeyError:
            continue
        for p in d.outputs:
            if p.kind == "video":
                video_srcs.add(nid)
                vid_port_of[nid] = p.name
    if not video_srcs:
        return None
    # 2) 找出针对这些视频的 qc 节点（qc 的 video 输入来自 video_srcs）
    verdicts: list[str] = []
    for qid, q in nodemap.items():
        if not q or q.get("type") != "qc":
            continue
        for e in graph.get("edges", []):
            if e.get("to") == qid and e.get("to_port") == "video" \
                    and e.get("from") in video_srcs:
                st = q.get("state") or {}
                o = ((st.get("outputs") or {}).get("report") or {})
                vq = (o.get("meta") or {}).get("verdict") if o else None
                if vq in ("ok", "pass"):
                    continue
                name = str((q.get("params") or {}).get("display_name")
                           or "QC 质检")
                if not st.get("ok") or vq is None:
                    reason = "质检未运行（先运行 QC 节点给出结论）"
                else:
                    reason = f"质检未通过：verdict={vq}"
                return (f"{name}（{q['id']}）拦截下游——{reason}。"
                        f"请先让质检通过（或 force 强制）")
    return None


def _exec_card(graph: dict, node: dict, nodemap: dict[str, dict]) -> dict:
    """成品卡：汇总成片 + 标题/标签/时长 → 卡片 JSON（供前端成品区/交付用）"""
    p = node.get("params") or {}
    video = resolved_input(graph, node, "final_video", nodemap)
    if not video:
        raise ValueError("card 需要成片连线")
    vp = _owned_artifact(graph["id"], video)
    from shipin_platform.assembly import _ffprobe_duration  # noqa: PLC0415
    dur = round(_ffprobe_duration(vp), 2)
    card = {
        "title": str(p.get("title") or "我的成片").strip(),
        "subtitle": str(p.get("subtitle") or "").strip(),
        "tags": [t.strip() for t in str(p.get("tags") or "").split(",") if t.strip()],
        "video": str(vp),
        "duration_sec": dur,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    rep = _artifact_path(graph["id"], node["id"], "json")
    rep.write_text(json.dumps(card, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    return {"kind": "card", "value": str(rep), "meta": card}


def _exec_dub(graph: dict, node: dict, nodemap: dict[str, dict]) -> dict:
    """轮73:配音驱动口型(InfiniteTalk 照帧重说)。

    输入首帧(人物近照)+驱动音频,输出嘴型跟着音频走的视频轨——音频轨
    本身由 assemble 从 tts 节点混入(视频与音频彻底分离,24G 显存档位的
    标准架构)。first_frame 取 image_gen 产物或视频首帧皆可。
    """
    from shipin_platform.generation.local_media import local_dub
    p = node.get("params") or {}
    first = resolved_input(graph, node, "first_frame", nodemap)
    audio = resolved_input(graph, node, "audio", nodemap)
    if not first:
        raise ValueError("dub 需要首帧连线（image_gen 输出即可）")
    if not audio:
        raise ValueError("dub 需要音频连线（tts 输出即可）")
    engine = str(p.get("engine") or "infinitetalk")
    if engine != "infinitetalk":
        raise ValueError(f"未知配音引擎 {engine!r}(仅 infinitetalk)")
    dur = max(1, int(p.get("duration", 5) or 5))
    out = str(_artifact_path(graph["id"], node["id"], "mp4"))
    aspect = str(p.get("aspect") or "portrait")
    w, h = (720, 1280) if aspect == "portrait" else (1280, 720)
    r = local_dub(str(first), str(audio), out, duration=dur,
                  engine=engine, width=w, height=h)
    if not r.get("ok"):
        raise RuntimeError(f"配音失败({engine}): {r.get('error', r)}")
    _record_graph_cost(graph, "video", model="infinitetalk-dub",
                       units=float(dur), note=node["id"])
    return {"kind": "video", "value": out,
            "meta": {"model": "infinitetalk-dub", "anchored": True,
                     "warnings": [], "dub": True,
                     "transition": str(p.get("transition") or "softcut"),
                     "transition_duration": float(
                         p.get("transition_duration", 0.4) or 0.4)}}


# 轮50(九审 P1-4):花钱节点类型——执行前过预算熔断、成功后入账
_SPEND_NODE_TYPES = ("image_gen", "video_gen", "tts", "dub")

EXECUTORS: dict[str, Callable[[dict, dict, dict], dict]] = {
    "text": _exec_text,
    "image_gen": _exec_image_gen,
    "video_gen": _exec_video_gen,
    "dub": _exec_dub,
    "tts": _exec_tts,
    "qc": _exec_qc,
    "assemble": _exec_assemble,
    "script": _exec_script,
    "storyboard": _exec_storyboard,
    "frame_prompts": _exec_frame_prompts,
    "video_prompt": _exec_video_prompt,    "review": _exec_review,
    "card": _exec_card,
}


# ---------------------------------------------------------------- 运行

def add_node(g: dict, type_: str, x: float = 0.0, y: float = 0.0,
             params: dict | None = None,
             title: str | None = None,
             created_by: str = "",
             connect_to: Optional[dict] = None) -> dict:
    """追加一个节点（校验类型后分配不重复 id）。

    轮71(AI 编排通道):x/y 缺省(<=0)时自动排布——旧实现默认 (0,0),
    外部 AI(Codex 等)经 POST /nodes 批量建节点全部叠在左上角,画布不可用。
    现在取现有节点的右/下方第一个空位(每节点占 260x170 槽位)。
    created_by: "ai" | "manual" | ""——来源标识,前端给 AI 节点打角标。
    connect_to: {"from": node_id, "port": "prompt"...} 建完即连到既有节点,
    解决 AI 建 video_gen 后不连 prompt/first_frame 的空节点问题。
    """
    get_def(type_)  # 未知类型抛 ValueError
    used = {n.get("id") for n in g.get("nodes", [])}
    i = 1
    while f"n{i}" in used:
        i += 1
    nodes = g.setdefault("nodes", [])
    if x <= 0 and y <= 0:
        occupied = {(round(float(n.get("x") or 0)),
                     round(float(n.get("y") or 0))) for n in nodes}
        x, y = 0.0, 0.0
        for gy in range(60):
            for gx in range(12):
                cand = (gx * 280 + 40, gy * 190 + 40)
                if cand not in occupied:
                    x, y = float(cand[0]), float(cand[1])
                    break
            if (round(x), round(y)) not in occupied and x:
                break
    node = {"id": f"n{i}", "type": type_, "x": x, "y": y,
            "params": params or {}}
    if title:
        node["title"] = title
    if created_by:
        node["created_by"] = created_by
    nodes.append(node)
    if connect_to and isinstance(connect_to, dict):
        src_id = str(connect_to.get("from") or "")
        src_port = str(connect_to.get("port") or "prompt")
        if src_id and any(n.get("id") == src_id for n in nodes):
            g.setdefault("edges", []).append({
                "from": src_id, "from_port": src_port,
                "to": node["id"], "to_port": connect_to.get("to_port") or "prompt",
                "order": len(g.get("edges", [])),
            })
    save_graph(g)
    return node


def remove_node(g: dict, node_id: str) -> bool:
    before = g.get("nodes", [])
    g["nodes"] = [n for n in before if n.get("id") != node_id]
    if len(g["nodes"]) == len(before):
        return False
    g["edges"] = [e for e in g.get("edges", [])
                  if e.get("from") != node_id and e.get("to") != node_id]
    save_graph(g)
    return True


def patch_node(g: dict, node_id: str, title: str | None,
               params: dict | None) -> dict:
    """增量更新节点（参数改动会经 input_hash 自动使下游重跑）。"""
    for n in g.get("nodes", []):
        if n.get("id") == node_id:
            if title is not None:
                n["title"] = title
            if params:
                n.setdefault("params", {}).update(params)
            save_graph(g)
            return n
    raise KeyError(node_id)


def _review_gate_blocked(graph: dict, node: dict,
                         nodemap: dict[str, dict]) -> str | None:
    """审核门强制拦截：目标节点的上游闭包内任一 review 节点未通过 → 拦下。

    状态判定：review 的已执行输出（state.outputs…meta.verdict）优先；
    未执行/无输出/执行失败一律视为「未通过」。返回拒绝理由或 None。
    """
    if node.get("type") == "review":
        return None  # 审核节点自身可以运行（先跑起来给结论）
    closed: set[str] = {node["id"]}
    changed = True
    while changed:
        changed = False
        for e in graph.get("edges", []):
            if e.get("to") in closed and e.get("from") not in closed:
                closed.add(e.get("from"))
                changed = True
    for rid in sorted(closed):
        r = nodemap.get(rid)
        if not r or r.get("type") != "review":
            continue
        p = r.get("params") or {}
        st = r.get("state") or {}
        verdict = None
        if st.get("ok") and st.get("outputs"):
            for out in (st.get("outputs") or {}).values():
                if isinstance(out, dict) and out.get("meta"):
                    verdict = out["meta"].get("verdict")
        if verdict == "pass":
            continue
        name = str(p.get("display_name") or "审核门")
        if verdict is None and not st.get("ok"):
            reason = "审核节点未运行或执行失败"
        elif verdict is None or not str(verdict).strip():
            reason = "待审核（先运行审核节点给出结论）"
        else:
            reason = f"驳回：{verdict}"
        cmt = str(p.get("comments") or "").strip()
        if cmt:
            reason += f"；意见：{cmt[:120]}"
        return (f"{name}（{r['id']}）拦截下游运行——{reason}。"
                f"请先通过审核（或 force 强制）")
    return None


def _upstream_order(g: dict, node_id: str) -> list[str]:
    indeg = {n["id"]: 0 for n in g.get("nodes", [])}
    adj: dict[str, list[str]] = {}
    for e in g.get("edges", []):
        f, t = e.get("from"), e.get("to")
        adj.setdefault(f, []).append(t)
        if t in indeg:
            indeg[t] += 1
    want: set[str] = set()
    stack = [node_id]
    while stack:
        u = stack.pop()
        if u in want:
            continue
        want.add(u)
        for e in g.get("edges", []):
            if e.get("to") == u:
                stack.append(e.get("from"))
    order: list[str] = []
    q = [nid for nid, d in indeg.items() if d == 0 and nid in want]
    while q:
        u = q.pop()
        order.append(u)
        for v in adj.get(u, []):
            if v in want and indeg[v] > 0:
                indeg[v] -= 1
                if indeg[v] == 0:
                    q.append(v)
    for nid in want:
        if nid not in order:
            order.append(nid)
    return order


def run_node(graph: dict, node_id: str, force: bool = False) -> dict:
    """执行 node_id 及其未运行/已变更的上游；返回目标节点状态。"""
    from .events import publish
    errs = validate_graph(graph)
    if errs:
        raise ValueError("图校验未通过: " + "; ".join(errs))
    nodemap = _nodemap(graph)
    if node_id not in nodemap:
        raise KeyError(f"node not found: {node_id}")
    order = _upstream_order(graph, node_id)
    publish(graph["id"], {"type": "run", "node_id": node_id,
                          "pending": order, "force": bool(force)})
    for nid in order:
        node = nodemap[nid]
        # 审核门：目标（运行按钮点击的节点）执行前，其上游闭包内
        # 的所有 review 必须已通过；force=True 显式强制跳过。
        if not force and not node["type"] in ("review", "qc"):
            blocked = _review_gate_blocked(graph, node, nodemap)
            if blocked:
                st = {"ok": False, "error": blocked,
                      "blocked_by_review": True,
                      "executed_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
                node["state"] = st
                save_graph(graph)
                publish(graph["id"], {"type": "node", "node_id": nid,
                                      "status": "failed", "kind": node.get("type"),
                                      "error": blocked,
                                      "blocked_by_review": True})
                raise RuntimeError(f"节点 {nid} 被审核门拦截: {blocked}")
            qblocked = _qc_gate_blocked(graph, node, nodemap)
            if qblocked:
                st = {"ok": False, "error": qblocked,
                      "blocked_by_review": True,
                      "executed_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
                node["state"] = st
                save_graph(graph)
                publish(graph["id"], {"type": "node", "node_id": nid,
                                      "status": "failed", "kind": node.get("type"),
                                      "error": qblocked,
                                      "blocked_by_review": True})
                raise RuntimeError(f"节点 {nid} 被质量门拦截: {qblocked}")
        h = node_input_hash(graph, node, nodemap)
        st = node.get("state") or {}
        if not force and st.get("input_hash") == h and st.get("ok"):
            continue
        fn = EXECUTORS.get(node.get("type"))
        if not fn:
            raise ValueError(f"未知节点类型: {node.get('type')}")
        # 轮50(九审 P1-4):花钱节点执行前预算熔断(与 pipeline 车道
        # 每 attempt 前复查同范式)——画布车道此前零预算闸,同一把 key
        # 在 pipeline 被 422、这里可无限花钱
        if node.get("type") in _SPEND_NODE_TYPES:
            bblocked = _budget_blocked(graph)
            if bblocked:
                st = {"ok": False, "error": bblocked,
                      "blocked_by_budget": True,
                      "executed_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
                node["state"] = st
                save_graph(graph)
                publish(graph["id"], {"type": "node", "node_id": nid,
                                      "status": "failed",
                                      "kind": node.get("type"),
                                      "error": bblocked,
                                      "blocked_by_budget": True})
                raise RuntimeError(f"节点 {nid} 预算熔断: {bblocked}")
        publish(graph["id"], {"type": "node", "node_id": nid,
                              "status": "running",
                              "kind": node.get("type")})
        try:
            out = fn(graph, node, nodemap)
            d = get_def(node["type"])
            out_names = [p.name for p in d.outputs]
            # 多输出执行器按端口名返回 dict；单输出直接包到第一个端口
            if isinstance(out, dict) and "kind" in out:
                outputs = {out_names[0]: out}
            else:
                outputs = {k: v for k, v in out.items()
                           if k in out_names and isinstance(v, dict)
                           and v.get("kind")}
            node["state"] = {
                "ok": True, "input_hash": h,
                "outputs": outputs,
                "executed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            meta = next(iter(outputs.values()), {}).get("meta") or {}
            publish(graph["id"], {"type": "node", "node_id": nid,
                                  "status": "ok",
                                  "kind": node.get("type"),
                                  "meta": meta})
        except Exception as ex:
            node["state"] = {
                "ok": False, "error": str(ex), "input_hash": h,
                "executed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            publish(graph["id"], {"type": "node", "node_id": nid,
                                  "status": "failed",
                                  "kind": node.get("type"),
                                  "error": str(ex)})
            save_graph(graph)
            raise RuntimeError(f"节点 {nid} 执行失败: {ex}") from ex
    save_graph(graph)
    return dict(nodemap[node_id]["state"]) | {"node_id": node_id}


def run_all(graph: dict, force: bool = False) -> dict:
    """运行全图（ComfyUI 的 Queue Prompt）：拓扑序逐个执行非审核节点。

    review 节点跳过（它的职责是拦截而不是自己花钱跑）；结果里每个
    executed=失败的节点携带 error/blocked 原因，供前端逐节点标注。
    """
    from .events import publish
    errs = validate_graph(graph)
    if errs:
        raise ValueError("图校验未通过: " + "; ".join(errs))
    nodemap = _nodemap(graph)
    gid = graph.get("id", "")
    # 全局拓扑序（含孤立节点），审核门语义照旧由 run_node 内部执行
    order: list[str] = []
    indeg = {n["id"]: 0 for n in graph.get("nodes", [])}
    adj: dict[str, list[str]] = {}
    for e in graph.get("edges", []):
        f, t = e.get("from"), e.get("to")
        adj.setdefault(f, []).append(t)
        if t in indeg:
            indeg[t] += 1
    q = [nid for nid, d in indeg.items() if d == 0]
    seen: set[str] = set()
    while q:
        u = q.pop()
        if u in seen:
            continue
        seen.add(u)
        order.append(u)
        for v in adj.get(u, []):
            if v in indeg and indeg[v] > 0:
                indeg[v] -= 1
                if indeg[v] == 0:
                    q.append(v)
    for nid in nodemap:
        if nid not in seen:
            order.append(nid)
    publish(gid, {"type": "run_all", "pending": order, "force": bool(force)})
    results = {"order": order, "nodes": {}}
    failed: list[str] = []
    for nid in order:
        n = nodemap[nid]
        # 轮57(真实使用发现,子智能体 B):上游已败的节点不再执行,标记
        # skipped + upstream_failed——旧代码让下游也去跑(或复述上游
        # 错误),无法区分「本节点失败」与「上游失败被跳过」,且
        # run-all 顶层恒 ok:true(只读 ok 的 agent 会判定全图成功)。
        _up_bad = [u for u in _upstream_of(graph, nid)
                   if results["nodes"].get(u, {}).get("ok") is False]
        if _up_bad:
            st = {"ok": False, "skipped": True,
                  "upstream_failed": _up_bad,
                  "error": f"上游 {_up_bad[0]} 失败,跳过本节点执行",
                  "executed_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
            n["state"] = st
            results["nodes"][nid] = {"ok": False, "state": st,
                                     "skipped": True,
                                     "upstream_failed": _up_bad}
            failed.append(nid)
            save_graph(graph)
            publish(gid, {"type": "node", "node_id": nid, "status": "skipped",
                          "kind": n.get("type"),
                          "error": st["error"]})
            continue
        try:
            st = run_node(graph, nid, force=force)
            entry = {"ok": bool(st.get("ok")), "state": st,
                     "blocked": bool(st.get("blocked_by_review")
                                     or st.get("blocked_by_budget")
                                     or st.get("blocked_by_qc"))}
            results["nodes"][nid] = entry
            if not entry["ok"]:
                failed.append(nid)
        except RuntimeError as ex:
            msg = str(ex)
            results["nodes"][nid] = {
                "ok": False,
                "error": msg,
                "blocked": "拦截" in msg,
                "state": n.get("state"),
            }
            failed.append(nid)
        except (ValueError, KeyError) as ex:
            results["nodes"][nid] = {"ok": False, "error": str(ex),
                                     "state": n.get("state")}
            failed.append(nid)
    save_graph(graph)
    results["failed"] = failed
    results["ok"] = not failed  # 轮57:有失败节点就不是全图成功(消灭假成功)
    return results