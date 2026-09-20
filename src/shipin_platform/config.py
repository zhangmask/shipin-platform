"""Platform configuration management."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class ModelConfig:
    """Video/image generation model provider config."""
    provider: str  # "openai", "gemini", "seedance", "kling", "hunyuan", "ltx"
    api_key_env: str
    base_url: str = ""
    default_model: str = ""
    cost_per_image: float = 0.0
    cost_per_video_sec: float = 0.0


@dataclass
class WhisperConfig:
    """Whisper ASR config."""
    model: str = "medium"  # tiny/base/small/medium/large/turbo
    device: str = "cpu"
    language: str = "zh"
    word_timestamps: bool = True
    output_formats: tuple[str, ...] = ("srt", "json")


@dataclass
class FFmpegConfig:
    """FFmpeg settings."""
    preset: str = "veryfast"
    crf: int = 18
    fps: int = 24
    width: int = 1088
    height: int = 1920
    audio_sample_rate: int = 48000
    audio_bitrate: str = "192k"
    # Ducking defaults
    duck_threshold: float = 0.02
    duck_ratio: float = 9.0
    duck_attack_ms: float = 200.0
    duck_release_ms: float = 500.0
    # Loudness targets
    loudness_target_lufs: float = -14.0
    loudness_tp_dbtp: float = -1.5


@dataclass
class ReviewConfig:
    """Review engine config."""
    max_rounds: dict[str, int] = field(default_factory=lambda: {
        "brief": 2,
        "script": 3,
        "storyboard": 3,
        "image_prompt": 3,
        "image_gen": 2,
        "video_prompt": 3,
        "video_gen": 1,
        "post_production": 2,
    })
    continue_threshold: float = 0.1
    pass_threshold: float = 0.85
    suggestion_threshold: float = 0.70


@dataclass
class PlatformConfig:
    """Top-level platform configuration."""
    project_root: Path = field(default_factory=Path.cwd)
    openmontage_root: Path = field(
        # src/shipin_platform/config.py → parents[3] is the workspace root
        # (D:/aishipin) where the OpenMontage repo lives as
        # OpenMontage-main/OpenMontage-main.
        default_factory=lambda: Path(__file__).resolve().parents[3] / "OpenMontage-main" / "OpenMontage-main"
    )
    models: dict[str, ModelConfig] = field(default_factory=dict)
    whisper: WhisperConfig = field(default_factory=WhisperConfig)
    ffmpeg: FFmpegConfig = field(default_factory=FFmpegConfig)
    review: ReviewConfig = field(default_factory=ReviewConfig)

    def model(self, name: str) -> Optional[ModelConfig]:
        return self.models.get(name)

    def set_model(self, name: str, config: ModelConfig) -> None:
        self.models[name] = config


def load_config(config_path: Optional[Path] = None) -> PlatformConfig:
    """Load platform configuration from YAML or defaults."""
    import yaml
    cfg = PlatformConfig()

    if config_path and config_path.exists():
        with open(config_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if "models" in data:
            for name, mc in data["models"].items():
                cfg.set_model(name, ModelConfig(**mc))
        if "whisper" in data:
            cfg.whisper = WhisperConfig(**data["whisper"])
        if "ffmpeg" in data:
            cfg.ffmpeg = FFmpegConfig(**data["ffmpeg"])
        if "review" in data:
            cfg.review = ReviewConfig(**data["review"])

    return cfg
