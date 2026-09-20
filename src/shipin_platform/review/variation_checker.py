"""Scene variation checker — adapted from OpenMontage lib/variation_checker.py."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any


GENERIC_PHRASES = {
    "scene", "shot", "camera", "lighting", "angle", "framing",
    "medium", "close-up", "wide", "extreme close-up", "cowboy shot",
}


@dataclass
class VariationResult:
    score: float
    verdict: str
    violations: list[str]
    suggestions: list[str]


def check_scene_variation(scenes: list[dict]) -> dict:
    """Check scene variation across shots.

    Each scene dict should have shot info with:
      - shot_size: str
      - camera: str
      - lighting: str
      - scene_id: str
    """
    if not scenes:
        return {"score": 0.0, "verdict": "strong", "violations": [], "suggestions": []}

    violations = []
    suggestions = []

    # Check 1: Too many medium shots
    medium_count = sum(1 for s in scenes if s.get("shot_size", "").lower() in ("medium", "medium shot"))
    medium_ratio = medium_count / len(scenes)
    if medium_ratio > 0.5:
        violations.append(f"{medium_count}/{len(scenes)} shots are medium — exceeds 50% threshold")
        suggestions.append("Diversify shot sizes: mix ECU/MCU/WS with medium")

    # Check 2: Consecutive same shot size
    for i in range(2, len(scenes)):
        if (scenes[i].get("shot_size") == scenes[i - 1].get("shot_size") == scenes[i - 2].get("shot_size")
                and scenes[i].get("shot_size")):
            violations.append(f"3 consecutive shots with same size at positions {i-2},{i-1},{i}")
            suggestions.append("Alternate shot sizes to maintain visual interest")

    # Check 3: Static shot ratio
    static_count = sum(1 for s in scenes
                       if "static" in s.get("camera", "").lower()
                       or "holding" in s.get("motion", "").lower())
    static_ratio = static_count / len(scenes)
    if static_ratio > 0.6:
        violations.append(f"{static_ratio:.0%} shots are static — exceeds 60% threshold")
        suggestions.append("Add subtle camera movement to static shots")

    # Check 4: Lighting consistency within scenes
    scene_lighting: dict[str, list[str]] = {}
    for s in scenes:
        sid = s.get("scene_id", "unknown")
        light = s.get("lighting", "")
        if light:
            scene_lighting.setdefault(sid, []).append(light)
    for sid, lights in scene_lighting.items():
        if len(set(lights)) > 2:
            suggestions.append(f"Scene {sid} has inconsistent lighting: {lights}")

    score = len(violations) * 1.0 + len(suggestions) * 0.5
    if score < 2.0:
        verdict = "strong"
    elif score < 3.0:
        verdict = "acceptable"
    elif score < 4.0:
        verdict = "revise"
    else:
        verdict = "fail"

    return {"score": score, "verdict": verdict, "violations": violations, "suggestions": suggestions}
