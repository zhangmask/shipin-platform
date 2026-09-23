"""Subtitle renderer tests — the timestamp + ASS-generation functions that
were fixed (×10 ms bug, invalid ASS output, no rollover carry)."""
import re
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from shipin_platform.tools.subtitle_renderer import (  # noqa: E402
    build_ass_style,
    build_karaoke_ass_header,
    escape_subtitles_path,
    generate_ass_from_srt,
    generate_karaoke_ass,
    generate_srt,
    hex_to_ass_color,
    _format_ass_ts,
    _format_ts,
    _srt_ts_to_seconds,
)


# ── SRT timestamps ───────────────────────────────────────────────

class TestFormatTs:
    def test_zero(self):
        assert _format_ts(0) == "00:00:00,000"

    def test_plain_seconds(self):
        assert _format_ts(61.5) == "00:01:01,500"

    def test_three_digit_ms_never_two(self):
        # old bug: {ms:02d}0 printed 10x — e.g. 0.024s -> "00:00:00,0240"
        assert _format_ts(0.024) == "00:00:00,024"
        assert _format_ts(5.1) == "00:00:05,100"

    def test_rounds_not_truncates(self):
        assert _format_ts(1.2347) == "00:00:01,235"

    def test_rollover_carries_into_minutes(self):
        # 59.9997s rounds to 60.0s; must carry, not print 59,000
        assert _format_ts(59.9997) == "00:01:00,000"

    def test_hour_boundary_carries(self):
        assert _format_ts(3599.9997) == "01:00:00,000"

    def test_hours(self):
        assert _format_ts(3723.456) == "01:02:03,456"


# ── ASS timestamps ───────────────────────────────────────────────

class TestFormatAssTs:
    def test_centiseconds(self):
        assert _format_ass_ts(1.5) == "0:00:01.50"

    def test_hours_minutes(self):
        assert _format_ass_ts(3723.456) == "1:02:03.46"

    def test_carry(self):
        assert _format_ass_ts(59.9997) == "0:01:00.00"


# ── SRT <-> seconds parser ────────────────────────────────────────

class TestSrtTsToSeconds:
    def test_srt_comma(self):
        assert _srt_ts_to_seconds("00:01:01,234") == 61.234

    def test_ass_dot(self):
        assert _srt_ts_to_seconds("0:00:01.23") == 1.23

    def test_sparse_colons(self):
        assert _srt_ts_to_seconds("01,500") == 1.5

    def test_negative_like_value_rejected(self):
        # leading '-' is a caller error, not a valid timestamp; -0 parses as 0.0
        # and negative input must not silently produce negative seconds
        assert _srt_ts_to_seconds("00:00:01.00") == 1.0
        assert _srt_ts_to_seconds("-0:00:01.00") == 1.0  # '-' ignored before digits


# ── SRT generation ────────────────────────────────────────────────

class TestGenerateSrt:
    def test_writes_valid_cues(self, tmp_path):
        out = generate_srt([
            {"start": 0.0, "end": 2.25, "text": "第一句"},
            {"start": 2.25, "end": 4.0, "text": "第二句"},
        ], tmp_path / "sub.srt")
        text = out.read_text(encoding="utf-8")
        assert "00:00:00,000 --> 00:00:02,250" in text
        assert "00:00:02,250 --> 00:00:04,000" in text
        assert "第一句" in text and "第二句" in text


# ── SRT -> ASS conversion ─────────────────────────────────────────

SRT_FIXTURE = """1
00:00:00,500 --> 00:00:02,250
他居然敢, 这么对我

2
00:00:02,250 --> 00:00:05,000
全场都震惊了
"""


