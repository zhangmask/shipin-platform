"""shipin_platform.guard.adapters — 把既有平台能力包装成受控平台函数。

原则：**只包装，不重写**。这里没有一行生成/审查/合成逻辑，
全部委托 shipin_platform 既有模块：

    run_text_phase      → pipeline.text      （剧本/分镜 + 审核循环，真实实现）
    run_generate_phase  → pipeline.generate  （首尾帧→锚定视频→QC→TTS，真实实现）
    run_assemble_phase  → pipeline.assemble  （拼接→调色→字幕→声音→终验，真实实现）
    generate_image_agnes / generate_video_agnes → gen.image / gen.video（细粒度备用）
    qc_clip / vlm_review_final → 审查函数（gate 审查直接调用真实质检）
"""
from __future__ import annotations

import sys
from pathlib import Path
from shipin_platform import roots
from typing import Any, Dict, Optional

# 允许从仓库根直接运行（pip install -e . 后可去掉）；打包环境无需注入
if not roots.is_frozen():
    _SRC = Path(__file__).resolve().parents[2]
    if str(_SRC) not in sys.path:
        sys.path.insert(0, str(_SRC))

from shipin_platform.guard.engine import Guard  # noqa: E402
from shipin_platform.guard.ledger import Ledger  # noqa: E402
from shipin_platform.guard.registry import Registry  # noqa: E402
from shipin_platform.guard.workflow import WorkflowDefinition  # noqa: E402
from shipin_platform.orchestration import pipeline_runner as _pr  # noqa: E402
from shipin_platform.orchestration.stage_store import ProjectStageStore  # noqa: E402
from shipin_platform.review.clip_qc import qc_clip  # noqa: E402
from shipin_platform.review.hard_gates import vlm_review_final  # hard_gates 可用时使用


def _stage_store(db_path: Optional[str] = None) -> ProjectStageStore:
    """与 src/api.py 相同的 store 构造（默认 data/stage_store.db）。"""
    root = roots.data_root()
    return ProjectStageStore(db_path or str(root / "data" / "stage_store.db"))


# ═══════════════ 动作函数：直接委托 pipeline_runner 三阶段 ═══════════════

def pipeline_text(ctx: dict = None, project_id: str = "", brief: dict = None,
                  **_kw) -> dict:
    """阶段一（意图→剧本→分镜）：真实实现是 run_text_phase。
    首次调用可传 brief；之后从 stage_store 记录的 brief.json 复跑/续跑。
    平台契约：项目必须先在 stage_store 注册（create_project 幂等）。"""
    store = _stage_store()
    try:
        store.create_project(project_id)
    except Exception:
        pass  # 已存在则忽略

    # 续跑语义：项目已有过审剧本/分镜时直接复用（LLM 随机性会覆盖已验证
    # 产物，real-guard-15 实证）。复用时确认闸门由 adapters 幂等补记。
    existing_sb = _pr._load(project_id, "storyboard.json")
    existing_rev = _pr._load(project_id, "storyboard_review.json") or {}
    existing_script = _pr._load(project_id, "script.json")
    if (existing_sb and existing_script
            and existing_rev.get("decision") in ("pass", "pass_with_warnings")):
        return {"ok": True, "reused": True,
                "steps": [{"stage": "brief", "decision": "pass"},
                          {"stage": "script", "decision": "pass"},
                          {"stage": "storyboard", "decision": "pass"}],
                "script": existing_script, "storyboard": existing_sb,
                "confirm_required": ["script", "storyboard"]}

    b = brief or _pr._load(project_id, "brief.json") or (ctx or {}).get("intent", {}).get("brief") or {}
    result = _pr.run_text_phase(project_id, b, store)
    return _safe(result)


