"""P1-② 参考视频纵深剖析测试(方案「P1-②」落地)。

断言:blackdetect 参数名跨版本探测、黑段检出与统计、旁白密度
(silencedetect → speech_ratio)、9 维 brief 预填三态、profile_reference
落盘 JSON、输入校验(拒绝 `-` 开头 / 不存在)。
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from shipin_platform.analysis.reference import (  # noqa: E402
    MediaProbeError, ReferenceError,
)
from shipin_platform.analysis.reference_profiler import (  # noqa: E402
    BRIEF_DIMENSIONS, _black_segments, _narration_density,
    _silence_segments, black_stats, blackdetect_pix_arg,
    build_brief_prefill, profile_reference,
)

FFMPEG = shutil.which("ffmpeg")
OUT = Path(__file__).resolve().parents[1] / "out"
OUT.mkdir(exist_ok=True)


def _make_sample_video() -> Path:
    """6.0s 样例: 白(2.4s)->黑(0.6s)->白(3.0s); 音轨 4s 440Hz + 2s 静音。"""
    base = OUT / "_prof_src.mp4"
    subprocess.run([
        FFMPEG, "-y",
        "-f", "lavfi", "-i", "testsrc2=s=320x180:d=2.4",
        "-f", "lavfi", "-i", "color=black:s=320x180:d=0.6",
        "-f", "lavfi", "-i", "testsrc2=s=320x180:d=3.0",
        "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1[v]",
        "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        str(base),
    ], capture_output=True, text=True, check=True)
    subprocess.run([
        FFMPEG, "-y",
        "-i", str(base),
        "-f", "lavfi", "-i", "sine=frequency=440:duration=4.0",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
        "-filter_complex", "[1:a]volume=0.5[tone];[tone][2:a]concat=n=2:v=0:a=1[aout]",
        "-map", "0:v", "-map", "[aout]",
        "-c:v", "copy", "-c:a", "aac", "-t", "6.0",
        "-y", str(OUT / "sample_av.mp4"),
    ], capture_output=True, text=True, check=True)
    return OUT / "sample_av.mp4"


@pytest.fixture(scope="module")
def media():
    if FFMPEG is None:
        pytest.skip("ffmpeg 不可用")
    return _make_sample_video()


class TestBlackDetect:
    def test_pix_arg_probed(self):
        from shipin_platform.analysis.reference_profiler import blackdetect_pix_arg
        arg = blackdetect_pix_arg()
        assert arg in ("pix_th", "pix_thresh")

    def test_black_segments_detected(self, media):
        segs = _black_segments(str(media))
        assert any(s["dur"] >= 0.4 for s in segs), segs
        for s in segs:
            assert s["start"] < s["end"]

    def test_black_stats_ratio(self, media):
        import subprocess as sp
        r = sp.run([FFMPEG, "-hide_banner", "-i", str(media)],
                   capture_output=True, text=True)
        import re
        m = re.search(r"Duration:\s*([0-9:.]+)", r.stderr)
        assert m
        h, mi, sec = (float(x) for x in m.group(1).split(":"))
        dur = h * 3600 + mi * 60 + sec
        segs = _black_segments(str(media))
        stats = black_stats(segs, dur)
        # 样例含 0.6s 纯黑段 -> 黑占比应在 5%-20% 之间
        assert 0.02 < stats["ratio"] < 0.3, stats
        assert stats["count"] >= 1


class TestNarrationDensity:
    def test_silence_segments_parsed(self, media):
        sil = _silence_segments(str(media))
        # 4s 音 + 2s 静音 → 应当检出≥1 段静音(从 ~4s 起)
        assert len(sil) >= 1
        assert all(s["end"] >= s["start"] for s in sil)

    def test_density_label(self, media):
        sil = _silence_segments(str(media))
        d = _narration_density(sil, 6.0)
        # 4s 有音 / 6s → ~0.67 附近;不设死值,容差宽松
        assert 0.4 <= d["speech_ratio"] <= 0.99
        assert d["density"] in ("dense_narration", "spoken_mix", "music_lead")


class TestBriefPrefill:
    def test_nine_dimensions(self):
        assert len(BRIEF_DIMENSIONS) == 9

    def test_prefill_states(self):
        meta = {"duration_sec": 30.0, "width": 1080, "height": 1920}
        pacing = {"label": "fast"}
        black = {"ratio": 0.01, "count": 0, "total_sec": 0.0, "max_sec": 0.0}
        narr = {"speech_ratio": 0.85, "silence_count": 1,
                "silence_total_sec": 2.0, "longest_silence": 1.0}
        p = build_brief_prefill(meta, pacing, black, narr, scene_count=4,
                                source_path="/tmp/ref.mp4")
        assert set(p) == set(BRIEF_DIMENSIONS)
        assert p["content_type"]["state"] == "filled"  # 口播判定
        assert p["content_type"].get("value") == "talking_head"
        assert p["duration_sec"]["value"] == 30
        assert p["product_info"]["state"] == "pending"
        # 竖屏 9:16 → 抖音系
        assert p["target_platform"]["value"] == "douyin,kuaishou"
        assert p["target_audience"]["state"] == "pending"
        assert p["reference_materials"]["value"] == "/tmp/ref.mp4"

    def test_prefill_states_slow_black_heavy(self):
        meta = {"duration_sec": 30.0, "width": 1280, "height": 720}
        pacing = {"label": "slow"}
        black = {"ratio": 0.2, "count": 3, "total_sec": 6.0, "max_sec": 2.0}
        narr = {"speech_ratio": 0.2, "silence_count": 4,
                "silence_total_sec": 24.0, "longest_silence": 8.0}
        p = build_brief_prefill(meta, pacing, black, narr, scene_count=12)
        assert p["content_type"]["value"] == "product"  # 黑占比大
        assert p["special_requirements"]["value"] == "横屏 16:9"
        assert p["tone"]["state"] == "suggested"


class TestProfileReference:
    def test_report_structure_and_roundtrip(self, media, tmp_path):
        r = profile_reference(str(media), name="样例", save_dir=tmp_path)
        assert r["ok"] is True
        rep = r["report"]
        assert rep["profile"] == "shipin.reference@1"
        assert rep["metadata"]["has_audio"] is True
        assert rep["shots"] and rep["pacing"]
        assert rep["black"]["count"] >= 1
        assert rep["narration"]["speech_ratio"] > 0
        assert set(rep["brief_prefill"]) == set(BRIEF_DIMENSIONS)
        assert r["report_path"] == str(tmp_path / "样例.json")
        saved = json.loads((tmp_path / "样例.json").read_text(encoding="utf-8"))
        assert saved["name"] == "样例"
        assert saved["black"]["count"] == rep["black"]["count"]

    def test_rejects_dash_path(self):
        with pytest.raises(ReferenceError):
            profile_reference("-rf.mp4")

    def test_rejects_missing_file(self):
        with pytest.raises(MediaProbeError):
            profile_reference("Z:/no_such_video.mp4")


class TestIngestReferenceApi:
    """POST /api/ingest/reference: 落盘 + 返回 report + 非法参数 422。"""

    def test_ingest_writes_report(self, media, tmp_path, monkeypatch):
        import api
        from fastapi.testclient import TestClient
        from shipin_platform.analysis import reference_profiler
        real_profile = reference_profiler.profile_reference

        def fake_profile(*a, **k):
            k["save_dir"] = tmp_path
            return real_profile(*a, **k)
        monkeypatch.setattr(reference_profiler, "profile_reference",
                            fake_profile)
        client = TestClient(api.app)
        r = client.post("/api/ingest/reference", json={
            "video_path": str(media), "name": "e2e-sample"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert (tmp_path / "e2e-sample.json").exists()
        assert set(body["brief_prefill"]) == set(BRIEF_DIMENSIONS)
        saved = json.loads((tmp_path / "e2e-sample.json").read_text("utf-8"))
        assert saved["name"] == "e2e-sample"

    def test_ingest_bad_name_422(self, media):
        import api
        from fastapi.testclient import TestClient
        r = TestClient(api.app).post("/api/ingest/reference", json={
            "video_path": str(media), "name": "a/b"})
        assert r.status_code == 422

    def test_ingest_missing_video_422(self):
        import api
        from fastapi.testclient import TestClient
        r = TestClient(api.app).post("/api/ingest/reference", json={
            "video_path": "Z:/nope.mp4"})
        assert r.status_code == 422