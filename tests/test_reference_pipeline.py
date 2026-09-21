"""reference_pipeline 测试：切镜→抽帧→ASR→VLM 反推的编排正确性。

用 `mock` 隔离所有外部依赖（ffmpeg 抽帧、whisper、VLM），验证：
- 产物 JSON 结构（script.json / shots.json / brief.json）齐全
- 台词按时间轴对齐到镜头
- VLM 未配置时降级（不阻断，镜像字段留空 + vlm_error）
- VLM 配置时逐镜反推并被写入 shots
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from shipin_platform.analysis import reference_pipeline as rp


FAKE_SHOTS = [
    {"start": 0.0, "end": 4.0},     # 4s → 抽 2 帧（中/尾）
    {"start": 4.0, "end": 14.0},    # 10s → 抽 3 帧
    {"start": 14.0, "end": 16.0},   # 2s → 抽 1 帧
]

FAKE_META = {"duration_sec": 16.0, "title": "sample", "has_audio": True,
             "width": 1280, "height": 720, "avg_frame_rate": "24/1"}

FAKE_PACING = {"label": "quick", "bpm": 3.0}
FAKE_BLACK = {"count": 0, "ratio": 0.0}
FAKE_NARR = {"speech_ratio": 0.5}


def _fake_profiler(monkeypatch, out_dir: Path):
    """把 profile_reference 桩成确定性返回（不走 ffprobe/ffmpeg）。"""
    def fake_profile(video_path, *, save_dir=None, scene_threshold=0.3,
                     max_shots=60):
        report = {
            "profile": "shipin.reference@1",
            "name": "sample",
            "source_path": str(video_path),
            "metadata": FAKE_META,
            "shots": FAKE_SHOTS,
            "pacing": FAKE_PACING,
            "black": FAKE_BLACK,
            "narration": FAKE_NARR,
            "brief_prefill": {
                "style": {"value": "参考: 咖啡暖调", "state": "filled"},
                "platform": {"value": "竖屏", "state": "filled"},
            },
        }
        result = {"ok": True, "report": report,
                  "brief_prefill": report["brief_prefill"]}
        if save_dir is not None:
            Path(save_dir).mkdir(parents=True, exist_ok=True)
            (Path(save_dir) / "sample.json").write_text(
                json.dumps(report, ensure_ascii=False), encoding="utf-8")
            result["report_path"] = str(save_dir / "sample.json")
        return result
    monkeypatch.setattr(rp.profiler, "profile_reference", fake_profile)


def _fake_ffmpeg(monkeypatch, out_dir: Path):
    """桩 ffmpeg 抽帧：每次调用写一个 jpg 文件。"""
    import re as _re

    def fake_run(argv, **kw):
        out = None
        for i, a in enumerate(argv):
            if i > 0 and argv[i - 1] == "-y":
                out = a
        if out is None:
            matches = [a for a in argv if a.endswith(".jpg")]
            out = matches[0] if matches else None
        if out:
            p = Path(out)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"\xff\xd8jpeg")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(rp.subprocess, "run", fake_run)


def _fake_asr(monkeypatch, srt_text: str):
    """桩 WhisperService：直接写一份 SRT，再走真实 _load_srt_lines 解析。

    返回值与真实 _transcribe 一致（台词行列表），避免对齐逻辑拿到 dict。
    """
    def fake_transcribe(video, output_dir, **kw):
        d = Path(output_dir)
        d.mkdir(parents=True, exist_ok=True)
        p = d / "sample.srt"
        p.write_text(srt_text, encoding="utf-8")
        return rp._load_srt_lines(p)
    monkeypatch.setattr(rp, "_transcribe", fake_transcribe)


_FAKE_VLM_RESULT = {
    "scene": "咖啡店吧台", "subject": "咖啡师", "camera": "中景",
    "lighting": "暖光", "tone_and_palette": "暖棕",
    "composition": "居中",
    "image_prompt": "a barista pouring coffee, warm light",
    "video_prompt": "slow push-in, steam rising",
}


def _fake_vlm(monkeypatch, fail=False):
    calls = []
    if fail:
        def f(*a, **k):
            calls.append(a)
            from shipin_platform.services.vision_llm import VisionError
            raise VisionError("provider down")
        monkeypatch.setattr(rp.vision_llm, "analyze_frame", f)
    else:
        def f(frame, *, context="", cfg=None):
            calls.append(frame)
            return dict(_FAKE_VLM_RESULT)
        monkeypatch.setattr(rp.vision_llm, "analyze_frame", f)
    return calls


FAKE_SRT = """1
00:00:00,500 --> 00:00:03,200
第一句台词

