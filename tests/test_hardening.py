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

    def test_tail_beyond_shots_is_sampled(self, monkeypatch, tmp_path):
        """轮38(五审 #3 尾部子项):final.mp4 长于 Σ分镜时长时(拼接余量/
        音频床溢出),旧代码尾段一帧不采也无覆盖断言——未审内容直接进
        发布物。现在尾段必须进采样与覆盖计数。"""
        _vlm_stub(monkeypatch)
        from shipin_platform.review import hard_gates
        clip = _make_motion_clip(tmp_path / "tail.mp4", 6.0)
        ctx = {"shots": [
            {"shot_id": "S01", "duration_sec": 2.0, "subject": "主角"},
            {"shot_id": "S02", "duration_sec": 2.0, "subject": "主角"}]}
        r = hard_gates.vlm_review_final(str(clip), frames_count=8,
                                        context=ctx)
        cov = r["shot_coverage"]
        assert any(k.startswith("尾部") for k in cov), cov
        assert sum(v for k, v in cov.items()
                   if k.startswith("尾部")) >= 2, cov

    def test_tail_coverage_does_not_force_thin(self, monkeypatch, tmp_path):
        """轮40(六审 CRITICAL 回归):轮38 曾给每个尾段帧唯一 tag
        「尾部+X.XXs」→ 每条 cnt 恒 1 → COVERAGE_THIN 必触 → 凡有尾段
        (>0.5s)的成片终审恒 fix 无法交付。现在固定 tag + 按计划帧判定:
        尾段足额采样时不得出 COVERAGE_THIN。"""
        _vlm_stub(monkeypatch)
        from shipin_platform.review import hard_gates
        clip = _make_motion_clip(tmp_path / "tail2.mp4", 6.8)
        ctx = {"shots": [
            {"shot_id": "S01", "duration_sec": 2.0, "subject": "主角"},
            {"shot_id": "S02", "duration_sec": 2.0, "subject": "主角"},
            {"shot_id": "S03", "duration_sec": 2.0, "subject": "主角"}]}
        r = hard_gates.vlm_review_final(str(clip), frames_count=8,
                                        context=ctx)
        codes = {f["code"] for f in r["findings"]}
        assert "COVERAGE_THIN" not in codes, r["findings"]
        assert "COVERAGE_GAP" not in codes, r["findings"]
        assert r["shot_coverage"].get("尾部", 0) >= 1

    def test_tail_frame_break_not_exempted(self, monkeypatch, tmp_path):
        """轮40(六审 #2):尾段帧(超出 Σ)的 break 即使标 kind=boundary
        也不豁免——Σ 右侧没有镜,无所谓"采样偏移";旧实现末镜起点边界
        拿左邻镜长定尺,短末镜+尾段一起被 2.0s 窗吞掉。"""
        _vlm_stub(monkeypatch, breaks=[
            {"t": 18.5, "desc": "尾段画面出现条纹崩坏", "kind": "boundary"}])
        from shipin_platform.review import hard_gates
        clip = _make_motion_clip(tmp_path / "tb.mp4", 6.0)
        ctx = {"shots": [
            {"shot_id": "S01", "duration_sec": 4.0, "subject": "A"},
            {"shot_id": "S02", "duration_sec": 2.0, "subject": "A"}]}
        r = hard_gates.vlm_review_final(str(clip), frames_count=4, context=ctx)
        codes = {f["code"] for f in r["findings"]}
        assert "VLM_BREAK" in codes, r["findings"]
        assert r["boundary_transitions"] == []

    def test_vlm_request_exception_is_protocol_violation(self, monkeypatch,
                                                         tmp_path):
        """轮40(六审 #3):_ask_vlm 重试后仍抛异常(端点宕机)时,旧代码
        无 try → RuntimeError 逃出终审变 500;与"返回垃圾"给两种默认值
        (垃圾=拦截/宕机=崩溃)。现在同记 VLM_PROTOCOL_VIOLATION critical。"""
        from shipin_platform.review import hard_gates
        monkeypatch.setattr(hard_gates, "_vlm_credentials", lambda: "fake-key")

        def _boom(*a, **k):
            raise RuntimeError("endpoint down after retries")

        monkeypatch.setattr(hard_gates, "_ask_vlm", _boom)
        clip = _make_motion_clip(tmp_path / "down.mp4", 4.0)
        ctx = {"shots": [
            {"shot_id": "S01", "duration_sec": 4.0, "subject": "主角"}]}
        r = hard_gates.vlm_review_final(str(clip), frames_count=4, context=ctx)
        codes = {f["code"] for f in r["findings"]}
        assert "VLM_PROTOCOL_VIOLATION" in codes, r["findings"]
        assert r["verdict"] == "fix"

    def test_spec_field_drift_tolerated(self, monkeypatch, tmp_path):
        """轮40(六审 #4):模型把 spec 字段改名成 difference/points 时,
        旧代码 spec="" → 轮35c 的"空→critical"误杀正常片。现在取首个
        非空别名字段。"""
        import json as _json_d
        from shipin_platform.review import hard_gates
        monkeypatch.setattr(hard_gates, "_vlm_credentials", lambda: "fake-key")
        monkeypatch.setattr(
            hard_gates, "_ask_vlm",
            lambda *a, **k: _json_d.dumps(
                {"same": False, "difference": "服装颜色不同"},
                ensure_ascii=False))
        clip = _make_motion_clip(tmp_path / "drift.mp4", 4.0)
        ctx = {"shots": [
            {"shot_id": "S01", "duration_sec": 2.0, "subject": "女主角"},
            {"shot_id": "S02", "duration_sec": 2.0, "subject": "女主角"}]}
        r = hard_gates.vlm_review_final(str(clip), frames_count=4, context=ctx)
        hits = [f for f in r["findings"]
                if f["code"] in ("IDENTITY_SWITCH", "COSTUME_SWAP")]
        assert hits and hits[0]["severity"] == "warning", hits
        assert "服装" in hits[0]["message"]

    def test_shot_boundaries_and_frames(self):
        """_context_frames: 每镜全覆盖采样——不丢镜头、帧落在镜内、含中段。"""
        from shipin_platform.review.hard_gates import (_shot_boundaries,
                                                       _context_frames)
        shots = [{"duration_sec": 5}, {"duration_sec": 5}, {"duration_sec": 3}]
        assert _shot_boundaries(shots) == [0.0, 5.0, 10.0]
        # 轮40:with_total=True 追加 Σ(片尾锚点)——供末镜右侧豁免窗定尺;
        # 默认 False 保持旧语义(确定性滤波的"临近边界=合法转场"不认 Σ,
        # 否则临近片尾的闪帧/黑屏会被当合法过渡放行)
        assert _shot_boundaries(shots, with_total=True) == [0.0, 5.0, 10.0, 13.0]
        frames = _context_frames(13.0, shots, 12)
        # 返回 [(t, shot_idx)...], t 不越界,帧数不超预算
        assert all(0 <= t <= 13.0 for t, _ in frames)
        assert len(frames) <= 12
        # 每镜至少 3 帧(开/中/合),且帧时间落在对应镜头区间内
        by_shot: dict[int, list[float]] = {}
        bounds = _shot_boundaries(shots)
        for t, idx in frames:
            by_shot.setdefault(idx, []).append(t)
            assert bounds[idx] <= t <= bounds[idx] + shots[idx]["duration_sec"]
        assert len(by_shot) == 3, "任何镜头都不允许被整体遗漏"
        for idx, ts in by_shot.items():
            assert len(ts) >= 3
            # 中段采样:同一镜内帧间距 ≥0.3s(不是同一帧,也不是贴边两帧)
            assert max(ts) - min(ts) >= 0.3

    def test_drop_prefers_boundary_frames_keeps_middle(self):
        """超预算丢帧时丢「紧贴镜头边界」的帧,中段最容易崩坏的帧必须留下。"""
        from shipin_platform.review.hard_gates import _context_frames
        shots = [{"duration_sec": 7} for _ in range(3)]   # 3 镜×7s,各铺 5 帧
        frames = _context_frames(21.0, shots, 12)          # 15 帧超预算 → 丢到 12
        assert len(frames) == 12
        by_shot: dict[int, list[float]] = {}
        for t, idx in frames:
            by_shot.setdefault(idx, []).append(t)
        for idx, ts in by_shot.items():
            assert len(ts) >= 3
            start, end = idx * 7.0, (idx + 1) * 7.0
            # 每镜头中段三分之一区间必须有帧(丢的是边界帧,不是中段帧)
            assert any(start + 7.0 / 3 < t < end - 7.0 / 3 for t in ts), \
                f"镜头{idx} 中段无帧,丢帧策略错误"

    def test_long_board_scales_frames_per_shot(self):
        """镜头多时按每镜配额扩容预算,而非一刀切 16 帧封顶。"""
        from shipin_platform.review.hard_gates import _context_frames
        shots = [{"duration_sec": 4} for _ in range(9)]   # 9 镜 36s
        frames = _context_frames(36.0, shots, 12)
        assert len(frames) >= 9 * 2, "每镜至少 2 帧,9 镜不下于 18 帧"
        by_shot = {}
        for t, idx in frames:
            by_shot.setdefault(idx, []).append(t)
        assert len(by_shot) == 9

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


# 每镜一句不重复的旁白（旁白查重门：同一句只能出现一次）
NARRATIONS = [
    "夜色下的城市街道闪着点微光",
    "加班的人拖着步子走向公交站",
    "街角的咖啡店还亮着那盏灯",
    "推门进去，暖气迎面涌过来",
    "一杯热咖啡，正好接到电话",
    "这就是今天下班后的好时光",
]


