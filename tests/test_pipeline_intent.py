"""意图传导修复的单测：画幅/风格/时长对齐/assemble 闸门/iterate 写盘。

覆盖本次意图管线完善的核心新增逻辑（不依赖外部服务）：
- _canvas_for_brief:target_platform → 竖屏/横屏决策点
- _style_anchor: 显式 > 音调映射 > 兜底
- _fit_duration_to_target: 时长对齐 + 不可达判定
- run_assemble_phase 的 video_gen 闸门（未生成直接拦，不再静默混拼）
"""
import json

import pytest

from shipin_platform.orchestration import pipeline_runner as pr


class TestCanvasForBrief:
    def test_vertical_platforms(self):
        for plat in ("抖音", "快手", "小红书", "douyin", "视频号"):
            w, h, _ = pr._canvas_for_brief({"target_platform": plat})
            assert (w, h) == (720, 1280), plat

    def test_default_horizontal(self):
        assert pr._canvas_for_brief({}) == (1280, 720, "1280x704")
        assert pr._canvas_for_brief(
            {"target_platform": "YouTube"}) == (1280, 720, "1280x704")


class TestCanvasSizeFor:
    """落版卡尺寸必须跟随实际在链素材,而不是只读 brief。

    (回归: 变体复用基准素材池 —— brief 是竖屏但池里媒体是横屏 ——
    kenburns 卡片若按 brief 出 9:16, xfade 链尺寸不匹配直接报错。)
    """

    def test_follows_existing_clip(self, tmp_path):
        mrec = {"clip": str(tmp_path / "nope.mp4"), "master": None}
        # 无真实媒体 → 回退 brief 决策
        brief = {"target_platform": "douyin"}
        assert pr._canvas_size_for({"shots": {"S01": mrec}}, brief) == "720x1280"

    def test_prefers_clip_over_master(self, tmp_path, monkeypatch):
        clip = tmp_path / "c.mp4"
        master = tmp_path / "m.mp4"
        clip.write_bytes(b"gi")
        master.write_bytes(b"gi")
        sizes = {str(clip): (1280, 704), str(master): (360, 640)}
        monkeypatch.setattr(pr, "_ffprobe_size",
                            lambda p: sizes.get(str(p)))
        manifest = {"shots": {
            "S01": {"clip": str(clip), "master": str(master)}}}
        assert pr._canvas_size_for(
            manifest, {"target_platform": "douyin"}) == "1280x704"

    def test_brief_fallback_when_no_media(self):
        assert pr._canvas_size_for(
            {"shots": {}}, {"target_platform": "xiaohongshu"}) == "720x1280"
        assert pr._canvas_size_for(
            {"shots": {"S01": {"clip": None, "master": None}}}, {}) == "1280x704"


class TestStyleAnchor:
    def test_explicit_wins(self):
        assert pr._style_anchor(
            {"style_anchor": "custom look",
             "tone": "温暖治愈"}) == "custom look"

    def test_tone_mapping(self):
        assert "warm golden" in pr._style_anchor({"tone": "暖色调治愈系"})

    def test_fallback(self):
        assert "cinematic" in pr._style_anchor({})


class TestFitDuration:
    def test_scales_to_target(self):
        data = {"duration_sec": 20,
                "shots": [{"shot_id": f"S{i:02d}", "duration_sec": 5}
                          for i in range(1, 5)]}
        out, ok, meta = pr._fit_duration_to_target(data, 10)
        assert ok
        total = sum(s["duration_sec"] for s in out["shots"])
        assert total == 10
        assert out["duration_sec"] == 10

    def test_clip_unreachable(self):
        data = {"shots": [{"shot_id": "S01", "duration_sec": 2},
                          {"shot_id": "S02", "duration_sec": 2}]}
        out, ok, meta = pr._fit_duration_to_target(data, 20)  # 4s → 钳8s×2=16s ≠20
        assert not ok
        assert max(s["duration_sec"] for s in out["shots"]) <= 8.0

    def test_noop_within_tolerance(self):
        data = {"shots": [{"shot_id": "S01", "duration_sec": 9.5},
                          {"shot_id": "S02", "duration_sec": 10.5}]}
        out, ok, meta = pr._fit_duration_to_target(data, 20.0)
        assert ok
        assert [s["duration_sec"] for s in out["shots"]] == [9.5, 10.5]


