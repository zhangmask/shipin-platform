"""Assembly v2 tests: narration alignment gate, duration-preserving
transitions, master mix (ducked BGM + synthesized SFX), Ken Burns."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shipin_platform.assembly import (  # noqa: E402
    align_narration, build_transition_stitch, master_audio, kenburns,
)

FFMPEG = shutil.which("ffmpeg")
pytestmark = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not available")


def _sine_wav(out: Path, dur: float) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([FFMPEG, "-y", "-f", "lavfi", "-t", str(dur),
                    "-i", "sine=frequency=440", str(out)],
                   capture_output=True, text=True, check=True)
    return out


def _color_clip(out: Path, color: str, dur: float) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([FFMPEG, "-y", "-f", "lavfi", "-t", str(dur),
                    "-i", f"color=c={color}:s=320x240:r=24",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)],
                   capture_output=True, text=True, check=True)
    return out


class TestAlign:
    def test_window_extends_for_long_narration(self, tmp_path):
        nar = _sine_wav(tmp_path / "n.wav", 2.5)
        r = align_narration([{"shot_id": "S1", "duration_sec": 2.0,
                              "narration_path": str(nar)}])
        t = r["timeline"][0]
        assert t["window_sec"] == pytest.approx(2.75, abs=0.1)  # 2.5+0.25
        assert t["extended"] is True
        assert r["verdict"] == "ok"

    def test_spill_beyond_master_is_critical(self, tmp_path):
        nar = _sine_wav(tmp_path / "long.wav", 9.9)
        r = align_narration([{"shot_id": "S1", "duration_sec": 2.0,
                              "narration_path": str(nar)}],
                            master_duration=10.0)
        assert r["verdict"] == "fix"
        assert any(f["code"] == "SPILL" for f in r["findings"])

    def test_corrupt_audio_probe_failure_is_critical(self, tmp_path):
        """轮52(九审 P3-6):文件存在但不可解码(截断/损坏 mp3)——旧代码
        探测返回 0.0 静默,「旁白比窗长」整类漏拦(fail-open)。必须
        VOICE_PROBE_FAILED critical:测不出时长 = 无法保证人声落在窗内。"""
        bad = tmp_path / "broken.mp3"
        bad.write_bytes(b"not an audio file at all")
        r = align_narration([{"shot_id": "S1", "duration_sec": 2.0,
                              "narration_path": str(bad)}])
        assert r["verdict"] == "fix", r["findings"]
        assert any(f["code"] == "VOICE_PROBE_FAILED" for f in r["findings"])

    def test_corrupt_dialogue_probe_failure_is_critical(self, tmp_path):
        """台词轨坏文件同样 fail-closed(纯台词镜的窗口约束同源)。"""
        bad = tmp_path / "broken_dlg.mp3"
        bad.write_bytes(b"\x00\x01garbage")
        r = align_narration([{"shot_id": "S1", "duration_sec": 2.0,
                              "narration_path": None,
                              "dialogue_path": str(bad)}])
        assert r["verdict"] == "fix", r["findings"]
        assert any(f["code"] == "VOICE_PROBE_FAILED" for f in r["findings"])

    def test_dead_air_flagged(self, tmp_path):
        nar = _sine_wav(tmp_path / "n.wav", 1.0)
        r = align_narration([{"shot_id": "S1", "duration_sec": 4.0,
                              "narration_path": str(nar)}], max_gap=0.8)
        assert any(f["code"] == "DEAD_AIR" for f in r["findings"])

    def test_missing_narration_is_critical(self, tmp_path):
        r = align_narration([{"shot_id": "S1", "duration_sec": 3.0,
                              "narration_path": str(tmp_path / "nope.wav")}])
        assert r["verdict"] == "fix"
        assert any(f["code"] == "NARRATION_MISSING" for f in r["findings"])

    def test_timeline_positions_cumulative(self, tmp_path):
        n1 = _sine_wav(tmp_path / "a.wav", 2.0)
        n2 = _sine_wav(tmp_path / "b.wav", 2.0)
        r = align_narration([
            {"shot_id": "S1", "duration_sec": 3.0, "narration_path": str(n1)},
            {"shot_id": "S2", "duration_sec": 3.0, "narration_path": str(n2)}])
        assert r["timeline"][0]["audio_start_sec"] == 0.0
        assert r["timeline"][1]["audio_start_sec"] == 3.0
        assert r["total_sec"] == pytest.approx(6.0, abs=0.1)


class TestTransitionStitch:
    def test_duration_preserved_with_masters(self, tmp_path):
        masters = [str(_color_clip(tmp_path / f"m{i}.mp4", c, 6.0))
                   for i, c in enumerate(("red", "blue", "green"))]
        windows = [2.0, 2.5, 2.0]
        r = build_transition_stitch(masters, windows,
                                    str(tmp_path / "out.mp4"),
                                    transition="dissolve",
                                    transition_duration=0.4, masters=masters)
        assert r["ok"], r.get("error")
        assert r["boundary_preserved"] is True
        assert abs(r["duration"] - sum(windows)) <= 0.2

    def test_freeze_tail_not_head_without_masters(self, tmp_path):
        """轮64:无 master 时不再降级硬切,也不冻结入镜首帧——重叠区由
        出镜侧冻结尾帧供给(上一镜「收势让位」),边界保持叠化意图。"""
        clips = [str(_color_clip(tmp_path / f"c{i}.mp4", c, 2.0))
                 for i, c in enumerate(("red", "blue", "green"))]
        r = build_transition_stitch(clips, [2.0, 2.0, 2.0],
                                    str(tmp_path / "out2.mp4"),
                                    transition="dissolve",
                                    transition_duration=0.4,
                                    masters=[None, None, None])
        assert r["ok"]
        assert r["transitions"] == ["dissolve", "dissolve"]
        assert r["boundary_preserved"] is True
        # 借位全部落在出镜侧:part0(边界0出镜)/part1(边界1出镜)各 +0.4,
        # 末镜 part2 无出镜边界,零借位。
        assert r["parts"][0]["want_sec"] == pytest.approx(2.4, abs=0.01)
        assert r["parts"][1]["want_sec"] == pytest.approx(2.4, abs=0.01)
        assert r["parts"][2]["want_sec"] == pytest.approx(2.0, abs=0.01)
        # 出镜侧不足 → 冻结尾帧兜底(记 freeze,透明可审);末镜无借位,
        # clip 刚好够 → 真实素材。
        assert r["parts"][0]["source"] == "freeze"
        assert r["parts"][1]["source"] == "freeze"
        assert r["parts"][2]["source"] == "clip"
        assert any("冻结尾帧借位" in w for w in r.get("warnings") or [])

    def test_unreadable_clip_fails_fast(self, tmp_path):
        """素材不可读=明确 fail-fast(带文件名),不是降级后 _fit_part
        抛一句无信息的 "pad failed"(2026-09-27 修复的洞)。"""
        good = str(_color_clip(tmp_path / "c0.mp4", "red", 2.0))
        missing = str(tmp_path / "nope.mp4")
        r = build_transition_stitch([good, missing], [2.0, 2.0],
                                    str(tmp_path / "out3.mp4"),
                                    transition="dissolve",
                                    transition_duration=0.4,
                                    masters=[None, None])
        assert r["ok"] is False
        assert "不可读" in r["error"] and "nope.mp4" in r["error"]

    def test_softcut_boundary(self, tmp_path):
        """softcut=3 帧 soft fade:同场景换景别,不构成可感知的过渡表演。"""
        clips = [str(_color_clip(tmp_path / f"s{i}.mp4", c, 5.0))
                 for i, c in enumerate(("red", "blue", "green"))]
        r = build_transition_stitch(clips, [2.0, 2.0, 2.0],
                                    str(tmp_path / "out4.mp4"),
                                    boundary_transitions=["softcut", "dissolve"],
                                    transition_duration=0.4)
        assert r["ok"], r.get("error")
        assert r["transitions"] == ["softcut", "dissolve"]
        # softcut 3 帧≈0.125s@24fps,dissolve 0.4s;clip 5s 有余量,零冻结
        assert r["transitions_dur"][0] <= 0.13
        assert r["transitions_dur"][1] == 0.4
        assert all(p["source"] == "clip" for p in r["parts"]), r["parts"]
        assert r["boundary_preserved"] is True

    def test_cut_exact(self, tmp_path):
        clips = [str(_color_clip(tmp_path / f"c{i}.mp4", c, 2.0))
                 for i, c in enumerate(("red", "blue"))]
        r = build_transition_stitch(clips, [2.0, 2.0], str(tmp_path / "o3.mp4"),
                                    transition="cut")
        assert r["ok"] and r["duration"] == pytest.approx(4.0, abs=0.1)

    # ── 轮13:part 来源透明化(堵"审A拼B") ─────────────────────────
    # align 窗口 > clip 时长时 stitch 从 master 裁料补足——该 part 的
    # 内容从未过审(clip 审的是另一份),必须记 source 并落盘供复审。

    def test_master_sourced_part_recorded(self, tmp_path):
        clips = [str(_color_clip(tmp_path / f"sc{i}.mp4", c, 2.0))
                 for i, c in enumerate(("red", "blue"))]
        masters = [str(_color_clip(tmp_path / f"sm{i}.mp4", c, 6.0))
                   for i, c in enumerate(("red", "blue"))]
        r = build_transition_stitch(clips, [3.25, 2.0],
                                    str(tmp_path / "o4.mp4"),
                                    transition="dissolve",
                                    transition_duration=0.4, masters=masters)
        assert r["ok"], r.get("error")
        parts = r["parts"]
        assert len(parts) == 2
        # 轮64:转场时长由出镜侧吸收——part0 want = 3.25+0.4(出镜),
        # part1 want = 2.0(入镜零借位,clip 刚好够,不再吃 master)。
        assert parts[0]["source"] == "master"
        assert parts[0]["src"] == masters[0]
        assert parts[0]["want_sec"] == pytest.approx(3.65, abs=0.01)
        assert parts[1]["source"] == "clip"
        assert parts[1]["want_sec"] == pytest.approx(2.0, abs=0.01)
        for p in parts:
            assert Path(p["part_path"]).is_file()
            assert Path(p["part_path"]).parent.name == "parts"

    def test_clip_sourced_part_recorded(self, tmp_path):
        clips = [str(_color_clip(tmp_path / f"cc{i}.mp4", c, 4.0))
                 for i, c in enumerate(("red", "blue"))]
        masters = [str(_color_clip(tmp_path / f"cm{i}.mp4", c, 6.0))
                   for i, c in enumerate(("red", "blue"))]
        r = build_transition_stitch(clips, [2.0, 2.0],
                                    str(tmp_path / "o5.mp4"),
                                    transition="dissolve",
                                    transition_duration=0.4, masters=masters)
        assert r["ok"], r.get("error")
        parts = r["parts"]
        assert all(p["source"] == "clip" for p in parts), parts
        assert parts[0]["src"] == clips[0]


class TestMasterAudio:
    def test_narration_events_bgm_sfx(self, tmp_path):
        n1 = _sine_wav(tmp_path / "a.wav", 1.5)
        n2 = _sine_wav(tmp_path / "b.wav", 1.5)
        bgm = _sine_wav(tmp_path / "bgm.wav", 8.0)
        r = master_audio(None, 6.0, str(tmp_path / "master.wav"),
                         bgm_path=str(bgm), bgm_gain_db=-19.0, duck=True,
                         narration_events=[{"path": str(n1), "time": 0.0},
                                           {"path": str(n2), "time": 2.0}],
                         sfx_events=[{"time": 2.0, "kind": "whoosh"},
                                     {"time": 4.0, "kind": "pop"}])
        assert r["ok"], r.get("error")
        assert r["bgm_ducked"] is True
        assert r["sfx_count"] == 2
        assert abs(r["duration"] - 6.0) <= 0.3

    def test_missing_input_rejected(self, tmp_path):
        r = master_audio(None, 5.0, str(tmp_path / "x.wav"),
                         narration_events=[{"path": "nope.wav", "time": 0}])
        assert r["ok"] is False


class TestKeyframePolicy:
    def test_chain_vs_own_end(self):
        from shipin_platform.orchestration.pipeline_runner import plan_keyframes
        sb = {"shots": [
            {"shot_id": "S01A", "shot_size": "ws", "scene": "深夜写字楼玻璃门口", "beat": "hook"},
            {"shot_id": "S01B", "shot_size": "cs", "scene": "同一夜街", "beat": "hook"},
            {"shot_id": "S02", "shot_size": "mcu", "scene": "街道", "beat": "pain"},
            {"shot_id": "S03", "shot_size": "ms", "scene": "咖啡店木门口", "beat": "turn"},
            {"shot_id": "S04", "shot_size": "cu", "scene": "店内吧台", "beat": "value"},
            {"shot_id": "S05", "shot_size": "ms", "scene": "店内吧台前", "beat": "value"},
            {"shot_id": "S06", "shot_size": "cu", "scene": "吧台面部特写", "beat": "value"},
            {"shot_id": "S07", "shot_size": "ws", "scene": "店内走向窗边", "beat": "value"},
            {"shot_id": "S08", "shot_size": "ms", "scene": "窗边座位", "beat": "value"},
            {"shot_id": "S09", "shot_size": "ecu", "scene": "品牌落版", "beat": "outro"},
        ]}
        p = {x["shot_id"]: x for x in plan_keyframes(sb)}
        assert p["S01A"]["boundary"] == "cut"      # 「同一夜街」回指 → 链式
        assert p["S01B"]["boundary"] == "dissolve"  # 街 → 门口 跳变
        assert p["S04"]["boundary"] == "cut"        # 吧台 → 吧台前 链式
        assert p["S06"]["boundary"] == "dissolve"   # 吧台特写 → 走向窗边（v6 问题边界）
        assert p["S07"]["boundary"] == "cut"        # 走向窗边 → 窗边座位
        assert p["S09"]["mode"] == "outro_card"


class TestMixedBoundaryStitch:
    def test_cut_and_dissolve_mixed(self, tmp_path):
        masters = [str(_color_clip(tmp_path / f"m{i}.mp4", c, 6.0))
                   for i, c in enumerate(("red", "blue", "green"))]
        r = build_transition_stitch(masters, [2.0, 2.5, 2.0],
                                    str(tmp_path / "mix.mp4"),
                                    transition_duration=0.4, masters=masters,
                                    boundary_transitions=["cut", "dissolve"])
        assert r["ok"], r.get("error")
        assert r["transitions"] == ["cut", "dissolve"]
        assert r["boundary_preserved"] is True


class TestKenBurns:
    def test_image_to_motion_clip(self, tmp_path):
        img = tmp_path / "card.png"
        subprocess.run([FFMPEG, "-y", "-f", "lavfi", "-t", "0.1",
                        "-i", "color=c=orange:s=1280x704",
                        "-frames:v", "1", str(img)],
                       capture_output=True, text=True, check=True)
        r = kenburns(str(img), 3.0, str(tmp_path / "kb.mp4"))
        assert r["ok"], r.get("error")
        assert abs(r["duration"] - 3.0) <= 0.2