2
00:00:05,000 --> 00:00:09,000
第二句台词在这镜
"""
_SRT_TEXT = FAKE_SRT


def _make_video(tmp_path: Path) -> Path:
    v = tmp_path / "fake.mp4"
    v.write_bytes(b"not-a-real-video")   # profiler 已桩,不真正 ffprobe
    return v


class TestPipelineAssets:
    def test_all_outputs_written(self, tmp_path, monkeypatch):
        video = _make_video(tmp_path)
        _fake_profiler(monkeypatch, tmp_path / "out")
        _fake_ffmpeg(monkeypatch, tmp_path / "out")
        _fake_asr(monkeypatch, _SRT_TEXT)
        monkeypatch.setattr(rp.vision_llm, "resolve_vision",
                            lambda: {"base_url": "https://api.agnes-ai.com/v1",
                                     "model": "vlm-x", "key": "k",
                                     "source": "agnes"})
        vlm_calls = _fake_vlm(monkeypatch)

        res = rp.run_reference_pipeline(video,
                                        tmp_path / "out",
                                        skip_asr=False)
        assert res["ok"]
        assert res["shot_count"] == 3
        assert res["vlm_used"] is True

        out = tmp_path / "out"
        assert (out / "shots.json").is_file()
        assert (out / "script.json").is_file()
        assert (out / "brief.json").is_file()

        shots = json.loads((out / "shots.json").read_text(encoding="utf-8"))
        assert len(shots) == 3
        # VLM 结果进入 shots
        assert shots[0]["camera"] == "中景"
        assert shots[0]["image_prompt"].startswith("a barista")
        # 台词对齐：第一句在镜头1（0-4s），第二句在镜头2(4-14s)
        assert "第一句台词" in shots[0]["dialogue"]
        assert "第二句台词" in shots[1]["dialogue"]
        # 帧抽取：4s 镜 2 帧、10s 镜 3 帧、2s 镜 1 帧
        assert len(shots[0]["frames"]) == 2
        assert len(shots[1]["frames"]) == 3
        assert len(shots[2]["frames"]) == 1

        script = json.loads((out / "script.json").read_text(encoding="utf-8"))
        assert script["shot_count"] == 3
        assert script["lines"][1]["dialogue"] == "第二句台词在这镜"
        assert script["lines"][0]["image_prompt"]

        brief = json.loads((out / "brief.json").read_text(encoding="utf-8"))
        assert brief["brief_prefill"]["style"] == "参考: 咖啡暖调"
        assert brief["vlm_used"] is True

    def test_vlm_disable_fallback(self, tmp_path, monkeypatch):
        """无多模态配置时流水线仍完成,提示词留空 + vlm_error 说明。"""
        video = _make_video(tmp_path)
        _fake_profiler(monkeypatch, tmp_path / "out")
        _fake_ffmpeg(monkeypatch, tmp_path / "out")
        _fake_asr(monkeypatch, "")
        from shipin_platform.services.vision_llm import VisionError
        monkeypatch.setattr(rp.vision_llm, "resolve_vision",
                            lambda: (_ for _ in ()).throw(
                                VisionError("没有可用的多模态大模型")))
        res = rp.run_reference_pipeline(video,
                                        tmp_path / "out", skip_asr=True)
        assert res["ok"] and res["shot_count"] == 3
        assert res["vlm_used"] is False
        assert "多模态" in res["vlm_error"]
        shots = json.loads((tmp_path / "out" / "shots.json")
                           .read_text(encoding="utf-8"))
        assert shots[0]["image_prompt"] == ""

    def test_vlm_error_per_shot(self, tmp_path, monkeypatch):
        """单镜 VLM 抛错：记 _error 不整条失败。"""
        video = _make_video(tmp_path)
        _fake_profiler(monkeypatch, tmp_path / "out")
        _fake_ffmpeg(monkeypatch, tmp_path / "out")
        _fake_asr(monkeypatch, "")
        monkeypatch.setattr(rp.vision_llm, "resolve_vision",
                            lambda: {"base_url": "https://x/v1",
                                     "model": "m", "key": "k",
                                     "source": "agnes"})
        from shipin_platform.services.vision_llm import VisionError
        def boom(*a, **k):
            raise VisionError("provider timeout")
        monkeypatch.setattr(rp.vision_llm, "analyze_frame", boom)

        res = rp.run_reference_pipeline(
            video, tmp_path / "out", skip_asr=True)
        assert res["ok"] and res["shot_count"] == 3
        shots = json.loads((tmp_path / "out" / "shots.json")
                           .read_text(encoding="utf-8"))
        assert any(s.get("_error") for s in shots)

    def test_skip_asr(self, tmp_path, monkeypatch):
        """skip_asr=True 完全不碰 ASR（显式关闭 VLM，避免依赖本机密钥环境）。"""
        video = _make_video(tmp_path)
        _fake_profiler(monkeypatch, tmp_path / "out")
        _fake_ffmpeg(monkeypatch, tmp_path / "out")
        from shipin_platform.services.vision_llm import VisionError
        monkeypatch.setattr(rp.vision_llm, "resolve_vision",
                            lambda: (_ for _ in ()).throw(
                                VisionError("测试环境无视觉模型")))
        calls = []

        def spy(*a, **k):
            calls.append(a)
            return None
        monkeypatch.setattr(rp, "_transcribe", spy)
        rp.run_reference_pipeline(video,
                                  tmp_path / "out", skip_asr=True)
        assert calls == []   # skip_asr=True 完全不碰 ASR