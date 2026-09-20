"""Hypit estimate 移植单测:语言检测 / 单位计数 / 时长估算 / 预算。

参照 @hypit/estimate 的确定性语义(中文按字、嵌入英文按音节、启发式
英文音节、PACE_RATE 密度表、rounding/padding)。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from shipin_platform.services.estimate import (  # noqa: E402
    NARRATION_RATE_ZH,
    PACE_RATE,
    count_speech_units,
    detect_language,
    estimate_speech_duration,
    narration_char_budget,
)


class TestDetectLanguage:
    def test_zh(self):
        assert detect_language("今天天气真不错,我们出发吧") == "zh"

    def test_ja(self):
        assert detect_language("こんにちは、世界") == "ja"

    def test_en(self):
        assert detect_language("Video editing begins with meaning.") == "en"

    def test_zh_with_english_words(self):
        # 汉字占多数时仍判 zh,嵌入式英文按音节计
        assert detect_language("今天 Meeting 很重要") == "zh"

    def test_spanish_hints(self):
        assert detect_language("¿Dónde está la biblioteca por favor?") in ("es", "en")


class TestCountUnits:
    def test_zh_counts_han_characters(self):
        assert count_speech_units("今天出发", "zh") == 4

    def test_zh_embedded_english_uses_syllables(self):
        # 今天(2)+很顺利(3)=5 汉字,Network(2 音节) → 7
        assert count_speech_units("今天 Network 很顺利", "zh") == 5 + 2

    def test_en_counts_word_syllables(self):
        # meeting=2, editing=2, meaning=2, video=2
        assert count_speech_units("meeting editing meaning video", "en") in (8, 9)

    def test_punctuation_not_counted_zh(self):
        assert count_speech_units("你好。世界!", "zh") == 4

    def test_empty(self):
        assert count_speech_units("", "zh") == 0


class TestEstimateDuration:
    def test_zh_normal_pace_matches_table(self):
        # 4 汉字 / 5.25 字每秒 ≈ 0.762
        d = estimate_speech_duration("今天天气", language="zh", pace="normal")
        assert d == pytest.approx(4 / PACE_RATE["zh"]["normal"])

    def test_explicit_rate_wins_over_pace(self):
        d = estimate_speech_duration("今天天气", language="zh", rate=2.0)
        assert d == pytest.approx(2.0)

    def test_padding(self):
        d = estimate_speech_duration("今天天气", language="zh", rate=4.0,
                                     padding_sec=0.5)
        assert d == pytest.approx(1.5)

    def test_rounding_ceil(self):
        assert estimate_speech_duration("你好", language="zh", rate=3.0,
                                        rounding="ceil") == 1.0

    def test_empty_text_zero(self):
        assert estimate_speech_duration("   ", language="zh") == 0.0

    def test_auto_language_zh(self):
        d = estimate_speech_duration("今天天气不错")  # 自动判 zh
        assert d == pytest.approx(
            count_speech_units("今天天气不错", "zh") / PACE_RATE["zh"]["normal"])


class TestNarrationBudget:
    def test_rate_value_is_160_char_per_minute(self):
        # 160 字/分钟 ≈ 2.667 字/秒;2.67 是沿用 engine 原字面值的两位小数
        assert NARRATION_RATE_ZH == pytest.approx(160 / 60, abs=0.01)

    def test_budget_floor_matches_engine(self):
        # engine:746 原 max_words = duration * 2.67, int(max_words) 展示
        assert narration_char_budget(60.0) == int(60.0 * NARRATION_RATE_ZH)
        assert narration_char_budget(20.0) == 53
        assert narration_char_budget(0) == 0