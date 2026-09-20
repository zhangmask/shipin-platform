"""Slideshow risk scoring — adapted from OpenMontage lib/slideshow_risk.py."""
# Re-implemented without external dependencies for standalone use.
from __future__ import annotations
from dataclasses import dataclass
from typing import Any


@dataclass
class SlideshowRisk:
    """Result of slideshow risk assessment."""
    average: float
    verdict: str  # strong / acceptable / revise / fail
    dimensions: dict[str, float]


def score_slideshow_risk(scenes: list[dict]) -> dict:
    """Score slideshow risk across scenes.

    Each scene dict should have:
      - shot_size: str (ecuC/ECU/MCU/Medium/Cowboy/WS)
      - motion_type: str (static/dolly/pan/etc.)
      - duration_sec: float
    """
    if not scenes:
        return {"average": 0.0, "verdict": "strong", "dimensions": {}}

    n = len(scenes)
    dims = {
        "repetition": _score_repetition(scenes),
        "weak_motion": _score_weak_motion(scenes),
        "static_pace": _score_static_pace(scenes),
        "no_variation": _score_variation(scenes),
        "text_heavy": 0.0,  # TODO: detect overlay-heavy shots
        "predictable": 0.0,  # TODO: detect predictable patterns
    }
    avg = sum(dims.values()) / len(dims)
    if avg < 2.0:
        verdict = "strong"
    elif avg < 3.0:
        verdict = "acceptable"
    elif avg < 4.0:
        verdict = "revise"
    else:
        verdict = "fail"
    return {"average": avg, "verdict": verdict, "dimensions": dims}


def _score_repetition(scenes: list[dict]) -> float:
    """Score based on repeated shot sizes in adjacent shots."""
    if len(scenes) < 2:
        return 0.0
    violations = 0
    for i in range(1, len(scenes)):
        s1 = scenes[i - 1].get("shot_size", "")
        s2 = scenes[i].get("shot_size", "")
        if s1 and s2 and s1 == s2:
            violations += 1
    return violations / max(len(scenes) - 1, 1) * 5.0  # Scale to 0-5


def _score_weak_motion(scenes: list[dict]) -> float:
    """Score based on static shots lacking motion description."""
    static_count = 0
    for s in scenes:
        motion = s.get("motion", "").lower()
        if "static" in motion or "holding" in motion or not motion:
            static_count += 1
    return (static_count / max(len(scenes), 1)) * 5.0


def _score_static_pace(scenes: list[dict]) -> float:
    """Score based on average shot duration being too long."""
    if not scenes:
        return 0.0
    durations = [s.get("duration_sec", 0) for s in scenes]
    avg_dur = sum(durations) / max(len(durations), 1)
    # Long average duration = slower pace = more slideshow-like
    return min(avg_dur / 5.0, 5.0)


def _score_variation(scenes: list[dict]) -> float:
    """Score based on lack of variety in shot sizes."""
    if len(scenes) < 2:
        return 0.0
    sizes = [s.get("shot_size", "") for s in scenes if s.get("shot_size")]
    unique = len(set(sizes))
    # More unique sizes = better variation = lower score
    return max(0, (len(set(sizes)) / max(len(sizes), 1)) * -5 + 5)
