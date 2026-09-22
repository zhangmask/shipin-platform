"""Tests for the multi-round review engine and mechanical auto-fixes."""
import pytest

from shipin_platform.review.engine import (
    Decision,
    FailureClassifier,
    ReviewEngine,
    RevisionEngine,
    Severity,
)


# ── FailureClassifier ────────────────────────────────────────────

class TestFailureClassifier:
    def test_timeline_disorder_typo_fixed(self):
        r = FailureClassifier().classify("video_prompt", "timeline_disorder", "")
        assert r["mode"] == "TIMELINE_DISORDER"
        assert r["strategy"] == "H2"

    def test_missing_prefix_match(self):
        r = FailureClassifier().classify("brief", "missing_content_type", "content_type")
        assert r["mode"] == "MISSING_DIMENSION"

    def test_critical_mode_membership_beats_suffix(self):
        # "STYLE_ANCHOR_MISSING" ends with 'G' — legacy suffix heuristic said
        # SUGGESTION; CRITICAL_MODES membership must win.
        r = FailureClassifier().classify("image_prompt", "style_anchor_missing", "")
        assert r["severity"] == "CRITICAL"

    def test_explicit_severity_wins(self):
        r = FailureClassifier().classify(
            "image_prompt", "subjective_word cinematic", "", severity="SUGGESTION")
        assert r["severity"] == "SUGGESTION"

    def test_chinese_keyword_match(self):
        r = FailureClassifier().classify("script", "时长不匹配", "")
        assert r["mode"] == "DURATION_MISMATCH"

    def test_unknown_falls_back_to_manual(self):
        r = FailureClassifier().classify("script", "something utterly novel", "")
        assert r == {"mode": "UNKNOWN", "strategy": "MANUAL", "severity": "SUGGESTION"}


# ── Stage reviewers ──────────────────────────────────────────────

