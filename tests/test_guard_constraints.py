"""shipin-platform guard 约束用例（demo 双轨 + 真实装配冒烟）。

双轨说明：
  A. 约束逻辑用例（白名单/顺序/审查门/台账/扩展）——使用 demo 模拟函数 +
     与其配套的 demo 工作流（内联于本文件），不触网、不调真实生成 API；
  B. 真实装配冒烟用例——校验 build_real_registry() 与
     config/guard_workflow_video.json（pipeline.text/generate/assemble 真实链路）
     完全对得上：函数齐、审查齐、Guard 能初始化。

运行：  python tests/test_guard_constraints.py   或   pytest tests/test_guard_constraints.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shipin_platform.guard import (  # noqa: E402
    Guard, Ledger, ManualPassDisabledError, NotAllowedInStepError,
    Registry, ReviewerNotCallableError, StepNotReadyError,
    UnknownFunctionError, WorkflowDefinition, WorkflowValidationError,
)
from shipin_platform.guard.demo_functions import build_registry  # noqa: E402
from shipin_platform.guard.adapters import build_registry as build_real_registry  # noqa: E402

INTENT = {"raw_input": "给 XX 咖啡做一条 30 秒抖音 TVC，温暖治愈，无真人出镜，落版要有 Logo"}

# demo 工作流（与 demo_functions 的函数名一一对应；六步节奏压缩为四步便于测试）
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


def _fresh(tmp: Path, name: str):
    ledger_root = tmp / name
    registry = build_registry()
    wf = WorkflowDefinition.from_dict(json.loads(json.dumps(DEMO_WF)))
    return Guard(wf, registry, Ledger(ledger_root)), ledger_root


def _to_keyframes(guard: Guard) -> None:
    guard.call("clarify_intent", {"intent": INTENT["raw_input"]})
    guard.call("draft_rough_script", {})
    guard.request_review()
    guard.call("refine_script", {})
    guard.request_review()
    guard.call("make_storyboard", {})
    guard.request_review()


def _full_pass(guard: Guard) -> None:
    guard.call("clarify_intent", {"intent": INTENT["raw_input"]})
    guard.call("draft_rough_script", {})
    guard.request_review()
    guard.call("refine_script", {})
    guard.request_review()
    guard.call("make_storyboard", {})
    guard.request_review()
    guard.call("write_frame_prompts", {})
    guard.call("generate_keyframes", {"resolution": "720x1280", "subject_drift": 0.0})
    guard.request_review()
    guard.call("write_video_prompts", {})
    guard.call("generate_video", {"fps": 24, "motion_consistency": 0.9, "internal_cuts": 0})
    guard.request_review()
    for fn in ("edit_timeline", "add_voiceover", "add_music", "add_effects", "render_final"):
        guard.call(fn, {})
    guard.request_review()


# ── 1. 白名单 ────────────────────────────────────────────────

def test_unregistered_function_rejected():
    tmp = Path(tempfile.mkdtemp())
    guard, root = _fresh(tmp, "wl")
    guard.start(intent=INTENT)
    try:
        guard.call("hack_the_planet", {})
        raise AssertionError("未注册函数未被拒绝")
    except UnknownFunctionError as exc:
        assert "未注册" in str(exc)
    rej = [e for e in Ledger(root).get_events(guard.run_id)
           if e["event"] == "rejected"]
    assert rej and rej[-1]["code"] == "UNKNOWN_FUNCTION"
    assert rej[-1]["function"] == "hack_the_planet"
    shutil.rmtree(tmp, ignore_errors=True)


def test_reviewer_not_directly_callable():
    tmp = Path(tempfile.mkdtemp())
    guard, root = _fresh(tmp, "rev")
    guard.start(intent=INTENT)
    try:
        guard.call("review_keyframes", {})
        raise AssertionError("审查函数被 Agent 直接调用却未拒绝")
    except ReviewerNotCallableError:
        pass
    rej = [e for e in Ledger(root).get_events(guard.run_id)
           if e["event"] == "rejected"]
    assert rej[-1]["code"] == "REVIEWER_NOT_CALLABLE"
    shutil.rmtree(tmp, ignore_errors=True)


# ── 2. 顺序约束 ──────────────────────────────────────────────

def test_out_of_order_blocked():
    tmp = Path(tempfile.mkdtemp())
    guard, root = _fresh(tmp, "seq")
    guard.start(intent=INTENT)
    try:
        guard.call("generate_keyframes", {})   # 第 4 步的函数，当前在第 1 步
        raise AssertionError("跳步未被阻断")
    except NotAllowedInStepError as exc:
        assert "第 1/6" in str(exc) and "intake_script" in str(exc)
    rej = [e for e in Ledger(root).get_events(guard.run_id)
           if e["event"] == "rejected"]
    assert rej[-1]["code"] == "NOT_ALLOWED_IN_STEP"
    shutil.rmtree(tmp, ignore_errors=True)


# ── 3. 审查门 ────────────────────────────────────────────────

def test_review_fail_blocks_next_step():
    tmp = Path(tempfile.mkdtemp())
    guard, root = _fresh(tmp, "gate")
    guard.start(intent=INTENT)
    _to_keyframes(guard)
    guard.call("write_frame_prompts", {})
    guard.call("generate_keyframes", {"resolution": "640x360"})  # 不达标
    report = guard.request_review()
    assert report["verdict"] == "fail"
    assert guard.status()["current_step"]["name"] == "keyframes"
    try:
        guard.call("write_video_prompts", {})
        raise AssertionError("审查未通过却调到了下一步函数")
    except NotAllowedInStepError:
        pass
    events = Ledger(root).get_events(guard.run_id)
    assert any(e["event"] == "review_finished" and e["verdict"] == "fail"
               for e in events)
    shutil.rmtree(tmp, ignore_errors=True)


def test_fix_and_pass_unlocks_next_step():
    tmp = Path(tempfile.mkdtemp())
    guard, _ = _fresh(tmp, "fix")
    guard.start(intent=INTENT)
    _to_keyframes(guard)
    guard.call("write_frame_prompts", {})
    guard.call("generate_keyframes", {"resolution": "640x360"})
    assert guard.request_review()["verdict"] == "fail"
    guard.call("generate_keyframes", {"resolution": "720x1280", "subject_drift": 0.0})
    assert guard.request_review()["verdict"] == "pass"
    assert guard.status()["current_step"]["name"] == "video_gen"
    shutil.rmtree(tmp, ignore_errors=True)


def test_manual_pass_disabled():
    tmp = Path(tempfile.mkdtemp())
    wf_dict = json.loads(json.dumps(DEMO_WF))
    step = next(s for s in wf_dict["steps"] if s["name"] == "keyframes")
    step["manual_pass_enabled"] = False
    guard = Guard(WorkflowDefinition.from_dict(wf_dict),
                  build_registry(), Ledger(tmp / "mpd"))
    guard.start(intent=INTENT)
    _to_keyframes(guard)
    guard.call("write_frame_prompts", {})
    guard.call("generate_keyframes", {"resolution": "640x360"})
    guard.request_review()
    try:
        guard.manual_pass(reason="测试", approver="测试员")
        raise AssertionError("未开启人工放行的步骤被放行了")
    except ManualPassDisabledError:
        pass
    shutil.rmtree(tmp, ignore_errors=True)


def test_manual_pass_requires_failed_review():
    tmp = Path(tempfile.mkdtemp())
    guard, _ = _fresh(tmp, "mpr")
    guard.start(intent=INTENT)
    try:
        guard.manual_pass(reason="测试")
        raise AssertionError("未审查就想人工放行")
    except StepNotReadyError:
        pass
    shutil.rmtree(tmp, ignore_errors=True)


def test_blocked_after_max_attempts_then_manual_unlock():
    tmp = Path(tempfile.mkdtemp())
    guard, root = _fresh(tmp, "blk")
    guard.start(intent=INTENT)
    _to_keyframes(guard)
    guard.call("write_frame_prompts", {})
    for i in range(3):  # max_review_attempts = 3
        guard.call("generate_keyframes", {"resolution": "720x1280", "subject_drift": 0.4})
        guard.request_review()
        if i < 2:
            assert guard.status()["status"] == "running"
    st = guard.status()
    assert st["status"] == "blocked", st
    assert "keyframes" in (st["block_reason"] or "")
    try:
        guard.call("generate_keyframes", {"subject_drift": 0.0})
        raise AssertionError("blocked 后调用未被拒")
    except StepNotReadyError:
        pass
    guard.manual_pass(reason="人工确认可接受", approver="产品经理")
    assert guard.status()["status"] == "running"
    assert guard.status()["current_step"]["name"] == "video_gen"
    events = Ledger(root).get_events(guard.run_id)
    assert any(e["event"] == "run_blocked" for e in events)
    assert any(e["event"] == "manual_pass" for e in events)
    shutil.rmtree(tmp, ignore_errors=True)


# ── 4. 全流程 + 台账追溯 ─────────────────────────────────────

def test_happy_path_completed_and_ledger_traceable():
    tmp = Path(tempfile.mkdtemp())
    guard, root = _fresh(tmp, "happy")
    rid = guard.start(intent=INTENT)
    _full_pass(guard)
    st = guard.status()
    assert st["status"] == "completed"
    assert all(s["state"] == "passed" for s in st["steps"])
    led = Ledger(root)
    assert led.get_meta(rid)["status"] == "completed"
    events = led.get_events(rid)
    kinds = {e["event"] for e in events}
    assert {"run_started", "step_entered", "called", "review_requested",
            "review_finished", "step_passed", "advanced",
            "run_finished"} <= kinds
    assert len([e for e in events if e["event"] == "step_passed"]) == 6
    completed = [m["run_id"] for m in led.list_runs(status="completed")]
    running = [m["run_id"] for m in led.list_runs(status="running")]
    assert rid in completed and rid not in running
    shutil.rmtree(tmp, ignore_errors=True)


# ── 5. 配置校验与扩展演练 ────────────────────────────────────

def test_workflow_validation_rejects_unregistered():
    tmp = Path(tempfile.mkdtemp())
    wf_dict = json.loads(json.dumps(DEMO_WF))
    wf_dict["steps"][0]["actions"].append("not_registered_anywhere")
    try:
        Guard(WorkflowDefinition.from_dict(wf_dict), build_registry(),
              Ledger(tmp / "v"))
        raise AssertionError("含未注册函数的配置未被校验拦截")
    except WorkflowValidationError as exc:
        assert "not_registered_anywhere" in str(exc)
    shutil.rmtree(tmp, ignore_errors=True)


def test_extension_without_core_change():
    tmp = Path(tempfile.mkdtemp())
    reg = build_registry()

    def gen_custom_asset(ok: bool = True, **_kw):
        return {"asset": "asset.png", "ok": ok}

    def package_custom(**_kw):
        return {"packed": True, "target": "dist.zip"}

    def review_custom_asset(artifacts, context):
        ok = artifacts.get("gen_custom_asset", {}).get("ok", False)
        return {"verdict": "pass" if ok else "fail",
                "rules": [{"rule": "asset.ok", "dimension": "artifact_quality",
                           "passed": ok, "detail": f"素材产出 ok={ok}"}],
                "summary": f"自定义素材审查 {'通过' if ok else '未通过'}"}

    def review_package(artifacts, context):
        ok = bool(artifacts.get("package_custom", {}).get("packed"))
        return {"verdict": "pass" if ok else "fail",
                "rules": [{"rule": "pack.done", "dimension": "artifact_quality",
                           "passed": ok, "detail": "打包完成" if ok else "未打包"}],
                "summary": "打包审查"}

    reg.register("gen_custom_asset", gen_custom_asset)
    reg.register("package_custom", package_custom)
    reg.register_reviewer("review_custom_asset", review_custom_asset)
    reg.register_reviewer("review_package", review_package)

    custom_wf = {
        "name": "custom_two_step_wf",
        "intent_schema": {"fields": ["raw_input"]},
        "steps": [
            {"name": "make_asset", "actions": ["gen_custom_asset"],
             "reviewer": "review_custom_asset", "max_review_attempts": 2},
            {"name": "package", "actions": ["package_custom"],
             "reviewer": "review_package", "max_review_attempts": 2},
        ],
    }
    guard = Guard(WorkflowDefinition.from_dict(custom_wf), reg, Ledger(tmp / "ext"))
    guard.start(intent={"raw_input": "自定义任务"})
    guard.call("gen_custom_asset", {"ok": True})
    assert guard.request_review()["verdict"] == "pass"
    guard.call("package_custom", {})
    assert guard.request_review()["verdict"] == "pass"
    assert guard.status()["status"] == "completed"
    shutil.rmtree(tmp, ignore_errors=True)


# ── 6. 真实装配冒烟（B 轨）：guard ↔ shipin-platform 真实链路对齐 ──

def test_real_registry_matches_guard_workflow():
    """config/guard_workflow_video.json 引用的函数/审查必须全部已在
    build_real_registry() 注册，且 Guard 能完成初始化校验。"""
    real = build_real_registry()
    wf_path = ROOT / "config" / "guard_workflow_video.json"
    wf = WorkflowDefinition.from_json_file(wf_path)
    # 不触网：只初始化（内部会做 workflow↔registry 一致性校验）
    guard = Guard(wf, real, Ledger(Path(tempfile.mkdtemp()) / "smoke"))
    assert guard.allowed_functions() == ["pipeline.text"]
    actions = {f["name"] for f in real.describe()["actions"]}
    reviewers = {f["name"] for f in real.describe()["reviewers"]}
    assert {"pipeline.text", "pipeline.generate", "pipeline.assemble",
            "gen.image", "gen.video"} <= actions
    assert {"review.text_stage", "review.clip_quality",
            "review.final_film"} <= reviewers


def test_real_adapters_delegate_not_duplicate():
    """确认 adapters 是包装而非重写：动作函数内部必须调用
    pipeline_runner / generate_assets 的既有实现。"""
    import inspect
    from shipin_platform.guard import adapters
    src = inspect.getsource(adapters)
    for real_fn in ("_pr.run_text_phase", "_pr.run_generate_phase",
                    "_pr.run_assemble_phase", "generate_image_agnes",
                    "generate_video_agnes", "qc_clip", "vlm_review_final"):
        assert real_fn in src, f"adapters 未复用真实实现：{real_fn}"


# ── 内置 runner ─────────────────────────────────────────────

def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as exc:  # noqa: BLE001
            failed.append((name, exc))
            print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
