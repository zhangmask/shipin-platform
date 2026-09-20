"""OpenMontage library integrations — re-export key modules.

Finds the OpenMontage root by searching upward from this file.
"""
from __future__ import annotations

import sys
from pathlib import Path


def _find_openmontage_root() -> Path | None:
    """Search upward for OpenMontage-main/OpenMontage-main."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "OpenMontage-main" / "OpenMontage-main"
        if (candidate / "tools").is_dir():
            return candidate
        candidate2 = parent / "OpenMontage-main"
        if (candidate2 / "tools").is_dir():
            return candidate2
    return None


_OM_ROOT = _find_openmontage_root()
if _OM_ROOT:
    sys.path.insert(0, str(_OM_ROOT))

try:
    from lib.slideshow_risk import score_slideshow_risk  # noqa: F401
    from lib.variation_checker import check_scene_variation  # noqa: F401
    from lib.scoring import ProviderScore, ProductionPathScore  # noqa: F401
    from tools.video.video_stitch import VideoStitch  # noqa: F401
    from tools.audio.audio_mixer import AudioMixer  # noqa: F401
    from tools.video.video_compose import VideoCompose  # noqa: F401
    from tools.subtitle.subtitle_gen import SubtitleGen  # noqa: F401
    from tools.enhancement.color_grade import ColorGrade  # noqa: F401
    from tools.analysis.frame_sampler import FrameSampler  # noqa: F401
    OM_AVAILABLE = True
    __all__ = [
        "OM_AVAILABLE",
        "score_slideshow_risk",
        "check_scene_variation",
        "ProviderScore",
        "ProductionPathScore",
        "VideoStitch",
        "AudioMixer",
        "VideoCompose",
        "SubtitleGen",
        "ColorGrade",
        "FrameSampler",
    ]
except ImportError as e:
    print(f"OpenMontage import warning: {e}", file=sys.stderr)
    # 降级：平台自带同名实现（review 包内部持有相同算法），
    # 保证 tools/__init__.py 的 re-export 在无 OpenMontage 环境依旧可用；
    # 占位 scoring 类型仅作兼容导出，业务侧未消费。
    from shipin_platform.review.slideshow_risk import (  # noqa: E402
        score_slideshow_risk)
    from shipin_platform.review.variation_checker import (  # noqa: E402
        check_scene_variation)

    class ProviderScore:
        def __init__(self, *a, **k):
            raise NotImplementedError("OpenMontage 未安装")

    class ProductionPathScore:
        def __init__(self, *a, **k):
            raise NotImplementedError("OpenMontage 未安装")

    OM_AVAILABLE = False
    __all__ = [
        "OM_AVAILABLE", "score_slideshow_risk", "check_scene_variation",
        "ProviderScore", "ProductionPathScore",
    ]