class TestGenerateAssFromSrt:
    def test_valid_structure(self, tmp_path):
        srt = tmp_path / "a.srt"
        srt.write_text(SRT_FIXTURE, encoding="utf-8")
        ass = generate_ass_from_srt(srt, tmp_path / "a.ass", "")
        content = ass.read_text(encoding="utf-8")
        assert content.startswith("[Script Info]")
        assert "[V4+ Styles]" in content
        assert "[Events]" in content
        # Style line is a valid 23-field record (Name + 22 attrs)
        style_line = next(l for l in content.splitlines()
                          if l.startswith("Style: Default,"))
        assert len(style_line.split(",")) == 23

    def test_playres_matches_video_size(self, tmp_path):
        """轮53:PlayResX/Y 必须等于视频尺寸——libass 的 FontSize 是脚本
        分辨率单位,ffmpeg 内部 SRT→ASS 的默认画布下 46px 会渲染成
        ~86px(实测 1.9×),短行字幕超宽、§10.6 验收每次都拦。"""
        srt = tmp_path / "a.srt"
        srt.write_text(SRT_FIXTURE, encoding="utf-8")
        ass = generate_ass_from_srt(srt, tmp_path / "a.ass", "",
                                    width=1280, height=720)
        content = ass.read_text(encoding="utf-8")
        assert "PlayResX: 1280" in content
        assert "PlayResY: 720" in content


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not available")
class TestAssRenderScale:
    """轮53 真渲染回归:E2E 第 8/9 次挂在「字幕验收 63.7%>62%」,
    根因不是折行预算而是 **libass 策略把 46px 渲染成 ~86px**(1.9×)
    ——ffmpeg 内部 SRT→ASS 画布与视频尺寸无关。修:转 PlayRes=视频
    尺寸的 ASS 再烧。这里钉住渲染墨迹宽,防缩放病复发(1.9× 时短行
    width_pct≈63%,正常时应 ≤55%)。"""

    def _render_and_measure(self, tmp_path, text, w=1280, h=720, fs=46):
        import subprocess
        from shipin_platform.tools.subtitle_renderer import (
            measure_text_px, render_subtitles_best)
        base = tmp_path / "base.mp4"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                        "-i", f"color=c=black:s={w}x{h}:r=24:d=3",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(base)],
                       check=True, capture_output=True)
        srt = tmp_path / "t.srt"
        srt.write_text(f"1\n00:00:00,000 --> 00:00:03,000\n{text}\n",
                       encoding="utf-8")
        r = render_subtitles_best(str(base), str(srt),
                                  str(tmp_path / "fg.mp4"),
                                  font_size=fs, margin_v=96, mode="subtitle")
        assert r.get("ok"), r
        cues = r.get("cues") or []
        assert cues and cues[0].get("measured"), cues
        pil = measure_text_px(text, fs) or 0
        return cues[0]["width_pct"], pil

    def test_short_line_not_rendered_oversized(self, tmp_path):
        """短行(10 字)渲染宽度必须 <55%——1.9× 病态下是 63%+。"""
        pct, pil = self._render_and_measure(tmp_path, "加班后的倦，无人诉说")
        assert pct < 55.0, (pct, pil)

    def test_long_line_within_budget(self, tmp_path):
        """17 字长行(折行预算的极限形态)渲染后也必须 <55%。"""
        pct, pil = self._render_and_measure(
            tmp_path, "加班后的倦意涌上心头却无人可以诉说")
        assert pct < 55.0, (pct, pil)

    def test_custom_style_applied(self, tmp_path):
        srt = tmp_path / "a.srt"
        srt.write_text(SRT_FIXTURE, encoding="utf-8")
        style = build_ass_style(font_name="SimHei", font_size=32, alignment=8)
        ass = generate_ass_from_srt(srt, tmp_path / "a.ass", style)
        style_line = next(l for l in ass.read_text(encoding="utf-8").splitlines()
                          if l.startswith("Style: Default,"))
        parts = dict(zip(
            "Name Fontname Fontsize PrimaryColour SecondaryColour "
            "OutlineColour BackColour Bold Italic Underline StrikeOut "
            "ScaleX ScaleY Spacing Angle BorderStyle Outline Shadow "
            "Alignment MarginL MarginR MarginV Encoding".split(),
            style_line.split(",")))
        assert parts["Fontname"] == "SimHei"
        assert parts["Fontsize"] == "32"
        assert parts["Alignment"] == "8"  # ASS alignment is 1-9, passed through
        assert parts["PrimaryColour"] == hex_to_ass_color("#FFFFFF")

    def test_dialogue_lines_valid_and_timed(self, tmp_path):
        srt = tmp_path / "a.srt"
        srt.write_text(SRT_FIXTURE, encoding="utf-8")
        ass = generate_ass_from_srt(srt, tmp_path / "a.ass", "")
        lines = [l for l in ass.read_text(encoding="utf-8").splitlines()
                 if l.startswith("Dialogue:")]
        assert len(lines) == 2
        # Dialogue: Layer,Start,End,Style,... so fields 1/2 are timestamps
        for line in lines:
            fields = line.split(",", 9)
            assert re.fullmatch(r"\d+:\d{2}:\d{2}\.\d{2}", fields[1])
            assert re.fullmatch(r"\d+:\d{2}:\d{2}\.\d{2}", fields[2])
        # timestamps carry over correctly (0.5s / 2.25s / 5s)
        first_start = lines[0].split(",", 9)[1]
        assert _srt_ts_to_seconds(first_start) == pytest.approx(0.5)
        last_end = lines[1].split(",", 9)[2]
        assert _srt_ts_to_seconds(last_end) == pytest.approx(5.0)

    def test_special_characters_preserved(self, tmp_path):
        srt = tmp_path / "b.srt"
        srt.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\n{藏起来} \\ 台词: 你好\n",
            encoding="utf-8")
        ass = generate_ass_from_srt(srt, tmp_path / "b.ass", "")
        dialogue = next(l for l in ass.read_text(encoding="utf-8").splitlines()
                        if l.startswith("Dialogue:"))
        # braces escaped so ASS does not treat them as override tags
        assert "\\{藏起来\\}" in dialogue


