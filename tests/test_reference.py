"""B2: 参考视频分析(元数据 + scene 镜头切分 + 节奏 + brief hint)测试。

使用真实 ffmpeg 构建确定性拼接待测视频:
  - pair.mp4   : 纯蓝 3s + testsrc2 2s → 唯一切点在 3.0s, 全片 5s, 2 镜
  - single.mp4 : 纯红 4s → 无切点, 1 镜
断言镜头覆盖全长、切点定位、节奏统计与 brief hint,并覆盖路径安全。
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from fastapi.testclient import TestClient  # noqa: E402

from shipin_platform.analysis.reference import (  # noqa: E402
    MediaProbeError, ReferenceError, analyze_reference_video,
    build_brief_hint, build_summary, validate_media_path,
)

import api  # noqa: E402

client = TestClient(api.app)
FFMPEG = shutil.which("ffmpeg")

pytestmark = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg 不可用")

OUT = Path(__file__).resolve().parents[1] / "out"
OUT.mkdir(exist_ok=True)


def _make_pair_video() -> Path:
    """3s 纯蓝 + 2s testsrc2 → 5s, 切点应落在 3.0s。"""
    p = OUT / "ref_pair.mp4"
    r = subprocess.run([
        FFMPEG, "-y",
        "-f", "lavfi", "-i", "color=c=blue:s=320x180:r=25:d=3",
        "-f", "lavfi", "-i", "testsrc2=s=320x180:r=25:d=2",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0",
        "-pix_fmt", "yuv420p", str(p),
    ], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-800:]
    return p


def _make_single_video() -> Path:
    """4s 纯红: 无内部镜头切换,应为单镜。"""
    p = OUT / "ref_single.mp4"
    r = subprocess.run([
        FFMPEG, "-y",
        "-f", "lavfi", "-i", "color=c=red:s=320x180:r=25:d=4",
        "-pix_fmt", "yuv420p", p,
    ], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-800:]
    return p


@pytest.fixture(scope="module")
def pair_video():
    return _make_pair_video()


@pytest.fixture(scope="module")
def single_video():
    return _make_single_video()


# ── 直接调用单元级 ───────────────────────────────────────────────

class TestAnalyzeShots:
    def test_pair_two_shots_full_coverage(self, pair_video):
        r = analyze_reference_video(pair_video, scene_threshold=0.2)
        assert r["ok"] is True
        shots = r["shots"]
        # 首镜从 0 开始、末镜到全长结束、两镜连续无间隙
        assert shots[0]["start"] == 0.0
        assert shots[-1]["end"] == pytest.approx(5.0, abs=0.06)
        assert [s["start"] for s in shots] == [0.0, 3.0]
        assert [s["end"] for s in shots] == [pytest.approx(3.0, abs=0.06),
                                             pytest.approx(5.0, abs=0.06)]

    def test_pair_pacing_stats(self, pair_video):
        r = analyze_reference_video(pair_video, scene_threshold=0.2)
        p = r["pacing"]
        assert p["count"] == 2
        assert p["avg"] == pytest.approx(2.5, abs=0.02)   # (3+2)/2
        assert p["median"] == pytest.approx(2.5, abs=0.02)
        assert p["fast"] + p["medium"] == 2
        assert p["label"] == "medium"

    def test_metadata_fields(self, pair_video):
        r = analyze_reference_video(pair_video, scene_threshold=0.2)
        m = r["metadata"]
        assert m["width"] == 320 and m["height"] == 180
        assert m["fps"] == pytest.approx(25.0, abs=0.5)
        assert m["duration_sec"] == pytest.approx(5.0, abs=0.06)
        assert m["has_audio"] is False

    def test_single_shot_video(self, single_video):
        r = analyze_reference_video(single_video, scene_threshold=0.3)
        assert len(r["shots"]) == 1
        s = r["shots"][0]
        assert s["start"] == 0.0
        assert s["end"] == pytest.approx(4.0, abs=0.06)
        assert r["pacing"]["count"] == 1
        assert r["pacing"]["label"] == "medium"

    def test_threshold_damping_no_crash(self, pair_video):
        # 越界阈值被钳制, 不抛错
        r = analyze_reference_video(pair_video, scene_threshold=9.9)
        assert r["ok"] is True


class TestBriefHint:
    def test_hint_shape(self, pair_video):
        r = analyze_reference_video(pair_video, scene_threshold=0.2)
        h = r["brief_hint"]
        assert h["duration_sec"] == 5
        assert h["scene_count"] == 2
        assert h["pacing"] == "medium"
        assert "5s" in h["reference_note"] and "2 镜" in h["reference_note"]

    def test_summary_string(self, pair_video):
        r = analyze_reference_video(pair_video, scene_threshold=0.2)
        s = r["summary"]
        assert isinstance(s, str) and "320x180" in s and "25.0fps" in s


class TestValidation:
    def test_reject_missing_file(self):
        with pytest.raises(MediaProbeError):
            analyze_reference_video(OUT / "no_such_video.mp4")

    def test_reject_dash_prefix(self):
        with pytest.raises(ReferenceError):
            validate_media_path("-i /dev/tcp/1.2.3.4/443")


# ── API 集成 ─────────────────────────────────────────────────────

class TestReferenceEndpoint:
    def test_endpoint_ok(self, pair_video):
        r = client.post("/api/analysis/reference", json={
            "video_path": str(pair_video), "scene_threshold": 0.2})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert len(body["shots"]) == 2
        assert body["pacing"]["count"] == 2
        assert body["brief_hint"]["scene_count"] == 2

    def test_endpoint_missing_file_422(self):
        r = client.post("/api/analysis/reference", json={
            "video_path": str(OUT / "does_not_exist.mp4")})
        assert r.status_code == 422
        assert "不存在" in r.json()["detail"]

    def test_endpoint_bad_path_422(self):
        r = client.post("/api/analysis/reference", json={
            "video_path": "-i anything"})
        assert r.status_code == 422