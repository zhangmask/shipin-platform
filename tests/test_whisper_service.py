"""WhisperService path-traversal guard tests (Mimosa L2: 路径穿越).

/api/subtitle/transcribe 的用户入参直接到达 transcribe(),输出文件基于
output_dir 拼接 —— .. 逃逸必须在校验层被拒,失败要快、且不触发模型加载。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

pytest.importorskip("whisper", reason="openai-whisper 是可选依赖")

from shipin_platform.tools.whisper_service import (  # noqa: E402
    WhisperService,
    _reject_traversal,
)


class TestRejectTraversal:
    def test_accepts_absolute_and_relative(self):
        # 现有接口契约允许绝对路径;``..`` 才是要拒的
        assert _reject_traversal(Path("/data/audio/a.mp3")) == \
            Path("/data/audio/a.mp3")
        assert _reject_traversal(Path("outputs/x")) == Path("outputs/x")

    def test_rejects_audio_traversal(self):
        with pytest.raises(ValueError, match="audio_path"):
            _reject_traversal(Path("../../etc/passwd.mp3"), name="audio_path")

    def test_rejects_output_dir_traversal(self):
        with pytest.raises(ValueError, match="output_dir"):
            _reject_traversal(Path("out/../../elsewhere"), name="output_dir")
        with pytest.raises(ValueError):
            _reject_traversal(Path(".."), name="output_dir")


class TestTranscribeValidatesBeforeWork:
    def test_traversal_rejected_before_model_load(self, tmp_path, monkeypatch):
        """穿越路径在 _get_model 之前就被拒:不加载模型、不写任何文件。"""
        ws = WhisperService()

        def boom(*a, **k):
            raise AssertionError("model must not be loaded for bad paths")

        monkeypatch.setattr(ws, "_get_model", boom)
        with pytest.raises(ValueError, match="audio_path"):
            ws.transcribe(Path("../evil.mp3"), output_dir=tmp_path)
        with pytest.raises(ValueError, match="output_dir"):
            ws.transcribe(tmp_path / "ok.mp3",
                          output_dir=Path(tmp_path) / ".." / "outside")

    def test_valid_call_writes_only_inside_output_dir(self, tmp_path, monkeypatch):
        audio = tmp_path / "voice.mp3"
        audio.write_bytes(b"not really audio")
        # 轮73:显式 openai 后端——本测试验路径围栏,不碰 sidecar
        ws = WhisperService(backend="openai")

        class FakeModel:
            def transcribe(self, *a, **k):
                return {"segments": [
                    {"start": 0.0, "end": 1.2, "text": "你好 世界",
                     "words": [{"word": "你好", "start": 0.0, "end": 0.6},
                               {"word": "世界", "start": 0.6, "end": 1.2}]}]}

        monkeypatch.setattr(ws, "_get_model", lambda: FakeModel())
        out_dir = tmp_path / "subs"
        r = ws.transcribe(audio, output_dir=out_dir)
        assert r["ok"] and r["words"] == 2
        for key in ("srt_path", "json_path", "word_anchors_path"):
            p = Path(r[key])
            assert out_dir in p.parents, f"{key} 逃出 output_dir: {p}"
        assert out_dir / "voice.word_anchors.json" in map(Path, [
            r["srt_path"], r["json_path"], r["word_anchors_path"]])