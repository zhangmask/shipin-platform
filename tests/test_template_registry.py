"""品类模板注册表测试(对标 hypit SVS 的轻量等价物)。

关键验收:缺省/未知品类行为与旧 SCRIPT_PROMPT 逐字节一致(零回归);
显式品类走模板硬规则。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from shipin_platform.orchestration.pipeline_runner import (  # noqa: E402
    SCRIPT_PROMPT,
    _script_prompt,
)
from shipin_platform.services.template_registry import (  # noqa: E402
    TemplateRegistry,
    TemplateUnavailableError,
    get_registry,
)


class TestRegistry:
    def test_categories_loaded(self):
        reg = get_registry()
        assert {"tvc", "drama", "talk", "tutorial"} <= set(reg.categories())

    def test_default_is_tvc(self):
        assert get_registry().get().category == "tvc"

    def test_unknown_category_falls_back_to_tvc(self):
        assert get_registry().get("nope").category == "tvc"

    def test_missing_registry_file_raises(self, tmp_path):
        with pytest.raises(TemplateUnavailableError):
            TemplateRegistry(tmp_path / "nope.json")

    def test_template_fields(self):
        tpl = get_registry().get("drama")
        assert tpl.shot_range == (6, 14)
        assert tpl.pace == "normal"
        assert tpl.brand_shots_min >= 2
        assert "反转" in tpl.rules


class TestNarrationBudgetLink:
    def test_template_rate_drives_budget(self):
        talk = get_registry().get("talk")
        # talk 语速 3.2 字/秒 → 30s 预算 96
        assert talk.narration_budget(30.0) == 96
        assert talk.narration_budget(0) == 0

    def test_tvc_rate_matches_platform_rate(self):
        # tvc 模板创作密度 = 生产语速实测值(与 engine 收敛值一致)
        assert get_registry().get("tvc").narration_rate == 2.7


class TestScriptPromptAssembly:
    def test_no_category_returns_byte_identical_default(self):
        brief = {"product": "咖啡", "duration_sec": 30, "tone": "温暖"}
        assert _script_prompt(brief) == SCRIPT_PROMPT.replace(
            "{brief}", '{"product": "咖啡", "duration_sec": 30, "tone": "温暖"}')

    def test_tvc_category_keeps_default_prompt(self):
        brief = {"product": "咖啡"}
        assert _script_prompt(brief, "tvc") == _script_prompt(brief, None)

    def test_unknown_category_keeps_default_prompt(self):
        brief = {"product": "咖啡"}
        assert _script_prompt(brief, "not-a-category") == _script_prompt(brief)

    def test_drama_uses_template_rules(self):
        brief = {"product": "咖啡"}
        p = _script_prompt(brief, "drama")
        assert "短剧" in p
        assert "反转" in p
        assert "落版镜专项" in p
        assert p.startswith("你是严格的") and "输出严格 JSON(无 markdown、无解释)" in p

    def test_brief_json_embedded(self):
        brief = {"name": "茶", "duration_sec": 20}
        p = _script_prompt(brief, "talk")
        assert '"name": "茶"' in p and '"duration_sec": 20' in p