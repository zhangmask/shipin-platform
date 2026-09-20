"""Assembly v2 — narration-first timeline, duration-preserving transitions,
sound design.  融合开源做法（OpenMontage 的 xfade 链、video-agent-kit 系的
「旁白为主轨」思想、xfade-easing 的转场观感），全部用确定性 ffmpeg 实现。

coffee-v5 复盘的两个病根，这里从机制上解决：
1. 配音与画面「各铺各的」——S06 旁白 2.16s 溢出 2.0s 的镜头、S05/S07 旁白
   说完干晾 1.1s+。对齐门按旁白实长计算镜头窗口（window = max(分镜时长,
   tts+气口)），旁白永远在镜头内说完。
2. 成片没有后期——BGM 闪避、切点 whoosh、保时长叠化转场、品牌字卡
   Ken Burns、调色，全部在这里落地为确定性管线步骤。
"""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

# ffmpeg 8.x 内置 xfade 过渡（xfade-easing 的常见观感子集，无需魔改构建）
XFADE_TRANSITIONS = frozenset({
    "fade", "dissolve", "wipeleft", "wiperight", "wipeup", "wipedown",
    "slideleft", "slideright", "slideup", "slidedown", "smoothleft",
    "smoothright", "smoothup", "smoothdown", "circleopen", "circleclose",
    "rectcrop", "distance", "hblur", "pixelize", "radial", "hlwind",
    "diagtl", "diagtr", "diagbl", "diagbr", "squeezeh", "squeezev",
})

DEFAULT_TD = 0.4          # 转场时长（s）
MIN_TAIL = 0.25           # 旁白说完到镜头切走的最小气口（s）
MAX_GAP = 0.8             # 超过即算「干晾」
MASTER_MAX = 10.0         # agnes master 素材上限


def _ffprobe_duration(path: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, shell=False)
    try:
        return float(r.stdout.strip() or 0)
    except ValueError:
        return 0.0


# ── 1. 旁白-镜头对齐门 ────────────────────────────────────────────────


def align_narration(shots: list[dict], min_tail: float = MIN_TAIL,
                    max_gap: float = MAX_GAP,
                    master_duration: float = MASTER_MAX) -> dict:
    """按旁白实长计算每个镜头的最终窗口（旁白驱动剪辑）。

    shots: [{shot_id, duration_sec, narration_path}]
    返回 {verdict, timeline, total_sec, findings, next_action}

    规则（确定性）：
    - window = max(storyboard_dur, tts + min_tail)，再 clamp 到 master 可供长度
    - tts + min_tail > master 可供 → SPILL（旁白比素材还长，必须缩句）
    - window - tts > max_gap → DEAD_AIR finding（分镜给的时间太长，缩窗）
    """
    findings: list[dict] = []
    timeline: list[dict] = []
    t = 0.0
    for s in shots:
        sid = str(s.get("shot_id", "?"))
        sb_dur = float(s.get("duration_sec") or 0)
        np_ = s.get("narration_path")
        tts = _ffprobe_duration(Path(np_)) if np_ else 0.0
        if not np_ or not Path(np_).exists():
            findings.append({"severity": "critical", "code": "NARRATION_MISSING",
                             "message": f"{sid} 旁白音频不存在: {np_}"})
            tts = 0.0
        needed = tts + min_tail
        window = max(sb_dur, needed) if tts else sb_dur
        window = min(window, master_duration)
        if tts and needed > master_duration:
            findings.append({"severity": "critical", "code": "SPILL",
                             "message": (f"{sid} 旁白 {tts:.2f}s + 气口 {min_tail}s "
                                         f"超过素材上限 {master_duration}s——缩句或拆镜")})
        if tts and window - tts > max_gap:
            findings.append({"severity": "suggestion", "code": "DEAD_AIR",
                             "message": (f"{sid} 旁白 {tts:.2f}s / 窗口 {window:.2f}s，"
                                         f"干晾 {window - tts:.2f}s（>{max_gap}s）")})
        timeline.append({
            "shot_id": sid,
            "storyboard_sec": sb_dur,
            "window_sec": round(window, 2),
            "tts_sec": round(tts, 2),
            "audio_start_sec": round(t, 2),
            "extended": bool(tts and window > sb_dur + 0.01),
        })
        t += window
    verdict = "fix" if any(f["severity"] == "critical" for f in findings) else "ok"
    return {
        "verdict": verdict,
        "timeline": timeline,
        "total_sec": round(t, 2),
        "findings": findings,
        "next_action": ("旁白比素材长，缩句后重 TTS" if verdict == "fix"
                        else "把 timeline 的 window_sec 传给 /api/video/stitch（保时长转场）"
                        "，audio_start_sec 用于 /api/audio/master 定位旁白床"),
    }


