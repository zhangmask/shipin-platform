"""AI Video Generation Platform — shipin-platform."""

__version__ = "0.1.0"

from shipin_platform.config import load_config, PlatformConfig
from shipin_platform.tools import FFmpegEngine
from shipin_platform.review import ReviewEngine
from shipin_platform.workflows import Pipeline, create_pipeline


def __getattr__(name: str):
    # PEP 562：WhisperService 依赖可选的 openai-whisper，用到时才加载
    if name == "WhisperService":
        from shipin_platform.tools import WhisperService
        return WhisperService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "load_config",
    "PlatformConfig",
    "FFmpegEngine",
    "WhisperService",
    "ReviewEngine",
    "Pipeline",
    "create_pipeline",
]