class TestReviewers:
    def test_brief_missing_dimensions(self):
        report = ReviewEngine().run_review("brief", {"content_type": "ad"})
        assert report.decision is Decision.REVISE
        dims = {f.dimension for f in report.findings}
        assert "product_info" in dims and "duration_sec" in dims

    def test_brief_complete_passes(self):
        brief = {k: "x" for k in [
            "content_type", "product_info", "target_platform", "duration_sec",
            "target_audience", "tone", "creative_direction",
            "reference_materials", "special_requirements"]}
        brief["duration_sec"] = 30
        report = ReviewEngine().run_review("brief", brief)
        assert report.decision is Decision.PASS

    def test_brief_non_numeric_duration_does_not_crash(self):
        brief = {k: "x" for k in [
            "content_type", "product_info", "target_platform", "duration_sec",
            "target_audience", "tone", "creative_direction",
            "reference_materials", "special_requirements"]}
        brief["duration_sec"] = "thirty seconds"
        report = ReviewEngine().run_review("brief", brief)
        assert report.decision in (Decision.PASS, Decision.PASS_WITH_WARNINGS)

    def test_brief_duration_too_short(self):
        brief = {k: "x" for k in [
            "content_type", "product_info", "target_platform", "duration_sec",
            "target_audience", "tone", "creative_direction",
            "reference_materials", "special_requirements"]}
        brief["duration_sec"] = 5
        report = ReviewEngine().run_review("brief", brief)
        assert any(f.dimension == "duration" and f.severity is Severity.CRITICAL
                   for f in report.findings)

    def test_script_duration_mismatch(self):
        script = {"duration_sec": 10, "shots": [{"narration": "字" * 100}]}
        report = ReviewEngine().run_review("script", script)
        assert any(f.dimension == "duration" for f in report.findings)

    def test_script_match_passes(self):
        # 10s * 2.67 ≈ 27 chars budget (±10%) — two 13-char sentences pass
        script = {"duration_sec": 10,
                  "shots": [{"narration": "清晨的咖啡香气飘满整个厨房。轻音乐随阳光缓缓流淌进角落"}]}
        report = ReviewEngine().run_review("script", script)
        assert report.decision in (Decision.PASS, Decision.PASS_WITH_WARNINGS)

    def test_storyboard_accepts_base_fields(self):
        """TS-backend style shots (no `_en` suffix) must not be flagged."""
        sb = {"shots": [{
            "shot_id": "S1", "subject": "a young woman in a red coat",
            "motion": "walking forward", "scene": "rainy neon street",
            "spatial": "near a convenience store", "camera": "dolly in",
            "narration": "雨夜的街角还亮着灯",
        }]}
        report = ReviewEngine().run_review("storyboard", sb)
        assert report.decision is Decision.PASS, report.to_json()

    def test_storyboard_missing_aspect_flagged(self):
        sb = {"shots": [{"shot_id": "S1", "subject": "a woman", "motion": "walking",
                         "scene": "street", "spatial": "", "camera": "dolly in"}]}
        report = ReviewEngine().run_review("storyboard", sb)
        assert any(f.dimension == "spatial" and f.severity is Severity.CRITICAL
                   for f in report.findings)

    def test_i2v_no_false_positive_on_scene_phrases(self):
        prompts = {"shot_prompts": [{
            "shot_id": "S1", "model": "seedance",
            "prompt_text": "In a modern office, the woman slowly turns her head "
                           "toward the window as sunlight moves across the desk.",
        }]}
        report = ReviewEngine().run_review("video_prompt", prompts)
        assert report.decision is Decision.PASS, report.to_json()

    def test_i2v_appearance_repeat_flagged(self):
        prompts = {"shot_prompts": [{
            "shot_id": "S1", "model": "seedance",
            "prompt_text": "A woman wearing a red dress, with long black hair, "
                           "turns slowly toward the window.",
        }]}
        report = ReviewEngine().run_review("video_prompt", prompts)
        modes = [f.failure_mode for f in report.findings]
        assert "I2V_APPEARANCE_REPEAT" in modes

    def test_i2v_motion_missing_flagged(self):
        prompts = {"shot_prompts": [{
            "shot_id": "S1", "model": "seedance",
            "prompt_text": "A static view of a bright room with white walls.",
        }]}
        report = ReviewEngine().run_review("video_prompt", prompts)
        assert any(f.failure_mode == "I2V_MOTION_MISSING" for f in report.findings)

    def test_image_prompt_subjective_and_anchor(self):
        prompts = {
            "style_anchor": "soft warm palette, 35mm film grain",
            "shot_prompts": [{"shot_id": "S1",
                              "prompt_en": "a cinematic close-up of steaming coffee"}],
        }
        report = ReviewEngine().run_review("image_prompt", prompts)
        dims = [(f.failure_mode, f.severity) for f in report.findings]
        assert ("SUBJECTIVE_WORD", Severity.SUGGESTION) in dims
        assert ("STYLE_ANCHOR_MISSING", Severity.CRITICAL) in dims


# ── Decisions & iteration ────────────────────────────────────────

class TestRevisionPlan:
    """revision_plan: the actionable 'how to fix' contract handed back to the LLM."""

    def _script(self, narration="字" * 100, duration=10):
        return {"duration_sec": duration, "shots": [{"shot_id": "S1", "narration": narration}]}

    def test_plan_carries_must_fix_and_lock_line(self):
        report = ReviewEngine().run_review("script", self._script())
        d = report.to_dict()
        assert d["revision_plan"]
        assert any(p.startswith("必改1") for p in d["revision_plan"])
        assert any("锁项" in p for p in d["revision_plan"])
        assert d["revision_plan"][0] == report.revision_plan[0]

    def test_plan_empty_on_pass(self):
        script = {"duration_sec": 10,
                  "shots": [{"narration": "清晨的咖啡香气飘满整个厨房。轻音乐随阳光缓缓流淌进角落"}]}
        report = ReviewEngine().run_review("script", script)
        assert report.revision_plan == []

    def test_plan_how_to_fix_is_an_action(self):
        # every 必改 line must contain an actionable "怎么改" direction
        report = ReviewEngine().run_review("script", self._script(narration="也" * 100))
        for line in report.revision_plan:
            if line.startswith("必改"):
                assert "→ 怎么改：" in line