# ── 2. 保时长 xfade 转场拼接 ──────────────────────────────────────────


def _trim(src: Path, dst: Path, dur: float, head: bool = False) -> Path:
    """裁 src 到 dur 秒写入 dst；head=True 时从尾部回退 dur（保留结尾段）。
    参数全部走字面量列表 + shell=False（无 shell 拼接）。"""
    dur_s = f"{max(dur, 0.1):.3f}"
    if head:
        ss = max(0.0, _ffprobe_duration(src) - dur)
        r = subprocess.run(
            ["ffmpeg", "-y", "-ss", f"{ss:.3f}", "-i", str(src),
             "-t", dur_s, "-c:v", "libx264", "-pix_fmt", "yuv420p",
             "-an", str(dst)],
            capture_output=True, text=True, shell=False)
    else:
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", str(src), "-t", dur_s,
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(dst)],
            capture_output=True, text=True, shell=False)
    if r.returncode != 0:
        raise RuntimeError(f"trim failed: {r.stderr[-300:]}")
    return dst


def build_transition_stitch(clips: list[str], windows: list[float],
                            output: str, transition: str = "dissolve",
                            transition_duration: float = DEFAULT_TD,
                            fps: int = 24,
                            masters: Optional[list[str]] = None,
                            boundary_transitions: Optional[list[str]] = None) -> dict:
    """保时长拼接，支持逐边界转场选型（coffee-v6 教训的机制化）：

    - 链式首尾帧边界（上一镜尾帧=下一镜首帧，动作直接连续）→ **硬切**：
      动作无缝接力，叠化反而制造重影与运动停滞感；
    - 跳变边界（换景/换机位，只能自末帧收尾）→ **dissolve** 软过渡。
    boundary_transitions: 长度 n-1，每项 "cut" 或 xfade 名称；缺省全用 transition。
    混合图：连续 xfade 边界分组链式（组内 offset 用实测长度迭代推进），
    组间 concat。总长恒 == sum(windows)。
    """
    n = len(clips)
    if n == 0 or len(windows) != n:
        return {"ok": False, "error": "clips/windows 数量不匹配"}
    if boundary_transitions is not None and len(boundary_transitions) != n - 1:
        return {"ok": False, "error": "boundary_transitions 长度必须为 n-1"}
    bts = list(boundary_transitions) if boundary_transitions else \
        [transition] * (n - 1)
    for k, bt in enumerate(bts):
        if bt != "cut" and bt not in XFADE_TRANSITIONS:
            return {"ok": False, "error": f"boundary {k}: unknown transition '{bt}'"}
    warnings: list[str] = []
    td = round(max(0.1, float(transition_duration)) * fps) / fps

    # 降级判定：xfade 边界的入镜必须能从 master 借出 td 头帧，否则该边界硬切
    for k in range(n - 1):
        if bts[k] == "cut":
            continue
        m = Path(masters[k + 1]) if masters and k + 1 < len(masters) and masters[k + 1] else None
        need = windows[k + 1] + td
        have_m = _ffprobe_duration(m) if m and m.exists() else 0.0
        if have_m < need - 0.05:
            warnings.append(f"boundary {k}: 入镜 {Path(clips[k+1]).name} 无 master "
                            f"或不足 {need:.2f}s，该边界降级硬切")
            bts[k] = "cut"

    tmp = Path(tempfile.mkdtemp(prefix="stitch2_"))
    try:
        parts: list[Path] = []
        for i in range(n):
            src = Path(clips[i])
            pad = td if (i > 0 and bts[i - 1] != "cut") else 0.0
            want = windows[i] + pad
            m = Path(masters[i]) if masters and i < len(masters) and masters[i] else None
            if want > _ffprobe_duration(src) + 0.05 and m and m.exists():
                src = _trim(m, tmp / f"m{i:02d}.mp4", want)
            parts.append(_trim(src, tmp / f"p{i:02d}.mp4", want))

        # 分组：xfade 边界连接的 part 为一组（组内 xfade 链），组间 concat
        groups: list[list[int]] = [[0]]
        for k in range(1, n):
            if bts[k - 1] != "cut":
                groups[-1].append(k)
            else:
                groups.append([k])

        inputs: list[str] = []
        for p in parts:
            inputs += ["-i", str(p)]
        fg: list[str] = []
        group_labels: list[str] = []
        consumed = 0

        for gi, grp in enumerate(groups):
            if len(grp) == 1:
                fg.append(f"[{consumed}:v]null[g{gi}]")
                group_labels.append(f"[g{gi}]")
                consumed += 1
                continue
            prev = f"{consumed}:v"
            chain_len = _ffprobe_duration(parts[grp[0]])
            for j, idx in enumerate(grp[1:], start=1):
                k = grp[j] - 1  # 入镜 idx 的入边界
                offset = max(chain_len - td, 0.0)
                label = f"x{gi}_{j}"
                fg.append(f"[{prev}][{consumed + j}:v]xfade=transition={bts[k]}"
                          f":duration={td:.3f}:offset={offset:.3f}[{label}]")
                prev = label
                chain_len = offset + _ffprobe_duration(parts[grp[j]])
            group_labels.append(f"[{prev}]")
            consumed += len(grp)

        if len(groups) == 1:
            final_map = group_labels[0].strip("[]")
        else:
            fg.append(f"{''.join(group_labels)}concat=n={len(groups)}:v=1:a=0[vout]")
            final_map = "vout"
        r = subprocess.run(
            ["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(fg),
             "-map", f"[{final_map}]", "-c:v", "libx264", "-pix_fmt",
             "yuv420p", "-an", str(output)],
            capture_output=True, text=True, shell=False)
        if r.returncode != 0:
            return {"ok": False, "error": r.stderr[-500:]}
        dur = _ffprobe_duration(Path(output))
        out = {"ok": True, "output": str(output), "duration": round(dur, 2),
               "expected_sec": round(sum(windows), 2),
               "transitions": bts, "transition_duration": td,
               "boundary_preserved": abs(dur - sum(windows)) <= 0.25}
        if warnings:
            out["warnings"] = warnings
        return out
    finally:
        for p in tmp.iterdir():
            p.unlink(missing_ok=True)
        tmp.rmdir()


