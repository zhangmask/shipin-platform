"""占位符绑定：LLM 把 brief 里的「XX咖啡」抄进分镜的兜底修复

2026-09-21 e2e 实证：S08 narration='XX咖啡'、S09='享受每一刻'，真品牌名
从未进入任何文本通道 → TTS/字幕都不含品牌 → 终验 BRAND_MISSING。
_bind_brand 在分镜过审后全字段绑真实品牌名 + 落版镜旁白兜底品牌名+slogan。
"""
import pytest

from shipin_platform.orchestration.pipeline_runner import (
    _bind_brand, _PH_RE, _resolve_brand)

BRIEF = {"brand_name": "晨光咖啡", "slogan": "享受每一刻",
         "product_info": "精品咖啡"}


def _sb(narr="XX咖啡", dlg=None, subject="XX咖啡店"):
    return {"shots": [
        {"shot_id": "S01", "narration": narr,
         "dialogue": dlg or {"role_code": "hero", "text": "XX咖啡好喝吗"},
         "scene": subject, "spatial": "居中"},
    ]}


def test_placeholder_bound_across_text_fields():
    out = _bind_brand(_sb(), BRIEF)
    s = out["shots"][0]
    assert s["narration"] == "晨光咖啡"
    assert s["scene"] == "晨光咖啡店"
    assert s["dialogue"]["text"] == "晨光咖啡好喝吗"


def test_outro_narration_forced_when_brand_missing():
    sb = {"shots": [
        {"shot_id": "S01", "narration": "开场", "dialogue": ""},
        {"shot_id": "S02", "narration": "享受每一刻", "dialogue": ""},
    ]}
    out = _bind_brand(sb, BRIEF)
    assert out["shots"][-1]["narration"] == "晨光咖啡，享受每一刻"


def test_no_brand_returns_unchanged():
    sb = _sb("XX咖啡")
    old = dict(sb)
    out = _bind_brand(sb, {"slogan": "x"})
    assert out == old
    # 无品牌名时逐字不动（占位符逃逸由审查规则拦截，这里不硬改文本）
    assert out["shots"][0]["narration"] == "XX咖啡"


def test_binding_idempotent_when_brand_already_present():
    # 规范形态（品牌名+slogan）原样保留，不叠加粘连
    sb = {"shots": [
        {"shot_id": "S01", "narration": "晨光咖啡，享受每一刻", "dialogue": ""}]}
    before = dict(sb)
    out = _bind_brand(sb, BRIEF)
    assert out["shots"][0]["narration"] == before["shots"][0]["narration"]


def test_outro_normalizes_to_brand_slogan_shorthand():
    # 落版镜旁白是品牌短写「晨光」而非全名 → 兜底补全为 品牌全名+slogan
    sb = {"shots": [
        {"shot_id": "S01", "narration": "晨光，享受每一刻", "dialogue": ""}]}
    out = _bind_brand(sb, BRIEF)
    assert out["shots"][0]["narration"] == "晨光咖啡，享受每一刻"


def test_placeholder_regex_matches():
    assert _PH_RE.search("XX")
    assert _PH_RE.search("XX咖啡")
    assert _PH_RE.search("XX品牌")
    assert not _PH_RE.match("纯文本")


def test_resolve_brand_prefers_explicit_name():
    # 显式 brand_name 优先，即使 product_info 又写了一遍占位
    b = {"brand_name": "晨光咖啡",
         "product_info": "精品咖啡；品牌「XX咖啡」，slogan「享受每一刻」"}
    assert _resolve_brand(b) == "晨光咖啡"


def test_resolve_brand_from_product_info_literal():
    # 无 brand_name/product_name → 从「品牌『XX』」字面解析，
    # 绝不能把 product_info 开头的品类词（精品咖啡）当品牌名
    b = {"product_info": "精品咖啡，手工烘焙，香醇顺滑；品牌「XX咖啡」，slogan「享受每一刻」"}
    assert _resolve_brand(b) == "XX咖啡"
    b2 = {"product_info": "现磨豆浆；品牌《XX豆浆》"}
    assert _resolve_brand(b2) == "XX豆浆"


def test_resolve_brand_no_info_returns_empty():
    # 无品牌信息：不猜测（旧实现会抓出品类词当品牌名 → 终验错杀）
    assert _resolve_brand({}) == ""
    assert _resolve_brand({"product_info": "精品咖啡"}) == ""