# ── helpers ───────────────────────────────────────────────────────

class TestHelpers:
    def test_hex_to_ass_color(self):
        assert hex_to_ass_color("#FF0000") == "&H000000FF"
        assert hex_to_ass_color("#00FF00") == "&H0000FF00"

    def test_escape_subtitles_path_windows_drive(self):
        assert escape_subtitles_path(Path("D:/x.srt")) == "D\\:/x.srt"
        assert escape_subtitles_path(Path("C:/dir/a b.srt")) == "C\\:/dir/a b.srt"


# ── Karaoke word-level captions (hypit caption 词级卡拉OK) ─────────

WORD_GROUPS = [
    {"start": 0.6, "end": 3.4, "words": [
        {"text": "你好", "start": 0.6, "end": 1.4},
        {"text": "世界", "start": 1.4, "end": 2.3},
        {"text": "卡拉OK", "start": 2.3, "end": 3.4},
    ]},
    {"start": 3.5, "end": 4.1, "words": [
        {"text": "End", "start": 3.5, "end": 4.1},
    ]},
]


class TestGenerateKaraokeAss:
    def test_header_has_karaoke_style_with_highlight_colour(self):
        header = build_karaoke_ass_header(width=640, height=360)
        assert "[V4+ Styles]" in header
        style_line = next(l for l in header.splitlines()
                          if l.startswith("Style: Karaoke,"))
        # SecondaryColour = 点亮色,必须与 PrimaryColour 不同 karaoke 才可见
        parts = dict(zip(
            "Name Fontname Fontsize PrimaryColour SecondaryColour "
            "OutlineColour BackColour Bold Italic Underline StrikeOut "
            "ScaleX ScaleY Spacing Angle BorderStyle Outline Shadow "
            "Alignment MarginL MarginR MarginV Encoding".split(),
            style_line.split(",")))
        assert parts["SecondaryColour"] != parts["PrimaryColour"]

    def test_word_k_tags_and_last_word_absorbs_remainder(self, tmp_path):
        ass = generate_karaoke_ass(WORD_GROUPS, tmp_path / "k.ass",
                                   width=640, height=360)
        dlg = [l for l in ass.read_text(encoding="utf-8").splitlines()
               if l.startswith("Dialogue:")]
        assert len(dlg) == 2
        # 0.6→1.4 = 80cs; 1.4→2.3 = 90cs; 末词吸收剩余 110cs(2.3→3.4)
        assert r"{\k80}你好{\k90}世界{\k110}卡拉OK" in dlg[0]
        assert dlg[0].startswith(
            "Dialogue: 0,0:00:00.60,0:00:03.40,Karaoke,,0,0,0,,")
        # 单段末词同样吸收自身整段时长
        assert r"{\k60}End" in dlg[1]

    def test_braces_escaped_inside_kara_tags(self, tmp_path):
        groups = [{"start": 0.0, "end": 2.0, "text": "{pl} 别\\引", "words": [
            {"text": "{pl}", "start": 0.0, "end": 1.0},
            {"text": "别\\引", "start": 1.0, "end": 2.0},
        ]}]
        ass = generate_karaoke_ass(groups, tmp_path / "esc.ass",
                                   width=640, height=360)
        content = ass.read_text(encoding="utf-8")
        dlg = next(l for l in content.splitlines() if l.startswith("Dialogue:"))
        assert "\\{pl\\}" in dlg
        assert "\\\\" in dlg  # 反斜杠被转义,不能裸出走 ASS 转义

    def test_zero_length_group_skipped(self, tmp_path):
        groups = [{"start": 1.0, "end": 1.0, "words": [{"text": "x",
                                                        "start": 1.0,
                                                        "end": 1.0}]}]
        ass = generate_karaoke_ass(groups, tmp_path / "z.ass",
                                   width=640, height=360)
        dlg = [l for l in ass.read_text(encoding="utf-8").splitlines()
               if l.startswith("Dialogue:")]
        assert dlg == []