# ── 深度审查：力度大、第一轮抓全 ─────────────────────────────────

class TestHardReview:
    def test_script_shot_too_short_flagged(self):
        script = {"duration_sec": 10, "shots": [
            {"shot_id": "S1", "narration": "好", "duration_sec": 0.6},
            {"shot_id": "S2", "narration": "好", "duration_sec": 9.0},
        ]}
        report = ReviewEngine().run_review("script", script)
        assert any(f.failure_mode == "SHOT_TOO_SHORT" and f.severity is Severity.CRITICAL
                   for f in report.findings)

    def test_script_then_connection_flagged(self):
        script = {"duration_sec": 10, "shots": [
            {"shot_id": "s1", "narration": "先拿起电脑，然后关上门出发"}]}
        report = ReviewEngine().run_review("script", script)
        assert any(f.failure_mode == "THEN_CONNECTION" for f in report.findings)

    def test_script_infeasible_scene_flagged(self):
        script = {"duration_sec": 10, "shots": [
            {"shot_id": "s1", "narration": "他一抬手，文件瞬移到会议桌", "scene": "会议室"}]}
        report = ReviewEngine().run_review("script", script)
        assert any(f.failure_mode == "INFEASIBLE_SCENE" for f in report.findings)

    def test_storyboard_adjacent_same_size_flagged(self):
        shots = []
        for i in range(4):
            shots.append({
                "shot_id": f"s{i+1}", "shot_size": "CU" if i % 2 == 0 else "CU",
                "subject": "a", "motion": "walk", "scene": "street",
                "spatial": "center", "camera": "dolly",
            })
        report = ReviewEngine().run_review("storyboard", {"shots": shots})
        assert any(f.failure_mode == "REPEATED_SHOT_SIZE" for f in report.findings)

    def test_storyboard_hero_missing_on_long_board(self):
        shots = []
        for i in range(5):
            shots.append({
                "shot_id": f"s{i+1}", "shot_size": "CU" if i % 2 else "WS",
                "subject": "a", "motion": "walk", "scene": "street",
                "spatial": "center", "camera": "dolly",
            })
        report = ReviewEngine().run_review("storyboard", {"shots": shots})
        assert any(f.failure_mode == "MISSING_HERO" for f in report.findings)

    def test_video_camera_term_zoom_flagged(self):
        data = {"shot_prompts": [{
            "shot_id": "s1", "prompt_text": "camera zoom in on the laptop lid"}]}
        report = ReviewEngine().run_review("video_prompt", data)
        modes = [f.failure_mode for f in report.findings]
        assert "CAMERA_TERM_ERROR" in modes

    def test_video_word_count_flagged(self):
        long = "the laptop slowly turns toward the light, the keyboard glows, " * 8
        data = {"shot_prompts": [{"shot_id": "s1", "prompt_text": long}]}
        assert len(long) > 380
        report = ReviewEngine().run_review("video_prompt", data)
        assert any(f.failure_mode == "WORD_COUNT_EXCEEDED" for f in report.findings)

class TestDecisions:
    def test_stall_when_no_fix_and_no_improvement(self):
        engine = ReviewEngine()
        first = engine.run_review("script", {"duration_sec": 10, "shots": [{"narration": "字" * 100}]})
        second = engine.run_review("script", {"duration_sec": 10, "shots": [{"narration": "字" * 100}]},
                                   round_num=2, previous_report=first)
        assert second.decision is Decision.STALL

    def test_revise_when_fix_applied_despite_same_criticals(self):
        engine = ReviewEngine()
        first = engine.run_review("script", {"duration_sec": 10, "shots": [{"narration": "字" * 100}]})
        second = engine.run_review("script", {"duration_sec": 10, "shots": [{"narration": "字" * 100}]},
                                   round_num=2, previous_report=first, fix_applied=True)
        assert second.decision is Decision.REVISE

    def test_stop_respects_configured_max_rounds(self):
        engine = ReviewEngine({"max_rounds": {"script": 2}})
        first = engine.run_review("script", {"duration_sec": 10, "shots": [{"narration": "字" * 100}]})
        second = engine.run_review("script", {"duration_sec": 10, "shots": [{"narration": "字" * 100}]},
                                   round_num=2, previous_report=first, fix_applied=True)
        assert second.decision is Decision.STOP


