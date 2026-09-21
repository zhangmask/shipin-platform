"""注入式故障评测套件（来源：GitHub 调研规则10 — agentic-vbench + video-editor-agent
clean canary）。

对 vlm_review_final 的新确定性扇区做「故障注入 = 必命中、健康样本 = 不误报」回归：
- 冻结（全画面近乎静止）→ FINAL_FROZEN
- 无音轨 → FINAL_NO_AUDIO
- 正常运动 + 有声 → 两者都不命中
- 无分镜上下文 → 不崩、不虚构

全部用 ffmpeg 合成视频（lavfi），不触真实 API key：
monkeypatch _vlm_credentials 与 _ask_vlm，把 VLM 层降级为「无意见」。
"""
import subprocess
from pathlib import Path

from shipin_platform.review import hard_gates

_FAKE_VLM = '{"breaks": [], "brand_seen": true, "frames": [], "anomaly": 0}'


def _blank_vlm(monkeypatch):
    monkeypatch.setattr(hard_gates, "_vlm_credentials", lambda: "test-key")
    monkeypatch.setattr(hard_gates, "_ask_vlm", lambda *a, **k: _FAKE_VLM)


def _clip(tmp_path) -> str:
    return str(tmp_path / "clip.mp4")


def test_injected_frozen_is_caught(tmp_path, monkeypatch):
    """冻结故障注入：全灰静止画面必须命中 FINAL_FROZEN。"""
    _blank_vlm(monkeypatch)
    r = subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi",
        "-i", "color=c=gray:size=320x240:rate=25",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=4", "-t", "4",
        "-map", "0:v", "-map", "1:a", "-c:v", "libx264",
        "-pix_fmt", "yuv420p", "clip.mp4",
    ], cwd=str(tmp_path), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[:300]
    rev = hard_gates.vlm_review_final(_clip(tmp_path), frames_count=4,
                                      context=None)
    codes = [f["code"] for f in rev["findings"]]
    assert "FINAL_FROZEN" in codes, f"冻结未命中: {codes}"


def test_injected_mute_fault_is_caught(tmp_path, monkeypatch):
    """静音故障注入：可动但无音轨必须命中 FINAL_NO_AUDIO。"""
    _blank_vlm(monkeypatch)
    r = subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi",
        "-i", "testsrc2=size=320x240:rate=24", "-t", "4", "-an",
        "-map", "0:v", "-c:v", "libx264", "-pix_fmt", "yuv420p", "clip.mp4",
    ], cwd=str(tmp_path), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[:300]
    rev = hard_gates.vlm_review_final(_clip(tmp_path), frames_count=4,
                                      context=None)
    codes = [f["code"] for f in rev["findings"]]
    assert "FINAL_NO_AUDIO" in codes, f"静音未命中: {codes}"


def test_healthy_video_not_falsely_alarmed(tmp_path, monkeypatch):
    """正常注入：运动+音轨都不该命中冻结/静音告警。"""
    _blank_vlm(monkeypatch)
    r = subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi",
        "-i", "testsrc2=size=320x240:rate=24",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=4", "-t", "4",
        "-map", "0:v", "-map", "1:a", "-c:v", "libx264",
        "-pix_fmt", "yuv420p", "clip.mp4",
    ], cwd=str(tmp_path), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[:300]
    rev = hard_gates.vlm_review_final(_clip(tmp_path), frames_count=4,
                                      context=None)
    codes = [f["code"] for f in rev["findings"]]
    assert "FINAL_FROZEN" not in codes
    assert "FINAL_NO_AUDIO" not in codes


def test_contextless_review_smoke(tmp_path, monkeypatch):
    """无镜头上下文的直调路径:不崩、有帧、确定性结构字段齐全。"""
    _blank_vlm(monkeypatch)
    r = subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi",
        "-i", "testsrc2=size=320x240:rate=24",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=4", "-t", "3",
        "-map", "0:v", "-map", "1:a", "-c:v", "libx264",
        "-pix_fmt", "yuv420p", "clip.mp4",
    ], cwd=str(tmp_path), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[:300]
    rev = hard_gates.vlm_review_final(_clip(tmp_path), frames_count=8,
                                      context=None)
    assert rev["verdict"] in ("pass", "fix")
    assert rev["frames_reviewed"] > 0
    assert rev.get("deterministic") is not None


def test_morph_check_passes_healthy(tmp_path, monkeypatch):
    """形态突变检查:健康动态素材 VLM 返回 intact 时不得误报。"""
    monkeypatch.setattr(hard_gates, "_vlm_credentials", lambda: "test-key")
    monkeypatch.setattr(
        hard_gates, "_ask_vlm",
        lambda *a, **k: '{"morph": false, "reason": "同一主体持续呈现"}')
    from shipin_platform.review.clip_qc import vlm_morph_check
    r = subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi",
        "-i", "testsrc2=size=320x240:rate=24",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=4", "-t", "3",
        "-map", "0:v", "-map", "1:a", "-c:v", "libx264",
        "-pix_fmt", "yuv420p", "clip.mp4",
    ], cwd=str(tmp_path), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[:300]
    res = vlm_morph_check(_clip(tmp_path))
    assert res["available"] is True
    assert res["verdict"] == "intact"