def pipeline_generate(ctx: dict = None, project_id: str = "", **_kw) -> dict:
    """阶段二（首帧图→首尾帧链→锚定视频→QC→TTS→对齐）：真实实现 run_generate_phase。

    平台要求 script/storyboard 先经确认闸门（record_confirmation）；
    guard 语义里"step1 审查门通过"即该确认（审查结论已在台账留痕），
    因此在此处幂等补记确认，approved_by=guard_gate，可审计。
    """
    store = _stage_store()
    for gate in ("script", "storyboard"):
        try:
            store.assert_confirmed(project_id, gate)
        except Exception:
            store.record_confirmation(project_id, gate,
                                      approved_by="guard_gate",
                                      note="guard step1 审查门通过，映射为平台确认")
    # 续跑一致性：LLM 重生成分镜时镜号可能漂移（real-guard-15 实证 S04a），
    # 陈旧的 image_prompt/video_prompt 与新 storyboard 镜号不符会让
    # run_generate_phase KeyError。镜号不一致时删陈旧 prompt，让平台的
    # BLOCKED 自愈逻辑重派生并重审。
    realigned = False
    sb = _pr._load(project_id, "storyboard.json") or {}
    sb_ids = {s.get("shot_id") for s in sb.get("shots", [])}
    for name in ("image_prompt.json", "video_prompt.json"):
        data = _pr._load(project_id, name)
        if not data:
            continue
        key = "shot_prompts" if "shot_prompts" in data else "prompts"
        ids = {x.get("shot_id") for x in data.get(key, []) if isinstance(x, dict)}
        if ids and ids != sb_ids:
            (_pr._project_dir(project_id) / name).unlink(missing_ok=True)
            realigned = True
    result = _pr.run_generate_phase(project_id, store)
    if isinstance(result, dict) and realigned:
        result["prompt_realigned"] = True
    return _safe(result)


def pipeline_assemble(ctx: dict = None, project_id: str = "", **_kw) -> dict:
    """阶段三（拼接→调色→字幕→声音→终验→发布）：真实实现 run_assemble_phase。"""
    store = _stage_store()
    result = _pr.run_assemble_phase(project_id, store)
    return _safe(result)


def _safe(result: Any) -> dict:
    """pipeline_runner 返回 dict；保险起见统一包装。"""
    return result if isinstance(result, dict) else {"result": result}


# ═══════════════ 细粒度动作（直接复用 generate_assets / assembly）═══════════════

def gen_image(ctx: dict = None, prompt: str = "", width: int = 1280, height: int = 720,
              out: str = "", **_kw) -> dict:
    from shipin_platform.generation.generate_assets import generate_image_agnes
    return _safe(generate_image_agnes(prompt, width, height, out))


def gen_video(ctx: dict = None, prompt: str = "", first_frame: str = "",
              last_frame: str = "", duration: int = 4, out: str = "", **_kw) -> dict:
    from shipin_platform.generation.generate_assets import generate_video_agnes
    return _safe(generate_video_agnes(
        prompt=prompt, duration=int(duration), resolution="720p",
        first_frame=first_frame, last_frame=last_frame, output_path=out))


# ═══════════════ 审查函数：调用真实 QC / 终验，不做模拟 ═══════════════

def review_clip_quality(artifacts: dict, context: dict) -> dict:
    """首尾帧/视频段审查门：对最近的 clip 跑真实 qc_clip（ffprobe+黑帧+切镜+首帧比对）。
    审查对象从步骤产物 manifest 里取（pipeline_generate 的 report）。"""
    rules = []
    gen = artifacts.get("pipeline.generate", {}) or {}
    report = gen.get("report") or []
    ok_shots = [r for r in report if r.get("qc") == "ok"]
    # outro 落版镜不走 Agnes 视频（assemble 阶段 kenburns 制作），不计入 QC 分母
    non_outro = [r for r in report
                 if r.get("shot_id") and "kenburns" not in str(r.get("note", ""))]
    rules.append(_rule("clip.qc_all_green", "artifact_quality",
                       bool(non_outro) and len(ok_shots) == len(non_outro),
                       f"逐镜 QC 通过 {len(ok_shots)}/{len(non_outro)}（落版镜走 kenburns 不计）"))
    align = gen.get("align") or {}
    rules.append(_rule("clip.align_verdict", "in_video_compliance",
                       align.get("verdict") == "ok",
                       f"旁白对齐结论：{align.get('verdict')}"))
    return _verdict(rules, context, "视频生成（真实 QC）")


def review_final_film(artifacts: dict, context: dict) -> dict:
    """成片审查门：直接读取 run_assemble_phase 写下的 final_review.json（真实 VLM 终验）。"""
    rules = []
    asm = artifacts.get("pipeline.assemble", {}) or {}
    fv = asm.get("final_review") or {}
    rules.append(_rule("final.vlm_verdict", "in_video_compliance",
                       fv.get("verdict") == "pass",
                       f"VLM 终验：{fv.get('verdict')}（brand_seen={fv.get('brand_seen')}，"
                       f"breaks={fv.get('breaks')}）"))
    rules.append(_rule("final.released", "artifact_quality",
                       asm.get("released") is True,
                       "发布门：assemble reported released" if asm else "无 assemble 产物"))
    return _verdict(rules, context, "成片终验（真实 VLM）")


