"""Server-side pipeline runner - the ComfyUI-ification of the workflow.

用户痛点(coffee-v5/v6 复盘):编排权在 agent 手里时,SKILL 写得再细子
智能体也会走偏;首尾帧链不链、失败了怎么重试、转场用切还是叠化,这些
判断分散在文档里靠 agent 自觉。本模块把整个制作流程收进平台,agent 只剩
四个调用:

    POST /api/pipeline/text      → 服务端 LLM 生成剧本/分镜 + 审核循环
    POST /api/project/confirm    → 人工确认闸门(不变)
    POST /api/pipeline/generate  → 首帧图→锚定视频→QC→重试→TTS→对齐
    POST /api/pipeline/assemble  → 转场拼接→调色→字幕→声音设计→终验→发布

所有判断都是确定性代码;LLM 只在生成文案/语义审查两个点出现,输出必须
通过同一套规则引擎。

首尾帧链式策略(coffee-v6 机制化):
- 相邻两镜景别相差 ≤1 档 且 主场景词相同 → **链式**:last_frame = 下一镜
  首帧图,拼接边界用硬切(动作无缝接力);
- 否则 → **自末帧**:为本镜生成"动作完成态"末帧图,拼接边界用 dissolve。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from shipin_platform import roots
from typing import Optional

from shipin_platform.assembly import (
    align_narration, build_transition_stitch, master_audio, kenburns,
    mux_audio_video, normalize_loudness, color_grade_warm, burn_srt,
    loudness_measure, DEFAULT_TD,
)
from shipin_platform.review.engine import ReviewEngine
from shipin_platform.review.clip_qc import qc_clip
from shipin_platform.review.llm_review import llm_stage_review
from shipin_platform.generation.generate_assets import (
    generate_image_agnes, generate_video_agnes,
)
from shipin_platform.services.template_registry import (
    get_registry as get_template_registry,
)
from shipin_platform.services.component_registry import (
    get_registry as get_component_registry,
)
from shipin_platform.services.costing import record_cost

ROOT = roots.data_root()
PROJECTS_DIR = ROOT / "data" / "projects"
BGM_PATH = ROOT / "assets" / "bgm" / "warm_60s.wav"

SIZE_ORDER = ["ecu", "cu", "mcu", "cs", "ms", "ws", "ows"]
LOCATION_TOKENS = ["写字楼", "街道", "街", "门口", "门", "吧台", "窗边", "店内", "落版"]

SCRIPT_PROMPT = """你是严格的 TVC 编剧。依据 brief 创作剧本,输出严格 JSON(无 markdown、无解释):
{"duration_sec": <int>, "shots": [{"shot_id": "S01", "duration_sec": <2-5>, "narration": "<一句≤14字>", "scene": "<单一地点单一事件,≤30字>"}]}
硬规则:镜头 5-10 个(短片取下限,保证每镜 ≥2.5s);结构 hook→pain→turn→value→outro(末镜=品牌落版);总旁白字数 ≈ duration×2.7;禁"然后";每个 scene 只有一个地点一个事件(单镜头必须可连续拍完);品牌元素≥3镜。
落版镜专项:末镜必须是【静态可拍画面】--暖色调背景上产品静置,品牌名与 slogan 以后期叠加字幕呈现(不在 scene 里写"叠加/浮现/多景切换"等非拍摄描述);末镜 narration=品牌名+slogan。

brief:
{brief}"""

STORYBOARD_PROMPT = """你是严格的 TVC 分镜师。把剧本展开为分镜，输出严格 JSON（无 markdown、无解释）：
{"hero_shot": "<shot_id>", "shots": [{"shot_id": "...", "duration_sec": <秒>, "beat": "hook|pain|turn|value|outro", "rhythm": "slow|medium|fast", "sfx": "sfx_<n>", "shot_size": "ecu|cu|mcu|cs|ms|ws|ows", "subject": "<与全片逐字一致的主角锚定>", "motion": "<英文运动短语，必含具体动作动词+速度/幅度词，如 slowly lifts the cup / turns her head gently / steam rises softly，禁中文>", "scene": "<单一地点>", "spatial": "<构图>", "camera": "<机位/运动术语：dolly/truck/crane/pedestal，禁zoom表移动>", "cause": "<承接上一镜的可拍视觉过渡>", "effect": "<给下一镜的可拍衔接点>", "narration": "<逐字复制剧本旁白>"}]}
硬规则：每镜只一个连续动作；motion 必须是英文且含动作动词与速度/幅度副词（i2v 规则强校验）；相邻镜 shot_size 必须不同档；主角锚定逐字一致（第一镜的 subject 后续镜逐字复制）；相邻镜间 cause/effect 必须可拍（动作接力/视线/光线）；末镜为品牌落版（静态画面+大字 logo，motion 写 static product shot with soft light drift）；品牌元素≥3镜。

