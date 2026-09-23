"""API contract tests (TestClient) + real-ffmpeg integration tests."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from fastapi.testclient import TestClient  # noqa: E402

import api  # noqa: E402

client = TestClient(api.app)
FFMPEG = shutil.which("ffmpeg")


# ── Review endpoints ─────────────────────────────────────────────

class TestReviewEndpoints:
    def test_health(self):
        r = client.get("/api/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["version"] == api.app.version

    def test_review_brief_finds_missing_dims(self):
        r = client.post("/api/review/brief", json={"brief": {}, "round": 1})
        assert r.status_code == 200
        body = r.json()
        assert body["decision"] == "revise"
        assert body["stats"]["critical"] + body["stats"]["suggestion"] > 0

    def test_review_script_endpoint(self):
        script = {"duration_sec": 10, "shots": [{"narration": "字" * 100}]}
        r = client.post("/api/review/script", json={"script": script, "round": 1})
        assert r.status_code == 200
        assert r.json()["decision"] == "revise"

    def test_review_storyboard_includes_quality_checks(self):
        scenes = [{
            "shot_id": "S1", "shot_size": "ECU", "motion": "dolly in",
            "camera": "dolly in", "lighting": "warm key", "scene_id": "sc1",
            "duration_sec": 2.0, "narration": "雨夜的街角还亮着灯",
            "subject": "a young woman", "scene": "rainy street", "spatial": "near neon",
        }]
        r = client.post("/api/review/storyboard",
                        json={"storyboard": {"scenes": scenes}, "round": 1})
        assert r.status_code == 200
        body = r.json()
        assert body["decision"] == "pass"
        assert "slideshow_risk" in body and "variation_check" in body

    def test_iterate_converges_server_side(self):
        data = {
            "style_anchor": "soft warm palette, 35mm film grain",
            "shot_prompts": [
                {"shot_id": "S1", "prompt_en": "a cinematic close-up of steaming coffee"},
            ],
        }
        r = client.post("/api/review/iterate",
                        json={"stage": "image_prompt", "data": data, "max_rounds": 3})
        assert r.status_code == 200
        body = r.json()
        assert body["decision"] == "pass"
        assert body["rounds_run"] == 2
        assert body["data"]["style_anchor"] in body["data"]["shot_prompts"][0]["prompt_en"]
        assert body["manual_modes"] == []

    def test_iterate_invalid_stage_422(self):
        r = client.post("/api/review/iterate",
                        json={"stage": "bogus", "data": {}, "max_rounds": 3})
        assert r.status_code == 422

    def test_iterate_manual_modes_reported(self):
        """轮58 语义更新(轮57 字数门方向化的连带):旧断言
        `decision in ("stall","stop")` 依赖旧双向 ±10% 检查在机械裁剪后
        继续判 critical 维持 stall——那正是轮57 修掉的死锁本身
        (34 字 vs 40 字预算被打死、round2 stall)。新行为:第一轮
        duration critical + NARRATION_TOO_LONG 进 manual_modes,机械
        裁剪到 ≤14 字后轮2 只剩 suggestion → 收敛放行。这里钉住
        manual_modes 上报 + 收敛后不再死锁两个事实。"""
        script = {"duration_sec": 10, "shots": [{"narration": "字" * 100}]}
        r = client.post("/api/review/iterate",
                        json={"stage": "script", "data": script, "max_rounds": 3})
        assert r.status_code == 200
        body = r.json()
        assert "DURATION_MISMATCH" in body["manual_modes"], body["manual_modes"]
        # 收敛:不再无限 stall(轮57 修死锁后的正确行为)
        assert body["decision"] in ("pass", "pass_with_warnings",
                                    "stall", "stop"), body["decision"]
        final = (body.get("data") or {}).get("shots") or []
        assert final and all(len(s.get("narration", "")) <= 14
                             for s in final), final


# ── FFmpeg-backed endpoints (real media, skipped without ffmpeg) ──

@pytest.fixture(scope="module")
def media(tmp_path_factory):
    """Tiny real media: two 1s color clips + one sine-tone audio."""
    if not FFMPEG:
        pytest.skip("ffmpeg not available")
    tmp = tmp_path_factory.mktemp("media")

    def make_video(path, color):
        r = subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c={color}:s=320x240:d=1:r=24",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
            capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[-300:]

    red = tmp / "red.mp4"; make_video(red, "red")
    blue = tmp / "blue.mp4"; make_video(blue, "blue")

    tone = tmp / "tone.wav"
    r = subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-c:a", "pcm_s16le", str(tone)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-300:]

    return {"red": red, "blue": blue, "tone": tone, "dir": tmp}


class TestFFmpegEndpoints:
    def test_probe(self, media):
        r = client.post("/api/video/probe", json={"path": str(media["red"])})
        assert r.status_code == 200
        body = r.json()
        assert body["codec"] == "h264"
        assert body["width"] == 320 and body["height"] == 240
        assert 0.5 < body["duration"] < 2.0

    def test_encode(self, media):
        out = media["dir"] / "encoded.mp4"
        r = client.post("/api/video/encode", json={
            "input_path": str(media["red"]), "output_path": str(out)})
        assert r.status_code == 200
        assert out.exists()

    def test_normalize_one_pass(self, media):
        out = media["dir"] / "norm1.m4a"
        r = client.post("/api/audio/normalize", json={"audio_path": str(media["tone"])})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert Path(body["output"]).exists()

    def test_normalize_two_pass(self, media):
        out = media["dir"] / "norm2.m4a"
        r = client.post("/api/audio/normalize", json={
            "audio_path": str(media["tone"]),
            "output_path": str(out), "two_pass": True, "target_lufs": -16.0})
        assert r.status_code == 200
        assert out.exists()

    def test_normalize_missing_file_404(self):
        r = client.post("/api/audio/normalize", json={"audio_path": "Z:/nope.wav"})
        assert r.status_code == 404

    def test_black_detect_solid_color(self, media):
        r = client.post("/api/video/black-detect?min_dur=0.3",
                        json={"path": str(media["red"])})
        assert r.status_code == 200
        assert r.json()["count"] == 0

    def test_concat_via_openmontage(self, media):
        """concat() must map to a VALID transition ('cut'), not 'hard_cut'."""
        out = media["dir"] / "concat.mp4"
        r = client.post("/api/video/concat",
                        json={"clips": [str(media["red"]), str(media["blue"])],
                              "output": str(out), "transition": "cut"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert out.exists()
        probe = client.post("/api/video/probe", json={"path": str(out)}).json()
        assert 1.5 < probe["duration"] < 3.5


# ── AI agent self-discovery + intake ─────────────────────────────

class TestAgentGuideAndIntake:
    def test_agent_guide_self_describes(self):
        r = client.get("/api/agent-guide")
        assert r.status_code == 200
        body = r.json()
        assert body["service"] == "shipin-platform"
        assert len(body["flow"]) >= 10
        assert any(e["path"] == "/api/review/iterate" for e in body["endpoints"])
        assert body["review_contract"]["stages"] == [
            "brief", "script", "storyboard", "image_prompt", "video_prompt"]

    def test_intake_questions_covers_required_dims(self):
        r = client.post("/api/intake/questions", json={
            "intent": "爽文短剧", "answered": {"tone": "燃"}})
        assert r.status_code == 200
        qs = r.json()["questions"]
        required = [q for q in qs if q["required"]]
        assert len(required) == 9
        marked_answered = [q for q in qs if q["answered"]]
        assert [q["dimension"] for q in marked_answered] == ["tone"]

    def test_intake_draft_fills_all_dims_and_reviews(self):
        r = client.post("/api/intake/draft", json={
            "answers": {"content_type": "short_drama", "duration_sec": 90,
                        "tone": "燃"},
            "intent": "废柴觉醒打脸反派"})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        # every required dimension present in the draft brief
        for dim in ["content_type", "product_info", "target_platform",
                    "duration_sec", "target_audience", "tone",
                    "creative_direction", "reference_materials",
                    "special_requirements"]:
            assert dim in body["brief"], dim
        # free-text intent becomes creative direction when unanswered
        assert body["brief"]["creative_direction"] == "废柴觉醒打脸反派"
        # draft already ran one review round
        assert body["review"]["stage"] == "brief"
        assert body["next"]["action"] == "POST /api/review/iterate"
        assert body["next"]["body"]["stage"] == "brief"

    def test_draft_missing_fields_flagged(self):
        r = client.post("/api/intake/draft", json={
            "answers": {"content_type": "short_drama"},
            "intent": "随便一个短剧"})
        body = r.json()
        assert set(body["missing"]) == {
            "product_info", "reference_materials", "special_requirements"}
