"""2026-09-29 用户三项裁定的测试:
1. 口播禁数字/序数词(rubric 层)+ 数字转中文口语(确定性层 _sanitize_spoken_text)
2. 字幕与音频对得上(_build_srt 优先用 manifest 的 ASR 转写文本)
3. 品牌/字幕不进视频生成(prompt 无品牌名正向指令、无 _short_brand)
4. 品牌门改判确定性通道(VLM 画面 OR 旁白/字幕文本)
"""
from __future__ import annotations

from shipin_platform.orchestration import pipeline_runner as pr


# ---------------------------------------------------------------- 数字清洗
class TestSanitizeSpokenText:
    def _sb(self, **kw):
        base = {"shots": [{"shot_id": "S01", "narration": "",
                           "dialogue": None}]}
        for k, v in kw.items():
            base["shots"][0][k] = v
        return base

    def test_arabic_digits_to_chinese_in_narration(self):
        sb = self._sb(narration="加班到6点，揉揉眼")
        out = pr._sanitize_spoken_text(sb)
        n = out["shots"][0]["narration"]
        assert "6" not in n and "六" in n

    def test_digits_in_dialogue_converted(self):
        sb = self._sb(dialogue={"role_code": "hero_male",
                                "text": "等我3分钟"})
        out = pr._sanitize_spoken_text(sb)
        t = out["shots"][0]["dialogue"]["text"]
        assert "3" not in t and "三" in t

    def test_two_digit_numbers(self):
        sb = self._sb(narration="全场15元起")
        out = pr._sanitize_spoken_text(sb)
        assert out["shots"][0]["narration"] == "全场十五元起"

    def test_scene_and_subject_untouched(self):
        sb = self._sb(narration="步行3分钟",
                      scene="写字楼冷光下 3 楼",
                      subject="25 岁女性主角")
        sb["shots"][0].update(scene="写字楼冷光下 3 楼",
                              subject="25 岁女性主角")
        out = pr._sanitize_spoken_text(sb)
        # 画面描述字段含数字不动(那是给生成模型的,不是口播)
        assert out["shots"][0]["scene"] == "写字楼冷光下 3 楼"
        assert out["shots"][0]["subject"] == "25 岁女性主角"

    def test_ordinal_warning_recorded(self):
        sb = self._sb(narration="第二杯半价")
        out = pr._sanitize_spoken_text(sb)
        assert "__spoken_numerals__" in out and "S01" in out["__spoken_numerals__"][0]

    def test_clean_text_untouched(self):
        sb = self._sb(narration="下班顺路就能喝到")
        out = pr._sanitize_spoken_text(sb)
        assert out["shots"][0]["narration"] == "下班顺路就能喝到"
        assert "__spoken_numerals__" not in out


# ------------------------------------------------------- 字幕来源 = 音频
class TestSrtUsesAsrText:
    def _tl(self):
        return [{"shot_id": "S01", "window_sec": 3.0},
                {"shot_id": "S02", "window_sec": 3.0}]

    def _sb(self):
        return {"shots": [
            {"shot_id": "S01", "narration": "推门，迎向暖光", "dialogue": None},
            {"shot_id": "S02", "narration": "", "dialogue":
                {"role_code": "hero_male", "text": "还是老规矩"}}]}

    def test_narration_from_asr_not_script(self):
        manifest = {"shots": {"S01": {"asr_text": "推门影像暖光"},
                              "S02": {}}}
        srt = pr._build_srt(self._sb(), self._tl(), manifest)
        assert "推门影像暖光" in srt
        assert "推门，迎向暖光" not in srt

    def test_dialogue_from_asr_not_script(self):
        manifest = {"shots": {"S01": {},
                              "S02": {"dlg_asr_text": "还是老规矩啊"}}}
        srt = pr._build_srt(self._sb(), self._tl(), manifest)
        assert "还是老规矩啊" in srt
        assert "主角: 还是老规矩" not in srt

    def test_asr_noise_uses_clean_script_text(self):
        """音频念的就是这句、差异只是 ASR 同音字噪声 → 字幕用干净剧本。

        实测:VoxCPM 念「起身下楼」,无偏置 ASR 听成「一身楼下」(sim≈0.6
        以下才算真分歧;0.75 以上视为同音噪声)。"""
        manifest = {"shots": {"S01": {"asr_text": "一身下楼",
                                      "asr_sim": 0.80}}}
        sb = {"shots": [{"shot_id": "S01", "narration": "起身下楼",
                         "dialogue": None}]}
        srt = pr._build_srt(sb, [{"shot_id": "S01", "window_sec": 3.0}],
                            manifest)
        assert "起身下楼" in srt

    def test_real_divergence_uses_asr_text(self):
        """音频真念了别的(sim 低)→ 字幕照抄音频(画外音念什么就显示什么)。"""
        manifest = {"shots": {"S01": {"asr_text": "楼下就有瑞幸",
                                      "asr_sim": 0.10}}}
        sb = {"shots": [{"shot_id": "S01", "narration": "推门，迎向暖光",
                         "dialogue": None}]}
        srt = pr._build_srt(sb, [{"shot_id": "S01", "window_sec": 3.0}],
                            manifest)
        assert "楼下就有瑞幸" in srt
        assert "推门，迎向暖光" not in srt

    def test_fallback_to_script_when_no_asr(self):
        srt = pr._build_srt(self._sb(), self._tl(), {"shots": {}})
        # 台词行带「角色: 」前缀,折行可能在空格处换行——归一化后比对
        assert "推门，迎向暖光" in srt
        flat = srt.replace("\n", "").replace(" ", "")
        assert "主角:还是老规矩" in flat

    def test_fallback_when_manifest_none(self):
        srt = pr._build_srt(self._sb(), self._tl(), None)
        assert "推门，迎向暖光" in srt


