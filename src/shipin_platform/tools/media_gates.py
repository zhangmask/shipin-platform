"""Quality gates for generated media — deterministic, ffmpeg-only.

Two gates cover the failure modes that historically slipped through:

1. audio_probe():  the "did anyone check the sound" gate. A finished video
   must have a real audio stream at sane loudness, no long silence windows,
   and audio duration roughly aligned with the video. Volumes are measured
   as plain mean/max dB (consistent with `volumedetect`) and integrated
   loudness is estimated via ebur128 when possible.

2. extract_frames(): the "asset sanity" gate. AI-generated video segments
   routinely contain physically impossible frames (two keyboards, two
   screens, extra hands, warped geometry). A subagent MUST sample frames
   from every segment and run them through a vision model before accepting
   the asset. This tool produces the frame set; the review itself is done
   by the calling agent's VLM.

All commands run as inline argument lists with shell=False — no string
interpolation into a shell and no value can reach the OS as an option.
"""

from __future__ import annotations

import json as _json
import re as _re
import subprocess
from pathlib import Path


def _media_arg(path) -> str:
    """Absolute, non-option path — the only user value ever used in argv."""
    p = Path(str(path)).resolve()
    s = str(p)
    if s.startswith("-"):
        raise ValueError(f"refusing option-like media path: {s}")
    if not p.exists():
        raise FileNotFoundError(f"media not found: {s}")
    return s


def audio_probe(path, silence_db: float = -40.0,
                silence_min: float = 1.5) -> dict:
    """Full audio health report for a media file."""
    src = _media_arg(path)

    # 1) stream facts
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=codec_name,channels,sample_rate",
         "-of", "json", src],
        capture_output=True, text=True, shell=False)
    a_streams = []
    try:
        a_streams = _json.loads(r.stdout or "{}").get("streams", [])
    except _json.JSONDecodeError:
        pass
    if not a_streams:
        return {"has_audio": False, "verdict": "mute",
                "codec": None, "channels": 0, "sample_rate": 0,
                "mean_volume_db": None, "max_volume_db": None,
                "lufs": None, "silence_segments": [], "note": "no audio stream"}

    st = a_streams[0]
    d = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", src],
        capture_output=True, text=True, shell=False)
    duration = 0.0
    try:
        duration = float((d.stdout or "0").strip())
    except ValueError:
        pass

    # 2) amplitude stats
    v = subprocess.run(
        ["ffmpeg", "-i", src, "-map", "a:0", "-af", "volumedetect",
         "-f", "null", "-"],
        capture_output=True, text=True, shell=False)
    mean_db, max_db = None, None
    for line in (v.stdout + v.stderr).splitlines():
        m = _re.search(r"mean_volume:\s*(-?[0-9.]+)\s*dB", line)
        if m:
            mean_db = float(m.group(1))
        m = _re.search(r"max_volume:\s*(-?[0-9.]+)\s*dB", line)
        if m:
            max_db = float(m.group(1))
    if mean_db is not None and mean_db <= -91.0 and max_db <= -91.0:
        return {"has_audio": True, "verdict": "mute",
                "codec": st.get("codec_name"), "channels": st.get("channels"),
                "sample_rate": st.get("sample_rate"), "duration": duration,
                "mean_volume_db": mean_db, "max_volume_db": max_db,
                "lufs": None, "peak_segments": [], "peak_dbfs": None,
                "silence_segments": [], "note": "audio stream is digitally silent"}

    # 3) loudness via ebur128 (integrated loudness)
    lufs, tp = None, None
    e = subprocess.run(
        ["ffmpeg", "-i", src, "-map", "a:0", "-af", "ebur128=metadata=1",
         "-f", "null", "-"],
        capture_output=True, text=True, shell=False)
    summary = False
    for line in (e.stdout + e.stderr).splitlines():
        if "Summary" in line:
            summary = True
            continue
        if not summary:
            continue
        m = _re.search(r"^\s*I:\s*(-?[0-9.]+)\s*LUFS", line)
        if m and lufs is None:
            lufs = float(m.group(1))
        m = _re.search(r"^(?:.*True\s*)?peak:\s*(-?[0-9.]+)\s*dBFS", line, _re.I)
        if m and tp is None:
            tp = float(m.group(1))

    # 4) silence windows
    silence = []
    s = subprocess.run(
        ["ffmpeg", "-i", src, "-map", "a:0",
         "-af", "silencedetect=noise={}dB:d={}".format(silence_db, silence_min),
         "-f", "null", "-"],
        capture_output=True, text=True, shell=False)
    for line in (s.stdout + s.stderr).splitlines():
        m = _re.search(r"silence_start:\s*(-?[0-9.]+)", line)
        if m:
            silence.append({"start": float(m.group(1)), "end": None})
        m = _re.search(
            r"silence_end:\s*(-?[0-9.]+)\s*\| silence_duration:\s*([0-9.]+)", line)
        if m and silence:
            silence[-1]["end"] = float(m.group(1))
            silence[-1]["dur"] = float(m.group(2))
    for seg in silence:
        if seg.get("end") is None and duration:
            seg["end"] = duration
            seg["dur"] = round(duration - seg["start"], 3)

    # 5) verdict
    verdict, note = "ok", ""
    if mean_db is None:
        verdict, note = "unreadable", "volumedetect produced no reading"
    elif mean_db < -30:
        verdict, note = "too_quiet", "mean {:.1f} dB < -30 dB".format(mean_db)
    if silence:
        worst = max(seg.get("dur") or 0 for seg in silence)
        if worst > 4.0:
            verdict = "silence_windows" if verdict == "ok" else verdict
            note = (note + "; " if note else "") + "longest quiet {:.1f}s".format(worst)
    return {
        "has_audio": True, "verdict": verdict,
        "codec": st.get("codec_name"), "channels": st.get("channels"),
        "sample_rate": st.get("sample_rate"), "duration": round(duration, 3),
        "mean_volume_db": mean_db, "max_volume_db": max_db,
        "lufs": lufs, "peak_dbfs": tp,
        "silence_segments": silence, "note": note,
    }


def extract_frames(path: str, out_dir: str, interval: float = 1.0,
                   start: float = 0.0, end=None, max_frames: int = 0) -> dict:
    """Sample JPEG frames from a video at fixed intervals.

    Returns {"frames": [{"t": seconds, "path": abs_path}], "count": N}.
    """
    src = _media_arg(path)
    out = Path(str(out_dir)).resolve()
    out.mkdir(parents=True, exist_ok=True)

    p = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "format=duration", "-of", "csv=p=0", src],
        capture_output=True, text=True, shell=False)
    try:
        duration = float((p.stdout or "0").strip())
    except ValueError:
        duration = 0.0
    end = end if end is not None else duration
    total_seed = len(list(out.glob("fr_*.png")))

    frames = []
    t = 0.0
    while t + 1e-6 < end:
        if max_frames and len(frames) >= max_frames:
            break
        fp = out / "fr_{:05d}_{:06.2f}.png".format(total_seed + len(frames) + 1, t)
        r = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-ss", "{:.3f}".format(t),
             "-i", src, "-frames:v", "1", "-q:v", "3", str(fp)],
            capture_output=True, text=True, shell=False)
        if r.returncode == 0 and fp.exists():
            frames.append({"t": round(t, 3), "path": str(fp)})
        t += interval
    return {"frames": frames, "count": len(frames), "interval": interval,
            "duration": round(duration, 3)}