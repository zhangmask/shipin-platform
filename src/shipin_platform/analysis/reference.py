"""参考视频分析(方案 B2 落地)。

借鉴点修正:hypit 仓库中 `packages/source` 只是 markup 源文件的 Header
引导器,并不存在 reference-video.md;可借鉴的是 hypit 的媒体理解理念
(`whisperx` 把媒体规整为 SemanticTake、`media-pipeline` 输出自包含
证据)——即"先确定性理解,再供创作层引用"。本模块等价落点:

- 对本地视频做纯 FFmpeg 确定性分析(元数据 + scene 滤镜镜头切分 +
  节奏统计),不依赖任何第三方库;
- 输出结构化 report,并给出可直接并入 brief 的建议字段(hint)。

安全:只吃本地文件路径(拒绝 `-` 开头;不存在即报错);不发任何网络
请求。远程素材需由上游经 provider_registry 安全网关落盘后再分析。
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Optional

_PTS_RE = re.compile(r"pts_time:([0-9.]+)")


class ReferenceError(ValueError):
    """参考分析失败(归类错误,可安全转 4xx/5xx)。"""


class MediaProbeError(ReferenceError):
    """ffprobe / ffmpeg 无法解析输入。"""


# ---------------------------------------------------------------------------
# 底层：ffprobe 元数据 + ffmpeg scene 切点
# ---------------------------------------------------------------------------

def _run(argv: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True,
                          shell=False, timeout=timeout)


def _ffprobe_meta(path: str) -> dict:
    r = _run(["ffprobe", "-v", "quiet", "-print_format", "json",
              "-show_format", "-show_streams", path])
    if r.returncode != 0:
        raise MediaProbeError(f"ffprobe 解析失败: {r.stderr[-400:]}")
    return json.loads(r.stdout)


def _ffmpeg_cuts(path: str, threshold: float) -> list[float]:
    """scene 滤镜输出的切点秒数列表(确定性)。"""
    r = _run(["ffmpeg", "-v", "info", "-i", path,
              "-vf", f"select='gt(scene,{threshold})',showinfo",
              "-f", "null", "-"], timeout=120)
    cuts = sorted({float(m) for m in _PTS_RE.findall(r.stderr) if float(m) > 0.05})
    return cuts


def _parse_fps(avg_frame_rate: str) -> float:
    try:
        num, sep, den = avg_frame_rate.partition("/")
        if not sep:
            return float(num)
        return float(num) / float(den)
    except (ValueError, ZeroDivisionError):
        return 0.0


# ---------------------------------------------------------------------------
# 镜头切分与节奏统计
# ---------------------------------------------------------------------------

def _cuts_to_shots(cuts: list[float], duration: float, max_shots: int) -> list[dict]:
    """切点 → 连续镜头列表(覆盖全长、无间隙; <0.15s 的碎片并给前镜)。"""
    bounds = _dedupe([0.0] + cuts + [duration])
    shots: list[dict] = []
    for i in range(len(bounds) - 1):
        s, e = bounds[i], bounds[i + 1]
        if e - s < 0.15:
            if shots:  # 合并进前镜
                shots[-1]["end"] = round(e, 3)
            continue
        shots.append({"no": len(shots) + 1, "start": round(s, 3),
                      "end": round(e, 3), "dur_sec": round(e - s, 3)})
        if len(shots) >= max_shots:
            break
    return shots


def _dedupe(values: list[float]) -> list[float]:
    """保序去重(只去相邻重复;0.0 起点必须保留,它是首镜边界)。"""
    out: list[float] = []
    for v in values:
        if not out or v - out[-1] > 1e-6:
            out.append(v)
    return out


def _pace(dur: float) -> str:
    if dur < 2.2:
        return "fast"
    if dur < 4.2:
        return "medium"
    return "slow"


def _pacing_stats(shots: list[dict]) -> dict:
    if not shots:
        return {"count": 0, "avg": 0.0, "median": 0.0, "min": 0.0,
                "max": 0.0, "fast": 0, "medium": 0, "slow": 0,
                "label": "unknown"}
    durs = sorted(s["dur_sec"] for s in shots)
    n = len(durs)
    median = durs[n // 2] if n % 2 else (durs[n // 2 - 1] + durs[n // 2]) / 2
    return {
        "count": len(shots),
        "avg": round(sum(durs) / n, 3),
        "median": round(median, 3),
        "min": round(durs[0], 3),
        "max": round(durs[-1], 3),
        "fast": sum(1 for s in shots if _pace(s["dur_sec"]) == "fast"),
        "medium": sum(1 for s in shots if _pace(s["dur_sec"]) == "medium"),
        "slow": sum(1 for s in shots if _pace(s["dur_sec"]) == "slow"),
        "label": _pace(sum(durs) / n),
    }


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

def validate_media_path(path: str | Path) -> str:
    """本地文件路径校验(与 ffmpeg_engine._media_arg 同一模式)。"""
    p = Path(path)
    if str(p).startswith("-"):
        raise ReferenceError(f"path starts with '-': {path!r}")
    p = p.resolve()
    if not p.exists():
        raise MediaProbeError(f"视频不存在: {p}")
    if not p.is_file():
        raise MediaProbeError(f"不是文件: {p}")
    return str(p)


def analyze_reference_video(
    video_path: str | Path,
    scene_threshold: float = 0.3,
    max_shots: int = 60,
) -> dict:
    """分析参考视频 → structured report(video dict)。

    report 结构:
      ok / metadata / shots / pacing / brief_hint / summary
    """
    path = validate_media_path(video_path)
    threshold = max(0.0, min(scene_threshold, 0.9))

    raw = _ffprobe_meta(path)
    fmt = raw.get("format", {})
    duration = float(fmt.get("duration", 0.0) or 0.0)
    vstream = next((s for s in (raw.get("streams") or [])
                    if s.get("codec_type") == "video"), None)
    if vstream is None:
        raise MediaProbeError("输入不含视频流,无法分析")
    astream = any(s.get("codec_type") == "audio"
                  for s in (raw.get("streams") or []))

    cuts = _ffmpeg_cuts(path, threshold)
    shots = _cuts_to_shots(cuts, duration, max_shots)
    pacing = _pacing_stats(shots)

    metadata = {
        "duration_sec": round(duration, 3),
        "width": int(vstream.get("width", 0)),
        "height": int(vstream.get("height", 0)),
        "fps": round(_parse_fps(vstream.get("avg_frame_rate", "0/0")), 3),
        "has_audio": astream,
        "format": fmt.get("format_name", ""),
    }
    hint = build_brief_hint(metadata, pacing, len(shots))
    summary = build_summary(metadata, pacing)

    return {
        "ok": True,
        "metadata": metadata,
        "shots": shots,
        "pacing": pacing,
        "brief_hint": hint,
        "summary": summary,
    }


def build_brief_hint(metadata: dict, pacing: dict, scene_count: int) -> dict:
    """压缩成可并入 brief 的字段:时长/节奏/镜头数/一句话参考。"""
    dur = float(metadata.get("duration_sec", 0.0) or 0.0)
    label = pacing.get("label", "medium")
    avg = float(pacing.get("avg", 0.0) or 0.0)
    return {
        "duration_sec": round(dur),
        "pacing": label,
        "scene_count": scene_count,
        "reference_note": (
            f"参考成片 {dur:.0f}s;{scene_count} 镜,平均 {avg:.1f}s/镜,"
            f"节奏 {label}"),
    }


def build_summary(metadata: dict, pacing: dict) -> str:
    dur = round(float(metadata.get("duration_sec", 0.0) or 0.0), 1)
    return (
        f"参考视频 {dur}s;{metadata.get('width', '?')}x{metadata.get('height', '?')}"
        f"@{metadata.get('fps', '?')}fps;平均镜长 "
        f"{pacing.get('avg', 0.0):.1f}s,节奏 {pacing.get('label', '?')}")