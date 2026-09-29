"""轮68:管线产物 → 画布图 的桥接器(验收闭环的地基)。

把一条 pipeline 项目(script/storyboard/image_prompt/video_prompt/manifest)
摊成一张画布图:每镜一组可编辑节点——「旁白/台词」text 节点(用户看见的
那句话)、tts 配音节点、视频提示词 text 节点、image_gen 首帧、video_gen
镜头节点(景别/motion/转场/引擎全部可改),末尾 assemble 按顺序拼接。
已有素材(first_frame/clip)以 reuse 参数挂载:用户只改一句话时不必重生
视频,改完重跑 tts/assemble 即可。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from shipin_platform import roots

# 景别映射:storyboard.shot_size(中文) → 画布 select 值
_SIZE_MAP = [
    (("大特写", "ecu"), "ecu"), (("特写", "cu"), "cu"),
    (("中近", "mcu"), "mcu"), (("中景", "ms"), "ms"),
    (("全景", "ls"), "ls"), (("远景", "ls"), "ls"),
]

# 转场映射:keyframe_plan.boundary → 画布 select 值
_TRANS_MAP = {"dissolve": "dissolve", "cut": "cut", "softcut": "softcut",
              "fade": "fade"}


def _shot_size(raw: str) -> str:
    t = str(raw or "")
    for keys, val in _SIZE_MAP:
        if any(k in t for k in keys):
            return val
    return "mcu"


def _transition(raw: str) -> str:
    return _TRANS_MAP.get(str(raw or "").strip().lower(), "softcut")


def _pdir(project_id: str) -> Path:
    return roots.data_dir() / "projects" / project_id


def _load(p: Path, name: str) -> dict:
    f = p / name
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {}


def graph_from_project(project_id: str,
                       aspect: str = "portrait") -> dict[str, Any]:
    """摊平项目为画布图(不落盘;由调用方建图保存)。返回 graph dict。"""
    p = _pdir(project_id)
    if not p.is_dir():
        raise FileNotFoundError(f"项目不存在: {project_id}")
    storyboard = _load(p, "storyboard.json")
    script = _load(p, "script.json")
    manifest = _load(p, "manifest.json")
    brief = _load(p, "brief.json")
    img_prompts = {x.get("shot_id"): x
                   for x in _load(p, "image_prompt.json").get("shot_prompts", [])}
    vid_prompts = {x.get("shot_id"): x
                   for x in _load(p, "video_prompt.json").get("shot_prompts", [])}
    man_shots = manifest.get("shots") or {}

    shots = storyboard.get("shots") or script.get("shots") or []
    if not shots:
        raise ValueError(f"项目无分镜: {project_id}")

    nodes: list[dict] = []
    edges: list[dict] = []
    nid = {"i": 0}

    def _add(type_: str, x: float, y: float, title: str,
             params: Optional[dict] = None) -> str:
        nid["i"] += 1
        node_id = f"n{nid['i']}"
        nodes.append({"id": node_id, "type": type_, "x": x, "y": y,
                      "params": params or {}, "title": title,
                      # 轮71:管线摊出的节点全部是 AI 生成的——前端打
                      # AI 角标,用户一眼分辨「哪些是自动搭的、哪些手改的」
                      "created_by": "ai"})
        return node_id

    def _edge(frm: str, frm_port: str, to: str, to_port: str, order: int = 0):
        edges.append({"from": frm, "from_port": frm_port, "to": to,
                      "to_port": to_port, "order": order})

    # 阶段链:brief → script → storyboard(整片可见可改)
    brief_n = _add("text", 40, 40, "需求简报",
                   {"text": json.dumps(brief, ensure_ascii=False)[:1500]
                    if brief else ""})
    script_n = _add("script", 40, 220, "剧本(整片)",
                    {"content": json.dumps(script.get("shots", []),
                                           ensure_ascii=False, indent=1)})
    board_n = _add("storyboard", 40, 400, "分镜(整片)",
                   {"content": json.dumps(shots, ensure_ascii=False,
                                          indent=1)})
    _edge(brief_n, "text", script_n, "brief")
    _edge(script_n, "progress", board_n, "script")

    video_nodes: list[str] = []
    for i, s in enumerate(shots):
        sid = str(s.get("shot_id") or f"S{i+1:02d}")
        x = 420.0 + i * 340.0
        mrec = man_shots.get(sid) or {}
        dur = float(s.get("duration_sec") or 3)

        # ── 用户看见的那句话(旁白 + 台词,各自独立可编辑) ──
        nar = str(s.get("narration") or "").strip()
        dlg = s.get("dialogue")
        dtext = str((dlg or {}).get("text") or "").strip() if isinstance(dlg, dict) else ""
        role = str((dlg or {}).get("role_code") or "biz_female") if isinstance(dlg, dict) else "biz_female"
        spoken = nar if (nar and not dtext) else (dtext or nar)
        spoken_node = _add("text", x, 40, f"{sid} 一句话(旁白/台词)",
                           {"text": spoken})
        tts_n = _add("tts", x, 170, f"{sid} 配音",
                     {"role": role, "speed": 1.0})
        _edge(spoken_node, "text", tts_n, "text")

        # ── 视频提示词(镜内运动,可改) ──
        vp = vid_prompts.get(sid) or {}
        vp_text = str(vp.get("prompt_text") or vp.get("prompt_en")
                      or f"{s.get('motion', '')} Scene: {s.get('scene', '')}")
        vp_node = _add("video_prompt", x, 300, f"{sid} 视频提示词",
                       {"content": vp_text})

        # ── 首帧图(复用已有 or 生图) ──
        ip = img_prompts.get(sid) or {}
        first_ref = str(mrec.get("first_frame") or "")
        if first_ref and Path(first_ref).is_file():
            # 已有首帧:image_gen + reuse_image 挂载(端口类型仍是 image,
            # 画布连线规则不破;执行时零成本拷贝,不调生图)
            img_node = _add("image_gen", x, 430, f"{sid} 首帧(复用)",
                            {"reuse_image": first_ref,
                             "prompt": "(复用管线首帧)",
                             "width": 720 if aspect == "portrait" else 1280,
                             "height": 1280 if aspect == "portrait" else 720})
            img_port = "image"
        else:
            img_node = _add("image_gen", x, 430, f"{sid} 首帧生图",
                            {"prompt": str(ip.get("prompt_en")
                                          or s.get("scene") or ""),
                             "width": 720 if aspect == "portrait" else 1280,
                             "height": 1280 if aspect == "portrait" else 720})
            img_port = "image"
            _edge(vp_node, "prompt", img_node, "prompt")

        # ── 镜头节点:剪辑方式全参数 + 素材复用 ──
        last_ref = str(mrec.get("last_frame") or "")
        clip_ref = str(mrec.get("clip") or "")
        vparams = {
            "engine": "h3",
            "shot_size": _shot_size(str(s.get("shot_size") or "")),
            "motion": str(s.get("motion") or ""),
            "transition": _transition(str(mrec.get("boundary")
                                          or s.get("boundary") or "softcut")),
            "transition_duration": 0.4,
            "duration": int(max(2, min(10, round(dur)))),
            "aspect": aspect,
            "prompt": vp_text,
        }
        if clip_ref and Path(clip_ref).is_file():
            vparams["reuse_clip"] = clip_ref
        vg = _add("video_gen", x, 560, f"{sid} 镜头", vparams)
        _edge(img_node, img_port, vg, "first_frame")
        if last_ref and Path(last_ref).is_file():
            tail = _add("image_gen", x + 170, 430, f"{sid} 尾帧(复用)",
                        {"reuse_image": last_ref,
                         "prompt": "(复用管线尾帧)"})
            _edge(tail, "image", vg, "last_frame")
        video_nodes.append(vg)

    # ── 尾部:assemble 按连线顺序拼接(转场取各镜节点参数) ──
    asm = _add("assemble", 420.0 + len(shots) * 340.0 / 2 - 160, 760,
               "拼接出片",
               {"fps": 24, "color_grade": True, "burn_audio": True,
                "default_transition": "softcut", "transition_duration": 0.4})
    for k, v in enumerate(video_nodes):
        _edge(v, "video", asm, "clips", order=k)
    for i, s in enumerate(shots):
        sid = str(s.get("shot_id") or f"S{i+1:02d}")
        tts_nodes = [n for n in nodes if n.get("title") == f"{sid} 配音"]
        if tts_nodes:
            _edge(tts_nodes[0]["id"], "audio", asm, "audio", order=i)

    return {"name": f"项目画布·{project_id}", "nodes": nodes, "edges": edges}
