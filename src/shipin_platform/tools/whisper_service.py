"""Whisper ASR service for subtitle generation.

对齐 hypit whisperx 包的词级锚点设计：transcribe 产出词级时间戳
(word_anchors)，供卡拉OK字幕、语义锚定与后续变体工作使用。
"""
from __future__ import annotations

import json as _json
import os
from pathlib import Path
from typing import Optional

# 轮73:openai-whisper 改惰性导入——节点无 openai CDN 可达性也不装
# openai-whisper(走 sidecar faster-whisper),顶层 import 会让整个模块
# (含 sidecar 后端)直接 ImportError,ASR 检查静默 skip 成假通过。


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
    """Wraps Whisper for subtitle generation.

    轮73:双后端——`sidecar`(默认,faster-whisper 常驻于 TTS sidecar,
    CPU int8,节点无 openai CDN 可达性时的唯一 ASR 通道)与
    `openai`(本地 openai-whisper,模型需从 openaipublic 下载)。
    由 SHIPIN_ASR_BACKEND 选择;sidecar 不可达时 transcribe 抛错由
    调用方转 skip(不吞,与全平台 fail-open 口径一致)。
    """

    def __init__(self, model: str = "medium", device: str = "cpu",
                 language: str = "zh", backend: str = ""):
        self.model_name = model
        self.device = device
        self.language = language
        self._model = None
        self.backend = (backend or os.environ.get(
            "SHIPIN_ASR_BACKEND", "sidecar")).strip().lower()

    def _get_model(self) -> "whisper.Whisper":
        if self.backend == "sidecar":
            raise RuntimeError("sidecar 后端不需本地模型")
        if self._model is None:
            import whisper  # 轮73:惰性导入(openai 后端才需要)
            self._model = whisper.load_model(self.model_name, device=self.device)
        return self._model

    def _sidecar_segments(self, audio_path: Path,
                          initial_prompt: str = "") -> list[dict]:
        """调 TTS sidecar 的 /asr(faster-whisper 常驻),返回 whisper
        同构 segments。URL 构造与 SSRF 守卫在
        generation.local_media.sidecar_asr(轮58 白名单范式),此处不
        自行拼 URL;失败向上抛,调用方负责 skip 语义。
        initial_prompt:预期文本偏置(轮73,短句降 ASR 噪声用)。"""
        from ..generation import local_media
        return local_media.sidecar_asr(str(audio_path), self.language,
                                       initial_prompt)

    def transcribe(
        self,
        audio_path: Path,
        output_dir: Optional[Path] = None,
        output_format: str = "srt",
        word_timestamps: bool = True,
    ) -> dict:
        """Transcribe audio and generate subtitles.

        Returns dict with paths to output files (srt/json/word_anchors).
        word_anchors.json 是词级锚点文件（对齐 whisperx 的 word-level
        evidence）：[{word, start, end}]，供卡拉OK字幕、语义锚定使用。
        """
        audio_path = _reject_traversal(audio_path, name="audio_path")
        output_dir = _reject_traversal(output_dir or audio_path.parent,
                                       name="output_dir")
        if self.backend == "sidecar":
            # faster-whisper 段级结果即 whisper segments 同构;词级锚点
            # 走 extract_word_anchors 的段级回落。initial_prompt 可经
            # transcribe 的关键字透传(轮73:短句校验压 ASR 噪声用)。
            result = {"segments": self._sidecar_segments(
                          audio_path,
                          initial_prompt=getattr(self, "_initial_prompt", "") or ""),
                      "language": self.language, "text": ""}
        else:
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