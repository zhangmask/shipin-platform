"""Hardening tests: per-clip QC gate, confirmation gates, anchor enforcement,
upstream-stale rejection, LLM-review merge, context-parameterized final review.

These tests isolate the stage store (in-memory) so they never touch the real
data/stage_store.db.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fastapi.testclient import TestClient  # noqa: E402

import api  # noqa: E402
from shipin_platform.orchestration.stage_store import ProjectStageStore  # noqa: E402

FFMPEG = shutil.which("ffmpeg")
pytestmark = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not available")


@pytest.fixture()
def client(monkeypatch, tmp_path):
    """TestClient with an isolated in-memory stage store."""
    api._STAGE_STORE = ProjectStageStore(":memory:")
    return TestClient(api.app)


def _make_clip(out: Path, spec: list[tuple[str, float]]) -> Path:
    """Concat solid-color segments with hard cuts: [('red', 2.0), ('blue', 2.0)]."""
    parts = []
    inputs = []
    for i, (color, dur) in enumerate(spec):
        inputs += ["-f", "lavfi", "-t", str(dur),
                   "-i", f"color=c={color}:s=320x240:r=24"]
        parts.append(f"[{i}:v]")
    out.parent.mkdir(parents=True, exist_ok=True)
    filt = "".join(parts) + f"concat=n={len(spec)}:v=1:a=0[out]"
    subprocess.run(
        [FFMPEG, "-y", *inputs, "-filter_complex", filt, "-map", "[out]",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)],
        capture_output=True, text=True, check=True)
    return out


def _make_motion_clip(out: Path, dur: float = 4.0) -> Path:
    """Continuous-motion clip (testsrc2), no cuts."""
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [FFMPEG, "-y", "-f", "lavfi", "-t", str(dur),
         "-i", f"testsrc2=duration={dur}:size=320x240:r=24",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)],
        capture_output=True, text=True, check=True)
    return out


# ── clip_qc unit behavior ─────────────────────────────────────────

class TestClipQc:
    def test_missing_clip_is_fix(self, tmp_path):
        from shipin_platform.review.clip_qc import qc_clip
        r = qc_clip(str(tmp_path / "nope.mp4"), shot_id="S01")
        assert r["verdict"] == "fix"
        assert r["findings"][0]["code"] == "CLIP_MISSING"

    def test_clean_motion_clip_passes(self, tmp_path):
        from shipin_platform.review.clip_qc import qc_clip
        clip = _make_motion_clip(tmp_path / "ok.mp4", 4.0)
        r = qc_clip(str(clip), shot_id="S01", expected_duration_sec=4.0,
                    check_motion=True)
        assert r["verdict"] == "ok", r["findings"]
        assert r["checks"]["internal_cuts"]["value"] == 0
        assert r["checks"]["motion"]["value"] > 1.0

    def test_internal_hard_cut_is_caught(self, tmp_path):
        """The core disease: one storyboard shot containing a model-invented
        sub-shot MUST fail the gate."""
        from shipin_platform.review.clip_qc import qc_clip
        clip = _make_clip(tmp_path / "cut.mp4", [("red", 2.0), ("blue", 2.0)])
        r = qc_clip(str(clip), shot_id="S03")
        assert r["verdict"] == "fix"
        codes = {f["code"] for f in r["findings"]}
        assert "INTERNAL_CUTS" in codes
        assert "single" in r["next_action"] or "重新生成" in r["next_action"]

    def test_duration_mismatch_is_caught(self, tmp_path):
        from shipin_platform.review.clip_qc import qc_clip
        clip = _make_motion_clip(tmp_path / "short.mp4", 3.0)
        r = qc_clip(str(clip), shot_id="S01", expected_duration_sec=6.0)
        assert r["verdict"] == "fix"
        assert any(f["code"] == "DURATION_MISMATCH" for f in r["findings"])

    def test_static_clip_fails_motion_floor(self, tmp_path):
        from shipin_platform.review.clip_qc import qc_clip
        clip = _make_clip(tmp_path / "static.mp4", [("gray", 3.0)])
        r = qc_clip(str(clip), shot_id="S07", check_motion=True)
        assert r["verdict"] == "fix"
        assert any(f["code"] == "STATIC_SLIDESHOW" for f in r["findings"])
        # …but a brand card is static by design and waives the motion floor
        r2 = qc_clip(str(clip), shot_id="S07", check_motion=False)
        assert r2["verdict"] == "ok", r2["findings"]

    def test_reference_match(self, tmp_path):
        from shipin_platform.review.clip_qc import qc_clip
        clip = _make_motion_clip(tmp_path / "ref.mp4", 4.0)
        ref = tmp_path / "ref.png"
        subprocess.run([FFMPEG, "-y", "-ss", "0.2", "-i", str(clip),
                        "-frames:v", "1", str(ref)],
                       capture_output=True, text=True, check=True)
        r = qc_clip(str(clip), shot_id="S01", reference_image=str(ref))
        assert r["verdict"] == "ok", r["findings"]
        assert r["checks"]["reference_match"]["dhash_hamming"] <= 32


# ── hard_gates context parameterization ───────────────────────────

class TestFinalReviewContext:
    def test_prompt_no_longer_hardcodes_laptop(self):
        from shipin_platform.review.hard_gates import _batch_prompt
        p = _batch_prompt("1.0, 2.0", 2, {"product_info": "精品咖啡",
                                          "duration_sec": 30})
        assert "笔记本" not in p
        assert "精品咖啡" in p
        # neutral fallback also contains no stale product
        p2 = _batch_prompt("1.0", 1, None)
        assert "笔记本" not in p2

    def test_shot_boundaries_and_frames(self):
        from shipin_platform.review.hard_gates import (_shot_boundaries,
                                                       _context_frames)
        shots = [{"duration_sec": 5}, {"duration_sec": 5}, {"duration_sec": 3}]
        assert _shot_boundaries(shots) == [0.0, 5.0, 10.0]
        frames = _context_frames(13.0, shots, 12)
        assert all(0 <= t <= 13.0 for t in frames)
        assert len(frames) <= 12

    def test_blocked_without_key(self, monkeypatch, tmp_path):
        from shipin_platform.review import hard_gates
        monkeypatch.setattr(hard_gates, "_vlm_credentials", lambda: "")
        r = hard_gates.vlm_review_final(str(tmp_path / "x.mp4"))
        assert r["verdict"] == "blocked"


# ── state machine: confirmation gates + clip QC ledger ────────────

class TestStoreGates:
    def test_confirmation_roundtrip(self):
        from shipin_platform.orchestration.stage_store import StageGateError
        store = ProjectStageStore(":memory:")
        store.create_project("p1")
        with pytest.raises(StageGateError):
            store.assert_confirmed("p1", "script")
        store.record_confirmation("p1", "script", approved_by="user")
        assert store.assert_confirmed("p1", "script")["approved_by"] == "user"

    def test_bad_gate_rejected(self):
        store = ProjectStageStore(":memory:")
        store.create_project("p1")
        from shipin_platform.orchestration.stage_store import StageGateError
        with pytest.raises(StageGateError):
            store.record_confirmation("p1", "nonsense")

    def test_clip_qc_ledger(self):
        store = ProjectStageStore(":memory:")
        store.create_project("p1")
        store.record_clip_qc("p1", "S01", "x/S01_clip.mp4", "ok", {"v": 1})
        store.record_clip_qc("p1", "S03", "x/S03_clip.mp4", "fix", {"v": 2})
        assert store.get_clip_qc("p1", "S02") is None
        rows = store.list_clip_qc("p1")
        assert {r["shot_id"]: r["verdict"] for r in rows} == {"S01": "ok", "S03": "fix"}


# ── API gates end to end ──────────────────────────────────────────

VALID_BRIEF = {
    "content_type": "product", "product_info": "精品咖啡",
    "target_platform": "douyin", "duration_sec": 30,
    "target_audience": "都市白领", "tone": "轻松治愈",
    "creative_direction": "下班后的一杯咖啡", "reference_materials": "",
    "special_requirements": "",
}


VALID_SCRIPT = {
    "duration_sec": 30,
    "shots": [{"shot_id": f"S{i+1:02d}", "duration_sec": 5,
               "narration": "夜色下的城市街道闪着点微光",
               "scene": "夜色街道"} for i in range(6)],
}
# 台词门(§10.7):全片 ≥2 镜合规 dialogue——测试样本跟上新 schema
VALID_SCRIPT["shots"][1]["dialogue"] = {"role_code": "colleague_male",
                                        "text": "就在前面那栋楼"}
VALID_SCRIPT["shots"][3]["dialogue"] = {"role_code": "hero_male",
                                        "text": "签完就能收工"}


def _valid_storyboard() -> dict:
    beats = ["hook", "pain", "turn", "value", "outro", "落版"]
    sizes = ["ws", "cu", "ms", "ecu", "ws", "cu"]
    return {"hero_shot": "S04",
            "shots": [{"shot_id": f"S{i+1:02d}", "duration_sec": 5,
                       "beat": beats[i], "rhythm": "slow" if i % 2 else "medium",
                       "sfx": f"sfx_{i}", "shot_size": sizes[i],
                       "narration": "夜色下的城市街道闪着点微光",
                       "subject": "a young woman in a wool coat",
                       "motion": "walks forward slowly",
                       "scene": "neon street then warm cafe",
                       "spatial": "medium wide, subject left third",
                       "camera": "dolly in"} for i in range(6)]}


class TestApiGates:
    def test_confirm_endpoint_and_status(self, client):
        client.post("/api/project/create", json={"project_id": "p1"})
        r = client.post("/api/project/confirm",
                        json={"project_id": "p1", "gate": "script",
                              "approved_by": "user", "note": "ok"})
        assert r.status_code == 200 and r.json()["confirmed"] is True
        st = client.get("/api/project/p1/status").json()
        assert st["confirmations"]["script"]["approved_by"] == "user"
        r2 = client.post("/api/project/confirm",
                         json={"project_id": "p1", "gate": "bogus"})
        assert r2.status_code == 409  # gate 错误统一走 StageGateError→409

    def test_iterate_rejects_stale_upstream(self, client):
        client.post("/api/project/create", json={"project_id": "p2"})
        # brief 存在但 BLOCKED → script 迭代必须被拒
        client.post("/api/review/iterate",
                    json={"stage": "brief", "data": {"content_type": ""}})
        # 无 project_id 的调用不接状态机，用另一个项目制造 BLOCKED 上游
        client.post("/api/project/create", json={"project_id": "p3"})
        bad = {k: "" for k in VALID_BRIEF}
        client.post("/api/review/iterate",
                    json={"stage": "brief", "data": bad, "project_id": "p3"})
        r = client.post("/api/review/iterate",
                        json={"stage": "script", "data": {"shots": []},
                              "project_id": "p3"})
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "UPSTREAM_STALE"

    def test_agnes_video_requires_confirmations_then_anchor(self, client):
        client.post("/api/project/create", json={"project_id": "p4"})
        # 未确认 → 409 GATE_NOT_CONFIRMED（先拦确认，再谈锚定）
        r = client.post("/api/generate/agnes-video", json={
            "prompt": "x", "output_path": "o.mp4", "project_id": "p4",
            "first_frame": "a.jpg", "last_frame": "b.jpg"})
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "GATE_NOT_CONFIRMED"
        # 确认齐了、storyboard PASS，但没给 first_frame → 422 ANCHOR_REQUIRED
        client.post("/api/project/confirm", json={"project_id": "p4", "gate": "script"})
        client.post("/api/project/confirm", json={"project_id": "p4", "gate": "storyboard"})
        client.post("/api/review/iterate",
                    json={"stage": "brief", "data": VALID_BRIEF, "project_id": "p4"})
        r = client.post("/api/review/iterate",
                        json={"stage": "script", "data": VALID_SCRIPT,
                              "project_id": "p4"})
        assert r.json()["decision"] in ("pass", "pass_with_warnings"), r.json()["final"]
        r = client.post("/api/review/iterate",
                        json={"stage": "storyboard", "data": _valid_storyboard(),
                              "project_id": "p4"})
        assert r.json()["decision"] in ("pass", "pass_with_warnings"), r.json()["final"]
        r = client.post("/api/generate/agnes-video", json={
            "prompt": "x", "output_path": "o.mp4", "project_id": "p4",
            "shot_id": "S01"})
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "ANCHOR_REQUIRED"

    def test_image_gen_requires_script_confirmation(self, client):
        client.post("/api/project/create", json={"project_id": "p5"})
        r = client.post("/api/generate/image", json={
            "prompt": "coffee", "output_path": "o.jpg",
            "project_id": "p5", "shot_id": "S01"})
        assert r.status_code == 409

    def test_stitch_rejects_unqced_clips(self, client, tmp_path):
        client.post("/api/project/create", json={"project_id": "p6"})
        client.post("/api/project/confirm", json={"project_id": "p6", "gate": "script"})
        client.post("/api/review/iterate",
                    json={"stage": "brief", "data": VALID_BRIEF, "project_id": "p6"})
        client.post("/api/review/iterate",
                    json={"stage": "script", "data": VALID_SCRIPT, "project_id": "p6"})
        client.post("/api/review/iterate",
                    json={"stage": "storyboard", "data": _valid_storyboard(),
                          "project_id": "p6"})
        r = client.post("/api/video/stitch", json={
            "clips": [str(tmp_path / "S01_clip.mp4")],
            "output": str(tmp_path / "out.mp4"), "project_id": "p6"})
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "CLIP_QC_REQUIRED"
        # QC 记录一个 ok 的 clip 后，同 clip 不再被拦（ffmpeg stitch 仍会因文件
        # 不存在而 500，但不再是 409 CLIP_QC_REQUIRED）
        clip = _make_motion_clip(tmp_path / "S01_clip.mp4", 5.0)
        client.post("/api/qc/clip", json={
            "clip_path": str(clip), "shot_id": "S01",
            "project_id": "p6", "expected_duration_sec": 5.0,
            "check_motion": False})
        r2 = client.post("/api/video/stitch", json={
            "clips": [str(clip)], "output": str(tmp_path / "out.mp4"),
            "project_id": "p6", "transition": "cut"})
        assert r2.status_code != 409

    def test_qc_clip_records_verdict(self, client, tmp_path):
        client.post("/api/project/create", json={"project_id": "p7"})
        clip = _make_clip(tmp_path / "S02_clip.mp4",
                          [("red", 2.0), ("blue", 2.0)])
        r = client.post("/api/qc/clip", json={
            "clip_path": str(clip), "shot_id": "S02",
            "project_id": "p7"})
        assert r.status_code == 200
        assert r.json()["verdict"] == "fix"
        st = client.get("/api/project/p7/status").json()
        assert st["clip_qc"][0]["verdict"] == "fix"

    def test_iterate_llm_review_merge(self, client, monkeypatch):
        """LLM 语义审查的 critical 发现必须压过规则引擎的 PASS。"""
        import api as api_mod
        def fake_llm(stage, data, brief=None):
            return {"available": True, "reason": "", "scores": {"continuity": 30},
                    "findings": [{"dimension": "continuity",
                                  "severity": "critical",
                                  "issue": "S03 与 S04 场景突跳，无可拍过渡",
                                  "evidence": "S03 effect 字段为空",
                                  "fix": "补 cause/effect 过渡"}],
                    "raw": ""}
        monkeypatch.setattr(api_mod, "llm_stage_review", fake_llm)
        client.post("/api/project/create", json={"project_id": "p8"})
        client.post("/api/review/iterate",
                    json={"stage": "brief", "data": VALID_BRIEF, "project_id": "p8"})
        r = client.post("/api/review/iterate",
                        json={"stage": "script", "data": VALID_SCRIPT,
                              "project_id": "p8", "llm_review": True})
        body = r.json()
        assert body["final"]["decision"] == "revise"
        dims = [f["dimension"] for f in body["final"]["findings"]]
        assert "llm_continuity" in dims
        st = client.get("/api/project/p8/status").json()
        assert st["stages"]["script"]["status"] == "BLOCKED"

    def test_llm_review_skips_without_key(self, client, monkeypatch):
        import shipin_platform.review.llm_review as lr
        monkeypatch.setattr(lr, "_llm_key", lambda: "")
        out = lr.llm_stage_review("script", {"shots": []})
        assert out["available"] is False

    def test_storyboard_requires_narration(self, client):
        """coffee-v5 教训：剧本/分镜分叉后 TTS 无权威数据源——分镜逐镜必须有 narration。"""
        client.post("/api/project/create", json={"project_id": "p9"})
        client.post("/api/review/iterate",
                    json={"stage": "brief", "data": VALID_BRIEF, "project_id": "p9"})
        client.post("/api/review/iterate",
                    json={"stage": "script", "data": VALID_SCRIPT, "project_id": "p9"})
        sb = _valid_storyboard()
        for s in sb["shots"]:
            s.pop("narration", None)
        r = client.post("/api/review/iterate",
                        json={"stage": "storyboard", "data": sb, "project_id": "p9"})
        body = r.json()
        # 无机械修复可做 → 首次迭代即 stall/revise 并 BLOCKED，等 LLM 改写后重投
        assert body["decision"] in ("revise", "stall")
        codes = {f["issue"] for f in body["final"]["findings"]}
        assert any("narration" in c for c in codes)
        # 补上 narration 后通过
        for i, s in enumerate(sb["shots"]):
            s["narration"] = "夜色下的城市街道闪着点微光"
        r2 = client.post("/api/review/iterate",
                         json={"stage": "storyboard", "data": sb, "project_id": "p9"})
        assert r2.json()["decision"] in ("pass", "pass_with_warnings")


def test_gate_endpoints_require_project_id(client):
    """B2-1 平台级：花钱/写产物端点不许有"无项目旁路"——缺 project_id 直接 422。"""
    r = client.post("/api/generate/image", json={
        "prompt": "x", "output_path": "o.jpg"})
    assert r.status_code == 422
    r = client.post("/api/generate/agnes-video", json={
        "prompt": "x", "output_path": "o.mp4"})
    assert r.status_code == 422
    r = client.post("/api/tts/narrate", json={
        "script": {"shots": []}, "output_dir": "./outputs"})
    assert r.status_code == 422
