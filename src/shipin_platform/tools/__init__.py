"""Tools package — OpenMontage integrations, Whisper, review engine.

重依赖（whisper 等）采用懒加载：包导入不再被可选依赖卡住，
实际使用 WhisperService 时才 import。这与 pyproject 的可选依赖定位一致。
"""
from shipin_platform.tools.ffmpeg_engine import FFmpegEngine
from shipin_platform.tools.om_lib import (
    score_slideshow_risk,
    check_scene_variation,
    ProviderScore,
    ProductionPathScore,
)


def __getattr__(name: str):
    # PEP 562 模块级懒加载：WhisperService 需要 openai-whisper（可选依赖）
    if name == "WhisperService":
        from shipin_platform.tools.whisper_service import WhisperService
        return WhisperService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "FFmpegEngine",
    "WhisperService",
    "score_slideshow_risk",
    "check_scene_variation",
    "ProviderScore",
    "ProductionPathScore",
]
