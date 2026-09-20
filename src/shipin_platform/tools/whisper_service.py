"""Whisper ASR service for subtitle generation.

对齐 hypit whisperx 包的词级锚点设计：transcribe 产出词级时间戳
(word_anchors)，供卡拉OK字幕、语义锚定与后续变体工作使用。
"""
from __future__ import annotations

import json as _json
import os
import whisper
from pathlib import Path
from typing import Optional


def _reject_traversal(p: Path, *, name: str = "path") -> Path:
    """Reject path traversal: any ``..`` component is an error.

    /api/subtitle/* 的用户入参直接到达本服务,输出文件基于 output_dir
    拼接——必须堵住 ``../../`` 逃逸,否则可写到期望目录之外。
    绝对路径仍允许(现有接口契约),只要不含 ``..`` 逃逸。
    """
    p = Path(p)
    if any(part == ".." for part in p.parts):
        raise ValueError(f"{name} 含路径穿越 '..',已拒绝: {p!s}")
    return p


def _out_paths(root: Path, audio_path: Path) -> tuple[Path, Path, Path]:
    """构建三个输出文件路径：文件名只取输入文件的纯文件名(stem)，
    剥离目录成分与危险字符，并做 root 目录围栏校验。"""
    base = Path(audio_path).name  # 仅文件名，不含任何目录成分
    stem = base.rsplit(".", 1)[0] if "." in base else base
    stem = stem.replace("\\", "").replace("/", "").strip(".")
    if not stem:
        stem = "output"
    outs = tuple(root / (stem + s)
                 for s in (".srt", ".json", ".word_anchors.json"))
    for p in outs:
        if not p.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"输出路径越界被拒绝: {p}")
    return outs


class WhisperService:
    """Wraps OpenAI Whisper for subtitle generation."""

    def __init__(self, model: str = "medium", device: str = "cpu", language: str = "zh"):
        self.model_name = model
        self.device = device
        self.language = language
        self._model = None

    def _get_model(self) -> whisper.Whisper:
        if self._model is None:
            self._model = whisper.load_model(self.model_name, device=self.device)
        return self._model

    def transcribe(
        self,
        audio_path: Path,
        output_dir: Optional[Path] = None,
        output_format: str = "srt",
        word_timestamps: bool = True,
    ) -> dict:
        """Transcribe audio and generate subtitles.

        Returns dict with paths to output files (srt/json/word_anchors).
        word_anchors.json 是词级锚点文件（对齐 hypit whisperx 的 word-level
        evidence）：[{word, start, end}]，供卡拉OK字幕、语义锚定使用。
        """
        audio_path = _reject_traversal(audio_path, name="audio_path")
        output_dir = _reject_traversal(output_dir or audio_path.parent,
                                       name="output_dir")
        model = self._get_model()
        args = {
            "word_timestamps": word_timestamps,
            "verbose": True,
        }
        if self.language:
            args["language"] = self.language

        result = model.transcribe(str(audio_path), **args)

        output_dir = output_dir or audio_path.parent
        root = output_dir.resolve()
        root.mkdir(parents=True, exist_ok=True)

        # 输出文件名只取输入文件纯 stem(剥离目录成分与危险字符)，
        # 一律拼接在 root 之下,并对 root 做 realpath 前缀围栏校验。
        _stem = Path(audio_path).name.rsplit(".", 1)[0]
        _stem = _stem.replace("\\", "").replace("/", "").strip(".") or "output"
        srt_path = root / (_stem + ".srt")
        json_path = root / (_stem + ".json")
        anchors_path = root / (_stem + ".word_anchors.json")
        _root_real = os.path.realpath(os.fspath(root)) + os.sep
        for _out in (srt_path, json_path, anchors_path):
            assert os.path.realpath(os.fspath(_out)).startswith(_root_real), \
                f"输出路径越界被拒绝: {_out}"

        # Write SRT (write_text 落盘)
        srt_path.write_text(
            "".join(f"{i}\n{self._format_ts(seg['start'])} --> "
                    f"{self._format_ts(seg['end'])}\n{seg['text'].strip()}\n\n"
                    for i, seg in enumerate(result["segments"], 1)),
            encoding="utf-8")

        # Write JSON (raw whisper result, word-level timestamps included)
        json_path.write_text(_json.dumps(result, ensure_ascii=False, indent=2),
                             encoding="utf-8")

        # Write word anchors (flat, segment-linked)
        anchors = extract_word_anchors(result)
        anchors_path.write_text(_json.dumps({
            "source": str(audio_path),
            "language": self.language,
            "word_count": len(anchors),
            "anchors": anchors,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        return {
            "ok": True,
            "srt_path": str(srt_path),
            "json_path": str(json_path),
            "word_anchors_path": str(anchors_path),
            "segments": len(result["segments"]),
            "words": len(anchors),
            "duration_sec": result.get("segments", [])[-1]["end"] if result.get("segments") else 0,
        }

    @staticmethod
    def _format_ts(seconds: float) -> str:
        """Convert seconds to SRT timestamp format."""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = seconds % 60
        ms = int((secs - int(secs)) * 1000)
        return f"{hours:02d}:{minutes:02d}:{int(secs):02d},{ms:03d}"


def extract_word_anchors(result: dict) -> list[dict]:
    """From a raw whisper result, extract flat word-level anchors.

    Each item: {"text": str, "start": float, "end": float, "segment": int}
    Falls back to segment-level anchors when word timestamps are absent,
    so callers always get a usable timeline (hyp: evidence adapter keeps
    word windows; local alignment maps them onto the authored units).
    """
    anchors: list[dict] = []
    for seg_idx, seg in enumerate(result.get("segments", [])):
        words = seg.get("words")
        if words:
            for w in words:
                text = (w.get("word") or w.get("text") or "").strip()
                if not text:
                    continue
                anchors.append({
                    "text": text,
                    "start": float(w.get("start", seg.get("start", 0.0))),
                    "end": float(w.get("end", seg.get("end", 0.0))),
                    "segment": seg_idx,
                })
        else:
            text = (seg.get("text") or "").strip()
            if text:
                anchors.append({
                    "text": text,
                    "start": float(seg.get("start", 0.0)),
                    "end": float(seg.get("end", 0.0)),
                    "segment": seg_idx,
                })
    return anchors