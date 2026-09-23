"""参考视频纵深剖析(方案 P1-② 落地)。

在 B2 的 `reference.py`(元数据 + scene 镜头切分 + 节奏)之上做升级,
把"参考视频 → 结构化报告"推进到"参考视频 → 可直接并入 brief 的约束":

- 黑切/黑场统计:blackdetect(复用 `/api/video/black-detect` 同款 FFmpeg
  滤镜,纯确定性);产出黑段数/总长/占比/最长黑段;
- 旁白密度估计:silencedetect → 静音窗 → 有声占比(ASR-free,不新增依赖,
  对齐方案「与 ffmpeg_engine/review 复用」);
- 9 维 brief 预填:对齐 api.BRIEF_DIMENSIONS,逐个给出三态
  (filled 确定 / suggested 建议 / pending 需人工)。

安全:与 reference.py 相同——只吃本地文件(拒绝 `-` 开头、必须存在),
不发任何网络请求;不写密钥。
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Optional

from .reference import (
    MediaProbeError, ReferenceError, analyze_reference_video,
    validate_media_path,
)

_SILENCE_START_RE = re.compile(r"silence_start:\s*([0-9.]+)")
_SILENCE_END_RE = re.compile(
    r"silence_end:\s*([0-9.]+)\s*\|\s*silence_duration:\s*([0-9.]+)")
_BLACK_RE = re.compile(
    r"black_start:\s*([0-9.]+)\s+black_end:\s*([0-9.]+)")

# 对齐 api.BRIEF_DIMENSIONS 的 9 维(自持一份避免依赖 api 模块引发环)
BRIEF_DIMENSIONS = (
    "content_type", "product_info", "target_platform", "duration_sec",
    "target_audience", "tone", "creative_direction", "reference_materials",
    "special_requirements",
)


# ---------------------------------------------------------------------------
# 黑场统计(blackdetect)
# ---------------------------------------------------------------------------

_BLACK_ARG: Optional[str] = None


def blackdetect_pix_arg() -> str:
    """探测当前 ffmpeg 的 blackdetect 像素阈值参数名(带缓存)。

    ffmpeg <=6 用 `pix_thresh`,>=7 改名 `pix_th`;写死任一都有一半环境
    输出空数组,因此探测一次并缓存(纯子进程,不缓存失败结果)。
    """
    global _BLACK_ARG
    if _BLACK_ARG is not None:
        return _BLACK_ARG
    r = subprocess.run(["ffmpeg", "-hide_banner", "-h", "filter=blackdetect"],
                       capture_output=True, text=True, shell=False, timeout=30)
    help_text = r.stdout + r.stderr
    if "pix_th " in help_text or "pix_th <" in help_text:
        _BLACK_ARG = "pix_th"
    else:
        _BLACK_ARG = "pix_thresh"
    return _BLACK_ARG


def _black_segments(path: str, min_dur: float = 0.3,
                    pix_thresh: float = 0.01) -> list[dict]:
    """blackdetect 滤镜输出的黑场窗口(与 api/video/black-detect 同款)。"""
    pix_arg = blackdetect_pix_arg()
    r = subprocess.run(
        ["ffmpeg", "-i", path, "-vf",
         f"blackdetect=d={min_dur}:{pix_arg}={pix_thresh}",
         "-an", "-f", "null", "-"],
        capture_output=True, text=True, shell=False, timeout=120)
    segs = []
    for m in _BLACK_RE.finditer(r.stdout + r.stderr):
        start, end = float(m.group(1)), float(m.group(2))
        segs.append({"start": round(start, 3), "end": round(end, 3),
                     "dur": round(max(end - start, 0.0), 3)})
    return segs


def black_stats(segs: list[dict], duration: float) -> dict:
    """黑场统计:段数 / 总黑长 / 占比 / 最长黑段 / 读法。"""
    if duration <= 0:
        return {"count": 0, "total_sec": 0.0, "ratio": 0.0,
                "max_sec": 0.0, "pacing": "unknown"}
    total = sum(s["dur"] for s in segs)
    maxd = max((s["dur"] for s in segs), default=0.0)
    ratio = round(total / duration, 4)
    pacing = ("intro/outro 黑场" if ratio >= 0.02 else "无黑场切")
    return {"count": len(segs), "total_sec": round(total, 3),
            "ratio": ratio, "max_sec": round(maxd, 3), "pacing": pacing}


# ---------------------------------------------------------------------------
# 旁白密度(静音检测 → 有声活跃度)
# ---------------------------------------------------------------------------

def _silence_segments(path: str, min_dur: float = 0.4,
                      noise_db: float = -35) -> list[dict]:
    """silencedetect 静音段(ASR-free: 静音越少 → 旁白/画外音越密)。"""
    r = subprocess.run(
        ["ffmpeg", "-i", path, "-map", "a:0",
         "-af", f"silencedetect=noise={noise_db}dB:d={min_dur}",
         "-f", "null", "-"],
        capture_output=True, text=True, shell=False, timeout=120)
    segs = []
    pending: Optional[float] = None
    for line in (r.stdout + r.stderr).splitlines():
        m = _SILENCE_START_RE.search(line)
        if m:
            pending = float(m.group(1))
            continue
        m = _SILENCE_END_RE.search(line)
        if m and pending is not None:
            end = float(m.group(1))
            segs.append({"start": round(pending, 3), "end": round(end, 3),
                         "dur": round(float(m.group(2)), 3)})
            pending = None
    return segs


def _narration_density(silences: list[dict], duration: float) -> dict:
    """旁白活跃度:1 - 静音占比;并给密度标签。"""
    if duration <= 0:
        return {"speech_ratio": 0.0, "silence_count": 0,
                "silence_total_sec": 0.0, "longest_silence": 0.0,
                "density": "unknown"}
    total_sil = min(sum(s["dur"] for s in silences), duration)
    ratio = round(1.0 - total_sil / duration, 3)
    longest = max((s["dur"] for s in silences), default=0.0)
    label = ("dense_narration" if ratio >= 0.7 else
             "spoken_mix" if ratio >= 0.4 else "music_lead")
    return {"speech_ratio": ratio, "silence_count": len(silences),
            "silence_total_sec": round(total_sil, 3),
            "longest_silence": round(longest, 3), "density": label}


# ---------------------------------------------------------------------------
# 9 维 brief 预填
# ---------------------------------------------------------------------------

def _platform_from_aspect(w: int, h: int) -> str:
    if w <= 0 or h <= 0:
        return ""
    ar = w / h
    if ar < 0.8:
        return "douyin,kuaishou"
    if ar > 1.2:
        return "youtube"
    return "douyin,youtube,xiaohongshu"


def _tone_hint(pacing: str, narration: dict, black_ratio: float) -> str:
    # 轮57:speech_ratio=None(无音轨)不可推断——不进任何旁白相关判据
    speech = narration.get("speech_ratio")
    if speech is None:
        return "中性,以参考片为准(参考片无音轨)"
    if str(pacing) == "fast" and speech >= 0.6:
        return "快节奏强信息(密集旁白)"
    if str(pacing) == "slow" and black_ratio >= 0.05:
        return "留白叙事,黑场收尾"
    return "中性,以参考片为准"


def _special_hints(metadata: dict) -> str:
    w, h = metadata.get("width", 0), metadata.get("height", 0)
    if w <= 0 or h <= 0:
        return ""
    ar = w / h
    if abs(ar - 9 / 16) < 0.02:
        return "竖屏 9:16"
    if abs(ar - 1.0) < 0.02:
        return "方屏 1:1"
    if abs(ar - 16 / 9) < 0.05:
        return "横屏 16:9"
    return f"{w}x{h}"


def build_brief_prefill(metadata: dict, pacing: dict, black: dict,
                        narration: dict, scene_count: int,
                        source_path: str = "") -> dict:
    """剖析结果 → 9 维 brief 预填(三态) + 来源说明。"""
    dur = round(float(metadata.get("duration_sec", 0.0) or 0.0))
    # 轮57:None(无音轨)= 不可推断,不参与高旁白判定
    speech = narration.get("speech_ratio")
    bl = black.get("ratio", 0.0)
    pace_label = str(pacing.get("label", "medium"))

    if speech is not None and speech >= 0.7 and bl < 0.05 and scene_count <= 6:
        content_type, content_note = "talking_head", "高旁白 + 少黑场 + 少镜头 → 口播"
    elif bl >= 0.12:
        content_type, content_note = "product", "黑场占比大 → 产品片(黑底硬切)"
    else:
        content_type, content_note = "montage", "多镜头混剪(需人工复核)"

    def dim(value, state, note=""):
        return {"value": value, "state": state, "note": note}

    prefill = {
        "content_type": dim(content_type, "filled" if content_type else
                            "pending", content_note),
        "product_info": dim("", "pending", "从材料说明/标题拿,媒体无法推断"),
        "target_platform": dim(_platform_from_aspect(
            metadata.get("width", 0), metadata.get("height", 0)),
            "filled", "按画面比例推断"),
        "duration_sec": dim(dur, "filled", "按参考视频时长取整"),
        "target_audience": dim("", "pending", "需人工指定目标受众"),
        "tone": dim(_tone_hint(pace_label, narration, bl), "suggested",
                    "按剪接节奏/旁白密度推断"),
        "creative_direction": dim("照抄参考片钩子+开局手法", "suggested",
                                  "具体文案需创作者填充"),
        "reference_materials": dim(source_path, "filled",
                                   "参考视频路径"),
        "special_requirements": dim(_special_hints(metadata), "filled",
                                    "按媒体属性生成"),
    }
    return prefill


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

def profile_reference(video_path: str | Path, *,
                      name: Optional[str] = None,
                      save_dir: Optional[Path] = None,
                      scene_threshold: float = 0.3,
                      max_shots: int = 60) -> dict:
    """纵深剖析参考视频并(可选)落盘 JSON 报告。

    返回 {ok, report, report_path?, brief_prefill}。
    """
    path = validate_media_path(video_path)
    base = analyze_reference_video(path, scene_threshold=scene_threshold,
                                   max_shots=max_shots)
    meta = base["metadata"]
    duration = float(meta.get("duration_sec", 0.0) or 0.0)
    shots = base["shots"]
    pacing = base["pacing"]
    scene_count = len(shots)

    black_segs = _black_segments(path)
    black = black_stats(black_segs, duration)
    silences = _silence_segments(path) if meta.get("has_audio") else []
    # 轮57(真实使用发现,子智能体 C):无音轨视频旧代码 silences=[] →
    # speech_ratio=1.0「密集旁白」,brief_prefill 误判 talking_head。
    # 无音轨 = 不可推断(该维 no_audio),不是 100% 语音。
    narr = (_narration_density(silences, duration) if meta.get("has_audio")
            else {"speech_ratio": None, "silence_count": 0,
                  "silence_total_sec": 0.0, "longest_silence": 0.0,
                  "density": "no_audio"})

    prefill = build_brief_prefill(
        meta, pacing, black, narr, scene_count, source_path=path)
    report_name = (name or Path(path).stem).strip()

    report = {
        "profile": "shipin.reference@1",
        "name": report_name,
        "source_path": path,
        "metadata": meta,
        "shots": shots,
        "pacing": pacing,
        "black": black,
        "narration": narr,
        "brief_prefill": prefill,
        "summary": (
            f"参考视频 {duration:.1f}s;{scene_count} 镜,节奏 "
            f"{pacing.get('label', '?')};黑场 {black['count']} 段"
            f"({black['ratio'] * 100:.0f}%);"
            + (f"旁白活跃 {narr['speech_ratio'] * 100:.0f}%"
               if narr.get("speech_ratio") is not None else "参考片无音轨")
            + f";预填 {sum(1 for d in prefill.values() if d['state'] == 'filled')}/9 维"),
    }

    result = {"ok": True, "report": report,
              "brief_prefill": prefill}
    if save_dir is not None:
        out_dir = Path(save_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{report_name}.json"
        _write_json(out_path, report)
        result["report_path"] = str(out_path)
    return result


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8")


__all__ = [
    "BRIEF_DIMENSIONS", "black_stats", "build_brief_prefill",
    "profile_reference",
]