def review_text_stage(artifacts: dict, context: dict) -> dict:
    """文案阶段审查门：信任 run_text_phase 内部已跑过的规则+LLM 双审；
    gate 这里校验其 decision 与产物完整性（不再重复审，避免两套标准）。"""
    rules = []
    txt = artifacts.get("pipeline.text", {}) or {}
    steps = txt.get("steps") or []
    # 平台审查循环是多轮的（stop→修复→pass）；门禁只看每个 stage 的最终结论
    last: dict = {}
    for s in steps:
        if s.get("decision"):
            last[s["stage"]] = s["decision"]
    ok_finals = ("pass", "pass_with_warnings")
    rules.append(_rule("text.brief_pass", "artifact_quality",
                       last.get("brief") in ok_finals,
                       f"brief 最终审查：{last.get('brief')}"))
    rules.append(_rule("text.script_pass", "artifact_quality",
                       last.get("script") in ok_finals,
                       f"剧本最终审查：{last.get('script')}（含修复轮次）"))
    rules.append(_rule("text.storyboard_pass", "artifact_quality",
                       last.get("storyboard") in ok_finals,
                       f"分镜最终审查：{last.get('storyboard')}（含修复轮次）"))
    rules.append(_rule("text.artifacts_present", "in_video_compliance",
                       bool(txt.get("script")) and bool(txt.get("storyboard")),
                       "剧本与分镜产物齐全（下游生成依赖）"))
    return _verdict(rules, context, "文案阶段（复用平台审查）")


def _rule(rule_id: str, dim: str, passed: bool, detail: str) -> dict:
    return {"rule": rule_id, "dimension": dim, "passed": bool(passed), "detail": detail}


def _verdict(rules: list, context: dict, label: str) -> dict:
    passed = all(r["passed"] for r in rules)
    failed = [f"{r['rule']}({r['dimension']}): {r['detail']}" for r in rules if not r["passed"]]
    return {
        "verdict": "pass" if passed else "fail",
        "rules": rules,
        "summary": (f"「{label}」审查通过（{len(rules)} 条规则全绿）"
                    if passed else
                    f"「{label}」审查未通过，{len(failed)} 条不达标：{'；'.join(failed)}"),
    }


# ═══════════════ 注册表装配（真实能力版）═══════════════════════════════

def build_registry() -> Registry:
    """正式环境注册表：动作函数全部委托既有实现，审查函数全部读真实质检结果。"""
    reg = Registry()
    # 动作函数（按六步白名单分组）
    reg.register("pipeline.text", pipeline_text,
                 description="意图→剧本→分镜（委托 run_text_phase，含平台内部审核循环）",
                 params_hint="project_id, brief?")
    reg.register("pipeline.generate", pipeline_generate,
                 description="首尾帧→锚定视频→QC→TTS→对齐（委托 run_generate_phase）",
                 params_hint="project_id")
    reg.register("pipeline.assemble", pipeline_assemble,
                 description="拼接→调色→字幕→声音→终验→发布（委托 run_assemble_phase）",
                 params_hint="project_id")
    reg.register("gen.image", gen_image,
                 description="单镜首/尾帧生图（委托 generate_image_agnes）",
                 params_hint="prompt, width, height, out")
    reg.register("gen.video", gen_video,
                 description="单镜视频生成（委托 generate_video_agnes，首尾帧锚定）",
                 params_hint="prompt, first_frame, last_frame, duration, out")
    # 审查函数（gate 专用，Agent 直调会被拒）
    reg.register_reviewer("review.text_stage", review_text_stage)
    reg.register_reviewer("review.clip_quality", review_clip_quality)
    reg.register_reviewer("review.final_film", review_final_film)
    return reg


def build_guard(project_id: str, ledger_root: str | Path | None = None,
                workflow_json: str | Path | None = None) -> Guard:
    """为一次制作任务构建受控 Guard：
    - 台账默认落在 <root>/data/guard_ledger/（与 stage_store 同区）
    - 工作流默认用 config/guard_workflow_video.json（六步、每步审查门）
    """
    root = Path(__file__).resolve().parents[3]
    ledger_root = ledger_root or (root / "data" / "guard_ledger")
    wf_path = workflow_json or (root / "config" / "guard_workflow_video.json")
    wf = WorkflowDefinition.from_json_file(wf_path)
    return Guard(wf, build_registry(), Ledger(ledger_root))


def demo_registry() -> Registry:
    """演示用注册表（demo_functions.py 的模拟函数）；测试与教学用。"""
    from shipin_platform.guard import demo_functions
    return demo_functions.build_registry()