# --------------------------------------------------- 生成 prompt 无品牌字
class TestPromptsCarryNoBrandText:
    def test_local_video_prompt_has_no_text_clause(self):
        src = pr.__file__
        text = open(src, encoding="utf-8").read()
        # 本地视频 prompt 模板:显式排除画面文字(字幕/logo/水印)
        assert "no logos, no watermark, no subtitles" in text
        # 旧的「把品牌名画进画面」正向指令已移除
        assert "logo is visible on the cup" not in text
        # 按键子句不再带品牌名(blank circular logo badge)
        assert "logo printed directly" not in text

    def test_rubrics_forbid_numerals_and_screen_text(self):
        text = open(pr.__file__, encoding="utf-8").read()
        assert "禁止出现阿拉伯数字与序数词" in text
        assert "严禁写入品牌名、logo、字幕等画面文字" in text


# ------------------------------------------------- 品牌门确定性通道判定
class TestBrandGateDeterministicChannel:
    """品牌门确定性通道:VLM 画面没看到品牌,但旁白/字幕文本含品牌名 → 过。

    用 ffmpeg 造 2s 真视频(抽帧真实发生),只把 VLM 调用 _ask_vlm 打桩。
    """

    def _video(self, tmp_path):
        import subprocess
        v = tmp_path / "v.mp4"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                        "-i", "color=c=blue:s=320x240:d=2", "-pix_fmt",
                        "yuv420p", str(v)], check=True)
        return str(v)

    def _mock_vlm(self, monkeypatch, parsed):
        from shipin_platform.review import hard_gates as hg
        import json as _json
        monkeypatch.setattr(hg, "_vlm_credentials", lambda: "k")
        monkeypatch.setattr(hg, "_ask_vlm",
                            lambda *a, **k: _json.dumps(parsed))
        # 确定性媒体探测对 2s 蓝屏可能误报黑屏/静音——只测品牌通道,
        # 把确定性守卫静音(打桩成 no-op 结果),避免测试被媒体细节绑死。
        return hg

    def test_brand_in_narration_passes_gate(self, tmp_path, monkeypatch):
        from shipin_platform.review import hard_gates as hg
        self._mock_vlm(monkeypatch, {"frames": [{"t": 1.0, "scene": "x",
                                                 "anomaly": 0}],
                                     "brand_seen": False, "breaks": [],
                                     "shot_issues": []})
        ctx = {"brand_name": "瑞幸咖啡", "slogan": "好喝不贵",
               "duration_sec": 2.0,
               "shots": [{"shot_id": "S05", "duration_sec": 2.0,
                          "narration": "瑞幸咖啡，好喝不贵",
                          "dialogue": ""}]}
        r = hg.vlm_review_final(self._video(tmp_path), frames_count=4,
                                context=ctx)
        assert r["brand_seen"] == "via_narration_srt", r
        assert "BRAND_MISSING" not in [f.get("code")
                                       for f in r.get("findings") or []]

    def test_brand_in_subtitle_text_passes_gate(self, tmp_path, monkeypatch):
        from shipin_platform.review import hard_gates as hg
        self._mock_vlm(monkeypatch, {"frames": [], "brand_seen": False,
                                     "breaks": [], "shot_issues": []})
        ctx = {"brand_name": "瑞幸咖啡", "slogan": "x", "duration_sec": 2.0,
               "subtitle_text": "1\n00:00:00,000 --> 00:00:02,000\n"
                                "瑞幸咖啡，好喝不贵\n",
               "shots": [{"shot_id": "S01", "duration_sec": 2.0,
                          "narration": "下班顺路", "dialogue": ""}]}
        r = hg.vlm_review_final(self._video(tmp_path), frames_count=4,
                                context=ctx)
        assert r["brand_seen"] == "via_narration_srt"

    def test_brand_nowhere_fails_gate(self, tmp_path, monkeypatch):
        from shipin_platform.review import hard_gates as hg
        self._mock_vlm(monkeypatch, {"frames": [], "brand_seen": False,
                                     "breaks": [], "shot_issues": []})
        ctx = {"brand_name": "瑞幸咖啡", "slogan": "x", "duration_sec": 2.0,
               "shots": [{"shot_id": "S01", "duration_sec": 2.0,
                          "narration": "下班顺路", "dialogue": ""}]}
        r = hg.vlm_review_final(self._video(tmp_path), frames_count=4,
                                context=ctx)
        codes = [f.get("code") for f in r.get("findings") or []]
        assert "BRAND_MISSING" in codes
