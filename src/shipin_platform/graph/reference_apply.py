"""参考复刻包 → 画布节点链（一键注入）。

把 reference_pipeline 的产物（script.json / shots.json / brief.json）转换成
现有 REGISTRY 节点链并自动连线，用户在原画布上继续跑 image_gen/video_gen
复刻：

    [text 参考简报（brief 预填）]
        └─> [script 剧本] ─> [storyboard 分镜] ─> [frame_prompts]
                                          └───> [video_prompt]

分镜内容按镜头逐条写入 storyboard 节点 content；首尾帧提示词写入
frame_prompts；视频提示词写入 video_prompt。连线按 kind 匹配（text→text）。
直接通过 engine.add_node / save_graph 落盘，经 validate_graph 校验后返回。

安全：只读本地 ref_dir 下的 JSON；不新增节点类型；输出全部经 validate_graph。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from shipin_platform.graph import engine


class ReferenceApplyError(ValueError):
    """参考包注入画布失败（可安全转 4xx）。"""


def _load_json(p: Path) -> dict:
    if not p.is_file():
        raise ReferenceApplyError(f"参考包缺少文件: {p.name}")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ReferenceApplyError(f"参考包 JSON 损坏: {p.name} ({e})") from e


def _fmt_lines(script_pack: dict) -> str:
    """剧本正文：每镜一行（镜头号 + 台词 + 参考时码）。"""
    out = []
    for ln in script_pack.get("lines", []):
        shot = ln.get("shot", "?")
        time = ln.get("time", "")
        dia = (ln.get("dialogue") or "").strip()
        out.append(f"[镜头{shot} @{time}] {dia}".rstrip())
    return "\n".join(out) or "（参考视频无反推台词，请在画布上补充）"


def _fmt_storyboard(shots: list[dict]) -> str:
    """分镜正文：每镜一行（机位/光线/色调/构图 + 英文提示词）。"""
    out = []
    for s in shots:
        cam = (s.get("camera") or "").strip()
        light = (s.get("lighting") or "").strip()
        tone = (s.get("tone_and_palette") or "").strip()
        comp = (s.get("composition") or "").strip()
        _img = (s.get("image_prompt") or "").strip()
        parts = "、".join(x for x in (cam, light, tone, comp) if x)
        out.append(f"[镜头{s.get('idx', '?')}] {parts}"
                   + (f" || {_img}" if _img else ""))
    return "\n".join(out) or "（参考视频无反推分镜，请在画布上补充）"


def build_reference_graph(
    ref_dir: str | Path,
    graph_id: str,
    *,
    script_pack: Optional[dict] = None,
) -> dict:
    """把参考包注入已有画布 graph_id，返回 {ok, graph_id, nodes, edges}。"""
    rd = Path(ref_dir).resolve()
    pack = script_pack or _load_json(rd / "script.json")
    shot_json = _load_json(rd / "shots.json")
    brief = _load_json(rd / "brief.json").get("brief_prefill") or {}

    g = engine.load_graph(graph_id)   # 不存在抛 FileNotFoundError → 上层 404
    start = len(g.get("nodes", []))

    # 1) 简报 → text 节点（brief 预填作为首段内容，供 script 参考）
    brief_text = "、".join(f"{k}: {v}" for k, v in brief.items() if v)
    t_node = engine.add_node(
        g, "text", x=40, y=80 + 40 * start,
        params={"text": brief_text or "参考视频复刻（无简报预填，请在画布补充）"},
        title="参考简报")

    # 2) 剧本 → script 节点（内容 = 逐镜台词 + 时码）
    s_node = engine.add_node(
        g, "script", x=330, y=40 + 40 * start,
        params={"content": _fmt_lines(pack)},
        title=f"剧本·{str(pack.get('title') or '参考')[:12]}")

    # 3) 分镜 → storyboard（逐镜机位/构图/提示词）
    b_node = engine.add_node(
        g, "storyboard", x=620, y=40 + 40 * start,
        params={"content": _fmt_storyboard(shot_json)},
        title="分镜·参考反推")

    # 4) 首尾帧提示词 → frame_prompts（逐镜一行）
    firsts, lasts = [], []
    for ln in pack.get("lines", []):
        ip = (ln.get("image_prompt") or "").strip()
        if ip:
            firsts.append(f"[镜头{ln.get('shot', '?')}] {ip}")
    fp_node = engine.add_node(
        g, "frame_prompts", x=900, y=20 + 40 * start,
        params={"first_prompt": "\n".join(firsts) or "（参考无首帧提示词，请补充）",
                "last_prompt": ""},
        title="首帧提示词·参考")

    # 5) 视频提示词 → video_prompt（每镜运动描述一行，供 video_gen 使用）
    vp_lines = [
        f"[镜头{ln.get('shot', '?')}] {(ln.get('video_prompt') or '').strip()}"
        for ln in pack.get("lines", [])
        if (ln.get("video_prompt") or "").strip()
    ]
    vp_node = engine.add_node(
        g, "video_prompt", x=900, y=260 + 40 * start,
        params={"content": "\n".join(vp_lines)
                or "（参考视频未反推出镜头运动，请在画布补充）"},
        title="镜头运动·参考")

    # 连线（kind 不匹配由 engine.validate_graph 兜底拒绝）
    edges = [
        {"from": t_node["id"], "from_port": "text", "to": s_node["id"],
         "to_port": "brief", "order": 0},
        {"from": s_node["id"], "from_port": "progress", "to": b_node["id"],
         "to_port": "script", "order": 1},
        {"from": b_node["id"], "from_port": "board", "to": fp_node["id"],
         "to_port": "board", "order": 2},
        {"from": b_node["id"], "from_port": "board", "to": vp_node["id"],
         "to_port": "board", "order": 3},
    ]
    g["edges"] = g.get("edges", []) + edges
    errs = engine.validate_graph(g)
    if errs:
        raise ReferenceApplyError(
            f"生成的画布校验不通过: {'; '.join(errs[:5])}")
    engine.save_graph(g)

    return {
        "ok": True,
        "graph_id": graph_id,
        "nodes_added": len(g["nodes"]) - start,
        "edges_added": len(edges),
        "nodes": [n for n in g["nodes"] if n.get("title")],
        "edges": g["edges"],
    }