# ── 3. 声音设计：whoosh 合成 + BGM 闪避主混音 ─────────────────────────


def synth_whoosh(out: str, dur: float = 0.5, kind: str = "whoosh") -> str:
    """确定性合成转场音效（无需音频素材库）：粉噪 + 滤波 + 包络。"""
    out_p = Path(out)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    if kind == "pop":
        af = ("sine=frequency=520:duration=0.12,"
              "afade=t=in:d=0.005,afade=t=out:st=0.06:d=0.06,volume=0.7")
    else:
        fade_out_st = f"{dur * 0.45:.3f}"
        af = (f"anoisesrc=colour=pink:duration={dur:.3f}:amplitude=0.55,"
              "lowpass=f=1800,highpass=f=280,"
              f"afade=t=in:d={dur * 0.45:.3f},afade=t=out:st={fade_out_st}"
              f":d={dur * 0.55:.3f},volume=0.6")
    r = subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", af,
         "-ar", "48000", "-ac", "2", str(out_p)],
        capture_output=True, text=True, shell=False)
    if r.returncode != 0:
        raise RuntimeError(f"sfx synth failed: {r.stderr[-300:]}")
    return str(out_p)


def master_audio(narration_path: Optional[str], duration_sec: float, output: str,
                 bgm_path: Optional[str] = None, bgm_gain_db: float = -19.0,
                 duck: bool = True,
                 sfx_events: Optional[list[dict]] = None,
                 narration_events: Optional[list[dict]] = None,
                 work_dir: Optional[str] = None) -> dict:
    """最终声音设计主混音 → 单条 48k 立体声。

    二选一或并用：
    - narration_path: 已拼好的旁白床
    - narration_events: [{path, time}] 逐镜旁白定位（平台内部 adelay，
      agent 不再自己混旁白床——coffee-v5 的 adelay=0:0 崩溃就是那步出的）
    sfx_events: [{"time": 4.0, "kind": "whoosh"|"pop", "gain_db": -6}]
    BGM 用 sidechaincompress 被旁白闪避（说话时音乐退后，气口时浮上来）。
    """
    tmp = Path(work_dir or tempfile.mkdtemp(prefix="masteraud_"))
    tmp.mkdir(parents=True, exist_ok=True)
    if not narration_path and not narration_events:
        return {"ok": False, "error": "narration_path 或 narration_events 必须提供一个"}

    narration: Optional[Path] = None
    if narration_path:
        narration = Path(narration_path)
        if not narration.exists():
            return {"ok": False, "error": f"narration not found: {narration}"}
    if bgm_path and not Path(bgm_path).exists():
        return {"ok": False, "error": f"bgm not found: {bgm_path}"}

    sfx_files: list[tuple[float, Path, float]] = []
    for i, ev in enumerate(sfx_events or []):
        t = float(ev.get("time", 0))
        gain = float(ev.get("gain_db", -6.0))
        p = Path(ev["path"]) if ev.get("path") else \
            Path(tmp) / f"sfx_{i:02d}.wav"
        if not ev.get("path"):
            synth_whoosh(str(p), dur=0.5, kind=ev.get("kind", "whoosh"))
        sfx_files.append((t, p, gain))

    inputs: list[str] = []
    mix_inputs: list[str] = []
    fg: list[str] = []
    idx = 0
    if narration:
        inputs += ["-i", str(narration)]
        mix_inputs.append("0:a")
        idx = 1
    for j, ev in enumerate(narration_events or []):
        p = Path(ev["path"])
        if not p.exists():
            return {"ok": False, "error": f"narration event {j} not found: {p}"}
        inputs += ["-i", str(p)]
        delay_ms = max(int(float(ev.get("time", 0)) * 1000), 1)
        g = 10 ** (float(ev.get("gain_db", 0.0)) / 20)
        fg.append(f"[{idx}:a]volume={g:.4f},adelay={delay_ms}|{delay_ms}[ne{j}]")
        mix_inputs.append(f"ne{j}")
        idx += 1
    if bgm_path:
        inputs += ["-stream_loop", "-1", "-i", str(bgm_path)]
        g = 10 ** (bgm_gain_db / 20)
        fg.append(f"[{idx}:a]volume={g:.4f}[bgm0]")
        if duck:
            # 闪避 sidechain 取主旁白（0 号或第一个旁白事件）
            side = "0:a" if narration else mix_inputs[0]
            fg.append(f"[bgm0][{side}]sidechaincompress=threshold=0.02:ratio=8"
                      f":attack=40:release=500[bgm]")
        else:
            fg.append(f"[{idx}:a]anull[bgm]")
        mix_inputs.append("bgm")
        idx += 1
    for j, (t, p, gain) in enumerate(sfx_files):
        inputs += ["-i", str(p)]
        delay_ms = max(int(t * 1000), 1)
        g = 10 ** (gain / 20)
        fg.append(f"[{idx}:a]volume={g:.4f},adelay={delay_ms}|{delay_ms}[s{j}]")
        mix_inputs.append(f"s{j}")
        idx += 1
    dur_s = f"{max(duration_sec, 0.5):.3f}"
    fg.append("".join(f"[{x}]" for x in mix_inputs)
              + f"amix=inputs={len(mix_inputs)}:duration=longest:normalize=0,"
              + f"apad=whole_dur={dur_s},atrim=0:{dur_s}[out]")

    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        ["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(fg),
         "-map", "[out]", "-ar", "48000", "-ac", "2", str(out)],
        capture_output=True, text=True, shell=False)
    if r.returncode != 0:
        return {"ok": False, "error": r.stderr[-500:]}
    return {"ok": True, "output": str(out),
            "duration": round(_ffprobe_duration(out), 2),
            "bgm_ducked": bool(bgm_path and duck),
            "narration_events": len(narration_events or []),
            "sfx_count": len(sfx_files)}