def test_morph_check_catches_fault(tmp_path, monkeypatch):
    """形态故障注入:VLM 报 morph=true 时必须命中。"""
    monkeypatch.setattr(hard_gates, "_vlm_credentials", lambda: "test-key")
    monkeypatch.setattr(
        hard_gates, "_ask_vlm",
        lambda *a, **k: '{"morph": true, "reason": "粉末瞬间变整颗豆"}')
    from shipin_platform.review.clip_qc import vlm_morph_check
    r = subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi",
        "-i", "testsrc2=size=320x240:rate=24",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=4", "-t", "3",
        "-map", "0:v", "-map", "1:a", "-c:v", "libx264",
        "-pix_fmt", "yuv420p", "clip.mp4",
    ], cwd=str(tmp_path), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[:300]
    res = vlm_morph_check(_clip(tmp_path))
    assert res["available"] is True
    assert res["verdict"] == "morph"


def test_qc_gate_blocks_morph_clip(tmp_path, monkeypatch):
    """整门测试:use_vlm 时 MORPH_DETECTED 出现 → verdict=fix。"""
    monkeypatch.setattr(hard_gates, "_vlm_credentials", lambda: "test-key")
    monkeypatch.setattr(
        hard_gates, "_ask_vlm",
        lambda *a, **k: '{"morph": true, "reason": "主体变形"}')
    from shipin_platform.review.clip_qc import qc_clip
    r = subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi",
        "-i", "testsrc2=size=320x240:rate=24",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=4", "-t", "3",
        "-map", "0:v", "-map", "1:a", "-c:v", "libx264",
        "-pix_fmt", "yuv420p", "clip.mp4",
    ], cwd=str(tmp_path), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[:300]
    res = qc_clip(_clip(tmp_path), shot_id="S01", use_vlm=True)
    assert res["verdict"] == "fix"
    assert any(f["code"] == "MORPH_DETECTED" for f in res["findings"])
    assert "morphing" in res["next_action"]


def _make_clip(tmp_path) -> str:
    """3s 有声合成视频，供终验门函数直接使用。"""
    clip = _clip(tmp_path)
    r = subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi",
        "-i", "testsrc2=size=320x240:rate=24",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=4", "-t", "3",
        "-map", "0:v", "-map", "1:a", "-c:v", "libx264",
        "-pix_fmt", "yuv420p", clip,
    ], cwd=str(tmp_path), capture_output=True, text=True)
    assert r.returncode == 0, (r.stderr or "")[:300]
    return clip


def _brand_ctx():
    """单镜 ctx：声明品牌名（触发 M8 品牌门）。"""
    return {"brand_name": "晨光咖啡", "slogan": "享受每一刻",
            "duration_sec": 3.0,
            "shots": [{"shot_id": "S01", "duration_sec": 3.0,
                       "subject": "深夜街头独行的主角"}]}


def test_brand_missing_gate_fires(tmp_path, monkeypatch):
    """brief 有品牌名 + VLM 全程 brand_seen=false → BRAND_MISSING 硬门拦截。"""
    monkeypatch.setattr(hard_gates, "_vlm_credentials", lambda: "test-key")
    monkeypatch.setattr(
        hard_gates, "_ask_vlm",
        lambda *a, **k: '{"breaks": [], "brand_seen": false, "frames": []}')
    clip = _make_clip(tmp_path)
    rev = hard_gates.vlm_review_final(clip, frames_count=8, context=_brand_ctx())
    assert rev["verdict"] == "fix"
    assert any(f["code"] == "BRAND_MISSING" for f in rev["findings"])


def test_brand_present_gate_passes(tmp_path, monkeypatch):
    """同一视频但 VLM 报 brand_seen=true → 无 BRAND_MISSING，正常放行。"""
    _blank_vlm(monkeypatch)  # 默认 _FAKE_VLM 带 brand_seen=true
    clip = _make_clip(tmp_path)
    rev = hard_gates.vlm_review_final(clip, frames_count=8, context=_brand_ctx())
    assert not any(f["code"] == "BRAND_MISSING" for f in rev["findings"])
    assert rev["verdict"] == "pass"


def test_brand_gate_not_applied_without_brand(tmp_path, monkeypatch):
    """ctx 无品牌名 → 即使 brand_seen=false 也不产生 BRAND_MISSING。"""
    monkeypatch.setattr(hard_gates, "_vlm_credentials", lambda: "test-key")
    monkeypatch.setattr(
        hard_gates, "_ask_vlm",
        lambda *a, **k: '{"breaks": [], "brand_seen": false, "frames": []}')
    clip = _make_clip(tmp_path)
    ctx = {"duration_sec": 3.0,
           "shots": [{"shot_id": "S01", "duration_sec": 3.0,
                      "subject": "无品牌的信息短片"}]}
    rev = hard_gates.vlm_review_final(clip, frames_count=8, context=ctx)
    assert not any(f["code"] == "BRAND_MISSING" for f in rev["findings"])