VALID_SCRIPT = {
    "duration_sec": 30,
    "shots": [{"shot_id": f"S{i+1:02d}", "duration_sec": 5,
               "narration": NARRATIONS[i],
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
    cams = ["dolly in", "truck right", "pedestal up", "crane down",
            "dolly out", "static"]
    return {"hero_shot": "S04",
            "shots": [{"shot_id": f"S{i+1:02d}", "duration_sec": 5,
                       "beat": beats[i], "rhythm": "slow" if i % 2 else "medium",
                       "sfx": f"sfx_{i}", "shot_size": sizes[i],
                       "narration": VALID_SCRIPT["shots"][i]["narration"],
                       "subject": "a young woman in a wool coat",
                       "motion": "walks forward slowly",
                       "scene": "neon street then warm cafe",
                       "spatial": "medium wide, subject left third",
                       "camera": cams[i]} for i in range(6)]}


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

    # ── 轮25:finalize 终审闸 ────────────────────────────────────────
    # 回归:finalize 此前只查阶段状态从不读 final_review.json——终审
    # verdict=fix(VLM 断帧/品牌未入画/时间轴红线/旁白缺失/单镜与入拼
    # critical 全并入)的成片照样能被置 RELEASED,所有门的阻断在发布
    # 入口被绕过。未跑过 assemble 同样必须拦。

    def _pass_all_stages(self, pid: str):
        import hashlib
        store = api._STAGE_STORE
        h = hashlib.sha256(b"final-bytes").hexdigest()
        for stage in ("brief", "script", "storyboard", "video_gen"):
            store.record_artifact(pid, stage, h)
        store.record_artifact(pid, "post_production", h)
        store.record_confirmation(pid, "script")

    def _write_final_review(self, pid: str, verdict: str,
                            with_artifact: bool = True):
        """轮33:默认连 final.mp4 一起落(与 post_production 指纹同内容的
        b"final-bytes")——finalize 的发布物三方哈希比对需要盘上成片存在
        且与凭证/post_production 指纹一致;with_artifact=False 造「有凭证
        无成片」的负例。"""
        import hashlib
        import json as _json
        work = api._project_dir(pid)
        work.mkdir(parents=True, exist_ok=True)
        if with_artifact:
            (work / "final.mp4").write_bytes(b"final-bytes")
        p = work / "final_review.json"
        p.write_text(_json.dumps(
            {"verdict": verdict, "reason": f"终审{verdict}",
             "video_sha256": hashlib.sha256(b"final-bytes").hexdigest(),
             "findings": [{"severity": "critical", "code": "VLM_BREAK",
                           "message": "x"}]},
            ensure_ascii=False), encoding="utf-8")
        return p

    def test_finalize_blocked_when_final_review_fix(self, client, tmp_path):
        pid = "fin-fix"
        client.post("/api/project/create", json={"project_id": pid})
        self._pass_all_stages(pid)
        self._write_final_review(pid, "fix")
        r = client.post(f"/api/project/{pid}/finalize")
        assert r.status_code == 409
        fails = r.json()["detail"]["fails"]
        gate = next(f for f in fails if f.get("gate") == "final_review")
        assert gate["status"] == "fix" and gate["critical"] == 1

    def test_finalize_blocked_when_no_final_review(self, client):
        pid = "fin-none"
        client.post("/api/project/create", json={"project_id": pid})
        self._pass_all_stages(pid)
        work = self._fresh_project_dir(pid)
        # 成片哈希与 post_production 一致(过 artifact 门), isolating 本测试
        # 关心的点:没有 final_review.json → final_review 门拦
        (work / "final.mp4").write_bytes(b"final-bytes")
        r = client.post(f"/api/project/{pid}/finalize")
        assert r.status_code == 409
        gate = next(f for f in r.json()["detail"]["fails"]
                    if f.get("gate") == "final_review")
        assert gate["status"] == "NOT_REVIEWED"

    def _fresh_project_dir(self, pid: str) -> Path:
        """轮33:清掉项目目录再建——API 测试写的是真实 data/projects,
        上一次运行的残留(尤其 final.mp4)会污染本轮断言。"""
        import shutil as _sh
        d = api._project_dir(pid)
        _sh.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def test_finalize_blocked_when_artifact_swapped_after_review(
            self, client):
        """轮33:发布物三方哈希比对——assemble 跑通后把 final.mp4 换掉
        再 finalize,必须 409(旧行为只读 verdict,内容门在发布入口被
        整体绕过:三审审计的共同根因)。"""
        import hashlib
        pid = "fin-swap"
        client.post("/api/project/create", json={"project_id": pid})
        self._pass_all_stages(pid)
        work = self._fresh_project_dir(pid)
        import json as _json_sw
        real = b"REAL-FINAL-VIDEO-BYTES"
        (work / "final.mp4").write_bytes(real)
        # 终审凭证:审的是 real,verdict pass
        (work / "final_review.json").write_text(_json_sw.dumps(
            {"verdict": "pass", "reason": "终验通过",
             "video_sha256": hashlib.sha256(real).hexdigest(),
             "findings": []}, ensure_ascii=False), encoding="utf-8")
        # 换文件(模拟 mux/直接替换)
        (work / "final.mp4").write_bytes(b"SWAPPED-DEFECTIVE-BYTES")
        r = client.post(f"/api/project/{pid}/finalize")
        assert r.status_code == 409
        fails = r.json()["detail"]["fails"]
        gate = next(f for f in fails if f.get("gate") == "final_artifact")
        assert gate["status"] in ("REVIEW_VIDEO_MISMATCH",
                                  "ARTIFACT_MISMATCH")

    def test_finalize_ok_when_artifact_hash_matches(self, client):
        """轮33 happy path:盘上 final.mp4 = 终审凭证记录的那条 = post_
        production 指纹 → 放行。"""
        import hashlib
        pid = "fin-hash-ok"
        client.post("/api/project/create", json={"project_id": pid})
        store = api._STAGE_STORE
        real = b"REAL-FINAL-VIDEO-BYTES-2"
        _h = hashlib.sha256(real).hexdigest()
        for stage in ("brief", "script", "storyboard", "video_gen"):
            store.record_artifact(pid, stage, "h")
        store.record_artifact(pid, "post_production", _h)
        store.record_confirmation(pid, "script")
        work = self._fresh_project_dir(pid)
        (work / "final.mp4").write_bytes(real)
        import json as _json_h
        (work / "final_review.json").write_text(_json_h.dumps(
            {"verdict": "pass", "reason": "终验通过",
             "video_sha256": _h, "findings": []},
            ensure_ascii=False), encoding="utf-8")
        r = client.post(f"/api/project/{pid}/finalize")
        assert r.status_code == 200, r.json()

    def test_finalize_blocked_when_final_mp4_missing(self, client):
        """轮33:final_review pass 但盘上无 final.mp4 → 409(不能对空气发布)。"""
        pid = "fin-nomp4"
        client.post("/api/project/create", json={"project_id": pid})
        self._pass_all_stages(pid)
        self._fresh_project_dir(pid)
        self._write_final_review(pid, "pass", with_artifact=False)
        r = client.post(f"/api/project/{pid}/finalize")
        assert r.status_code == 409
        fails = r.json()["detail"]["fails"]
        assert any(f.get("gate") == "final_artifact"
                   and f.get("status") == "MISSING" for f in fails)

    def test_final_video_endpoint_rejects_external_video_path(
            self, client, monkeypatch, tmp_path):
        """轮33:带 project_id 时被审视频必须位于项目目录内——否则可对
        preview cut/他项目视频跑终验,把 pass 凭证签给本项目。"""
        pid = "fv-external"
        client.post("/api/project/create", json={"project_id": pid})
        self._pass_all_stages(pid)
        outside = tmp_path / "outside.mp4"
        outside.write_bytes(b"not-in-project")
        r = client.post("/api/review/final-video",
                        json={"video_path": str(outside),
                              "project_id": pid})
        assert r.status_code == 200
        assert r.json()["verdict"] == "blocked"
        assert "不在项目" in r.json()["reason"]
        assert not (api._project_dir(pid)
                    / "final_review.json").exists()

    # ── 轮34:旧凭证 fail-closed + 端点项目绑定 ──────────────────────
    # 四审 #1:轮33 的「腿缺失跳过」遇上 mux/normalize 会把 post_
    # production 指纹改写成新文件哈希——旧凭证+可覆写指纹让「换片再
    # 发布」对全部存量项目依然开放(commit 声称堵死的路径复辟)。

    def test_finalize_blocks_legacy_credential_without_video_sha(
            self, client):
        """pass 凭证但没有 video_sha256(旧数据/读取失败)→ 409
        CREDENTIAL_STALE——不许「旧凭证+可覆写指纹」放过被换过的成片。"""
        pid = "fin-legacy"
        client.post("/api/project/create", json={"project_id": pid})
        self._pass_all_stages(pid)
        work = self._fresh_project_dir(pid)
        (work / "final.mp4").write_bytes(b"final-bytes")
        import json as _json_lg
        (work / "final_review.json").write_text(_json_lg.dumps(
            {"verdict": "pass", "reason": "终验通过", "findings": []},
            ensure_ascii=False), encoding="utf-8")  # 无 video_sha256
        r = client.post(f"/api/project/{pid}/finalize")
        assert r.status_code == 409
        fails = r.json()["detail"]["fails"]
        gate = next(f for f in fails if f.get("gate") == "final_artifact")
        assert gate["status"] == "CREDENTIAL_STALE"
        assert "final-video" in gate["detail"]  # 指明恢复路径

    def test_finalize_blocks_when_reviewed_video_swapped_then_remuxed(
            self, client):
        """四审 #1 的完整攻击序列:assemble 过审 → mux 换文件(顺手覆写
        post_production 指纹)→ finalize。旧代码 pp 腿与换过的文件自洽
        → 放行;新代码凭证腿(视频哈希)仍指向旧视频 → 409。"""
        import hashlib
        pid = "fin-swap-remux"
        client.post("/api/project/create", json={"project_id": pid})
        self._pass_all_stages(pid)
        work = self._fresh_project_dir(pid)
        good, bad = b"GOOD-REVIEWED-FINAL", b"BAD-SWAPPED-FINAL"
        import json as _json_sr
        (work / "final.mp4").write_bytes(good)
        (work / "final_review.json").write_text(_json_sr.dumps(
            {"verdict": "pass", "reason": "终验通过",
             "video_sha256": hashlib.sha256(good).hexdigest(),
             "findings": []}, ensure_ascii=False), encoding="utf-8")
        # mux 换文件 + 顺手把 post_production 指纹改成新文件的(端点行为)
        (work / "final.mp4").write_bytes(bad)
        api._STAGE_STORE.record_artifact(
            pid, "post_production", hashlib.sha256(bad).hexdigest())
        r = client.post(f"/api/project/{pid}/finalize")
        assert r.status_code == 409
        fails = r.json()["detail"]["fails"]
        gate = next(f for f in fails if f.get("gate") == "final_artifact")
        assert gate["status"] == "REVIEW_VIDEO_MISMATCH"

    def test_final_video_endpoint_enforces_project_binding(
            self, client, monkeypatch):
        """四审 #3:带 project_id 落凭证前必须过请求级项目绑定(离线模式
        绑定是 no-op,这里钉的是「端点确实调用了绑定」这条接线)。"""
        pid = "fv-bind"
        client.post("/api/project/create", json={"project_id": pid})
        seen: list = []
        monkeypatch.setattr(api, "_enforce_project_binding",
                            lambda request, project_id: seen.append(
                                project_id))
        r = client.post("/api/review/final-video",
                        json={"video_path": str(api._project_dir(pid)),
                              "project_id": pid})
        assert r.status_code == 200
        assert seen == [pid], "final-video 落凭证路径必须过项目绑定"

    def test_enforce_project_binding_mechanism_blocks_cross_tenant(self):
        """绑定机制本身:绑到 A 的 key 访问 B → 403。"""
        from fastapi import HTTPException
        from api import _enforce_project_binding

        class _Principal:
            project_id = "A"

        class _State:
            principal = _Principal()

        class _Req:
            state = _State()

        with pytest.raises(HTTPException) as ei:
            _enforce_project_binding(_Req(), "B")
        assert ei.value.status_code == 403

    def test_finalize_idempotent_when_already_released(self, client):
        """轮32:已发布项目重复 finalize(前端重复点击/发布后轮询)必须
        幂等 200——旧行为走 required 循环,RELEASED≠PASS → 409
        UPSTREAM_FAILED(语义是「上游未过」,实际「早已发布」)。"""
        pid = "fin-twice"
        client.post("/api/project/create", json={"project_id": pid})
        self._pass_all_stages(pid)
        self._write_final_review(pid, "pass")
        r1 = client.post(f"/api/project/{pid}/finalize")
        assert r1.status_code == 200
        r2 = client.post(f"/api/project/{pid}/finalize")
        assert r2.status_code == 200, r2.json()
        assert r2.json()["status"] == "RELEASED"

    def test_finalize_ok_when_final_review_pass_keeps_hash(self, client):
        pid = "fin-pass"
        client.post("/api/project/create", json={"project_id": pid})
        self._pass_all_stages(pid)
        self._write_final_review(pid, "pass")
        r = client.post(f"/api/project/{pid}/finalize")
        assert r.status_code == 200 and r.json()["status"] == "RELEASED"
        # 轮25:发布不得把成片完整性指纹覆写成字面量(那会让后续哈希
        # 校验永远 409,pipeline_runner 早已避开,这里同样保住了)
        row = api._STAGE_STORE.get_stage(pid, "post_production")
        assert row["artifact_hash"] != "RELEASED"
        assert row["artifact_hash"].startswith("a") or len(
            row["artifact_hash"]) == 64

    # ── 轮30:手工/agent 链路的终审凭证出口 ──────────────────────────
    # 回归:平台 stitch 端点的 next_action 文档化手工链路
    # (burn/mux/normalize → /api/review/final-video → finalize),但该
    # 端点此前没有 project_id、不落盘 final_review.json,而 finalize
    # 终审闸(轮25)只认 assemble 写的这个文件 → 整条手工链路被 409
    # NOT_REVIEWED 死锁(修 finalize 门时引入的回归)。

    def _stub_final_review(self, monkeypatch, verdict: str):
        """轮33:stub 也带 video_sha256(与 _write_final_review 落的
        b"final-bytes" 一致)——finalize 的发布物三方哈希比对需要凭证里
        有被审视频哈希才放行。"""
        import hashlib
        from shipin_platform.review import hard_gates
        monkeypatch.setattr(
            hard_gates, "vlm_review_final",
            lambda *a, **k: {
                "verdict": verdict, "reason": f"终验{verdict}",
                "video_sha256": hashlib.sha256(b"final-bytes").hexdigest(),
                "findings": [{"severity": "critical",
                              "code": "VLM_BREAK", "message": "x"}]})

    def test_final_video_endpoint_pass_unlocks_finalize(self, client,
                                                        monkeypatch):
        """pass 落盘 final_review.json → 手工链路 finalize 放行。"""
        pid = "manual-pass"
        client.post("/api/project/create", json={"project_id": pid})
        self._pass_all_stages(pid)
        # 盘上成片(内容与 post_production 指纹/stub 的视频哈希一致)
        (api._project_dir(pid) / "final.mp4").write_bytes(b"final-bytes")
        self._stub_final_review(monkeypatch, "pass")
        r = client.post("/api/review/final-video",
                        json={"video_path": str(api._project_dir(pid)),
                              "project_id": pid})
        assert r.status_code == 200 and r.json()["verdict"] == "pass"
        fr = api._project_dir(pid) / "final_review.json"
        assert fr.is_file(), "pass 必须落盘终审凭证"
        rz = client.post(f"/api/project/{pid}/finalize")
        assert rz.status_code == 200, rz.json()

    def test_final_video_endpoint_fix_still_blocks(self, client,
                                                   monkeypatch):
        """fix 不落盘 → finalize 仍拦(闸的严格性不变)。"""
        pid = "manual-fix"
        client.post("/api/project/create", json={"project_id": pid})
        self._pass_all_stages(pid)
        self._stub_final_review(monkeypatch, "fix")
        r = client.post("/api/review/final-video",
                        json={"video_path": str(api._project_dir(pid)),
                              "project_id": pid})
        assert r.status_code == 200 and r.json()["verdict"] == "fix"
        fr = api._project_dir(pid) / "final_review.json"
        if fr.exists():
            fr.unlink()  # 清掉可能的历史残留再断言
        rz = client.post(f"/api/project/{pid}/finalize")
        assert rz.status_code == 409

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
            s["narration"] = NARRATIONS[i]
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


class TestClipSrcFallback:
    """时间轴红线与拼接共用 _clip_src 回退：旧项目 manifest 缺 clip 字段
    （或空串）时按约定命名解析，不得算成全员复用空串而误杀 REUSE 红线
    （轮6 e2e 实战发现并修复的回归）。"""

    def test_recorded_clip_wins(self, tmp_path):
        from shipin_platform.orchestration.pipeline_runner import _clip_src
        m = {"shots": {"S01": {"clip": r"E:/v/shots/a.mp4"}}}
        assert _clip_src(m, "S01", tmp_path) == r"E:/v/shots/a.mp4"

    def test_missing_clip_falls_back_to_convention(self, tmp_path):
        from shipin_platform.orchestration.pipeline_runner import _clip_src
        m = {"shots": {"S01": {}}}
        assert _clip_src(m, "S01", tmp_path) == str(
            tmp_path / "S01_clip.mp4")

    def test_empty_clip_field_falls_back(self, tmp_path):
        from shipin_platform.orchestration.pipeline_runner import _clip_src
        m = {"shots": {"S01": {"clip": ""}}}
        assert _clip_src(m, "S01", tmp_path) == str(
            tmp_path / "S01_clip.mp4")

    def test_legacy_manifest_no_shots_record(self, tmp_path):
        from shipin_platform.orchestration.pipeline_runner import _clip_src
        assert _clip_src({}, "S01", tmp_path) == str(
            tmp_path / "S01_clip.mp4")

    def test_distinct_fallback_sources_do_not_trip_reuse(self, tmp_path):
        """9 镜各自回退到约定命名 → 9 个不同 src，REUSE 红线必须放行。"""
        from shipin_platform.orchestration.pipeline_runner import _clip_src
        from shipin_platform.review.hard_gates import check_timeline
        sids = [f"S{i:02d}" for i in range(1, 10)]
        tl = [{"src": _clip_src({}, s, tmp_path),
               "start": float(i), "end": float(i + 1), "at": float(i)}
              for i, s in enumerate(sids)]
        r = check_timeline(tl, duration_sec=9.0)
        assert r["verdict"] == "ok"


# ── 轮8e:审查升级门(瞬变闪帧 / 分镜符合度 / 跨镜身份 / 时间轴对账) ─────


def _make_flash_clip(out, bands: list[tuple[float, float]],
                     dur: float = 4.0) -> Path:
    """灰度底 + 指定时间段整帧闪白(如 [(1.0,1.4),(2.6,3.0)])。"""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    enables = ["between(t,{a},{b})".format(a=a, b=b) for a, b in bands]
    subprocess.run(
        [FFMPEG, "-y", "-f", "lavfi", "-t", str(dur),
         "-i", f"color=c=gray:s=320x240:r=24",
         "-vf", "drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:"
                f"enable='{'+'.join(enables)}'",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)],
        capture_output=True, text=True, check=True)
    return Path(out)


def _vlm_stub(monkeypatch, *, shot_issues=None, same_person=None, breaks=None,
              same_person_spec="脸换了"):
    """替换 hard_gates 的 VLM 通道:按提示词分派 SAME_PERSON 与批次问题。
    same_person_spec: 判定"不是同一人"时的不一致点(轮17 测 COSTUME_SWAP
    升级需要不含脸/发的纯服装 spec)。"""
    import json as _json
    from shipin_platform.review import hard_gates

    def ask(images, prompt, key, max_tokens=1800):
        if "同一人" in prompt:  # 跨镜身份成对判定(SAME_PERSON_PROMPT)
            if same_person is None:
                return '{"same": true, "spec": "", "reason": ""}'
            return _json.dumps({"same": False, "spec": same_person_spec,
                                "reason": "两镜不是同一人"},
                               ensure_ascii=False)
        body = {"frames": [], "breaks": [] if breaks is None else breaks,
                "brand_seen": True,
                "shot_issues": [] if shot_issues is None else shot_issues}
        return _json.dumps(body, ensure_ascii=False)

    monkeypatch.setattr(hard_gates, "_vlm_credentials", lambda: "fake-key")
    monkeypatch.setattr(hard_gates, "_ask_vlm", ask)


class TestTransientGate:
    def test_flash_bands_caught_on_clip(self, tmp_path):
        from shipin_platform.review.clip_qc import qc_clip
        flash = _make_flash_clip(str(tmp_path / "flash.mp4"),
                                 [(1.0, 1.4), (2.6, 3.0)])
        r = qc_clip(str(flash), shot_id="S01")
        codes = {f["code"] for f in r["findings"]}
        assert "TRANSIENT_FLASH" in codes, r["findings"]
        assert r["checks"]["transient"]["count"] >= 2
        assert r["verdict"] == "fix"

    def test_clean_clip_has_no_transient(self, tmp_path):
        from shipin_platform.review.clip_qc import qc_clip
        clip = _make_motion_clip(tmp_path / "ok.mp4", 4.0)
        r = qc_clip(str(clip), shot_id="S01", check_motion=True)
        assert r["checks"]["transient"]["count"] == 0
        codes = {f["code"] for f in r["findings"]}
        assert "TRANSIENT_FLASH" not in codes

    def test_final_transient_flags_flash_frames(self, monkeypatch, tmp_path):
        _vlm_stub(monkeypatch)
        from shipin_platform.review import hard_gates
        flash = _make_flash_clip(tmp_path / "final.mp4",
                                 [(1.0, 1.4), (2.6, 3.0)])
        ctx = {"shots": [{"shot_id": "S01", "duration_sec": 4.0,
                          "subject": "产品", "scene": "演播室"}]}
        r = hard_gates.vlm_review_final(str(flash), frames_count=4, context=ctx)
        codes = {f["code"] for f in r["findings"]}
        assert "FINAL_TRANSIENT_SPIKES" in codes, r["findings"]
        assert r["deterministic"]["transient_spikes"]


class TestStoryMismatchGate:
    def test_shot_issue_is_critical(self, monkeypatch, tmp_path):
        _vlm_stub(monkeypatch, shot_issues=[
            {"shot": "S01", "issue": "分镜写办公室,画面是厨房"}])
        from shipin_platform.review import hard_gates
        clip = _make_motion_clip(tmp_path / "m.mp4", 4.0)
        ctx = {"shots": [{"shot_id": "S01", "duration_sec": 4.0,
                          "subject": "女主角", "scene": "办公室"}]}
        r = hard_gates.vlm_review_final(str(clip), frames_count=4, context=ctx)
        codes = {f["code"] for f in r["findings"]}
        assert "SHOT_STORY_MISMATCH" in codes, r["findings"]

    def test_clean_board_no_shot_issues(self, monkeypatch, tmp_path):
        _vlm_stub(monkeypatch)
        from shipin_platform.review import hard_gates
        clip = _make_motion_clip(tmp_path / "m2.mp4", 4.0)
        ctx = {"shots": [{"shot_id": "S01", "duration_sec": 4.0,
                          "subject": "女主角", "scene": "办公室"}]}
        r = hard_gates.vlm_review_final(str(clip), frames_count=4, context=ctx)
        assert "SHOT_STORY_MISMATCH" not in {f["code"] for f in r["findings"]}

    def test_prompt_carries_story_expectations(self):
        from shipin_platform.review.hard_gates import _batch_prompt
        ctx = {"shots": [{"shot_id": "S01", "duration_sec": 4.0,
                          "subject": "女主角", "scene": "办公室",
                          "motion": "推门走进来"}]}
        batch = [{"t": 1.0, "shot": "镜头S01", "shot_idx": 0}]
        p = _batch_prompt("1.0", 1, ctx, batch)
        assert "分镜预期" in p
        assert "场景[办公室]" in p
        assert "主体[女主角]" in p
        assert "shot_issues" in p


class TestIdentityGate:
    def test_identity_switch_caught(self, monkeypatch, tmp_path):
        _vlm_stub(monkeypatch, same_person=False)
        from shipin_platform.review import hard_gates
        clip = _make_motion_clip(tmp_path / "i.mp4", 4.0)
        ctx = {"shots": [
            {"shot_id": "S01", "duration_sec": 2.0, "subject": "女主角"},
            {"shot_id": "S02", "duration_sec": 2.0, "subject": "女主角"}]}
        r = hard_gates.vlm_review_final(str(clip), frames_count=4, context=ctx)
        codes = {f["code"] for f in r["findings"]}
        assert "IDENTITY_SWITCH" in codes, r["findings"]
        assert r["identity"]["checked"] == 1

    def test_distinct_subjects_skipped(self, monkeypatch, tmp_path):
        """『顾客』vs『店员』不同角色,不做身份判定,不误报换头。"""
        _vlm_stub(monkeypatch)
        from shipin_platform.review import hard_gates
        clip = _make_motion_clip(tmp_path / "i2.mp4", 4.0)
        ctx = {"shots": [
            {"shot_id": "S01", "duration_sec": 2.0, "subject": "顾客"},
            {"shot_id": "S02", "duration_sec": 2.0, "subject": "店员"}]}
        r = hard_gates.vlm_review_final(str(clip), frames_count=4, context=ctx)
        assert r["identity"]["checked"] == 0
        assert "IDENTITY_SWITCH" not in {f["code"] for f in r["findings"]}

    # ── 轮16:跨镜配对判据重写(_person_pair) ──────────────────────────
    # 旧规则只认主体词元精确重叠:「主角端起咖啡杯」vs「主角」的同人
    # 不同写被跳过(coffee-v7 实证 S05→S05b 换人镜界从未进过跨镜门);
    # 「男生」「程序员」等主体不在旧 _PERSON_HINTS 表内,整镜被静默跳过。

    def test_generic_subject_variants_now_paired(self, monkeypatch, tmp_path):
        """同人不同写(动作描述 vs 泛称)必须比——旧规则整个跳过。"""
        _vlm_stub(monkeypatch)
        from shipin_platform.review import hard_gates
        clip = _make_motion_clip(tmp_path / "p1.mp4", 4.0)
        ctx = {"shots": [
            {"shot_id": "S05", "duration_sec": 2.0, "subject": "主角端起咖啡杯"},
            {"shot_id": "S05b", "duration_sec": 2.0, "subject": "主角"}]}
        r = hard_gates.vlm_review_final(str(clip), frames_count=4, context=ctx)
        assert r["identity"]["checked"] == 1, r["identity"]

    def test_same_role_variants_paired(self, monkeypatch, tmp_path):
        """同一角色的不同动作描述同样要比(咖啡师注水 → 咖啡师递杯)。"""
        _vlm_stub(monkeypatch)
        from shipin_platform.review import hard_gates
        clip = _make_motion_clip(tmp_path / "p2.mp4", 4.0)
        ctx = {"shots": [
            {"shot_id": "S01", "duration_sec": 2.0, "subject": "咖啡师注水冲泡"},
            {"shot_id": "S02", "duration_sec": 2.0, "subject": "咖啡师递出咖啡杯"}]}
        r = hard_gates.vlm_review_final(str(clip), frames_count=4, context=ctx)
        assert r["identity"]["checked"] == 1, r["identity"]

    def test_distinct_roles_still_skipped(self, monkeypatch, tmp_path):
        """双方明确不同角色(咖啡师 vs 顾客)仍跳过——合理切换不误判。"""
        _vlm_stub(monkeypatch)
        from shipin_platform.review import hard_gates
        clip = _make_motion_clip(tmp_path / "p3.mp4", 4.0)
        ctx = {"shots": [
            {"shot_id": "S01", "duration_sec": 2.0, "subject": "咖啡师在吧台冲煮"},
            {"shot_id": "S02", "duration_sec": 2.0, "subject": "顾客坐在座位区"}]}
        r = hard_gates.vlm_review_final(str(clip), frames_count=4, context=ctx)
        assert r["identity"]["checked"] == 0, r["identity"]

    def test_synonym_roles_now_paired(self, monkeypatch, tmp_path):
        """轮24:『白领在办公』vs『上班族在地铁』经同义词归一后同角色,
        必须比——旧逻辑字面不同被误判"不同角色"整对跳过,身份从不审。"""
        _vlm_stub(monkeypatch)
        from shipin_platform.review import hard_gates
        clip = _make_motion_clip(tmp_path / "p6.mp4", 4.0)
        ctx = {"shots": [
            {"shot_id": "S01", "duration_sec": 2.0, "subject": "白领在办公室加班"},
            {"shot_id": "S02", "duration_sec": 2.0, "subject": "上班族走进地铁"}]}
        r = hard_gates.vlm_review_final(str(clip), frames_count=4, context=ctx)
        assert r["identity"]["checked"] == 1, r["identity"]

    def test_gendered_synonym_roles_now_paired(self, monkeypatch, tmp_path):
        """轮24:『女人』vs『女生』同样归一到同一规范角色后比。"""
        _vlm_stub(monkeypatch)
        from shipin_platform.review import hard_gates
        clip = _make_motion_clip(tmp_path / "p7.mp4", 4.0)
        ctx = {"shots": [
            {"shot_id": "S01", "duration_sec": 2.0, "subject": "女人站在街角"},
            {"shot_id": "S02", "duration_sec": 2.0, "subject": "女生推门进入"}]}
        r = hard_gates.vlm_review_final(str(clip), frames_count=4, context=ctx)
        assert r["identity"]["checked"] == 1, r["identity"]

    def test_expanded_person_hints_paired(self, monkeypatch, tmp_path):
        """轮16 补表:『男生』这类主体此前不在表内,整镜身份判定被跳过。"""
        _vlm_stub(monkeypatch)
        from shipin_platform.review import hard_gates
        clip = _make_motion_clip(tmp_path / "p4.mp4", 4.0)
        ctx = {"shots": [
            {"shot_id": "S01", "duration_sec": 2.0, "subject": "男生在深夜街头独行"},
            {"shot_id": "S02", "duration_sec": 2.0, "subject": "男生坐在咖啡店里"}]}
        r = hard_gates.vlm_review_final(str(clip), frames_count=4, context=ctx)
        assert r["identity"]["checked"] == 1, r["identity"]
        assert r["identity"]["intra_checked"] == 2

    def test_non_person_subject_still_skipped(self, monkeypatch, tmp_path):
        """商品镜(女包特写)不能因单字『女』被当人物镜——只用复合词。"""
        _vlm_stub(monkeypatch)
        from shipin_platform.review import hard_gates
        clip = _make_motion_clip(tmp_path / "p5.mp4", 4.0)
        ctx = {"shots": [
            {"shot_id": "S01", "duration_sec": 2.0, "subject": "红色女包特写"},
            {"shot_id": "S02", "duration_sec": 2.0, "subject": "男装陈列架"}]}
        r = hard_gates.vlm_review_final(str(clip), frames_count=4, context=ctx)
        assert r["identity"]["checked"] == 0
        assert r["identity"]["intra_checked"] == 0

    # ── 轮17:剧本钉外观时 COSTUME_SWAP 升 critical ───────────────────
    # 剧本写了服装/发型式样(anchor 或镜主体含服装词)时,跨镜换装就是
    # 违反剧本,不是风格选择;没钉外观的脚本保持 warning(导演自由)。

    def test_costume_swap_critical_when_look_pinned(self, monkeypatch,
                                                    tmp_path):
        """主体写了服装式样 → 纯服装不一致也 critical,message 带说明。"""
        _vlm_stub(monkeypatch, same_person=False,
                  same_person_spec="服装")
        from shipin_platform.review import hard_gates
        clip = _make_motion_clip(tmp_path / "pk.mp4", 4.0)
        ctx = {"shots": [
            {"shot_id": "S01", "duration_sec": 2.0,
             "subject": "女主角穿米白针织开衫"},
            {"shot_id": "S02", "duration_sec": 2.0,
             "subject": "女主角穿深色外套"}]}
        r = hard_gates.vlm_review_final(str(clip), frames_count=4, context=ctx)
        hits = [f for f in r["findings"] if f["code"] == "COSTUME_SWAP"]
        assert hits, r["findings"]
        assert hits[0]["severity"] == "critical", hits[0]
        assert "剧本已钉死" in hits[0]["message"]

    def test_costume_swap_warning_without_pin(self, monkeypatch, tmp_path):
        """脚本没钉外观(泛称主角) → 换装仍是 warning(风格自由)。"""
        _vlm_stub(monkeypatch, same_person=False,
                  same_person_spec="服装")
        from shipin_platform.review import hard_gates
        clip = _make_motion_clip(tmp_path / "np.mp4", 4.0)
        ctx = {"shots": [
            {"shot_id": "S01", "duration_sec": 2.0, "subject": "主角"},
            {"shot_id": "S02", "duration_sec": 2.0, "subject": "主角"}]}
        r = hard_gates.vlm_review_final(str(clip), frames_count=4, context=ctx)
        hits = [f for f in r["findings"] if f["code"] == "COSTUME_SWAP"]
        assert hits, r["findings"]
        assert hits[0]["severity"] == "warning", hits[0]

    def test_costume_swap_critical_via_actor_anchor(self, monkeypatch,
                                                    tmp_path):
        """外观写在 brief 的 actor_anchor(而非镜主体)时同样生效。"""
        _vlm_stub(monkeypatch, same_person=False,
                  same_person_spec="服装")
        from shipin_platform.review import hard_gates
        clip = _make_motion_clip(tmp_path / "anc.mp4", 4.0)
        ctx = {"actor_anchor": "主角为25岁女性,黑色长直发披肩、米白色针织开衫",
               "shots": [
                   {"shot_id": "S01", "duration_sec": 2.0, "subject": "主角"},
                   {"shot_id": "S02", "duration_sec": 2.0, "subject": "主角"}]}
        r = hard_gates.vlm_review_final(str(clip), frames_count=4, context=ctx)
        hits = [f for f in r["findings"] if f["code"] == "COSTUME_SWAP"]
        assert hits and hits[0]["severity"] == "critical", r["findings"]

    def test_pronoun_subject_paired(self, monkeypatch, tmp_path):
        """轮18:主体写『她坐在窗边』(无"主角"字样)也要比——代词单字
        『她』无商品词包含,可安全入表;『他』不行(『其他』误命中)。"""
        _vlm_stub(monkeypatch)
        from shipin_platform.review import hard_gates
        clip = _make_motion_clip(tmp_path / "pron.mp4", 4.0)
        ctx = {"shots": [
            {"shot_id": "S01", "duration_sec": 2.0, "subject": "她坐在窗边喝咖啡"},
            {"shot_id": "S02", "duration_sec": 2.0, "subject": "她站在街角"}]}
        r = hard_gates.vlm_review_final(str(clip), frames_count=4, context=ctx)
        assert r["identity"]["checked"] == 1, r["identity"]
        assert r["identity"]["intra_checked"] == 2

    def test_empty_spec_falls_back_to_critical(self, monkeypatch, tmp_path):
        """轮35c(五审 #4):VLM 判"不是同一人"但 spec 空(没给不一致点)时,
        旧逻辑 is_face=False → COSTUME_SWAP warning,而单镜诊断的 warning
        会被 assemble 的 critical 过滤器丢弃——"换人"在模型措辞不利时
        静默降级。现在 spec 空向 critical 兜底。"""
        _vlm_stub(monkeypatch, same_person=False, same_person_spec="")
        from shipin_platform.review import hard_gates
        clip = _make_motion_clip(tmp_path / "es.mp4", 4.0)
        ctx = {"shots": [
            {"shot_id": "S01", "duration_sec": 2.0, "subject": "女主角"},
            {"shot_id": "S02", "duration_sec": 2.0, "subject": "女主角"}]}
        r = hard_gates.vlm_review_final(str(clip), frames_count=4, context=ctx)
        hits = [f for f in r["findings"]
                if f["code"] in ("IDENTITY_SWITCH", "COSTUME_SWAP")]
        assert hits and hits[0]["severity"] == "critical", hits

    def test_ta_word_not_person(self, monkeypatch, tmp_path):
        """『其他装饰特写』不能因『他』被当人物镜——表里刻意没有『他』。"""
        _vlm_stub(monkeypatch)
        from shipin_platform.review import hard_gates
        clip = _make_motion_clip(tmp_path / "ta.mp4", 4.0)
        ctx = {"shots": [
            {"shot_id": "S01", "duration_sec": 2.0, "subject": "其他装饰特写"},
            {"shot_id": "S02", "duration_sec": 2.0, "subject": "其他陈设"}]}
        r = hard_gates.vlm_review_final(str(clip), frames_count=4, context=ctx)
        assert r["identity"]["checked"] == 0
        assert r["identity"]["intra_checked"] == 0

    # ── 轮11a:镜内人物一致性(首帧 vs 末帧) ─────────────────────────
    # coffee-v7 实测教训:S02 在 t=4.03s 镜内换装、S06 在 19.89→21.5s
    # 镜内换人——同一镜头内部的更换此前只能靠跨镜中帧间接撞见,且归属
    # 错位(报成 S06→S07 边界)。镜内通道把这类更换直接钉在该镜上。

    def test_intra_shot_switch_caught(self, monkeypatch, tmp_path):
        """单镜内首/末帧判定不是同一人 → 该镜被钉 IDENTITY_SWITCH。"""
        _vlm_stub(monkeypatch, same_person=False)
        from shipin_platform.review import hard_gates
        clip = _make_motion_clip(tmp_path / "intra.mp4", 4.0)
        ctx = {"shots": [
            {"shot_id": "S01", "duration_sec": 4.0, "subject": "女主角在咖啡店"}]}
        r = hard_gates.vlm_review_final(str(clip), frames_count=4, context=ctx)
        hits = [f for f in r["findings"] if f.get("scope") == "intra"]
        assert hits, r["findings"]
        assert hits[0]["code"] == "IDENTITY_SWITCH"
        assert "S01 内部" in hits[0]["message"]
        assert r["identity"]["intra_checked"] == 1
        assert r["identity"]["checked"] == 0  # 单镜无跨镜对

    def test_intra_shot_same_person_clean(self, monkeypatch, tmp_path):
        """镜内首/末帧是同一人 → 不报 finding,但仍记 intra_checked。"""
        _vlm_stub(monkeypatch)
        from shipin_platform.review import hard_gates
        clip = _make_motion_clip(tmp_path / "intra_ok.mp4", 4.0)
        ctx = {"shots": [
            {"shot_id": "S01", "duration_sec": 4.0, "subject": "主角走位"}]}
        r = hard_gates.vlm_review_final(str(clip), frames_count=4, context=ctx)
        assert r["identity"]["intra_checked"] == 1
        assert not [f for f in r["findings"] if f.get("scope") == "intra"]

    def test_intra_skipped_for_non_person_subject(self, monkeypatch, tmp_path):
        """手冲特写/logo 落版等无人物画面不做镜内判定(问也白问)。"""
        _vlm_stub(monkeypatch, same_person=False)
        from shipin_platform.review import hard_gates
        clip = _make_motion_clip(tmp_path / "intra_np.mp4", 4.0)
        ctx = {"shots": [
            {"shot_id": "S01", "duration_sec": 4.0, "subject": "手冲咖啡壶与滤杯"}]}
        r = hard_gates.vlm_review_final(str(clip), frames_count=4, context=ctx)
        assert r["identity"]["intra_checked"] == 0
        assert r["identity"]["checked"] == 0

    def test_malformed_same_person_payload_not_a_verdict(self, monkeypatch,
                                                         tmp_path):
        """轮11a 回归:身份提问拿到不含 same 字段的载荷(协议错配/被路由到
        别的提示词/JSON 截断)→ available=False 跳过,不得冒充「不是同一人」
        的 critical(那会让一次 VLM hiccup 直接禁止交付)。"""
        from shipin_platform.review import hard_gates
        monkeypatch.setattr(hard_gates, "_vlm_credentials", lambda: "fake-key")
        monkeypatch.setattr(
            hard_gates, "_ask_vlm",
            lambda *a, **k: '{"breaks": [], "brand_seen": true}')
        clip = _make_motion_clip(tmp_path / "mal.mp4", 4.0)
        ctx = {"shots": [
            {"shot_id": "S01", "duration_sec": 4.0, "subject": "主角独行"}]}
        r = hard_gates.vlm_review_final(str(clip), frames_count=4, context=ctx)
        assert r["identity"]["intra_checked"] == 1
        assert not [f for f in r["findings"] if f.get("scope") == "intra"]
        assert "IDENTITY_SWITCH" not in {f["code"] for f in r["findings"]}


class TestShotReview:
    """轮12a:单镜 VLM 符合度诊断 vlm_review_shot——每镜独立 ctx 逐帧
    对照分镜文本预期(终审是一个 prompt 扛全部分镜,镜头一多预期被稀释),
    外加确定性层与轮11 镜内身份通道;不含品牌门与跨镜判定。"""

    def test_shot_story_mismatch_flagged(self, monkeypatch, tmp_path):
        _vlm_stub(monkeypatch, shot_issues=[
            {"shot": "S02", "issue": "画面是厨房,分镜预期办公室"}])
        from shipin_platform.review import hard_gates
        clip = _make_motion_clip(tmp_path / "s.mp4", 4.0)
        shot = {"shot_id": "S02", "duration_sec": 4.0,
                "subject": "女主角", "scene": "办公室",
                "motion": "走进办公室"}
        r = hard_gates.vlm_review_shot(str(clip), shot, frames_count=4)
        assert r["verdict"] == "fix"
        hits = [f for f in r["findings"]
                if f["code"] == "SHOT_STORY_MISMATCH"]
        assert hits and "单镜诊断" in hits[0]["message"], r["findings"]
        assert r["shot_id"] == "S02"
        assert r["frames_reviewed"] == 4

    def test_clean_shot_passes(self, monkeypatch, tmp_path):
        _vlm_stub(monkeypatch)
        from shipin_platform.review import hard_gates
        clip = _make_motion_clip(tmp_path / "s_ok.mp4", 4.0)
        shot = {"shot_id": "S01", "duration_sec": 4.0,
                "subject": "主角", "scene": "街头", "motion": "独行"}
        r = hard_gates.vlm_review_shot(str(clip), shot, frames_count=4)
        assert r["verdict"] == "pass", r["findings"]
        assert r["identity"]["intra_checked"] == 1  # 人物镜走镜内身份通道

    def test_no_key_blocked(self, monkeypatch, tmp_path):
        from shipin_platform.review import hard_gates
        monkeypatch.setattr(hard_gates, "_vlm_credentials", lambda: "")
        clip = _make_motion_clip(tmp_path / "s_nk.mp4", 4.0)
        r = hard_gates.vlm_review_shot(str(clip), {"shot_id": "S01",
                                                   "duration_sec": 4.0})
        assert r["verdict"] == "blocked"

    def test_intra_identity_switch_in_shot_review(self, monkeypatch, tmp_path):
        """轮11 镜内通道在单镜诊断里同样生效(15%/85% 首末帧对比)。"""
        _vlm_stub(monkeypatch, same_person=False)
        from shipin_platform.review import hard_gates
        clip = _make_motion_clip(tmp_path / "s_id.mp4", 4.0)
        shot = {"shot_id": "S05b", "duration_sec": 4.0, "subject": "主角"}
        r = hard_gates.vlm_review_shot(str(clip), shot, frames_count=4)
        hits = [f for f in r["findings"] if f.get("scope") == "intra"]
        assert hits, r["findings"]
        assert "S05b 内部" in hits[0]["message"]
        assert r["verdict"] == "fix"


def _narr_clip(out: Path, dur: float = 6.0, sound_until: float = 1.0) -> Path:
    """视频轨 + 音频轨:前 sound_until 秒有正弦,其后静音(测旁白门用)。
    volume=enable 的反向启用:t>sound_until 时音量 0(禁用期是直通,
    不能写成 enable='lt(t,x)')。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [FFMPEG, "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"color=c=blue:s=320x240:r=24:duration={dur}",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={dur}",
         "-map", "0:v", "-map", "1:a",
         "-af", f"volume=enable='gt(t,{sound_until})':volume=0",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-shortest", str(out)],
        capture_output=True, text=True, check=True)
    return out


class TestNarrationPresence:
    """轮14:每镜旁白声轨存在性(确定性)——TTS 缺失/音频错位时画面照演
    但嘴上没词;「符不符合剧本」此前只核视频,这是音频侧第一道门。"""

    def test_silent_window_is_critical(self, tmp_path):
        from shipin_platform.review.hard_gates import check_narration_presence
        clip = _narr_clip(tmp_path / "n.mp4")  # 0~1s 有声,1~6s 静音
        shots = [
            {"shot_id": "S01", "narration": "深夜街头", "narr_at": 0.2,
             "duration_sec": 2.0},
            {"shot_id": "S02", "narration": "加班后的倦", "narr_at": 3.5,
             "duration_sec": 2.0}]
        r = check_narration_presence(str(clip), shots)
        codes = {f["code"] for f in r["findings"]}
        assert r["verdict"] == "fix"
        assert "NARRATION_MISSING" in codes
        hit = next(f for f in r["findings"]
                   if f["code"] == "NARRATION_MISSING")
        assert "S02" in hit["message"], hit
        assert r["stats"]["checked"] == 2  # S01 有声不计,S02 缺失计

    def test_sounding_window_passes(self, tmp_path):
        from shipin_platform.review.hard_gates import check_narration_presence
        clip = _narr_clip(tmp_path / "n_ok.mp4")
        shots = [{"shot_id": "S01", "narration": "有旁白", "narr_at": 0.2,
                  "duration_sec": 2.0}]
        r = check_narration_presence(str(clip), shots)
        assert r["verdict"] == "ok", r["findings"]

    def test_no_narration_shot_skipped(self, tmp_path):
        """纯画面镜(手冲特写/logo 落版)不要求有声——不查更不报。"""
        from shipin_platform.review.hard_gates import check_narration_presence
        clip = _narr_clip(tmp_path / "n_skip.mp4")
        shots = [{"shot_id": "S04", "narration": "", "narr_at": 4.0,
                  "duration_sec": 2.0}]
        r = check_narration_presence(str(clip), shots)
        assert r["verdict"] == "ok"
        assert r["stats"]["checked"] == 0

    def test_no_audio_track_is_critical(self, tmp_path):
        from shipin_platform.review.hard_gates import check_narration_presence
        clip = _make_motion_clip(tmp_path / "n_noaud.mp4", 4.0)  # 无音轨
        shots = [{"shot_id": "S01", "narration": "有词无轨", "narr_at": 0.2,
                  "duration_sec": 2.0}]
        r = check_narration_presence(str(clip), shots)
        assert r["verdict"] == "fix"
        assert r["findings"][0]["code"] == "NO_AUDIO_TRACK"

    def test_missing_narr_at_skipped(self, tmp_path):
        """缺 narr_at(旧项目数据)无法定位窗口——跳过,不误报。"""
        from shipin_platform.review.hard_gates import check_narration_presence
        clip = _narr_clip(tmp_path / "n_noat.mp4")
        shots = [{"shot_id": "S01", "narration": "有词无 at", "narr_at": None,
                  "duration_sec": 2.0}]
        r = check_narration_presence(str(clip), shots)
        assert r["verdict"] == "ok"
        assert r["stats"]["checked"] == 0

    # ── 轮20:纯台词镜(dialogue-only)也要过声轨门 ────────────────────
    # 剧本要求「至少 2 镜必须有 dialogue」——这些镜 narration 为空,
    # 旧逻辑整镜跳过,台词 TTS 失败没人管。

    def test_dialogue_only_shot_checked(self, tmp_path):
        """narration 空 + dialogue 非空 → 查,静音时 critical。"""
        from shipin_platform.review.hard_gates import check_narration_presence
        clip = _narr_clip(tmp_path / "dlg.mp4")  # 0~1s 有声,其后静音
        shots = [{"shot_id": "S03", "narration": "", "narr_at": 3.5,
                  "duration_sec": 2.0,
                  "dialogue": {"role": "女主", "text": "这杯咖啡真暖"}}]
        r = check_narration_presence(str(clip), shots)
        assert r["verdict"] == "fix"
        hit = next(f for f in r["findings"]
                   if f["code"] == "NARRATION_MISSING")
        assert "S03" in hit["message"] and "台词窗口" in hit["message"], hit
        assert r["stats"]["checked"] == 1

    def test_dialogue_only_string_form_checked(self, tmp_path):
        """dialogue 为纯字符串形态同样认(两种历史结构)。"""
        from shipin_platform.review.hard_gates import check_narration_presence
        clip = _narr_clip(tmp_path / "dlg2.mp4")
        shots = [{"shot_id": "S04", "narration": "", "narr_at": 0.2,
                  "duration_sec": 2.0, "dialogue": "欢迎光临"}]
        r = check_narration_presence(str(clip), shots)
        assert r["verdict"] == "ok", r["findings"]  # 0.2~2.2s 内有声
        assert r["stats"]["checked"] == 1

    def test_no_text_shot_still_skipped(self, tmp_path):
        """narration 与 dialogue 都空(纯画面镜)仍不查——不误报。"""
        from shipin_platform.review.hard_gates import check_narration_presence
        clip = _narr_clip(tmp_path / "dlg3.mp4")
        shots = [{"shot_id": "S04", "narration": "", "dialogue": "",
                  "narr_at": 4.0, "duration_sec": 2.0}]
        r = check_narration_presence(str(clip), shots)
        assert r["verdict"] == "ok"
        assert r["stats"]["checked"] == 0


class TestTempHygiene:
    """轮19(磁盘打满事故回归):审查调用结束后不得在 TEMP 遗留抽帧/
    身份判定临时目录。事故实证:真实审查一晚泄漏 2540 个目录/2.7G,
    TEMP 盘 100% 满后 E2E 直接失败。"""

    def test_no_temp_dir_leak_after_review(self, monkeypatch, tmp_path):
        import glob
        import tempfile as _tf
        from shipin_platform.review import hard_gates
        _vlm_stub(monkeypatch)
        root = Path(_tf.gettempdir())
        pats = ("vlm_gate_*", "vlm_identity_*", "vlm_shot_*", "clipqc_*")

        def _snap():
            return {p for pat in pats
                    for p in glob.glob(str(root / pat))}

        clip = _make_motion_clip(tmp_path / "leak.mp4", 4.0)
        shot = {"shot_id": "S01", "duration_sec": 4.0, "subject": "主角"}
        before = _snap()
        # 终审(批次抽帧 + 镜内身份)与单镜诊断(抽帧 + qc_clip + 镜内身份)
        hard_gates.vlm_review_final(str(clip), frames_count=4,
                                    context={"shots": [shot]})
        hard_gates.vlm_review_shot(str(clip), shot)
        after = _snap()
        leaked = sorted(after - before)
        assert not leaked, f"审查后泄漏临时目录: {leaked[:5]}"


class TestBlackFrameGate:
    """轮22:黑帧检测(blackdetect)——审查链此前的空洞:闪帧有
    transient_spikes、冻结有 motion_energy,「整段变黑」没有一门在看。"""

    @staticmethod
    def _black_clip(out: Path, black_at: float, black_dur: float,
                    total: float = 8.0) -> Path:
        """total 秒片段,black_at 起插入 black_dur 秒纯黑段,其余彩色。"""
        out.parent.mkdir(parents=True, exist_ok=True)
        a, b = black_at, black_at + black_dur
        tail = total - b
        subprocess.run(
            [FFMPEG, "-y", "-loglevel", "error",
             "-f", "lavfi", "-i", f"color=c=blue:s=320x240:r=24:duration={a}",
             "-f", "lavfi", "-i",
             f"color=c=black:s=320x240:r=24:duration={black_dur}",
             "-f", "lavfi", "-i",
             f"color=c=red:s=320x240:r=24:duration={tail}",
             "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
             "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p",
             str(out)],
            capture_output=True, text=True, check=True)
        return out

    def test_clip_interior_black_is_critical(self, tmp_path):
        from shipin_platform.review.clip_qc import qc_clip
        clip = self._black_clip(tmp_path / "bk.mp4", 3.6, 0.8)
        r = qc_clip(str(clip), shot_id="S02", expected_duration_sec=8.0)
        codes = {f["code"] for f in r["findings"]}
        assert "BLACK_FRAMES" in codes, r["findings"]
        hit = next(f for f in r["findings"] if f["code"] == "BLACK_FRAMES")
        assert hit["severity"] == "critical"
        assert "3.6" in hit["message"] or "整段黑屏" in hit["message"]

    def test_clip_edge_black_is_warning(self, tmp_path):
        """贴边黑段(≤0.25s,生成淡入残留)只警告,不拦生成。"""
        from shipin_platform.review.clip_qc import qc_clip
        clip = self._black_clip(tmp_path / "bk_edge.mp4", 0.0, 0.5)
        r = qc_clip(str(clip), shot_id="S01", expected_duration_sec=8.0)
        codes = {f["code"] for f in r["findings"]}
        assert "BLACK_FRAMES" not in codes
        assert "BLACK_FRAMES_EDGE" in codes, r["findings"]

    def test_clean_clip_no_black_finding(self, tmp_path):
        from shipin_platform.review.clip_qc import qc_clip
        clip = _make_motion_clip(tmp_path / "bk_ok.mp4", 4.0)
        r = qc_clip(str(clip), shot_id="S01", expected_duration_sec=4.0)
        assert not [f for f in r["findings"]
                    if f["code"].startswith("BLACK_FRAMES")]

    def test_final_black_away_from_boundary_is_critical(self, monkeypatch,
                                                        tmp_path):
        """终审层:黑段距镜头边界 >1.5s(排除叠化压黑过渡)→ critical。"""
        _vlm_stub(monkeypatch)
        from shipin_platform.review import hard_gates
        clip = self._black_clip(tmp_path / "bk_final.mp4", 3.6, 0.8)
        ctx = {"shots": [
            {"shot_id": "S01", "duration_sec": 2.0, "subject": "主角"},
            {"shot_id": "S02", "duration_sec": 4.0, "subject": "主角"},
            {"shot_id": "S03", "duration_sec": 2.0, "subject": "主角"}]}
        r = hard_gates.vlm_review_final(str(clip), frames_count=6,
                                        context=ctx)
        codes = {f["code"] for f in r["findings"]}
        assert "FINAL_BLACK_FRAMES" in codes, r["findings"]

    def test_final_black_near_boundary_exempted(self, monkeypatch, tmp_path):
        """黑段贴在镜头边界 ±1.5s 内(叠化压黑)→ 不拦。"""
        _vlm_stub(monkeypatch)
        from shipin_platform.review import hard_gates
        clip = self._black_clip(tmp_path / "bk_bnd.mp4", 1.8, 0.3,
                                total=6.0)
        ctx = {"shots": [
            {"shot_id": "S01", "duration_sec": 2.0, "subject": "主角"},
            {"shot_id": "S02", "duration_sec": 4.0, "subject": "主角"}]}
        r = hard_gates.vlm_review_final(str(clip), frames_count=6,
                                        context=ctx)
        assert "FINAL_BLACK_FRAMES" not in {f["code"]
                                            for f in r["findings"]}

    def test_deterministic_guard_isolates_single_failure(self, monkeypatch,
                                                         tmp_path):
        """轮37(五审补充项):确定性层四检查独立守卫——motion_energy 炸
        只废 audio_motion 一项,黑帧检查照跑。旧代码一个 try 包全部,
        单点探测失败(ffprobe 字段 "N/A" 等)= 整体放弃确定性防线。"""
        _vlm_stub(monkeypatch)
        from shipin_platform.review import clip_qc, hard_gates

        def _boom(_v):
            raise ValueError("probe field N/A")

        monkeypatch.setattr(clip_qc, "motion_energy", _boom)
        clip = self._black_clip(tmp_path / "guard.mp4", 3.6, 0.8)
        ctx = {"shots": [
            {"shot_id": "S01", "duration_sec": 2.0, "subject": "主角"},
            {"shot_id": "S02", "duration_sec": 4.0, "subject": "主角"},
            {"shot_id": "S03", "duration_sec": 2.0, "subject": "主角"}]}
        r = hard_gates.vlm_review_final(str(clip), frames_count=6,
                                        context=ctx)
        codes = {f["code"] for f in r["findings"]}
        assert "FINAL_BLACK_FRAMES" in codes, r["findings"]  # 黑帧检查幸存
        err = [f for f in r["findings"]
               if f["code"] == "DETERMINISTIC_PASS_ERROR"]
        assert err and "audio_motion" in err[0]["message"], err


class TestSubtitleAcceptance:
    """轮27:§10.6 字幕验收硬门——此前只存在于手工 /burn 端点,assemble
    烧完字幕直接放行(violations 恒 None),无墨迹/超宽的不可读字幕直达
    终审。found=false 与宽度超红线=critical;y 带=warning(验收规则与
    margin_v 默认排版的矛盾:实测底缘 ~h-116,按原规则每个项目都"违规")。"""

    def test_missing_ink_is_critical(self, tmp_path):
        from shipin_platform.tools.subtitle_renderer import (
            check_subtitle_cues)
        clip = _make_motion_clip(tmp_path / "s.mp4", 2.0)
        cues = [{"index": 1, "found": False, "width_pct": 20.0,
                 "y_range": [500, 560]}]
        v = check_subtitle_cues(cues, str(clip))
        assert v and v[0]["severity"] == "critical"
        assert any("墨迹" in i for i in v[0]["issues"])

    def test_over_width_is_critical(self, tmp_path):
        from shipin_platform.tools.subtitle_renderer import (
            check_subtitle_cues)
        clip = _make_motion_clip(tmp_path / "s2.mp4", 2.0)
        cues = [{"index": 2, "found": True, "width_pct": 71.5,
                 "y_range": [500, 560]}]
        v = check_subtitle_cues(cues, str(clip))
        assert v and v[0]["severity"] == "critical"
        assert any("宽度" in i for i in v[0]["issues"])

    def test_y_band_is_warning_only(self, tmp_path):
        """y 带问题只警告——原验收规则与 margin_v=96 默认排版互相矛盾
        (coffee-v7 实测全部 10 条 cue 底缘 ~604px < 720−110=610),硬拦
        会误伤每个正常项目。"""
        from shipin_platform.tools.subtitle_renderer import (
            check_subtitle_cues)
        clip = tmp_path / "s720.mp4"
        subprocess.run(
            [FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi",
             "-i", "testsrc2=duration=2:size=1280x720:r=24",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)],
            capture_output=True, text=True, check=True)
        cues = [{"index": 3, "found": True, "width_pct": 30.0,
                 "y_range": [549, 604]}]  # 720 高:604 < 610 触发 y 警告
        v = check_subtitle_cues(cues, str(clip))
        assert v and v[0]["severity"] == "warning"

    def test_clean_cues_pass(self, tmp_path):
        from shipin_platform.tools.subtitle_renderer import (
            check_subtitle_cues)
        clip = _make_motion_clip(tmp_path / "s4.mp4", 2.0)
        cues = [{"index": i, "found": True, "width_pct": 30.0,
                 "y_range": [549, 604]} for i in range(1, 4)]
        assert check_subtitle_cues(cues, str(clip)) == []


class TestKeyframeGate:
    """轮21:关键帧 vs 分镜文本门——视频模型以 first_frame 为条件生成,
    关键帧跑偏整镜必歪;qc_clip 的 dHash 只比 clip 首帧 vs 参考图(同源
    几乎必然一致),没人审过参考图本身。"""

    @staticmethod
    def _kf_stub(monkeypatch, *, match=True, payload=None):
        import json as _json
        from shipin_platform.review import hard_gates

        def ask(images, prompt, key, max_tokens=300):
            if payload is not None:
                return payload
            return _json.dumps({"match": match, "reason": "判定理由"},
                               ensure_ascii=False)

        monkeypatch.setattr(hard_gates, "_vlm_credentials", lambda: "fake-key")
        monkeypatch.setattr(hard_gates, "_ask_vlm", ask)

    @staticmethod
    def _png(p: Path) -> str:
        from PIL import Image
        Image.new("RGB", (96, 72), (120, 80, 40)).save(p)
        return str(p)

    def test_keyframe_mismatch_flagged(self, monkeypatch, tmp_path):
        self._kf_stub(monkeypatch, match=False)
        from shipin_platform.review.hard_gates import check_keyframes
        img = self._png(tmp_path / "kf.png")
        r = check_keyframes([{"shot_id": "S02", "first_frame": img,
                              "subject": "女主角在办公室",
                              "scene": "办公室"}])
        assert r["verdict"] == "fix"
        hit = r["findings"][0]
        assert hit["code"] == "KEYFRAME_MISMATCH"
        assert hit["shot_id"] == "S02"
        assert "关键帧与分镜文本不符" in hit["message"]

    def test_keyframe_match_passes(self, monkeypatch, tmp_path):
        self._kf_stub(monkeypatch, match=True)
        from shipin_platform.review.hard_gates import check_keyframes
        img = self._png(tmp_path / "kf_ok.png")
        r = check_keyframes([{"shot_id": "S02", "first_frame": img,
                              "subject": "女主角在办公室",
                              "scene": "办公室"}])
        assert r["verdict"] == "ok", r["findings"]
        assert r["stats"]["checked"] == 1

    def test_no_key_skips(self, monkeypatch, tmp_path):
        from shipin_platform.review import hard_gates
        monkeypatch.setattr(hard_gates, "_vlm_credentials", lambda: "")
        img = self._png(tmp_path / "kf_nk.png")
        r = hard_gates.check_keyframes([{"shot_id": "S01",
                                         "first_frame": img,
                                         "subject": "主角", "scene": "街头"}])
        assert r["verdict"] == "ok"
        assert r["stats"]["checked"] == 0

    def test_missing_frame_skipped(self, monkeypatch, tmp_path):
        self._kf_stub(monkeypatch, match=False)
        from shipin_platform.review.hard_gates import check_keyframes
        r = check_keyframes([{"shot_id": "S01",
                              "first_frame": str(tmp_path / "nope.png"),
                              "subject": "主角", "scene": "街头"}])
        assert r["verdict"] == "ok"
        assert r["stats"]["skipped"] == 1

    def test_unparseable_payload_not_a_verdict(self, monkeypatch, tmp_path):
        """载荷没有 match 字段(协议错配/路由错)→ available=False 跳过,
        不冒充『不符』的 critical(与轮11 _same_person 同一教训)。"""
        self._kf_stub(monkeypatch,
                      payload='{"frames": [], "brand_seen": true}')
        from shipin_platform.review.hard_gates import check_keyframes
        img = self._png(tmp_path / "kf_junk.png")
        r = check_keyframes([{"shot_id": "S01", "first_frame": img,
                              "subject": "主角", "scene": "街头"}])
        assert r["verdict"] == "ok", r["findings"]
        assert r["stats"]["checked"] == 0


class TestTimelineAccounting:
    def test_missing_shot_is_critical(self):
        from shipin_platform.review.hard_gates import check_timeline
        tl = [{"shot_id": "S01", "start": 0, "end": 2, "at": 0},
              {"shot_id": "S02", "start": 0, "end": 2, "at": 2}]
        r = check_timeline(tl, duration_sec=4.0,
                           expected_shot_ids=["S01", "S02", "S03"])
        codes = {f["code"] for f in r["findings"]}
        assert "SHOT_MISSING" in codes
        assert r["verdict"] == "fix"

    def test_injected_shot_is_critical(self):
        from shipin_platform.review.hard_gates import check_timeline
        tl = [{"shot_id": "S01", "start": 0, "end": 2, "at": 0},
              {"shot_id": "S02", "start": 0, "end": 2, "at": 2},
              {"shot_id": "S09", "start": 0, "end": 2, "at": 4}]
        r = check_timeline(tl, duration_sec=6.0,
                           expected_shot_ids=["S01", "S02"])
        codes = {f["code"] for f in r["findings"]}
        assert "SHOT_INJECTED" in codes

    def test_full_cover_ok(self):
        from shipin_platform.review.hard_gates import check_timeline
        tl = [{"shot_id": "S01", "src": "a.mp4", "start": 0, "end": 2, "at": 0},
              {"shot_id": "S02", "src": "b.mp4", "start": 0, "end": 2, "at": 2}]
        r = check_timeline(tl, duration_sec=4.0,
                           expected_shot_ids=["S01", "S02"])
        assert r["verdict"] == "ok", r["findings"]


class TestBoundaryBreakGate:
    """轮9a:VLM 打断的 kind 豁免——'boundary' 且 t 落在真实镜头边界 ±2s 内
    → 移出 findings(正当换镜);否则(模型乱标/远距)仍 critical。
    容差 2.0s:采样帧距边界 0.45~1.6s,VLM 报的 t 是采样秒不是剪辑瞬间。"""

    def test_boundary_kind_exempted_intra_kept(self, monkeypatch, tmp_path):
        _vlm_stub(monkeypatch, breaks=[
            {"t": 2.0, "desc": "S01→S02 正常换镜", "kind": "boundary"},
            {"t": 3.5, "desc": "S02 镜内闪白", "kind": "intra"}])
        from shipin_platform.review import hard_gates
        clip = _make_motion_clip(tmp_path / "b.mp4", 4.0)
        ctx = {"shots": [
            {"shot_id": "S01", "duration_sec": 2.0, "subject": "女主角"},
            {"shot_id": "S02", "duration_sec": 2.0, "subject": "女主角"}]}
        r = hard_gates.vlm_review_final(str(clip), frames_count=4, context=ctx)
        codes = {f["code"] for f in r["findings"] if f["code"] != "FINAL_NO_AUDIO"}
        assert codes == {"VLM_BREAK"}, r["findings"]
        msgs = [f["message"] for f in r["findings"]]
        assert any("3.5" in m for m in msgs)
        assert not any("2.0" in m for m in msgs)
        assert len(r["boundary_transitions"]) == 1

    def test_boundary_kind_far_from_real_boundary_stays_critical(
            self, monkeypatch, tmp_path):
        """模型乱标 kind='boundary' 但 t 远离任何真实边界 → 仍拦截。"""
        _vlm_stub(monkeypatch, breaks=[
            {"t": 8.5, "desc": "乱标的转场", "kind": "boundary"}])
        from shipin_platform.review import hard_gates
        clip = _make_motion_clip(tmp_path / "b2.mp4", 4.0)
        ctx = {"shots": [
            {"shot_id": "S01", "duration_sec": 2.0, "subject": "A"},
            {"shot_id": "S02", "duration_sec": 2.0, "subject": "A"},
            {"shot_id": "S03", "duration_sec": 2.0, "subject": "A"}]}
        r = hard_gates.vlm_review_final(str(clip), frames_count=4, context=ctx)
        codes = {f["code"] for f in r["findings"]}
        assert "VLM_BREAK" in codes, r["findings"]
        assert r["boundary_transitions"] == []

    def test_legacy_string_break_still_critical(self, monkeypatch, tmp_path):
        """旧式字符串断句(无 kind)保持原语义:全部 critical。"""
        _vlm_stub(monkeypatch, breaks=["S01 内同一主角突然换装"])
        from shipin_platform.review import hard_gates
        clip = _make_motion_clip(tmp_path / "b3.mp4", 4.0)
        ctx = {"shots": [
            {"shot_id": "S01", "duration_sec": 2.0, "subject": "A"},
            {"shot_id": "S02", "duration_sec": 2.0, "subject": "A"}]}
        r = hard_gates.vlm_review_final(str(clip), frames_count=4, context=ctx)
        codes = {f["code"] for f in r["findings"]}
        assert "VLM_BREAK" in codes
        assert r["boundary_transitions"] == []

    # ── 轮10a:kind 缺失时的语义兜底 ────────────────────────────────
    # agnes 对同一批断帧的 kind 标注跨会话不稳定,缺 kind 的合法换镜
    # 回落到 intra 被误拦。desc 含换镜语义词且无告警词 → 豁免;只要
    # 带告警词(错位/疑似/异常…)或 t 远离边界 → 仍 critical。

    def test_unlabeled_switch_words_in_margin_exempted(self, monkeypatch, tmp_path):
        """无 kind + desc 含换镜语义 + t 在边界 ±2s 内 → 正常换镜,不算异常。"""
        _vlm_stub(monkeypatch, breaks=[
            {"t": 1.4, "desc": "从街角场景直接切换到咖啡店门口，跨镜头正常交接"}])
        from shipin_platform.review import hard_gates
        clip = _make_motion_clip(tmp_path / "b4.mp4", 4.0)
        ctx = {"shots": [
            {"shot_id": "S01", "duration_sec": 2.0, "subject": "女主角"},
            {"shot_id": "S02", "duration_sec": 2.0, "subject": "女主角"}]}
        r = hard_gates.vlm_review_final(str(clip), frames_count=4, context=ctx)
        codes = {f["code"] for f in r["findings"] if f["code"] != "FINAL_NO_AUDIO"}
        assert "VLM_BREAK" not in codes, r["findings"]
        assert len(r["boundary_transitions"]) == 1
        assert r["boundary_transitions"][0]["t"] == 1.4

    def test_unlabeled_alarm_word_stays_critical(self, monkeypatch, tmp_path):
        """缺 kind 但 desc 含告警词(29.18 类真实缺陷)→ 即使有"切换"也拦截。"""
        _vlm_stub(monkeypatch, breaks=[
            {"t": 1.4, "desc": "从品牌标志切换为纯色背景带slogan，疑似画面内容错位"}])
        from shipin_platform.review import hard_gates
        clip = _make_motion_clip(tmp_path / "b5.mp4", 4.0)
        ctx = {"shots": [
            {"shot_id": "S01", "duration_sec": 2.0, "subject": "A"},
            {"shot_id": "S02", "duration_sec": 2.0, "subject": "A"}]}
        r = hard_gates.vlm_review_final(str(clip), frames_count=4, context=ctx)
        codes = {f["code"] for f in r["findings"]}
        assert "VLM_BREAK" in codes, r["findings"]
        assert r["boundary_transitions"] == []

    def test_unlabeled_switch_words_far_from_boundary_stays_critical(
            self, monkeypatch, tmp_path):
        """缺 kind + 换镜语义齐全但 t 远离任何真实边界 → 仍是镜头内突变。"""
        _vlm_stub(monkeypatch, breaks=[
            {"t": 4.9, "desc": "镜头切换到与分镜顺序不符的另一幕"}])
        from shipin_platform.review import hard_gates
        clip = _make_motion_clip(tmp_path / "b6.mp4", 4.0)
        ctx = {"shots": [
            {"shot_id": "S01", "duration_sec": 2.0, "subject": "A"},
            {"shot_id": "S02", "duration_sec": 2.0, "subject": "A"}]}
        r = hard_gates.vlm_review_final(str(clip), frames_count=4, context=ctx)
        codes = {f["code"] for f in r["findings"]}
        assert "VLM_BREAK" in codes, r["findings"]
        assert r["boundary_transitions"] == []

    # ── 轮35:margin 按镜长缩放 + 协议违约 fail-closed + spec 空兜底 ──

    def test_mid_shot_break_not_swallowed_by_short_shot_margin(
            self, monkeypatch, tmp_path):
        """轮35a(五审 #1 实锤):4s 短镜(bounds=[0,4])中段崩坏标
        kind=boundary 不再被固定 2.0s 豁免窗吞掉——旧行为任意 t 都
        |t-b|≤2 → 静默放行;新行为 eff_margin=1.6,中段 1.8s 不再豁免。"""
        _vlm_stub(monkeypatch, breaks=[
            {"t": 1.8, "desc": "画面出现条纹状崩坏", "kind": "boundary"}])
        from shipin_platform.review import hard_gates
        clip = _make_motion_clip(tmp_path / "b7.mp4", 4.0)
        ctx = {"shots": [
            {"shot_id": "S01", "duration_sec": 4.0, "subject": "A"},
            {"shot_id": "S02", "duration_sec": 4.0, "subject": "A"}]}
        r = hard_gates.vlm_review_final(str(clip), frames_count=4, context=ctx)
        codes = {f["code"] for f in r["findings"]}
        assert "VLM_BREAK" in codes, r["findings"]
        assert r["boundary_transitions"] == []

    def test_edge_sample_still_exempted_for_short_shot(
            self, monkeypatch, tmp_path):
        """轮35a 的另一面:2s 短镜的合法边界采样(t=1.4,含换镜词)仍
        必须豁免——1.6s 采样覆盖保底不能把正常换镜误报成 critical。"""
        _vlm_stub(monkeypatch, breaks=[
            {"t": 1.4, "desc": "从街角场景直接切换到咖啡店门口，跨镜头正常交接"}])
        from shipin_platform.review import hard_gates
        clip = _make_motion_clip(tmp_path / "b8.mp4", 4.0)
        ctx = {"shots": [
            {"shot_id": "S01", "duration_sec": 2.0, "subject": "A"},
            {"shot_id": "S02", "duration_sec": 2.0, "subject": "A"}]}
        r = hard_gates.vlm_review_final(str(clip), frames_count=4, context=ctx)
        codes = {f["code"] for f in r["findings"]}
        assert "VLM_BREAK" not in codes, r["findings"]
        assert len(r["boundary_transitions"]) == 1

    def test_vlm_unparseable_payload_is_protocol_violation(
            self, monkeypatch, tmp_path):
        """轮35b(五审 #2):VLM 返回不可解析为 JSON 时旧代码 parsed={}
        静默放行(该批零 findings)——分镜文本可经注入单方面关闭内容门。
        现在按「没审到」记 critical。"""
        from shipin_platform.review import hard_gates
        monkeypatch.setattr(hard_gates, "_vlm_credentials", lambda: "fake-key")
        monkeypatch.setattr(hard_gates, "_ask_vlm",
                            lambda *a, **k: "我无法处理该请求")
        clip = _make_motion_clip(tmp_path / "proto.mp4", 4.0)
        ctx = {"shots": [
            {"shot_id": "S01", "duration_sec": 4.0, "subject": "主角"}]}
        r = hard_gates.vlm_review_final(str(clip), frames_count=4, context=ctx)
        codes = {f["code"] for f in r["findings"]}
        assert "VLM_PROTOCOL_VIOLATION" in codes, r["findings"]
        assert r["verdict"] == "fix"


class TestKeySanity:
    """轮9a(实跑事故回归):AGNES 凭据必须通过形状校验,任何非密钥内容
    (模型返回文本、断帧串)不得进入 Authorization 头——曾发生 boundary
    豁免分支把外层 key 覆写成 f"{t}|{desc}",后续批次装上『Bearer
    11.09|画面从…』直接 latin-1 崩。"""

    def test_junk_key_blocked_before_wire(self, monkeypatch):
        from shipin_platform.review import hard_gates
        hard_gates.time.sleep = lambda _s: None  # 不真的等退避
        import requests as _req
        hit = []
        def _nope(*a, **k):
            hit.append(a)
            raise AssertionError("不应触网")
        monkeypatch.setattr(_req, "post", _nope)
        with pytest.raises(ValueError):
            hard_gates._ask_vlm([], "看画面", "11.09|画面从咖啡店门口推门切换为吧台特写")
        assert hit == []

    def test_credentials_reject_junk_shapes(self):
        import shipin_platform.review.hard_gates as hg
        assert not hg._key_ok("11.09|从咖啡店门口突然切换")
        assert not hg._key_ok("Bearer cpk-xxxx")
        assert not hg._key_ok("短")
        assert hg._key_ok("cpk-ldbV0mCIwcZILFBkm1Wbc7Y7UUOJHiyYTEb0fayCJadfk4K4")
