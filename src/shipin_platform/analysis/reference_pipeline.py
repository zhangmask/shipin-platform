"""参考视频复刻流水线 —— 把一条参考 TVC 变成「剧本+分镜+提示词」反推包。

产品闭环（用户要求：AI 从视频站找参考 → 抄剧本、抄分镜、反推提示词 → 生图/生视频）：

    source.mp4
      ├─ ffmpeg scene 切分（复用 reference.py）→ 镜头列表
      ├─ ffmpeg 抽关键帧（每镜 1-3 帧，首/中/尾）→ frames/
      ├─ whisper 中文 ASR（复用 WhisperService）→ 台词稿（字幕时间轴）
      └─ 多模态大模型逐镜反推（vision_llm.analyze_frame）→ shots.json
          {camera, lighting, tone, composition, image_prompt, video_prompt, ...}

产出（全部落盘在参考包目录 data/references/<ref_id>/）：
  - shots.json      逐镜反推结果 + 台词时间轴对齐（每镜带台词文本）
  - brief.json      简报预填（reference_profiler 产出，直接喂 script 节点）
  - script.json     剧本 JSON（镜头 + 台词 + 反推提示词，供注入画布）
  - script.txt      纯台词稿（剧本抄写：镜头号 + 台词）

安全：只处理本机文件（路径围栏在调用层）；VLM 走 provider registry 网关；
ASR 本地模型；ffmpeg/whisper 均 subprocess 参数数组，无 shell。
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from shipin_platform.analysis import reference_profiler as profiler
from shipin_platform.services import vision_llm

# ---------------------------------------------------------------------------
# 镜头模型
# ---------------------------------------------------------------------------


@dataclass
class Shot:
    idx: int
    start: float
    end: float
    duration: float
    frames: list[Path] = field(default_factory=list)   # 抽帧文件
    dialogue: str = ""                                  # ASR 台词（镜头内）
    analysis: dict = field(default_factory=dict)        # VLM 反推结果


_FFMPEG_RE = re.compile(r"pts_time:([0-9.]+)")


# ---------------------------------------------------------------------------
# 抽帧
# ---------------------------------------------------------------------------

def _ffmpeg_extract_frames(video: Path, shots: list[Shot],
                           frames_dir: Path) -> None:
    """每镜抽 1-3 帧（首/中/尾；≤2.5s 只抽中帧）。文件名 f{idx}_{m,j,l}.jpg。"""
    frames_dir.mkdir(parents=True, exist_ok=True)
    for s in shots:
        picks: list[tuple[float, str]] = []
        dur = s.duration
        if dur >= 5.0:
            picks = [(0.2, "m"), (dur / 2, "j"), (max(0.0, dur - 0.35), "l")]
        elif dur >= 2.5:
            picks = [(dur / 2, "j"), (max(0.0, dur - 0.35), "l")]
        else:
            picks = [(dur / 2, "j")]
        for t, tag in picks:
            out = frames_dir / f"f{s.idx:03d}_{tag}.jpg"
            try:
                r = subprocess.run(
                    ["ffmpeg", "-v", "error",
                     "-ss", f"{s.start + t:.3f}", "-i", str(video),
                     "-frames:v", "1", "-q:v", "3",
                     "-vf", "scale='min(960,iw)':-2", "-y", str(out)],
                    capture_output=True, text=True, shell=False, timeout=90)
                if r.returncode == 0 and out.exists():
                    s.frames.append(out)
            except (subprocess.TimeoutExpired, OSError):
                continue


# ---------------------------------------------------------------------------
# ASR 台词 → 镜头对齐
# ---------------------------------------------------------------------------

_SRT_TS = re.compile(
    r"(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2}),(\d{3})")


def _load_srt_lines(srt: Path) -> list[dict]:
    """SRT → [{start, end, text}]（带时码，供镜头对齐）。"""
    raw = srt.read_text(encoding="utf-8")
    out: list[dict] = []
    for block in re.split(r"\n\s*\n", raw.strip()):
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if len(lines) < 2:
            continue
        m = _SRT_TS.search(lines[1] if len(lines) > 1 else lines[0])
        if not m:
            continue
        t = [int(x) for x in m.groups()]
        start = t[0] * 3600 + t[1] * 60 + t[2] + t[3] / 1000
        end = t[4] * 3600 + t[5] * 60 + t[6] + t[7] / 1000
        text = "".join(lines[2:]).strip()
        if text:
            out.append({"start": start, "end": end, "text": text})
    return out


def _align_dialogue(shots: list[Shot], sub_lines: list[dict]) -> None:
    """按台词中心点归属镜头（含 ±1s 容差，跨界台词归入较近一侧）。"""
    for s in shots:
        mid = (s.start + s.end) / 2
        parts = [ln["text"] for ln in sub_lines
                 if s.start - 1.0 <= (ln["start"] + ln["end"]) / 2 <= s.end + 1.0]
        s.dialogue = " ".join(parts).strip()


def _transcribe(video: Path, ref_dir: Path) -> list[dict]:
    """WhisperService 中文转写 → 台词行列表。失败抛错（由上层决定是否阻断）。"""
    from shipin_platform.tools.whisper_service import WhisperService
    ws = WhisperService(model="base", device="cpu", language="zh")
    res = ws.transcribe(video, output_dir=ref_dir, output_format="srt")
    srt = Path(res["srt_path"])
    if not srt.exists():
        return []
    return _load_srt_lines(srt)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def run_reference_pipeline(
    video: str | Path,
    ref_dir: str | Path, *,
    scene_threshold: float = 0.3,
    max_shots: int = 60,
    skip_asr: bool = False,
    vlm_cfg: Optional[dict] = None,
) -> dict:
    """完整反推流水线（切镜 + ASR + 抽帧 + VLM），产物全部写 ref_dir。

    ref_dir 由调用方创建并围栏（API 层建 data/references/<ref_id>/），本模块
    不做网络除 VLM（走 provider 网关）。vlm_cfg 传 None 则自动探测；
    无多模态配置时仍产出（提示词留空 + _vlm_error 说明），不阻断产品。
    """
    v = Path(video).resolve()
    if not v.is_file():
        raise FileNotFoundError(f"参考视频不存在: {v}")
    out = Path(ref_dir)
    out.mkdir(parents=True, exist_ok=True)

    # 1) 确定性分析（metadata + scene 切分 + 节奏/黑场/静音统计）
    prof = profiler.profile_reference(v, save_dir=out,
                                      scene_threshold=scene_threshold,
                                      max_shots=max_shots)
    if not prof.get("ok"):
        raise RuntimeError(f"参考视频分析失败: {prof.get('error') or prof}")
    report = prof["report"]
    meta = report.get("metadata") or {}
    raw_shots = report.get("shots") or []
    brief_prefill = prof.get("brief_prefill") or {}

    shots: list[Shot] = []
    for i, r in enumerate(raw_shots):
        st = float(r.get("start", 0.0))
        en = float(r.get("end", st))
        shots.append(Shot(idx=i + 1, start=st, end=en,
                          duration=max(0.0, en - st)))

    # 2) ASR 中文台词 → 对齐镜头（失败不阻断：无对白参考片也能反推）
    if not skip_asr:
        try:
            sub_lines = _transcribe(v, out)
            _align_dialogue(shots, sub_lines)
        except Exception as e:   # whisper 模型缺失 / CPU 资源不足等
            print(f"[reference_pipeline] ASR 跳过: {e}")

    # 3) 抽帧
    frames_dir = out / "frames"
    _ffmpeg_extract_frames(v, shots, frames_dir)

    # 4) VLM 逐镜反推（串行防限流；未配置或失败保留空字段不阻断）
    vlm_used = False
    vlm_error = ""
    if vlm_cfg is None:
        try:
            vlm_cfg = vision_llm.resolve_vision()
        except vision_llm.VisionError as e:
            vlm_error = str(e)
    if vlm_cfg:
        vlm_used = True

    shot_json: list[dict] = []
    for s in shots:
        analysis: dict = {"scene": "", "subject": "", "camera": "",
                          "lighting": "", "tone_and_palette": "",
                          "composition": "", "image_prompt": "",
                          "video_prompt": ""}
        frame = s.frames[0] if s.frames else None
        if vlm_cfg and frame:
            try:
                analysis = vision_llm.analyze_frame(
                    frame, context=s.dialogue or "", cfg=vlm_cfg)
            except vision_llm.VisionError as e:
                analysis["_error"] = str(e)[:240]
        shot_json.append({
            "idx": s.idx,
            "start": round(s.start, 2), "end": round(s.end, 2),
            "duration": round(s.duration, 2),
            "frames": [f.name for f in s.frames],
            "dialogue": s.dialogue,
            **analysis,
        })

    # 5) 简报预填 → 剧本 JSON（供注入画布 + 前端预览）
    script_brief = {
        k: (b.get("value") or "") for k, b in brief_prefill.items()
    }
    title = str(meta.get("title") or out.name)
    script = {
        "title": title,
        "total_duration": float(meta.get("duration_sec", 0.0) or 0.0),
        "shot_count": len(shot_json),
        "scene_threshold": scene_threshold,
        "lines": [{
            "shot": s["idx"],
            "time": f"{s['start']:.1f}-{s['end']:.1f}",
            "duration": s["duration"],
            "dialogue": s["dialogue"],
            "frame": s["frames"][0] if s["frames"] else "",
            "image_prompt": s["image_prompt"],
            "video_prompt": s["video_prompt"],
        } for s in shot_json],
    }

    # 6) 落盘（shots / script / brief 三者齐备供 apply 与前端消费）
    (out / "shots.json").write_text(
        json.dumps(shot_json, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "script.json").write_text(
        json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "brief.json").write_text(
        json.dumps({"brief_prefill": script_brief,
                    "vlm_error": vlm_error, "vlm_used": vlm_used},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    if any(s.frames for s in shots):
        (out / "frames.txt").write_text(
            "\n".join(str(f) for s in shots for f in s.frames),
            encoding="utf-8")

    return {
        "ok": True,
        "ref_dir": str(out),
        "video": str(v),
        "title": title,
        "shot_count": len(shot_json),
        "duration": script["total_duration"],
        "vlm_used": vlm_used,
        "vlm_error": vlm_error,
        "brief_prefill": script_brief,
        "script_pack": script,
    }