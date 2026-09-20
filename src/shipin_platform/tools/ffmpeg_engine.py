"""FFmpeg engine — delegates to OpenMontage tools with proper interface.

OpenMontage tools use the `execute(inputs: dict) -> ToolResult` pattern:
  - VideoStitch.execute({"operation": "stitch", "clips": [...], "output_path": ...})
  - AudioMixer.execute({"operation": "mix", "tracks": [...], "output_path": ...})
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional

# ── OpenMontage path setup (search upward) ───────────────────────
def _find_om_root() -> Path | None:
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / "OpenMontage-main" / "OpenMontage-main"
        if (cand / "tools").is_dir():
            return cand
        cand2 = parent / "OpenMontage-main"
        if (cand2 / "tools").is_dir():
            return cand2
    return None

_OM_ROOT = _find_om_root()
_OM_AVAILABLE = False
try:
    if _OM_ROOT.exists():
        sys.path.insert(0, str(_OM_ROOT))
    from tools.video.video_stitch import VideoStitch  # type: ignore
    from tools.audio.audio_mixer import AudioMixer  # type: ignore
    from lib.slideshow_risk import score_slideshow_risk  # type: ignore
    from lib.variation_checker import check_scene_variation  # type: ignore
    from lib.scoring import ProviderScore, ProductionPathScore  # type: ignore
    _OM_AVAILABLE = True
except Exception as _e:
    print(f"OpenMontage integration warning: {_e}", file=sys.stderr)

__all__ = [
    "FFmpegEngine",
    "OM_TOOLS_AVAILABLE",
    "score_slideshow_risk",
    "check_scene_variation",
    "ProviderScore",
    "ProductionPathScore",
]

OM_TOOLS_AVAILABLE = _OM_AVAILABLE


def _media_arg(path) -> str:
    """Validate a user-supplied media path before handing it to ffmpeg/ffprobe.

    Resolves to an absolute path and rejects option-like values (leading "-"),
    so the value can never be parsed as an ffmpeg option argument. Commands
    are always invoked as inline argument lists with shell=False.
    """
    p = Path(str(path)).resolve()
    s = str(p)
    if s.startswith("-"):
        raise ValueError(f"refusing option-like media path: {s}")
    return s


class ProbeResult:
    """ffprobe result: video stream + container info."""

    def __init__(self, codec=None, width=0, height=0, fps=None,
                 duration=0.0, size_bytes=0, audio_codec=None):
        self.codec = codec
        self.width = width
        self.height = height
        self.fps = fps
        self.duration = duration
        self.size_bytes = size_bytes
        self.audio_codec = audio_codec

    def to_dict(self) -> dict:
        return {
            "codec": self.codec, "width": self.width, "height": self.height,
            "fps": self.fps, "duration": self.duration,
            "size_bytes": self.size_bytes, "audio_codec": self.audio_codec,
        }


class FFmpegEngine:
    """High-level video post-production engine using OpenMontage tools."""

    VALID_TRANSITIONS = frozenset({
        "cut", "crossfade", "fade",  # "fade" = fade-through-black
    })

    def __init__(
        self,
        preset: str = "veryfast",
        crf: int = 18,
        fps: int = 24,
        work_dir: Optional[Path] = None,
    ):
        self.preset = preset
        self.crf = crf
        self.fps = fps
        self.work_dir = Path(work_dir) if work_dir else Path.cwd()
        self._stitcher: Optional[Any] = None
        self._mixer: Optional[Any] = None

    def _get_stitcher(self):
        """Lazy-load VideoStitch tool."""
        if not _OM_AVAILABLE:
            raise RuntimeError("OpenMontage not available — install at " + str(_OM_ROOT))
        if self._stitcher is None:
            self._stitcher = VideoStitch()
        return self._stitcher

    def _get_mixer(self):
        """Lazy-load AudioMixer tool."""
        if not _OM_AVAILABLE:
            raise RuntimeError("OpenMontage not available")
        if self._mixer is None:
            self._mixer = AudioMixer()
        return self._mixer

    # ── Video stitching ──────────────────────────────────────────

    def concat(
        self,
        clips: list[Path],
        output: Path,
        transition: str = "cut",
    ) -> dict:
        """Concatenate clips (hard cut, no transition)."""
        return self.stitch(clips, output, transition=transition)

    def stitch(
        self,
        clips: list[Path],
        output: Path,
        transition: str = "cut",
        transition_duration: float = 0.8,
        auto_normalize: bool = True,
    ) -> dict:
        """Stitch clips with optional transition via OpenMontage VideoStitch."""
        if transition not in self.VALID_TRANSITIONS:
            return {"ok": False, "error": f"invalid transition: {transition}"}

        stitcher = self._get_stitcher()
        result = stitcher.execute({
            "operation": "stitch",
            "clips": [str(c) for c in clips],
            "output_path": str(output),
            "transition": transition,
            "transition_duration": transition_duration,
            "auto_normalize": auto_normalize,
            "crf": self.crf,
            "preset": self.preset,
        })
        return {
            "ok": result.success,
            "output": str(output),
            "data": result.data if hasattr(result, "data") else None,
            "error": getattr(result, "error", None),
            "duration": getattr(result, "duration_seconds", None),
        }

    def dry_run_stitch(self, clips: list[Path], output: Path,
                       transition: str = "cut") -> dict:
        """Dry-run stitch to preview without rendering."""
        stitcher = self._get_stitcher()
        result = stitcher.execute({
            "operation": "stitch",
            "clips": [str(c) for c in clips],
            "output_path": str(output),
            "transition": transition,
            "dry_run": True,
        })
        return {"ok": result.success, "data": result.data}

    # Alias used by the CLI (`shipin stitch`); same contract as stitch().
    def xfade_chain(self, clips: list[Path], output: Path,
                    transition: str = "cut", duration: float = 0.8) -> dict:
        return self.stitch(clips, output, transition=transition,
                           transition_duration=duration)

    # ── Probe / analysis (direct FFmpeg, no OpenMontage needed) ──

    @staticmethod
    def probe(path: Path) -> ProbeResult:
        """ffprobe a media file into a ProbeResult."""
        import json as _json
        import subprocess
        src = _media_arg(path)
        r = subprocess.run([
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,width,height,r_frame_rate",
            "-show_entries", "format=duration,size",
            "-of", "json", src,
        ], capture_output=True, text=True, shell=False)
        if r.returncode != 0:
            raise RuntimeError(f"ffprobe failed: {r.stderr[-500:]}")
        data = _json.loads(r.stdout)
        streams = data.get("streams", [])
        v = streams[0] if streams else {}
        audio_codec = None
        r2 = subprocess.run([
            "ffprobe", "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=codec_name", "-of", "json", src,
        ], capture_output=True, text=True, shell=False)
        try:
            a_streams = _json.loads(r2.stdout).get("streams", [])
            audio_codec = a_streams[0].get("codec_name") if a_streams else None
        except _json.JSONDecodeError:
            pass
        return ProbeResult(
            codec=v.get("codec_name"),
            width=v.get("width", 0),
            height=v.get("height", 0),
            fps=v.get("r_frame_rate"),
            duration=float(data.get("format", {}).get("duration", 0) or 0),
            size_bytes=int(data.get("format", {}).get("size", 0) or 0),
            audio_codec=audio_codec,
        )

    @staticmethod
    def black_detect(path: Path, min_dur: float = 0.5) -> list[dict]:
        """Detect black segments. Returns [{"start", "end"}, ...]."""
        import re as _re
        import subprocess
        from shipin_platform.analysis.reference_profiler import blackdetect_pix_arg
        src = _media_arg(path)
        pix_arg = blackdetect_pix_arg()
        r = subprocess.run([
            "ffmpeg", "-i", src,
            "-vf", f"blackdetect=d={min_dur}:{pix_arg}=0.01",
            "-f", "null", "-",
        ], capture_output=True, text=True, shell=False)
        frames = []
        for line in (r.stdout + r.stderr).split("\n"):
            if "blackdetect" in line:
                m = _re.search(r"black_start:([0-9.]+) black_end:([0-9.]+)", line)
                if m:
                    frames.append({"start": float(m.group(1)),
                                   "end": float(m.group(2))})
        return frames

    # ── Subtitle burning ─────────────────────────────────────────

    @staticmethod
    def burn_srt(
        video: Path,
        srt: Path,
        output: Path,
        font_name: str = "Microsoft YaHei",
        font_size: int = 46,
        margin_v: int = 96,
        alignment: int = 2,
    ) -> dict:
        """Burn an SRT into video.

        Delegates to subtitle_renderer.render_subtitles_best, which probes the
        host libass capability once and uses the per-line drawtext fallback
        where subtitles= silently paints nothing (documented on this machine),
        then returns per-cue measurements for the reviewer."""
        from shipin_platform.tools.subtitle_renderer import render_subtitles_best
        try:
            result = render_subtitles_best(
                video, srt, output,
                font_size=font_size, margin_v=margin_v,
            )
        except FileNotFoundError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            return {"ok": False, "error": f"burn failed: {e}"}
        return result

    # ── Audio mixing ─────────────────────────────────────────────

    def mix_audio(
        self,
        tracks: list[dict],
        output: Path,
        normalize: bool = True,
        ducking: bool = False,
        target_lufs: float = -14.0,
    ) -> dict:
        """Mix audio tracks via OpenMontage AudioMixer.

        Each track: {"path": str, "role": "narration"|"music"|"sfx", "volume": float}
        """
        mixer = self._get_mixer()
        result = mixer.execute({
            "operation": "mix",
            "tracks": tracks,
            "output_path": str(output),
            "normalize": normalize,
            "ducking": ducking,
        })
        return {
            "ok": result.success,
            "output": str(output),
            "data": result.data if hasattr(result, "data") else None,
            "error": getattr(result, "error", None),
        }

    def duck_audio(
        self,
        primary: Path,
        secondary: Path,
        output: Path,
        duck_level: float = -12.0,
    ) -> dict:
        """Duck secondary audio under primary (BGM under narration).

        Uses OpenMontage simple format: duck_level is dB (negative).
        """
        mixer = self._get_mixer()
        result = mixer.execute({
            "operation": "duck",
            "primary_audio": str(primary),
            "secondary_audio": str(secondary),
            "output_path": str(output),
            "duck_level": duck_level,
        })
        return {"ok": result.success, "error": getattr(result, "error", None)}

    # ── Quality checks (re-exported from OpenMontage lib) ────────

    @staticmethod
    def slideshow_risk(scenes: list[dict]) -> dict:
        """Score slideshow risk (6 dimensions)."""
        if not _OM_AVAILABLE:
            raise RuntimeError("OpenMontage lib not available")
        return score_slideshow_risk(scenes)

    @staticmethod
    def variation_check(scenes: list[dict]) -> dict:
        """Check scene variation (8 items)."""
        if not _OM_AVAILABLE:
            raise RuntimeError("OpenMontage lib not available")
        return check_scene_variation(scenes)