# ── Mechanical fixes ─────────────────────────────────────────────

@pytest.fixture()
def image_prompts():
    return {
        "style_anchor": "soft warm palette, 35mm film grain",
        "shot_prompts": [
            {"shot_id": "S1", "prompt_en": "a cinematic close-up of steaming coffee"},
            {"shot_id": "S2", "prompt_en": "a beautiful wide shot of the café terrace"},
        ],
    }


class TestRevisionFixes:
    def test_style_anchor_appended(self, image_prompts):
        report = ReviewEngine().run_review("image_prompt", image_prompts)
        result = RevisionEngine().fix("image_prompt", image_prompts, report)
        assert "STYLE_ANCHOR_MISSING" in result["applied"]
        for shot in result["data"]["shot_prompts"]:
            assert image_prompts["style_anchor"] in shot["prompt_en"]
        # re-review passes
        assert ReviewEngine().run_review("image_prompt", result["data"]).decision is Decision.PASS

    def test_fix_does_not_mutate_input(self, image_prompts):
        import copy
        original = copy.deepcopy(image_prompts)
        report = ReviewEngine().run_review("image_prompt", image_prompts)
        RevisionEngine().fix("image_prompt", image_prompts, report)
        assert image_prompts == original

    def test_subjective_word_replaced(self):
        prompts = {"shot_prompts": [
            {"shot_id": "S1", "prompt_en": "a cinematic close-up of steaming coffee"},
        ]}
        report = ReviewEngine().run_review("image_prompt", prompts)
        result = RevisionEngine().fix("image_prompt", prompts, report)
        assert "SUBJECTIVE_WORD" in result["applied"]
        assert "cinematic" not in result["data"]["shot_prompts"][0]["prompt_en"].lower()

    def test_subjective_phrase_maps_to_visual_description(self):
        prompts = {"shot_prompts": [
            {"shot_id": "S1",
             "prompt_en": "portrait of a woman, confident smile, studio background"},
        ]}
        report = ReviewEngine().run_review("image_prompt", prompts)
        result = RevisionEngine().fix("image_prompt", prompts, report)
        assert "lips curved upward" in result["data"]["shot_prompts"][0]["prompt_en"]

    def test_camera_term_corrected(self):
        prompts = {"shot_prompts": [
            {"shot_id": "S1", "model": "seedance",
             "prompt_text": "Camera zooms in... wait — 'zoom in' is wrong: zoom in toward the subject"},
        ]}
        result = RevisionEngine().fix(
            "video_prompt", prompts, _fake_report("CAMERA_TERM_ERROR"))
        assert "CAMERA_TERM_ERROR" in result["applied"]
        assert "zoom in" not in result["data"]["shot_prompts"][0]["prompt_text"]
        assert "dolly in" in result["data"]["shot_prompts"][0]["prompt_text"]

    def test_i2v_appearance_stripped(self):
        prompts = {"shot_prompts": [
            {"shot_id": "S1", "model": "seedance",
             "prompt_text": "A woman wearing a red dress turns slowly toward the window."},
        ]}
        result = RevisionEngine().fix("video_prompt", prompts, _fake_report("I2V_APPEARANCE_REPEAT"))
        assert "I2V_APPEARANCE_REPEAT" in result["applied"]
        text = result["data"]["shot_prompts"][0]["prompt_text"]
        assert "wearing" not in text.lower()
        assert "turns slowly" in text

    def test_unfixable_mode_reported_manual(self):
        report = _fake_report("DURATION_MISMATCH")
        result = RevisionEngine().fix("script", {"duration_sec": 10, "shots": []}, report)
        assert result["applied"] == []
        assert "DURATION_MISMATCH" in result["manual"]