剧本:
{script}

主角锚定模板(第一镜 subject 用它,后续逐字复制):主角:25岁左右年轻女性,黑色长直发披肩、米白色针织开衫、深灰色围巾"""


def _project_dir(project_id: str) -> Path:
    d = PROJECTS_DIR / project_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save(project_id: str, name: str, data) -> Path:
    p = _project_dir(project_id) / name
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    # P2：可回溯产物每次落盘都全量快照（内容哈希去重；回滚走 /restore）
    try:
        from shipin_platform.services.artifact_store import (
            snapshot as _art_snapshot)
        _art_snapshot(p.parent, name.rsplit(".", 1)[0], data)
    except Exception:  # noqa: BLE001 - 版本化失败不阻断主流程
        pass
    return p


def _load(project_id: str, name: str):
    p = _project_dir(project_id) / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


SCRIPT_REPAIR_PROMPT = """你是严格的 TVC 编剧。下面是一份剧本 JSON 和审片意见。逐条修复所有"必改"项(其余内容与字段保持原样不动),输出修复后的完整剧本 JSON(同一 schema,无 markdown、无解释)。

剧本:
{data}

审片意见(逐条修复):
{plan}"""

STORYBOARD_REPAIR_PROMPT = """你是严格的 TVC 分镜师。下面是一份分镜 JSON 和审片意见。逐条修复所有"必改"项(其余内容与字段保持原样不动),输出修复后的完整分镜 JSON(同一 schema,无 markdown、无解释)。

分镜:
{data}

