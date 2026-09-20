"""Stage contracts: frozen dataclasses + hash + cross-stage binding.

Every pipeline stage must produce one of these contracts (or a dict that
validates against one).  The contracts are deliberately stdlib-only so the
review engine, API layer and generation adapters can all import them without
extra dependencies.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

def stable_artifact_hash(data: Any) -> str:
    """Deterministic sha256 for any JSON-serializable stage payload."""
    payload = json.dumps(
        data, ensure_ascii=False, sort_keys=True,
        default=str, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------
# Field helpers
# ---------------------------------------------------------------------------

def _req_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _req_positive(value: Any, name: str) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a positive number, got {value!r}")
    if num <= 0:
        raise ValueError(f"{name} must be > 0, got {num}")
    return num


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ShotContract:
    """One shot in script/storyboard; beats drive the five-act arc."""
    shot_id: str
    duration_sec: float
    beat: str                                    # 钩子/痛点/转折/延展/收束
    cause: str = ""                              # why this beat happens
    effect: str = ""                             # what changes for the next beat
    subject: str = ""
    motion: str = ""
    scene: str = ""
    camera: str = ""

    def __post_init__(self) -> None:
        _req_text(self.shot_id, "shot_id")
        _req_positive(self.duration_sec, "duration_sec")
        _req_text(self.beat, "beat")


@dataclass(frozen=True)
class BriefContract:
    content_type: str
    duration_sec: float
    tone: str = ""
    target_platform: str = ""

    def __post_init__(self) -> None:
        _req_text(self.content_type, "content_type")
        _req_positive(self.duration_sec, "duration_sec")


@dataclass(frozen=True)
class ScriptContract:
    shots: tuple
    duration_sec: float

    def __post_init__(self) -> None:
        if not self.shots:
            raise ValueError("script.shots must not be empty")
        _req_positive(self.duration_sec, "duration_sec")
        _unique_shot_ids(self.shots)


@dataclass(frozen=True)
class StoryboardContract:
    shots: tuple
    duration_sec: float

    def __post_init__(self) -> None:
        if not self.shots:
            raise ValueError("storyboard.shots must not be empty")
        _req_positive(self.duration_sec, "duration_sec")
        _unique_shot_ids(self.shots)


@dataclass(frozen=True)
class PromptContract:
    shot_id: str
    prompt_text: str
    parent_hash: str                             # storyboard artifact hash
    kind: str = "video"                          # image | video

    def __post_init__(self) -> None:
        _req_text(self.shot_id, "shot_id")
        _req_text(self.prompt_text, "prompt_text")
        _req_text(self.parent_hash, "parent_hash")
        if self.kind not in ("image", "video"):
            raise ValueError(f"kind must be image|video, got {self.kind!r}")


@dataclass(frozen=True)
class AssetContract:
    shot_id: str
    path: str
    parent_hash: str                             # prompt artifact hash
    candidate_index: int = 0

    def __post_init__(self) -> None:
        _req_text(self.shot_id, "shot_id")
        _req_text(self.path, "path")
        _req_text(self.parent_hash, "parent_hash")
        if self.candidate_index < 0:
            raise ValueError("candidate_index must be >= 0")


@dataclass(frozen=True)
class TimelineContract:
    clips: tuple                                 # tuple of dicts {shot_id, src, start, end, at}
    duration_sec: float

    def __post_init__(self) -> None:
        if not self.clips:
            raise ValueError("timeline.clips must not be empty")
        _req_positive(self.duration_sec, "duration_sec")
        ids = []
        for clip in self.clips:
            if not isinstance(clip, dict):
                raise ValueError("each timeline clip must be a dict")
            ids.append(_req_text(str(clip.get("shot_id", "")), "clip.shot_id"))
        if len(ids) != len(set(ids)):
            raise ValueError("timeline shot_id values must be unique within a cut")


def _unique_shot_ids(items: Iterable) -> None:
    ids = [getattr(s, "shot_id", None) or (s.get("shot_id") if isinstance(s, dict) else None)
           for s in items]
    if any(i is None for i in ids):
        raise ValueError("every shot must carry a shot_id")
    if len(ids) != len(set(ids)):
        raise ValueError("shot_id values must be unique within a stage")


# ---------------------------------------------------------------------------
# Cross-stage binding
# ---------------------------------------------------------------------------

def _collect_shot_ids(value: Any) -> set:
    """Extract shot ids from a contract, dict, or raw list of shots."""
    if value is None:
        return set()
    if hasattr(value, "shots"):
        value = value.shots
    elif isinstance(value, dict):
        value = value.get("shots", value.get("clips", value.get("shot_prompts", [])))
    if not isinstance(value, (list, tuple)):
        return set()
    ids = set()
    for item in value:
        if isinstance(item, dict):
            sid = item.get("shot_id") or item.get("id")
        else:
            sid = getattr(item, "shot_id", None)
        if sid:
            ids.add(str(sid))
    return ids


def validate_stage_binding(parent: Any, child: Any) -> dict:
    """Verify the child stage preserves the parent's shot identity set.

    Returns {"ok": bool, "missing": [...], "extra": [...]}.
    Raises ValueError when either side is structurally unusable.
    """
    parent_ids = _collect_shot_ids(parent)
    child_ids = _collect_shot_ids(child)
    if not parent_ids:
        raise ValueError("parent stage carries no shot ids — cannot bind")
    if not child_ids:
        raise ValueError("child stage carries no shot ids — cannot bind")
    missing = sorted(parent_ids - child_ids)
    extra = sorted(child_ids - parent_ids)
    return {"ok": not missing and not extra, "missing": missing, "extra": extra}


# ---------------------------------------------------------------------------
# Prompt compiler (deterministic, no LLM)
# ---------------------------------------------------------------------------

def compile_prompt_from_shot(shot: Any, style_anchor: str = "",
                             negative_prompt: str = "") -> dict:
    """Compile an image/video prompt deterministically from a locked shot.

    This is the single source of prompt truth once a storyboard passes review:
    agents must NOT freehand prompts for downstream generation.
    """
    if isinstance(shot, dict):
        sid = shot.get("shot_id", "")
        parts = [shot.get("subject", ""), shot.get("motion", ""),
                 shot.get("scene", ""), shot.get("spatial", ""),
                 shot.get("camera", ""), shot.get("lighting", "")]
    else:
        sid = getattr(shot, "shot_id", "")
        parts = [getattr(shot, a, "") or "" for a in
                 ("subject", "motion", "scene", "spatial", "camera", "lighting")]
    _req_text(str(sid), "shot_id")
    body = ", ".join(str(p).strip() for p in parts if p and str(p).strip())
    if not body:
        raise ValueError(f"shot {sid} has no visual fields to compile into a prompt")
    if style_anchor:
        body = f"{body}, {style_anchor}"
    return {
        "shot_id": str(sid),
        "prompt_text": body,
        "negative_prompt": negative_prompt,
        "compiled": True,
    }