def _fake_report(mode: str):
    """Minimal report carrying a single finding of the given failure mode."""
    from shipin_platform.review.engine import Finding, ReviewReport
    r = ReviewReport(stage="test", round=1)
    cls = FailureClassifier().classify(
        "video_prompt" if mode.startswith(("I2V", "CAMERA")) else "script", mode, "")
    r.findings.append(Finding(
        dimension="x", severity=Severity.CRITICAL, issue=mode, evidence="",
        failure_mode=mode, revision_strategy=cls["strategy"]))
    return r


# ── 抽象审核维度:旁白重复 / 相邻机位雷同 / 占位符泄漏 ──────────
# 实证来源 e2e-f3e40f87 成片(曾连过全部审核):同一句旁白两镜复用、
# 相邻镜头 camera 逐字相同、品牌占位符"XX"漏进旁白——结构审核查
# 不出,语义 LLM 审查也会放行。这三条确定性规则是『看起来正常』
# 的最低门槛,必须从引擎层拦下,不许再漏进成片。

NARR1 = "晨光落在一杯手冲咖啡上"
NARR2 = "热气从杯口缓缓升起"


def _mk_storyboard(cameras, narrations, **overrides):
    """构造分镜:camera/narration 逐镜给定,其余字段齐全避免无关告警。"""
    sizes = ["ws", "cu", "ms", "ecu"]
    shots = []
    for i, cam in enumerate(cameras):
        shots.append({
            "shot_id": f"S{i+1:02d}", "shot_size": sizes[i % len(sizes)],
            "beat": "hook", "rhythm": "medium", "sfx": f"sfx_{i}",
            "subject": "一只冒着热气的白瓷咖啡杯",
            "subject_en": "a porcelain coffee cup with steam",
            "motion": "缓缓推向镜头中央",
            "motion_en": "slowly drifts toward the lens",
            "scene": "清晨的木质早餐桌",
            "scene_en": "wooden breakfast table at dawn",
            "spatial": "中景微侧,主体占左三分之一",
            "spatial_en": "medium wide, subject on left third",
            "camera": cam,
            "narration": narrations[i],
        })
    board = {"hero_shot": "S04", "shots": shots}
    for k, v in overrides.items():
        board[k] = v
    return board


def _mk_script(narrations, duration_per_shot=4):
    shots = [
        {"shot_id": f"S{i+1:02d}", "duration_sec": duration_per_shot,
         "narration": n, "scene": "晨间厨房"} for i, n in enumerate(narrations)]
    return {"duration_sec": len(shots) * duration_per_shot, "shots": shots}