审片意见(逐条修复):
{plan}"""


def _repair(kind: str, data: dict, revision_plan: list[str]) -> Optional[dict]:
    """按审片意见定向修复(不是盲重生--盲重生成会引入新问题)。"""
    tpl = SCRIPT_REPAIR_PROMPT if kind == "script" else STORYBOARD_REPAIR_PROMPT
    prompt = tpl.replace("{data}", json.dumps(data, ensure_ascii=False)
                         ).replace("{plan}", "\n".join(revision_plan[:12]))
    return _llm_json(prompt, max_tokens=5000)


def _llm_json(prompt: str, max_tokens: int = 4000, retries: int = 2) -> Optional[dict]:
    """服务端 LLM 调用 + 严格 JSON 解析(失败收紧重试)。"""
    from shipin_platform.review.llm_review import _ask_llm, _llm_key
    key = _llm_key()
    if not key:
        return None
    raw = None
    for attempt in range(retries + 1):
        try:
            p = prompt if attempt == 0 else prompt + \
                "\n\n再次强调:只输出一个 JSON 对象本身,不要 markdown 代码块与解释。"
            raw = _ask_llm(p, key, max_tokens=max_tokens)
            m = re.search(r"\{.*\}", raw, re.S)
            if m:
                return json.loads(m.group(0))
        except Exception:
            continue
    return None


def _iterate(stage: str, data: dict, project_id: str, store,
             use_llm: bool = True, max_rounds: int = 3) -> dict:
    """审核循环(与 /api/review/iterate 同逻辑 + 状态机记录)。"""
    from shipin_platform.contracts import stable_artifact_hash
    engine = ReviewEngine({"max_rounds": {stage: max_rounds}})
    data = json.loads(json.dumps(data, ensure_ascii=False))
    rounds = []
    brief_ctx = None
    if stage == "script":
        b = _load(project_id, "brief.json")
        brief_ctx = b
    llm_info = {}
    for round_num in range(1, max_rounds + 1):
        report = engine.run_review(stage, data, round_num=round_num)
        rounds.append(report.to_dict())
        if report.decision.value in ("pass", "pass_with_warnings", "stall", "stop"):
            break
        fix = engine.revision.fix(stage, data, report)
        data = fix["data"]
    final = rounds[-1]
    if use_llm and stage in ("script", "storyboard"):
        llm = llm_stage_review(stage, data, brief=brief_ctx)
        if llm["available"]:
            llm_info = {"scores": llm.get("scores", {}), "n": len(llm["findings"])}
            for f in llm["findings"]:
                cls = engine.classifier.classify(stage, f["issue"], f["evidence"])
                final["findings"].append({
                    "dimension": f"llm_{f['dimension']}", "severity": f["severity"],
                    "issue": f["issue"], "evidence": f["evidence"],
                    "failure_mode": cls["mode"], "revision_strategy": cls["strategy"],
                    "proposed_fix": f["fix"], "status": "pending"})
            final["stats"]["critical"] = sum(
                1 for f in final["findings"] if f["severity"] == "critical")
            if final["stats"]["critical"] > 0 and final["decision"] in ("pass", "pass_with_warnings"):
                final["decision"] = "revise"

        # LLM critical 的 fix 同步进 revision_plan（否则 _repair 只修规则项，
        # 语义项原样保留 → 纯 LLM 循环不收敛，real-guard-8 实证）
        if use_llm:
            for f in final["findings"]:
                if f.get("severity") == "critical" and f.get("proposed_fix"):
                    final["revision_plan"].insert(
                        0, f"[LLM审片] {f['dimension']}: {f['issue'][:80]} "
                           f"→ 修复: {f['proposed_fix'][:150]}")

    decision = final["decision"]
    h = stable_artifact_hash(data)
    if decision in ("pass", "pass_with_warnings"):
        store.record_artifact(project_id, stage, h)
    else:
        store.record_artifact(project_id, stage, h, status="BLOCKED")
    _save(project_id, f"{stage}.json", data)
    _save(project_id, f"{stage}_review.json",
          {"decision": decision, "rounds": rounds, "llm": llm_info})
    return {"stage": stage, "decision": decision, "data": data,
            "stats": final["stats"], "revision_plan": final.get("revision_plan", []),
            "llm": llm_info}


# ── Phase 1: TEXT ─────────────────────────────────────────────────────


def _script_prompt(brief_data: dict, category: Optional[str] = None) -> str:
    """按品类组装剧本 prompt。

    缺省 category / tvc / 未注册品类 → 返回官方 SCRIPT_PROMPT(逐字节
    不变,保证既有管线行为零偏差);显式品类用模板硬规则拼装。
    模板注册表异常只回退默认,绝不阻塞生成链路。
    """
    brief_json = json.dumps(brief_data, ensure_ascii=False)
    if not category:
        return SCRIPT_PROMPT.replace("{brief}", brief_json)
    try:
        tpl = get_template_registry().get(category)
    except Exception:
        return SCRIPT_PROMPT.replace("{brief}", brief_json)
    if tpl.category == "tvc" or not tpl.rules:
        return SCRIPT_PROMPT.replace("{brief}", brief_json)
    schema = ('{"duration_sec": <int>, "shots": [{"shot_id": "S01", '
              '"duration_sec": <2-5>, "narration": "<一句≤14字>", '
              '"scene": "<单一地点单一事件,≤30字>"}]}')
    return (
        f"你是严格的{tpl.name}编剧。依据 brief 创作剧本,输出严格 JSON"
        f"(无 markdown、无解释):\n{schema}\n{tpl.rules}\n"
        f"落版镜专项:{tpl.landing_note}\n\nbrief:\n{brief_json}")


def run_text_phase(project_id: str, brief: dict, store,
                   category: Optional[str] = None) -> dict:
    """brief→剧本→分镜 全部服务端生成+审核。LLM 生成失败或审核不收敛时
    返回 blocked 与原因(agent 只转述,不自行创作)。"""
    steps = []
    # brief 审核(无 LLM)
    b = _iterate("brief", brief, project_id, store, use_llm=False)
    steps.append({"stage": "brief", "decision": b["decision"]})
    if b["decision"] not in ("pass", "pass_with_warnings"):
        return {"ok": False, "phase": "text", "steps": steps,
                "reason": f"brief 审核不通过: {b['stats']}"}
    _save(project_id, "brief.json", b["data"])

    # 剧本:LLM 生成 → 审核循环(revise 时按 revision_plan 定向修复,非盲重生)
    script = None
    draft = None
    for attempt in range(6):
        if draft is None:
            draft = _llm_json(_script_prompt(b["data"], category))
        else:
            draft = _repair("script", draft, last_plan) or draft
        if not draft or "shots" not in draft:
            draft = None
            continue
        for i, s in enumerate(draft.get("shots", [])):
            s.setdefault("shot_id", f"S{i+1:02d}")
        r = _iterate("script", draft, project_id, store, use_llm=True)
        steps.append({"stage": "script", "attempt": attempt + 1,
                      "decision": r["decision"], "criticals": r["stats"]["critical"]})
        last_plan = r["revision_plan"]
        if r["decision"] in ("pass", "pass_with_warnings"):
            script = r["data"]
            break
    if script is None:
        return {"ok": False, "phase": "text", "steps": steps,
                "reason": "剧本 6 次生成/修复未通过审核(LLM+规则),需要人工介入"}

    # 分镜:同样定向修复
    storyboard = None
    draft = None
    last_plan = []
    for attempt in range(6):
        if draft is None:
            draft = _llm_json(STORYBOARD_PROMPT.replace("{script}",
                              json.dumps(script, ensure_ascii=False)), max_tokens=5000)
        else:
            draft = _repair("storyboard", draft, last_plan) or draft
        if not draft or "shots" not in draft:
            draft = None
            continue
        for s in draft.get("shots", []):
            s.setdefault("narration", next((x["narration"] for x in script["shots"]
                                            if x["shot_id"] == s["shot_id"]), ""))
        r = _iterate("storyboard", draft, project_id, store, use_llm=True)
        steps.append({"stage": "storyboard", "attempt": attempt + 1,
                      "decision": r["decision"], "criticals": r["stats"]["critical"]})
        last_plan = r["revision_plan"]
        if r["decision"] in ("pass", "pass_with_warnings"):
            storyboard = r["data"]
            break
    if storyboard is None:
        return {"ok": False, "phase": "text", "steps": steps,
                "reason": "分镜 6 次生成/修复未通过审核,需要人工介入"}

    return {"ok": True, "phase": "text", "steps": steps,
            "script": script, "storyboard": storyboard,
            "confirm_required": ["script", "storyboard"]}


# ── Phase 2: GENERATE ─────────────────────────────────────────────────


def _shot_size(s: dict) -> str:
    v = str(s.get("shot_size") or "ms").strip().lower()
    for k in SIZE_ORDER:
        if k in v:
            return k
    return "ms"


def _scene_token(s: dict) -> str:
    scene = str(s.get("scene") or "")
    for tok in LOCATION_TOKENS:
        if tok in scene:
            return tok
    return scene[:6]


def plan_keyframes(storyboard: dict) -> list[dict]:
    """首尾帧链式策略(coffee-v6 实证校准):

    - 相邻两镜**同一位置**(场景词相同;「同一X」回指继承上一镜位置)→
      链式:last_frame = 下一镜首帧,边界硬切(动作无缝接力)。v6 实证
      景别跨档(cu→ms 等)链式照样成功,位置跳变才是真断点;
    - 换位置 / 落版 → 自末帧(边界 dissolve);末镜不参与出边界。
    返回 [{shot_id, mode: chain|own_end|outro_card, boundary}]。
    """
    shots = storyboard["shots"]
    plan = []
    prev_loc = None
    for i, s in enumerate(shots):
        scene = str(s.get("scene") or "")
        if "同一" in scene and prev_loc:
            loc = prev_loc
        else:
            loc = _scene_token(s)
            prev_loc = loc
        is_outro = bool(re.search(r"落|out|版|束", str(s.get("beat") or ""), re.I)) \
            and i == len(shots) - 1
        if i == len(shots) - 1:
            plan.append({"shot_id": s["shot_id"],
                         "mode": "outro_card" if is_outro else "own_end",
                         "boundary": "dissolve", "loc": loc})
            continue
        nxt = shots[i + 1]
        nxt_scene = str(nxt.get("scene") or "")
        nxt_loc = loc if "同一" in nxt_scene else _scene_token(nxt)
        if nxt_loc == loc:
            plan.append({"shot_id": s["shot_id"], "mode": "chain",
                         "boundary": "cut", "loc": loc})
        else:
            plan.append({"shot_id": s["shot_id"], "mode": "own_end",
                         "boundary": "dissolve", "loc": loc})
    return plan


def run_generate_phase(project_id: str, store, workdir: Optional[str] = None) -> dict:
    """首帧图 → 首尾帧链 → 锚定视频 → QC(重试≤2)→ TTS → 对齐。
    全程确定性;LLM 不参与。返回逐镜报告。"""
    gates = []
    for gate in ("script", "storyboard"):
        try:
            store.assert_confirmed(project_id, gate)
        except Exception as e:
            return {"ok": False, "phase": "generate", "reason": str(e)}
        try:
            store.assert_stage_pass(project_id, gate)
        except Exception as e:
            return {"ok": False, "phase": "generate", "reason": str(e)}
    storyboard = _load(project_id, "storyboard.json")

    # 提示词:从分镜确定性派生(无 LLM,无漂移空间)。放 generate 阶段:
    # BLOCKED 时本阶段会自动重派生并重审(动词表扩容后自愈)。
    style = str((_load(project_id, "brief.json") or {}).get("style_anchor")
                or "cinematic, soft natural light")
    img_prompts, vid_prompts = [], []
    for s in storyboard["shots"]:
        img_prompts.append({"shot_id": s["shot_id"], "prompt_en":
                            f"{s['subject']}. {s['motion']}. Scene: {s['scene']}. "
                            f"{s['spatial']}. {s['camera']}. {style}, no text"})
        vid_prompts.append({"shot_id": s["shot_id"],
                            "prompt_text": f"{s['motion']}. Camera: {s['camera']}. "
                                           f"Single continuous take, no cuts."})
    for stage, data in (("image_prompt", {"style_anchor": style, "shot_prompts": img_prompts}),
                        ("video_prompt", {"shot_prompts": vid_prompts})):
        row = store.get_stage(project_id, stage)
        if row is None or row["status"] != "PASS":
            r = _iterate(stage, data, project_id, store, use_llm=False)
            if r["decision"] not in ("pass", "pass_with_warnings"):
                return {"ok": False, "phase": "generate",
                        "reason": f"{stage} 审核不通过: {r['stats']}"}

    ip = _load(project_id, "image_prompt.json")
    shots = storyboard["shots"]
    img_map = {p["shot_id"]: p["prompt_en"] for p in ip["shot_prompts"]}
    vp = _load(project_id, "video_prompt.json")
    vid_map = {p["shot_id"]: p.get("prompt_text") or p.get("prompt_en", "")
               for p in vp["shot_prompts"]}
    work = _project_dir(project_id)
    manifest = _load(project_id, "manifest.json") or {"shots": {}}
    report = []
    NEG = ("cuts, jump cuts, scene transition, multiple shots, montage, "
           "split screen, zoom change, text overlay")

    # 1) 首帧图(缺则生成;变体继承的 manifest 已引用现成素材则直接复用)
    for s in shots:
        sid = s["shot_id"]
        mrec = manifest["shots"].setdefault(sid, {})
        ref = mrec.get("first_frame")
        if ref and Path(ref).is_file():
            continue  # 素材池命中(base dataRoot 共享),无需重生成
        fp = work / f"{sid}.jpg"
        if not fp.exists():
            r = generate_image_agnes(img_map[sid], 1280, 720, str(fp))
            if not r.get("ok"):
                return {"ok": False, "phase": "generate",
                        "reason": f"{sid} 首帧图生成失败", "report": report}
            record_cost(project_id, "image", model="agnes-image",
                        units=1.0, note=f"{sid} 首帧")
        manifest["shots"][sid]["first_frame"] = str(fp)

    # 2) 首尾帧策略 + 自末帧图
    plan = plan_keyframes(storyboard)
    manifest["keyframe_plan"] = plan
    by_id = {p["shot_id"]: p for p in plan}
    for i, s in enumerate(shots):
        sid = s["shot_id"]
        mode = by_id[sid]["mode"]
        if mode == "chain":
            nxt = shots[i + 1]["shot_id"]
            manifest["shots"][sid]["last_frame"] = str(work / f"{nxt}.jpg")
        elif mode in ("own_end", "outro_card"):
            ref_lp = manifest["shots"][sid].get("last_frame")
            lp = work / f"{sid}_last.jpg"
            if ref_lp and Path(ref_lp).is_file():
                pass  # 素材池命中,沿用基准末帧
            elif not lp.exists() and mode == "own_end":
                base = img_map[sid]
                r = generate_image_agnes(base + " , the action completed, end state of this exact shot, same framing and lighting", 1280, 720, str(lp))
                if not r.get("ok"):
                    return {"ok": False, "phase": "generate",
                            "reason": f"{sid} 末帧图生成失败", "report": report}
                record_cost(project_id, "image", model="agnes-image",
                            units=1.0, note=f"{sid} 自末帧")
            manifest["shots"][sid]["last_frame"] = str(ref_lp) if (ref_lp and Path(ref_lp).is_file()) else str(lp)
        manifest["shots"][sid]["boundary"] = by_id[sid]["boundary"]

    # 3) 视频 + QC + 重试(落版卡留给 assemble 的 kenburns,不跑 agnes)
    for i, s in enumerate(shots):
        sid = s["shot_id"]
        mrec = manifest["shots"][sid]
        if by_id[sid]["mode"] == "outro_card":
            report.append({"shot_id": sid, "note": "kenburns 落版卡,assemble 阶段生成"})
            continue
        if mrec.get("qc") == "ok":
            report.append({"shot_id": sid, "qc": "ok", "cached": True})
            continue
        dur = float(s.get("duration_sec") or 3)
        clip = work / f"{sid}_clip.mp4"
        attempts = []
        for attempt in range(3):
            # 重试阶梯(v6 实证):1) 原提示词 2) 固定机位+原运动
            # 3) 固定机位+主体极简运动。机位运动+主体运动叠加是切镜主诱因。
            if attempt == 0:
                prompt = vid_map.get(sid, s["motion"])
            elif attempt == 1:
                prompt = (f"ONE single uninterrupted take, FIXED camera, no camera "
                          f"movement at all. {s['motion']}")
            else:
                prompt = (f"ONE single uninterrupted take, FIXED camera, no camera "
                          f"movement. The subject moves minimally: {s['motion']}")
            net_retries = 0
            r = None
            while True:
                try:
                    r = generate_video_agnes(
                        prompt=prompt + " Single continuous take, no cuts.",
                        duration=int(max(dur, 2)), resolution="720p",
                        first_frame=mrec["first_frame"], last_frame=mrec["last_frame"],
                        output_path=str(clip),
                        negative_prompt=NEG)
                    break
                except Exception as e:
                    net_retries += 1
                    attempts.append({"attempt": attempt + 1, "net_retry": net_retries,
                                     "error": str(e)[:120]})
                    if net_retries >= 2:
                        break
            if r is None:
                continue
            mrec["master"] = r.get("master_path")
            record_cost(project_id, "video", model="agnes-video",
                        units=round(max(dur, 2), 1), note=sid)
            qc = qc_clip(str(clip), shot_id=sid,
                         expected_duration_sec=dur,
                         reference_image=mrec["first_frame"])
            attempts.append({"attempt": attempt + 1, "qc": qc["verdict"],
                             "cuts": qc["checks"]["internal_cuts"]["value"]})
            if qc["verdict"] == "ok":
                mrec["qc"] = "ok"
                mrec["clip"] = str(clip)
                store.record_clip_qc(project_id, sid, str(clip), "ok", {"attempts": attempts})
                _save(project_id, "manifest.json", manifest)
                break
            mrec["qc"] = "fix"
        store.record_clip_qc(project_id, sid, str(clip),
                             "ok" if mrec.get("qc") == "ok" else "fix", {"attempts": attempts})
        report.append({"shot_id": sid, "qc": mrec.get("qc"), "attempts": attempts})
        if mrec.get("qc") != "ok":
            _save(project_id, "manifest.json", manifest)
            return {"ok": False, "phase": "generate", "reason": f"{sid} 3 次生成未通过 QC",
                    "report": report}

    # 4) TTS(缺则生成;变体沿用的素材引用直接复用)+ 对齐
    from shipin_platform.services.tts_service import create_tts_service
    tts = create_tts_service(work, ROOT / "data" / "voice_cast.db")
    def _tts_of(sid: str) -> Optional[str]:
        local = glob_tts(work, sid)
        if local:
            return local
        ref = (manifest["shots"].get(sid) or {}).get("tts")
        return ref if (ref and Path(ref).is_file()) else None
    need = [s["shot_id"] for s in shots if not _tts_of(s["shot_id"])]
    if need:
        segs = [tts.build_segment(s["shot_id"], s["narration"], role_code="biz_female")
                for s in shots if s["shot_id"] in need]
        tts.synthesize_segments_sync(segs)
        record_cost(project_id, "tts", model="tts-v1",
                    units=float(len(segs)), note="旁白")
    for s in shots:
        p = _tts_of(s["shot_id"])
        if not p:
            return {"ok": False, "phase": "generate", "reason": f"{s['shot_id']} TTS 缺失"}
        manifest["shots"][s["shot_id"]]["tts"] = p
    align = align_narration([{"shot_id": s["shot_id"],
                              "duration_sec": float(s.get("duration_sec") or 3),
                              "narration_path": manifest["shots"][s["shot_id"]]["tts"]}
                             for s in shots])
    manifest["align"] = align
    _save(project_id, "manifest.json", manifest)
    bad = [t["shot_id"] for t in align["timeline"]]
    return {"ok": True, "phase": "generate", "report": report,
            "align": {"verdict": align["verdict"], "total_sec": align["total_sec"],
                      "findings": align["findings"]}}


def glob_tts(work: Path, shot_id: str) -> Optional[str]:
    import glob as _g
    fs = sorted(_g.glob(str(work / f"{shot_id}_*.mp3")), key=_mtime)
    return fs[-1] if fs else None


def _mtime(p: str) -> float:
    from os.path import getmtime
    return getmtime(p)


def _component_defaults(component_id: str) -> dict:
    """取组件配方默认参数;配方异常回退空(调用方用原常量兜底)。"""
    try:
        return get_component_registry().get(component_id).params()
    except Exception:
        return {}


# ── Phase 3: ASSEMBLE ─────────────────────────────────────────────────


def run_assemble_phase(project_id: str, store) -> dict:
    """对齐→落版卡→逐边界转场拼接→调色→字幕→声音设计→mux→归一化→终验→发布。"""
    manifest = _load(project_id, "manifest.json")
    storyboard = _load(project_id, "storyboard.json")
    if not manifest or not manifest.get("align"):
        return {"ok": False, "phase": "assemble", "reason": "先跑 generate 阶段"}
    work = _project_dir(project_id)
    shots = storyboard["shots"]
    tl = manifest["align"]["timeline"]
    windows = [t["window_sec"] for t in tl]
    sids = [t["shot_id"] for t in tl]
    # 组件配方默认值(远期3:散落常量升为可覆盖参数;异常时回退原常量)
    comp_outro = _component_defaults("outro_card")
    comp_trans = _component_defaults("transition")
    comp_sub = _component_defaults("subtitle")
    comp_snd = _component_defaults("sound_design")
    out = {}

    # 1) 落版卡:beat 含 落/out 的末镜 → kenburns(窗口+td,供转场借帧)
    last_sid = sids[-1]
    last_shot = next(s for s in shots if s["shot_id"] == last_sid)
    if re.search(r"落|out|版|束", str(last_shot.get("beat") or ""), re.I):
        w9 = windows[-1]
        # 首帧图:先取素材池引用,缺则回退本地约定命名(变体共享 dataRoot 时
        # 落版卡沿用基准首帧,避免为同一画面重新生成)
        kb_src = (manifest["shots"].get(last_sid) or {}).get("first_frame")
        if not kb_src or not Path(kb_src).is_file():
            kb_src = str(work / f"{last_sid}.jpg")
        kb = kenburns(kb_src, round(w9 + DEFAULT_TD, 2),
                      str(work / f"{last_sid}_clip.mp4"),
                      zoom_to=comp_outro.get("zoom_to", 1.18))
        if not kb.get("ok"):
            return {"ok": False, "phase": "assemble", "reason": f"kenburns: {kb.get('error')}"}
        manifest["shots"][last_sid]["clip"] = kb["output"]
        manifest["shots"][last_sid]["master"] = kb["output"]
        out["kenburns"] = kb

    # 2) 逐边界转场拼接(clip 路径缺失时回退到约定命名)
    clips = [manifest["shots"][s].get("clip") or str(work / f"{s}_clip.mp4")
             for s in sids]
    masters = [manifest["shots"][s].get("master") for s in sids]
    bts = [manifest["shots"][s].get("boundary", "dissolve") for s in sids[1:]]
    st = build_transition_stitch(clips, windows, str(work / "stitched.mp4"),
                                 transition_duration=comp_trans.get(
                                     "transition_duration", DEFAULT_TD),
                                 masters=masters,
                                 boundary_transitions=bts)
    if not st.get("ok"):
        return {"ok": False, "phase": "assemble", "reason": f"stitch: {st.get('error')}"}
    out["stitch"] = {k: st.get(k) for k in ("duration", "expected_sec",
                                            "boundary_preserved", "warnings")}
    _save(project_id, "stitch_result.json", st)

    # 3) 调色
    graded = str(work / "graded.mp4")
    g = color_grade_warm(st["output"], graded)
    if not g.get("ok"):
        return {"ok": False, "phase": "assemble", "reason": f"color-grade: {g}"}
    out["color_grade"] = {"output": graded}

    # 4) 字幕(服务端按 align 时间轴生成)
    srt = _build_srt(storyboard, tl)
    srt_path = work / "subs.srt"
    srt_path.write_text(srt, encoding="utf-8")
    burn = burn_srt(str(Path(graded).resolve()), str(srt_path.resolve()),
                    str((work / "subtitled.mp4").resolve()),
                    font_size=int(comp_sub.get("font_size", 46)),
                    margin_v=int(comp_sub.get("margin_v", 96)))
    if not burn.get("ok"):
        return {"ok": False, "phase": "assemble", "reason": f"burn: {burn.get('error', burn)}"}
    out["burn"] = {"violations": burn.get("violations")}

    # 5) 声音设计
    events = [{"path": manifest["shots"][s]["tts"], "time": t["audio_start_sec"]}
              for s, t in zip(sids, tl)]
    ma = master_audio(None, float(a_total(tl)), str(work / "soundbed.wav"),
                      bgm_path=str(BGM_PATH) if BGM_PATH.exists() else None,
                      bgm_gain_db=float(comp_snd.get("bgm_gain_db", -19.0)),
                      duck=bool(comp_snd.get("duck", True)),
                      narration_events=events,
                      sfx_events=[{"time": t["audio_start_sec"], "kind": "whoosh"}
                                  for t in tl[1:]])
    if not ma.get("ok"):
        return {"ok": False, "phase": "assemble", "reason": f"master: {ma.get('error')}"}
    out["audio"] = {k: ma.get(k) for k in ("bgm_ducked", "sfx_count")}

    # 6) mux + normalize(记录 post_production)
    mm = mux_audio_video(str(work / "subtitled.mp4"), str(work / "soundbed.wav"),
                         str(work / "final.mp4"))
    if not mm.get("ok"):
        return {"ok": False, "phase": "assemble", "reason": f"mux: {mm}"}
    nm = normalize_loudness(str(work / "final.mp4"), str(work / "final_norm.mp4"),
                            target_lufs=-14.0, two_pass=True)
    if not nm.get("ok"):
        return {"ok": False, "phase": "assemble", "reason": f"normalize: {nm}"}
    (work / "final_norm.mp4").replace(work / "final.mp4")
    import hashlib
    h = hashlib.sha256((work / "final.mp4").read_bytes()).hexdigest()
    store.record_artifact(project_id, "post_production", h)
    lm = loudness_measure(str(work / "final.mp4"))
    out["lufs"] = lm.get("input_i")

    # 7) 终验 + 发布
    ctx = {"product_info": str((_load(project_id, "brief.json") or {}).get("product_info", "")),
           "brand_name": "XX咖啡", "slogan": "享受每一刻",
           "duration_sec": round(sum(windows), 2),
           "shots": [{"shot_id": s["shot_id"], "duration_sec": t["window_sec"],
                      "subject": str(s.get("subject"))[:40]}
                     for s, t in zip(shots, tl)]}
    from shipin_platform.review.hard_gates import vlm_review_final
    fv = vlm_review_final(str(work / "final.mp4"), frames_count=16, context=ctx)
    _save(project_id, "final_review.json", fv)
    out["final_review"] = {"verdict": fv.get("verdict"),
                           "deterministic": fv.get("deterministic"),
                           "brand_seen": fv.get("brand_seen"), "breaks": fv.get("breaks")}
    if fv.get("verdict") != "pass":
        return {"ok": True, "phase": "assemble", "released": False,
                "reason": "终验未通过,按 findings 修复后重跑 assemble", **out}
    fin = store.record_artifact(project_id, "post_production", "RELEASED", status="RELEASED")
    out["released"] = True
    out["final_path"] = str(work / "final.mp4")
    return {"ok": True, "phase": "assemble", **out}


def a_total(tl: list[dict]) -> float:
    return round(sum(t["window_sec"] for t in tl), 2)


def _build_srt(storyboard: dict, tl: list[dict]) -> str:
    narr = {s["shot_id"]: s.get("narration", "") for s in storyboard["shots"]}

    def fmt(t):
        h, m = int(t // 3600), int(t % 3600 // 60)
        sec, ms = int(t % 60), int(round((t % 1) * 1000))
        return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"
    lines, t = [], 0.0
    for i, rec in enumerate(tl):
        w = rec["window_sec"]
        lines.append(f"{i+1}\n{fmt(t)} --> {fmt(t + w)}\n{narr.get(rec['shot_id'], '')}\n")
        t += w
    return "\n".join(lines)