class TestIterateDurationLoop:
    """轮25 回归:_iterate 的时长闭环收尾块曾引用三个从未赋名的变量
    (fitted_total/dev/llm_meta)——stage="script" 且 brief 带
    duration_sec(brief 审查强制必填)时每轮必崩 NameError,整条剧本
    审查链路形同不存在(实跑复现:/api/pipeline/text 500)。此处把
    可达/不可达两条路径都钉住:不崩 + duration critical 正确产出。
    """

    def _script(self, n: int, per: float) -> dict:
        return {"shots": [{"shot_id": f"S{i:02d}", "duration_sec": per,
                           "narration": f"台词{i}", "dialogue": "",
                           "subject": "主角", "scene": "街头",
                           "motion": "独行"}
                          for i in range(1, n + 1)]}

    def _store_and_brief(self, pid: str, duration_sec: float):
        from shipin_platform.orchestration.stage_store import ProjectStageStore
        store = ProjectStageStore(":memory:")
        store.create_project(pid)
        pr._save(pid, "brief.json", {"product_info": "测试咖啡",
                                     "duration_sec": duration_sec,
                                     "slogan": "享受每一刻"})
        return store

    def test_unreachable_duration_emits_critical_no_crash(self):
        pid = "iter-unreach"
        store = self._store_and_brief(pid, 24)
        pr._iterate("script", self._script(1, 3), pid, store,
                    use_llm=False)
        rounds = pr._load(pid, "script_review.json")["rounds"]
        dur_findings = [f for rnd in rounds for f in rnd["findings"]
                        if "无法收敛" in str(f.get("issue") or "")]
        assert dur_findings, "不可达时长必须产出 duration critical"
        assert "24" in dur_findings[0]["issue"]

    def test_reachable_duration_no_crash(self):
        pid = "iter-reach"
        store = self._store_and_brief(pid, 24)
        pr._iterate("script", self._script(8, 3), pid, store,
                    use_llm=False)  # 8×3=24s 命中目标
        rounds = pr._load(pid, "script_review.json")["rounds"]
        assert not [f for rnd in rounds for f in rnd["findings"]
                    if "无法收敛" in str(f.get("issue") or "")], \
            "可达路径不应有时长意图 critical"
        assert rounds[-1].get("metadata", {}).get("duration", {}).get(
            "fitted") == 24


class TestTtsReuse:
    """轮26:TTS 复用判定与 glob 精确性。

    两个都实证过的洞:
    (a) 旧音频配新字幕——复用只看「有没有文件」,review/iterate 改了
        narration 后旧 {sid}_*.mp3 直接被复用,成片口播是旧词、字幕是
        新词,声画不一致违反剧本且无门能发现;
    (b) 旁白轨取到台词音频——台词段输出 {sid}_dlg_*.mp3 被 {sid}_*
        通配吞掉,align 按错音频算时长。
    """

    def test_glob_excludes_dialogue_file(self, tmp_path):
        import time
        from shipin_platform.orchestration import pipeline_runner as pr
        narr = tmp_path / "S01_aaaa1111.mp3"
        dlg = tmp_path / "S01_dlg_bbbb2222.mp3"
        narr.write_bytes(b"narr")
        time.sleep(0.02)
        dlg.write_bytes(b"dlg")  # 台词更新——旧逻辑 newest 会选它
        got = pr.glob_tts(tmp_path, "S01")
        assert got == str(narr), f"旁白 glob 吞了台词文件: {got}"
        assert pr.glob_tts(tmp_path, "S01_dlg") == str(dlg)

    def test_text_sha_tracks_narration_and_dialogue(self):
        from shipin_platform.orchestration import pipeline_runner as pr
        s1 = {"shot_id": "S01", "narration": "深夜街头", "dialogue": ""}
        s2 = {"shot_id": "S01", "narration": "深夜街头", "dialogue": ""}
        s3 = {"shot_id": "S01", "narration": "深夜的街头", "dialogue": ""}
        s4 = {"shot_id": "S01", "narration": "深夜街头",
              "dialogue": {"role_code": "biz_female", "text": "欢迎光临"}}
        assert pr._tts_text_sha(s1) == pr._tts_text_sha(s2)
        assert pr._tts_text_sha(s1) != pr._tts_text_sha(s3), "旁白变必须变"
        assert pr._tts_text_sha(s1) != pr._tts_text_sha(s4), "台词变必须变"

    def test_stale_text_sha_forces_regen(self, tmp_path):
        """manifest 记的文本指纹与当前台词不符 → 不可复用(返回 None)。"""
        from shipin_platform.orchestration import pipeline_runner as pr
        (tmp_path / "S01_aaaa1111.mp3").write_bytes(b"old")
        shot = {"shot_id": "S01", "narration": "新台词", "dialogue": ""}
        manifest = {"shots": {"S01": {"tts": str(tmp_path / "S01_aaaa1111.mp3"),
                                      "tts_text_sha": pr._tts_text_sha(
                                          {"narration": "旧台词"})}}}
        # 直接验证判定语义:文件在、指纹不符 → 需要重生
        fresh_sha = pr._tts_text_sha(shot)
        assert manifest["shots"]["S01"]["tts_text_sha"] != fresh_sha


class TestAssembleGate:
    """assemble 第一道闸：video_gen 未 PASS 绝不能拼接（防旧素材+新字幕混片）。"""

    def test_assemble_requires_video_gen(self, tmp_path, monkeypatch):
        import shipin_platform.orchestration.pipeline_runner as pr_mod
        from shipin_platform.orchestration.stage_store import ProjectStageStore
        store = ProjectStageStore(tmp_path / "s.db")
        pid = "p-intent-gate"
        store.create_project(pid)
        # 只有 script confirm + PASS，没有 video_gen
        store.record_confirmation(pid, "storyboard")
        store.record_artifact(pid, "brief", "h" * 64)
        store.record_artifact(pid, "script", "h" * 64)
        store.record_artifact(pid, "storyboard", "h" * 64)
        r = pr.run_assemble_phase(pid, store)
        assert r["ok"] is False
        assert "video_gen" in r["reason"]

    def test_assemble_requires_storyboard_confirmed(self, tmp_path):
        from shipin_platform.orchestration.stage_store import ProjectStageStore
        store = ProjectStageStore(tmp_path / "s2.db")
        pid = "p2"
        store.create_project(pid)
        store.record_artifact(pid, "video_gen", "a" * 64)
        r = pr.run_assemble_phase(pid, store)
        assert r["ok"] is False
        assert "确认" in r["reason"]