class TestAbstractQualityGates:
    """『看起来正常』类毛病必须被确定性规则拦下,不许放行进成片。"""

    def test_script_narration_duplicated_flagged(self):
        report = ReviewEngine().run_review("script", _mk_script([NARR1, NARR1, NARR2]))
        assert any(f.failure_mode == "NARRATION_DUPLICATED" for f in report.findings)

    def test_script_clean_no_narration_duplicate(self):
        report = ReviewEngine().run_review("script", _mk_script([NARR1, NARR2]))
        assert not any(f.failure_mode == "NARRATION_DUPLICATED" for f in report.findings)

    def test_script_placeholder_leak_flagged(self):
        report = ReviewEngine().run_review("script", _mk_script([NARR1, "XX咖啡的夜香飘进街角"]))
        assert any(f.failure_mode == "PLACEHOLDER_LEAK" for f in report.findings)

    def test_script_clean_no_placeholder(self):
        report = ReviewEngine().run_review("script", _mk_script([NARR1, "杯壁上的水珠细密如雾气"]))
        assert not any(f.failure_mode == "PLACEHOLDER_LEAK" for f in report.findings)

    def test_storyboard_adjacent_camera_same_flagged(self):
        sb = _mk_storyboard(["dolly in", "dolly in", "truck right", "static"],
                            [NARR1, NARR2, "蒸汽在指缝间缠绕", "双手捧住杯底"])
        report = ReviewEngine().run_review("storyboard", sb)
        cams = [f for f in report.findings if f.failure_mode == "CAMERA_SAME_ADJACENT"]
        assert cams, [f.failure_mode for f in report.findings]
        assert "S01" in cams[0].evidence  # 指认具体相邻镜对

    def test_storyboard_brand_outro_static_exempted(self):
        # 落版镜(scene 含 logo/背景)允许与上一镜同机位收尾,不算雷同
        sb = _mk_storyboard(["dolly in", "static"], [NARR1, NARR2])
        sb["shots"][1]["scene"] = "纯黑背景,居中一枚品牌 logo"
        report = ReviewEngine().run_review("storyboard", sb)
        assert not any(f.failure_mode == "CAMERA_SAME_ADJACENT" for f in report.findings)

    def test_storyboard_cameras_varied_pass(self):
        sb = _mk_storyboard(["dolly in", "truck right", "pedestal up", "crane down"],
                            [NARR1, NARR2, "热气在玻璃上凝结", "原木台面映着晨光"])
        report = ReviewEngine().run_review("storyboard", sb)
        assert not any(f.failure_mode == "CAMERA_SAME_ADJACENT" for f in report.findings)

    def test_storyboard_narration_duplicated_flagged(self):
        sb = _mk_storyboard(["dolly in", "truck right", "static", "crane down"],
                            [NARR1, NARR2, NARR1, "指尖搭上杯沿"])
        report = ReviewEngine().run_review("storyboard", sb)
        assert any(f.failure_mode == "NARRATION_DUPLICATED" for f in report.findings)

    # ── 轮36:机位比对索引错位 + 标点逃逸的重复旁白(五审 #6c/#6d) ──

    def test_storyboard_camera_finding_attributes_real_shot(self):
        """轮36a:中间镜无 camera 字段时,雷同 finding 必须挂在真正持有
        该机位的镜上——旧代码用压缩缓存下标索引原 shots,camera=
        [static,'',static] 会把 finding 挂到没有 camera 字段的 S02,
        修订计划让 LLM 改不存在的字段(诱发 STALL)。"""
        sb = _mk_storyboard(["static", "", "static"],
                            [NARR1, NARR2, "蒸汽在指缝间缠绕"])
        report = ReviewEngine().run_review("storyboard", sb)
        cams = [f for f in report.findings
                if f.failure_mode == "CAMERA_SAME_ADJACENT"]
        assert cams, [f.failure_mode for f in report.findings]
        assert "S01" in cams[0].evidence and "S03" in cams[0].evidence, \
            cams[0].evidence

    def test_storyboard_camera_gap_does_not_false_pair(self):
        """轮36a:空 camera 的镜不得参与相邻比对——[dolly in,'',static]
        本无机位雷同,旧代码会拿缓存下标错位比较(可能误报)。"""
        sb = _mk_storyboard(["dolly in", "", "static"],
                            [NARR1, NARR2, "蒸汽在指缝间缠绕"])
        report = ReviewEngine().run_review("storyboard", sb)
        assert not any(f.failure_mode == "CAMERA_SAME_ADJACENT"
                       for f in report.findings)

    def test_storyboard_narration_dup_survives_punctuation(self):
        """轮36b:标点差异不构成逃逸——「每一杯都是匠心」vs「每一杯，
        都是匠心」归一化(空白+标点剥离)后同键,必须报重复。"""
        sb = _mk_storyboard(["dolly in", "truck right"],
                            ["每一杯都是匠心", "每一杯，都是匠心"])
        report = ReviewEngine().run_review("storyboard", sb)
        assert any(f.failure_mode == "NARRATION_DUPLICATED"
                   for f in report.findings), \
            [f.failure_mode for f in report.findings]

    def test_storyboard_placeholder_leak_flagged(self):
        sb = _mk_storyboard(["dolly in", "static"], [NARR1, NARR2])
        sb["shots"][0]["subject"] = "XX 牌咖啡豆倾泻而下"
        report = ReviewEngine().run_review("storyboard", sb)
        assert any(f.failure_mode == "PLACEHOLDER_LEAK" for f in report.findings)