# ── 5. 音画合流与响度归一（平台内核心，API 与 runner 共用）────────────


def mux_audio_video(video_path: str, audio_path: str, output: str,
                    shortest: bool = True, audio_offset: float = 0.0) -> dict:
    """旁白/主混音合入视频：视频流 copy（无损），音频 AAC 192k。"""
    v, a, out = Path(video_path), Path(audio_path), Path(output)
    if not v.exists():
        return {"ok": False, "error": f"video not found: {v}"}
    if not a.exists():
        return {"ok": False, "error": f"audio not found: {a}"}
    out.parent.mkdir(parents=True, exist_ok=True)
    offset_args = ["-itsoffset", f"{audio_offset:.3f}"] if audio_offset > 0 else []
    shortest_args = ["-shortest"] if shortest else []
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", str(v), "-i", str(a), *offset_args,
         "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
         "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
         *shortest_args, str(out)],
        capture_output=True, text=True, shell=False)
    if r.returncode != 0:
        return {"ok": False, "error": r.stderr[-400:]}
    return {"ok": True, "output": str(out)}


def normalize_loudness(audio_path: str, output: str, target_lufs: float = -14.0,
                       tp_dbtp: float = -1.5, lra: float = 11.0,
                       two_pass: bool = True) -> dict:
    """EBU R128 two-pass loudnorm（与 /api/audio/normalize 同实现）。"""
    inp = Path(audio_path)
    if not inp.exists():
        return {"ok": False, "error": f"not found: {inp}"}
    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    af = f"loudnorm=I={target_lufs}:TP={tp_dbtp}:LRA={lra}"
    if two_pass:
        r1 = subprocess.run(
            ["ffmpeg", "-y", "-i", str(inp),
             "-af", af + ":print_format=json", "-f", "null", "-"],
            capture_output=True, text=True, shell=False)
        m = re.findall(r'\{[^{}]*"input_i"[^{}]*\}', r1.stderr)
        if m:
            try:
                measured = json.loads(m[-1])
                af = (f"loudnorm=I={target_lufs}:TP={tp_dbtp}:LRA={lra}"
                      f":measured_I={measured.get('input_i')}"
                      f":measured_TP={measured.get('input_tp')}"
                      f":measured_LRA={measured.get('input_lra')}"
                      f":measured_thresh={measured.get('input_thresh')}"
                      f":offset={measured.get('target_offset')}:linear=true")
            except json.JSONDecodeError:
                pass
    enc = ["-c:a", "pcm_s16le", "-ar", "48000"] if str(out).lower().endswith(".wav") \
        else ["-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", str(inp), "-af", af, *enc, str(out)],
        capture_output=True, text=True, shell=False)
    if r.returncode != 0:
        return {"ok": False, "error": r.stderr[-400:]}
    return {"ok": True, "output": str(out)}


