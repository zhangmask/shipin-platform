# 确定性的语音时长估算(移植自 @hypit/estimate 的 speech-duration 逻辑)。
#
# hypit 的 estimate 包:创作期用语言感知的密语单位计数(汉字/音节)
# 加 PACE_RATE 密度表(单位/秒,含普通停顿)算时长——纯逻辑、无供应商、
# 无外部调用,文本永远只做确定性计数,不发明数字/名字/缩写的发音。
# 本模块是等价的 Python 落地:
#
#   detect_language(text)        汉字/假名/拉丁 比例 + 西语特征词 → zh/ja/en/es
#   count_speech_units(text)     单位数:zh 按汉字 + 嵌入英文按音节;en 按音节
#   estimate_speech_duration()   units / rate(+padding,可选 ceil/round)
#
# 平台实测密度 NARRATION_RATE_ZH(≈160 字/分钟,TTS 叙述语速)是审查层
# 字数预算的唯一定源;之前 engine.py 里散落的 2.67/2.7 硬编码全部
# 收敛到这一个常量。
from __future__ import annotations

import math
import re
from typing import Optional

# hypit PACE_RATE 原值:创作语速密度(单位/秒,含普通停顿)。
PACE_RATE: dict[str, dict[str, float]] = {
    "en": {"slow": 4.2, "normal": 4.6, "fast": 5.6},
    "zh": {"slow": 4.2, "normal": 5.25, "fast": 6.5625},
    "ja": {"slow": 6.0, "normal": 7.5, "fast": 9.375},
    "es": {"slow": 4.72, "normal": 5.9, "fast": 7.375},
}

# 平台生产语速实测(TTS 中文叙述,≈160 字/分钟)。
# 注意:生产语速比创作密度(5.25 字/秒)慢,以实测为准。
NARRATION_RATE_ZH: float = 2.67

_WORD_RE = re.compile(r"[^\W_]+(?:['\u2019-][^\W_]+)*",
                      flags=re.UNICODE)
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff]")
_ES_STRIP_RE = re.compile(r"(?:[^laeiouy]es|ed|[^laeiouy]e)$")
_VOWEL_RUN_RE = re.compile(r"[aeiouy]{1,2}", flags=re.IGNORECASE)
_ES_VOWELS_RE = re.compile(r"[aeiouáéíóúü]+")
_STRONG_VOWELS_RE = re.compile(r"[aeoáéó]")  # 重元音成核,弱元音 i/u 例外
_ES_HINT_WORDS = frozenset(
    "que de la el los las un una para por con sin pero porque como más muy "
    "bien este esta eso soy eres es son estoy estás está tengo quiero puedo "
    "ahora cuando todo nada aquí así".split())


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(text) or []


def _looks_spanish(text: str) -> bool:
    if re.search(r"[ñáéíóúü¿¡]", text, flags=re.IGNORECASE):
        return True
    tokens = [w.lower() for w in _words(text)]
    if not tokens:
        return False
    hits = sum(1 for t in tokens if t in _ES_HINT_WORDS)
    return hits >= max(2, int((len(tokens) * 0.18) + 0.999))


def detect_language(text: str) -> str:
    """按字符比例确定性识别:ja / zh / es / en(无模型)。"""
    han = len(re.findall(r"[一-鿿]", text))
    kana = len(re.findall(r"[\u3040-\u30ff]", text))
    ascii_letters = len(re.findall(r"[A-Za-z]", text))
    if kana > max(han, ascii_letters / 4):
        return "ja"
    if han > ascii_letters / 4:
        return "zh"
    if _looks_spanish(text):
        return "es"
    return "en"


def _english_syllables(word: str) -> int:
    """英文音节数:hypit 的启发式(无 CMU 词典时的确定性回退)。"""
    norm = re.sub(r"[^a-z']", "", word.lower().replace("\u2019", "'"))
    if not norm:
        return 0
    if len(norm) <= 3:
        return 1
    stripped = _ES_STRIP_RE.sub("", norm)
    if stripped.startswith("y"):
        stripped = stripped[1:]
    return max(1, len(_VOWEL_RUN_RE.findall(stripped)))


def _spanish_syllables(word: str) -> int:
    norm = re.sub(r"[^a-záéíóúüñ]", "", word.lower())
    if not norm:
        return 0
    if norm == "y":
        norm = "i"
    else:
        norm = re.sub(r"y$", "i", norm)
    runs = _ES_VOWELS_RE.findall(norm)
    nuclei = 0
    for run in runs:
        if "\u00ed" in run or "\u00fa" in run:  # í / ú 独立成核
            nuclei += 1
        else:
            nuclei += max(1, len(_STRONG_VOWELS_RE.findall(run)))
    return max(1, nuclei)


def count_speech_units(text: str, language: str | None = None) -> int:
    """语音时长单位数。

    zh/ja:汉字/假名逐字符计 1 单位,嵌在中文里的拉丁词按英文音节数计;
    en/es:按词计音节。标点只分词,不增加单位。
    """
    lang = language or detect_language(text)
    if lang in ("zh", "ja"):
        cjk = len(_CJK_RE.findall(text))
        latin = [w for w in _words(_CJK_RE.sub(" ", text))
                 if re.search(r"[A-Za-z]", w)]
        return cjk + sum(_english_syllables(w) for w in latin)
    if lang == "es":
        return sum(_spanish_syllables(w) for w in _words(text))
    return sum(_english_syllables(w) for w in _words(text))


def estimate_speech_duration(
    text: str,
    language: str | None = None,
    pace: str = "normal",
    rate: Optional[float] = None,
    padding_sec: float = 0.0,
    rounding: Optional[str] = None,
) -> float:
    """按 hypit estimate 语义返回语音时长(秒)。

    rate 显式给定则忽略 pace 表;rounding ∈ {None, 'round', 'ceil'}。
    """
    if not text.strip():
        return 0.0
    lang = language or detect_language(text)
    if rate is not None:
        per_sec = rate
    else:
        per_sec = PACE_RATE.get(lang, PACE_RATE["en"])[pace]
    seconds = count_speech_units(text, lang) / per_sec + padding_sec
    if rounding == "ceil":
        return float(math.ceil(seconds))
    if rounding == "round":
        return float(round(seconds))
    return seconds


def narration_char_budget(total_duration_sec: float,
                          rate: float = NARRATION_RATE_ZH) -> int:
    """审查层字数预算 = 时长 × 生产语速(整型,便于对齐既有检查)。"""
    return int(math.floor(total_duration_sec * rate))