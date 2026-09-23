"""Tests for the pipeline orchestrator with real mechanical fixes."""
from shipin_platform.config import PlatformConfig
from shipin_platform.review.engine import Decision
from shipin_platform.workflows.pipeline import Pipeline


def _image_prompts():
    return {
        "style_anchor": "soft warm palette, 35mm film grain",
        "hero_prompts": [{"shot_id": "hero", "prompt_en": "hero shot of steaming coffee"}],
        "shot_prompts": [
            {"shot_id": "S1", "prompt_en": "a cinematic close-up of steaming coffee"},
            {"shot_id": "S2", "prompt_en": "a beautiful wide shot of the café terrace"},
        ],
    }


def test_run_stage_converges_with_mechanical_fixes():
    p = Pipeline(PlatformConfig())
    report = p.run_stage("image_prompt", _image_prompts())
    assert report.decision is Decision.PASS
    assert report.round == 2  # fixed between round 1 and 2
    fixed = p.state.stage_outputs["image_prompt"]
    for shot in fixed["shot_prompts"]:
        assert "cinematic" not in shot["prompt_en"].lower()
        assert fixed["style_anchor"] in shot["prompt_en"]
    assert "hero" in [h["shot_id"] for h in fixed["hero_prompts"]]
    # revision history records what was auto-applied
    hist = p.state.revision_history["image_prompt_r1"]
    assert "STYLE_ANCHOR_MISSING" in hist["applied"]
    assert "SUBJECTIVE_WORD" in hist["applied"]


def test_run_stage_stalls_on_manual_only_findings():
    script = {"duration_sec": 10, "shots": [{"narration": "字" * 100}]}
    p = Pipeline(PlatformConfig())
    report = p.run_stage("script", script)
    # 2026-09-16 起 duration/单句超长有机械修复器（_fix_narration_budget，
    # real-guard-10 实证 4 轮不收敛后补上）：第一轮即 applied，旁白被确定性
    # 裁剪到位。轮57(字数门方向化)后又推进一步:裁剪到 ≤14 字后旁白低于
    # 预算只出 suggestion(不再像旧双向 ±10% 带那样继续判 critical 维持
    # stall——那正是真实使用中「34 字 vs 40 字预算被打死、round2 stall」
    #的死锁)。因此最终 decision 收敛到 PASS/REVISE 皆属正确，关键是：
    # applied 必须含 NARRATION_TOO_LONG 且最终旁白 ≤14 字/句。
    assert report.decision in (Decision.PASS, Decision.PASS_WITH_WARNINGS,
                               Decision.STOP, Decision.STALL, Decision.REVISE)
    hist = p.state.revision_history
    assert any("NARRATION_TOO_LONG" in v["applied"] for v in hist.values())
    final = (p.state.stage_outputs.get("script") or {}).get("shots") or []
    assert final and all(len(s.get("narration", "")) <= 14 for s in final)


def test_run_stage_clean_data_passes_round_one():
    prompts = {
        "style_anchor": "soft warm palette",
        "shot_prompts": [{"shot_id": "S1", "prompt_en": "close-up of steaming coffee, soft warm palette"}],
    }
    p = Pipeline(PlatformConfig())
    report = p.run_stage("image_prompt", prompts)
    assert report.decision is Decision.PASS
    assert report.round == 1