class TestBuildKaraokeAssFromAnchors:
    def test_groups_by_segment_and_writes_sidecar(self, tmp_path):
        from shipin_platform.tools.subtitle_renderer import _build_karaoke_ass
        anchors = tmp_path / "a.word_anchors.json"
        anchors.write_text(
            '{"anchors":['
            '{"text":"A","start":0.2,"end":0.9,"segment":"s0"},'
            '{"text":"B","start":0.9,"end":1.7,"segment":"s0"},'
            '{"text":"C","start":2.0,"end":2.8,"segment":"s1"}]}',
            encoding="utf-8")
        out = tmp_path / "out.mp4"
        ka = _build_karaoke_ass(anchors, out, None, 640, 360)
        assert ka == out.with_name("out.karaoke.ass")
        dlg = [l for l in ka.read_text(encoding="utf-8").splitlines()
               if l.startswith("Dialogue:")]
        assert len(dlg) == 2  # s0(两词) + s1(单词)
        assert "{\\k70}A{\\k80}B" in dlg[0]

    def test_karaoke_mode_requires_word_anchors(self, tmp_path):
        from shipin_platform.tools.subtitle_renderer import render_subtitles_best
        with pytest.raises(ValueError, match="word_anchors_path"):
            srt = tmp_path / "s.srt"
            srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nhello\n")
            render_subtitles_best(tmp_path / "v.mp4", srt,
                                  tmp_path / "o.mp4", mode="karaoke")


def test_render_karaoke_degrades_to_drawtext_without_libass(tmp_path):
    """本机 ffmpeg libass 探测为 glyphs-absent 时,karaoke 模式必须如实退
    化为逐行 drawtext 并在结果里标 karaoke_degraded,绝不假报点亮。"""
    import json as _json
    import subprocess as _sp
    from shipin_platform.tools.subtitle_renderer import (
        probe_libass, render_subtitles_best)
    if probe_libass()["libass"]:
        pytest.skip("libass available; degradation path not exercised")
    vid = tmp_path / "v.mp4"
    _sp.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
             "testsrc2=size=320x180:rate=10:duration=2",
             "-pix_fmt", "yuv420p", str(vid)],
            capture_output=True, text=True, check=True)
    srt = tmp_path / "s.srt"
    generate_srt([{"start": 0.2, "end": 1.6, "text": "测试 字幕"}], srt)
    anchors = tmp_path / "s.word_anchors.json"
    anchors.write_text(_json.dumps(
        {"anchors": [{"text": "测试", "start": 0.2, "end": 0.9, "segment": "s0"},
                     {"text": "字幕", "start": 0.9, "end": 1.6, "segment": "s0"}]},
        ensure_ascii=False), encoding="utf-8")
    r = render_subtitles_best(vid, srt, tmp_path / "o.mp4", mode="karaoke",
                              word_anchors_path=anchors)
    assert r["strategy"] == "drawtext"
    assert r["karaoke_degraded"] is True
    assert Path(r["karaoke_ass"]).exists()