def color_grade_warm(src: str, output: str) -> dict:
    """warm_tvc 调色（与 /api/video/color-grade 预设一致）。"""
    s, o = Path(src), Path(output)
    if not s.exists():
        return {"ok": False, "error": f"not found: {s}"}
    preset = ("curves=r='0/0 0.3/0.28 0.7/0.72 1/1',"
              "curves=g='0/0 0.5/0.5 1/1',"
              "curves=b='0/0.05 0.7/0.68 1/0.95'")
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", str(s), "-vf", preset,
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(o)],
        capture_output=True, text=True, shell=False)
    if r.returncode != 0:
        return {"ok": False, "error": r.stderr[-400:]}
    return {"ok": True, "output": str(o)}


def burn_srt(video_path: str, srt_path: str, output: str,
             font_size: int = 46, margin_v: int = 96) -> dict:
    """字幕烧录（libass 探测 → drawtext 回退，带 cue 验收）。"""
    from shipin_platform.tools.subtitle_renderer import render_subtitles_best
    return render_subtitles_best(video_path, srt_path, output,
                                 font_size=font_size, margin_v=margin_v)


def loudness_measure(path: str) -> dict:
    """实测响度（loudnorm pass-1 测量，用于交付报告的 LUFS 复核）。"""
    p = Path(path)
    if not p.exists():
        return {"ok": False, "error": f"not found: {p}"}
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", str(p),
         "-af", "loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json",
         "-f", "null", "-"],
        capture_output=True, text=True, shell=False)
    m = re.findall(r'\{[^{}]*"input_i"[^{}]*\}', r.stderr)
    if not m:
        return {"ok": False, "error": "loudnorm measure failed"}
    try:
        d = json.loads(m[-1])
        return {"ok": True, "input_i": float(d.get("input_i", 0)),
                "input_tp": float(d.get("input_tp", 0))}
    except (json.JSONDecodeError, ValueError, TypeError):
        return {"ok": False, "error": "measure parse failed"}


# ── 4. 品牌字卡 Ken Burns（静态卡运动感）──────────────────────────────


def kenburns(image_path: str, duration: float, output: str,
             zoom_to: float = 1.10, fps: int = 24,
             size: str = "1280x704") -> dict:
    """静态图 → 缓慢推近的视频片段（品牌落版不再死板）。"""
    src = Path(image_path)
    if not src.exists():
        return {"ok": False, "error": f"image not found: {src}"}
    w, h = size.split("x")
    frames = max(int(duration * fps), fps)
    zexpr = f"min(1+({zoom_to - 1:.4f}*on/{frames}),{zoom_to:.3f})"
    vf = (f"scale={w}:{h},setsar=1,"
          f"zoompan=z='{zexpr}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
          f":d={frames}:s={size}:fps={fps}")
    r = subprocess.run(
        ["ffmpeg", "-y", "-loop", "1", "-i", str(src), "-vf", vf,
         "-t", f"{max(duration, 0.5):.3f}", "-c:v", "libx264",
         "-pix_fmt", "yuv420p", "-an", str(output)],
        capture_output=True, text=True, shell=False)
    if r.returncode != 0:
        return {"ok": False, "error": r.stderr[-400:]}
    return {"ok": True, "output": str(output),
            "duration": round(_ffprobe_duration(Path(output)), 2)}
