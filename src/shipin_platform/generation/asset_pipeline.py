"""Provider-neutral generation pipeline + deterministic asset gates.

This module does NOT talk to any model service.  It records what a provider
*would* generate (DryRunProvider), applies a retry policy, and vets every
produced file with ffprobe-based checks.  Real providers (ComfyUI, external
APIs) implement the same interface; the asset gate is the same for all.
"""
from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GenerationRequest:
    shot_id: str
    prompt_text: str
    kind: str = "video"                          # image | video
    duration_sec: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    seed: Optional[int] = None
    parent_hash: str = ""

    def __post_init__(self) -> None:
        if not self.shot_id or not self.shot_id.strip():
            raise ValueError("shot_id must be non-empty")
        if not self.prompt_text or not self.prompt_text.strip():
            raise ValueError("prompt_text must be non-empty")
        if self.kind not in ("image", "video"):
            raise ValueError(f"kind must be image|video, got {self.kind!r}")


@dataclass(frozen=True)
class GenerationCandidate:
    shot_id: str
    path: str
    request: GenerationRequest
    candidate_index: int = 0
    created_at: float = field(default_factory=time.time)

    @property
    def candidate_id(self) -> str:
        return f"{self.shot_id}#{self.candidate_index}"


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_sec: float = 2.0
    max_delay_sec: float = 30.0

    def delay_for(self, attempt: int) -> float:
        exp = self.base_delay_sec * (2 ** max(0, attempt - 1))
        return min(exp, self.max_delay_sec)


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

class GenerationProvider:
    """Interface every provider must implement (submit → candidate)."""

    name = "base"

    def generate(self, request: GenerationRequest,
                 candidate_index: int) -> GenerationCandidate:
        raise NotImplementedError


class DryRunProvider(GenerationProvider):
    """Offline provider: writes a JSON stub instead of calling a model.

    Useful for wiring the pipeline end-to-end before real generation
    backends (ComfyUI etc.) are attached, and for unit tests.
    """

    name = "dry_run"

    def __init__(self, output_dir: str | Path):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate(self, request: GenerationRequest,
                 candidate_index: int) -> GenerationCandidate:
        out = self.output_dir / f"{request.shot_id}_c{candidate_index}.json"
        payload = {
            "shot_id": request.shot_id,
            "kind": request.kind,
            "prompt_text": request.prompt_text,
            "duration_sec": request.duration_sec,
            "seed": request.seed,
            "provider": self.name,
            "note": "dry-run stub; replace with a real provider for production",
        }
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        return GenerationCandidate(
            shot_id=request.shot_id, path=str(out),
            request=request, candidate_index=candidate_index)


# ---------------------------------------------------------------------------
# Asset gate (ffprobe-based; deterministic)
# ---------------------------------------------------------------------------

@dataclass
class AssetGateResult:
    shot_id: str
    path: str
    ok: bool
    codes: list = field(default_factory=list)     # FILE_MISSING / UNREADABLE / ...

    def to_dict(self) -> dict:
        return {"shot_id": self.shot_id, "path": self.path,
                "ok": self.ok, "codes": self.codes}


def check_asset(path: str, shot_id: str = "",
                expected_duration: Optional[float] = None,
                expected_width: Optional[int] = None,
                expected_height: Optional[int] = None,
                duration_tolerance: float = 0.5) -> AssetGateResult:
    """Vet a produced asset file; never silently pass on failure."""
    codes: list = []
    p = Path(path)
    if not p.exists():
        return AssetGateResult(shot_id, path, ok=False,
                               codes=["FILE_MISSING"])
    if p.stat().st_size == 0:
        return AssetGateResult(shot_id, path, ok=False,
                               codes=["FILE_EMPTY"])

    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_format", "-show_streams", str(p)],
        capture_output=True, text=True)
    try:
        info = json.loads(probe.stdout)
    except json.JSONDecodeError:
        return AssetGateResult(shot_id, path, ok=False,
                               codes=["UNREADABLE"])

    streams = info.get("streams", [])
    if not streams:
        return AssetGateResult(shot_id, path, ok=False,
                               codes=["NO_STREAMS"])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        codes.append("NO_VIDEO_STREAM")

    fmt = info.get("format", {})
    duration = fmt.get("duration")
    try:
        duration = float(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration = None
    if duration is None:
        codes.append("NO_DURATION")
    elif expected_duration is not None and abs(duration - expected_duration) > duration_tolerance:
        codes.append("DURATION_MISMATCH")

    if video is not None and expected_width is not None:
        if int(video.get("width", 0)) != expected_width:
            codes.append("RESOLUTION_MISMATCH")
    if video is not None and expected_height is not None:
        if int(video.get("height", 0)) != expected_height:
            codes.append("RESOLUTION_MISMATCH")

    return AssetGateResult(shot_id, path, ok=not codes, codes=codes)


# ---------------------------------------------------------------------------
# Retry loop
# ---------------------------------------------------------------------------

def generate_with_retry(provider: GenerationProvider,
                        request: GenerationRequest,
                        policy: Optional[RetryPolicy] = None,
                        validator=None) -> GenerationCandidate:
    """Generate candidates until one passes the validator or budget runs out.

    ``validator`` receives a GenerationCandidate and returns AssetGateResult
    (or any object with .ok).  Raises RuntimeError after the final attempt.
    """
    pol = policy or RetryPolicy()
    last_result = None
    for attempt in range(1, pol.max_attempts + 1):
        cand = provider.generate(request, candidate_index=attempt - 1)
        if validator is None:
            return cand
        last_result = validator(cand)
        if getattr(last_result, "ok", False):
            return cand
        if attempt < pol.max_attempts:
            time.sleep(pol.delay_for(attempt))
    raise RuntimeError(
        f"generation for shot {request.shot_id} failed after "
        f"{pol.max_attempts} attempts: {getattr(last_result, 'codes', 'unknown')}")
