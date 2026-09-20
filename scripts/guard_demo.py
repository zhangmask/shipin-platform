"""scripts/guard_demo.py — guard 约束机制四场景演示（模拟函数，不触网）。

场景 A（run_pass）      : 全流程一次通过 → completed
场景 B（run_rework）    : 首尾帧生成分辨率不达标 → 审查拦下 → 试图跳步被拒 → 修复重审 → completed
场景 C（run_blocked）   : 主体漂移连续 3 次审查失败 → 运行 blocked → 人工放行解锁 → completed
场景 D（run_overreach） : 越权演练（调未注册函数 / 跳步 / 直调审查函数）→ 全部被拒 + 台账留痕

运行：  cd D:\\aishipin\\shipin-platform && python scripts\\guard_demo.py
台账：  python src\\shipin_platform\\guard\\ledger.py data\\guard_ledger_demo --list
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shipin_platform.guard import (  # noqa: E402
    Guard, Ledger, NotAllowedInStepError, StepNotReadyError,
    UnknownFunctionError, WorkflowDefinition,
)
from shipin_platform.guard.demo_functions import build_registry  # noqa: E402

LEDGER_ROOT = ROOT / "data" / "guard_ledger_demo"
INTENT = {"raw_input": "给 XX 咖啡做一条 30 秒抖音 TVC，温暖治愈，无真人出镜，落版要有 Logo"}

# 与 demo_functions 函数名配套的演示工作流
DEMO_WF = {
    "name": "demo_video_wf",
    "intent_schema": {"fields": ["raw_input"]},
    "steps": [
        {"name": "intake_script", "title": "需求澄清与粗剧本",
         "actions": ["clarify_intent", "draft_rough_script"],
         "reviewer": "review_rough_script",
         "manual_pass_enabled": True, "max_review_attempts": 3},
        {"name": "script_refine", "title": "剧本细化",
         "actions": ["refine_script"], "reviewer": "review_script",
         "manual_pass_enabled": True, "max_review_attempts": 3},
        {"name": "storyboard", "title": "分镜制作",
         "actions": ["make_storyboard"], "reviewer": "review_storyboard",
         "manual_pass_enabled": True, "max_review_attempts": 3},
        {"name": "keyframes", "title": "首尾帧生成",
         "actions": ["write_frame_prompts", "generate_keyframes"],
         "reviewer": "review_keyframes",
         "manual_pass_enabled": True, "max_review_attempts": 3},
        {"name": "video_gen", "title": "视频生成",
         "actions": ["write_video_prompts", "generate_video"],
         "reviewer": "review_video_clips",
         "manual_pass_enabled": True, "max_review_attempts": 3},
        {"name": "post_production", "title": "后期制作",
         "actions": ["edit_timeline", "add_voiceover", "add_music",
                     "add_effects", "render_final"],
         "reviewer": "review_final_film",
         "manual_pass_enabled": True, "max_review_attempts": 3},
    ],
}


def banner(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def run_pass(registry) -> str:
    banner("场景 A｜全流程一次通过（六步，每步审查门自动触发）")
    guard = Guard(WorkflowDefinition.from_dict(json.loads(json.dumps(DEMO_WF))),
                  registry, Ledger(LEDGER_ROOT))
    rid = guard.start(intent=INTENT)
    print(f"运行 {rid} 启动 → 当前步骤：{guard.status()['current_step']['name']}")

    guard.call("clarify_intent", {"intent": INTENT["raw_input"]})
    guard.call("draft_rough_script", {})
    print("审查(粗剧本)：", guard.request_review()["summary"])
    guard.call("refine_script", {})
    print("审查(剧本)：", guard.request_review()["summary"])
    guard.call("make_storyboard", {})
    print("审查(分镜)：", guard.request_review()["summary"])
    guard.call("write_frame_prompts", {})
    guard.call("generate_keyframes", {"resolution": "720x1280", "subject_drift": 0.0})
    print("审查(首尾帧)：", guard.request_review()["summary"])
    guard.call("write_video_prompts", {})
    guard.call("generate_video", {"fps": 24, "motion_consistency": 0.9, "internal_cuts": 0})
    print("审查(视频段)：", guard.request_review()["summary"])
    for fn in ("edit_timeline", "add_voiceover", "add_music", "add_effects", "render_final"):
        guard.call(fn, {})
    print("审查(成片)：", guard.request_review()["summary"])

    st = guard.status()
    print(f"最终状态：{st['status']}；步骤推进：{[(x['name'], x['state']) for x in st['steps']]}")
    return rid


def run_rework(registry) -> str:
    banner("场景 B｜生图不达标被审查拦下 → 跳步被拒 → 修复重审 → 通过")
    guard = Guard(WorkflowDefinition.from_dict(json.loads(json.dumps(DEMO_WF))),
                  registry, Ledger(LEDGER_ROOT))
    rid = guard.start(intent=INTENT)
    guard.call("clarify_intent", {"intent": INTENT["raw_input"]})
    guard.call("draft_rough_script", {})
    guard.request_review()
    guard.call("refine_script", {})
    guard.request_review()
    guard.call("make_storyboard", {})
    guard.request_review()
    print(f"进入第 4 步「{guard.wf.step(3)['title']}」，允许函数：{guard.allowed_functions()}")

    guard.call("write_frame_prompts", {})
    guard.call("generate_keyframes", {"resolution": "640x360", "subject_drift": 0.0})
    report = guard.request_review()
    print("审查(首尾帧)→", report["verdict"], "：", report["summary"])

    try:
        guard.call("write_video_prompts", {})
        print("!! 不应到达这里：跳步未被拦截")
    except NotAllowedInStepError as exc:
        print(f"跳步被拒（{exc.code}）：已留痕台账 ✓")

    guard.call("generate_keyframes", {"resolution": "720x1280", "subject_drift": 0.0})
    report = guard.request_review()
    print("修复后重审 →", report["verdict"], "：", report["summary"])

    guard.call("write_video_prompts", {})
    guard.call("generate_video", {"fps": 24, "motion_consistency": 0.9, "internal_cuts": 0})
    guard.request_review()
    for fn in ("edit_timeline", "add_voiceover", "add_music", "add_effects", "render_final"):
        guard.call(fn, {})
    guard.request_review()
    print(f"最终状态：{guard.status()['status']}")
    return rid


def run_blocked(registry) -> str:
    banner("场景 C｜连续 3 次审查失败 → 运行 blocked → 人工放行解锁 → 走完")
    guard = Guard(WorkflowDefinition.from_dict(json.loads(json.dumps(DEMO_WF))),
                  registry, Ledger(LEDGER_ROOT))
    rid = guard.start(intent=INTENT)
    guard.call("clarify_intent", {"intent": INTENT["raw_input"]})
    guard.call("draft_rough_script", {})
    guard.request_review()
    guard.call("refine_script", {}); guard.request_review()
    guard.call("make_storyboard", {}); guard.request_review()
    guard.call("write_frame_prompts", {})

    for i in range(3):
        guard.call("generate_keyframes", {"resolution": "720x1280", "subject_drift": 0.4})
        report = guard.request_review()
        print(f"第 {i+1} 次审查 →", report["verdict"], "：",
              report["summary"][:80] + ("…" if len(report["summary"]) > 80 else ""))
        if guard.status()["status"] == "blocked":
            print(f"运行已被阻断（第 {i+1} 次后）✓")

    st = guard.status()
    print(f"阻断状态：{st['status']}，原因：{(st['block_reason'] or '')[:60]}…")
    try:
        guard.call("generate_keyframes", {"subject_drift": 0.0})
    except StepNotReadyError as exc:
        print(f"阻断后调用被拒（{exc.code}）✓")

    guard.manual_pass(reason="人工确认主体为同一演员，属于可接受的表达差异，放行",
                      approver="产品经理")
    print("人工放行 → 解锁，自动进入下一步：", guard.status()["current_step"]["name"])

    guard.call("write_video_prompts", {})
    guard.call("generate_video", {"fps": 24, "motion_consistency": 0.9, "internal_cuts": 0})
    guard.request_review()
    for fn in ("edit_timeline", "add_voiceover", "add_music", "add_effects", "render_final"):
        guard.call(fn, {})
    guard.request_review()
    print(f"最终状态：{guard.status()['status']}（含一次人工放行记录）")
    return rid


def run_overreach(registry) -> str:
    banner("场景 D｜越权演练：未注册函数 / 跳步 / 直调审查函数 → 全部拒绝+留痕")
    guard = Guard(WorkflowDefinition.from_dict(json.loads(json.dumps(DEMO_WF))),
                  registry, Ledger(LEDGER_ROOT))
    rid = guard.start(intent=INTENT)

    def expect_reject(label, fn):
        try:
            fn()
            print(f"!! {label}：未被拦截（不应发生）")
        except Exception as exc:
            print(f"{label} → 被拒（{type(exc).__name__}）✓")

    expect_reject("调用未注册函数 hack_the_planet",
                  lambda: guard.call("hack_the_planet", {}))
    expect_reject("第 1 步直接调用第 5 步函数 generate_video",
                  lambda: guard.call("generate_video", {}))
    expect_reject("Agent 直接调用审查函数 review_keyframes",
                  lambda: guard.call("review_keyframes", {}))

    st = guard.status()
    print(f"运行状态保持 {st['status']}，当前步骤不变：{st['current_step']['name']}")
    return rid


def main() -> None:
    registry = build_registry()
    rid_a = run_pass(registry)
    rid_b = run_rework(registry)
    rid_c = run_blocked(registry)
    rid_d = run_overreach(registry)

    banner("台账总览（成功与失败运行可区分）")
    led = Ledger(LEDGER_ROOT)
    for meta in led.list_runs()[:6]:
        steps = " ".join(f"{s['state'][:3]}" for s in meta.get("steps", []))
        print(f"{meta['run_id']}  {meta['status']:<10} 步骤状态[{steps}] "
              f"当前={ (meta.get('current_step') or {}).get('name', '-') }")

    print("\n—— 台账事件抽样（场景 B 的拒绝与放行记录）——")
    for ev in led.get_events(rid_b):
        if ev["event"] in ("rejected", "review_finished", "step_passed", "advanced"):
            body = {k: v for k, v in ev.items()
                    if k in ("event", "step", "function", "verdict", "code", "via")}
            print(f"  #{ev['seq']:>3} {ev['event']:<16} {body}")

    print("\n演示完成：A={} B={} C={} D={}".format(rid_a, rid_b, rid_c, rid_d))


if __name__ == "__main__":
    main()
