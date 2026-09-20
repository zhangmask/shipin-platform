"""品类模板注册表(对标 hypit SVS 样式表的轻量等价物)。

config/category_templates.json 声明每品类的创作硬规则(镜头数范围、
结构序列、语速、落版要求、品牌元素下限)。与 estimate 联动:

- narration_rate 是品类内定的密度(字/秒),供 brief 字数预算与时长估算
  引用;未登记时回退 estimate.NARRATION_RATE_ZH。
- pace 语义标签可换算为 estimate.PACE_RATE 的创作密度。

所有模板数据只是结构参数,不含任何凭据;未知品类一律回退 tvc,
保证服务端行为对未知输入始终确定。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from shipin_platform import roots
from typing import Optional

from shipin_platform.services.estimate import NARRATION_RATE_ZH

ROOT = roots.data_root()
TEMPLATE_PATH = ROOT / "config" / "category_templates.json"

DEFAULT_CATEGORY = "tvc"


class TemplateUnavailableError(Exception):
    """模板注册表缺失/损坏。"""


@dataclass(frozen=True)
class CategoryTemplate:
    """一个品类的创作参数(从 category_templates.json 反序化)。"""
    name: str
    category: str
    shot_range: tuple[int, int]
    shot_min_sec: float
    structure: str
    pace: str
    narration_rate: float
    rules: str
    landing_note: str
    brand_shots_min: int
    description: str = ""
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def default_narration_rate(self) -> float:
        """品类语境速率:模板显式值,否则生产语速实测值。"""
        return self.narration_rate if self.narration_rate > 0 else NARRATION_RATE_ZH

    def narration_budget(self, total_duration_sec: float) -> int:
        """该品类下时长对应的旁白字数预算(整型)。"""
        return max(0, int(total_duration_sec * self.default_narration_rate))


class TemplateRegistry:
    """加载 config/category_templates.json,按品类名查询。"""

    def __init__(self, path: Optional[Path] = None):
        p = path or TEMPLATE_PATH
        if not Path(p).exists():
            raise TemplateUnavailableError(f"模板注册表缺失: {p}")
        try:
            data = json.loads(Path(p).read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise TemplateUnavailableError(f"模板注册表损坏({p}): {e}") from e
        self._templates: dict[str, CategoryTemplate] = {}
        for cat, raw in (data.get("templates") or {}).items():
            self._templates[cat] = CategoryTemplate(
                name=raw.get("name", cat),
                category=cat,
                shot_range=tuple(raw.get("shot_range", [5, 10])),
                shot_min_sec=float(raw.get("shot_min_sec", 2.5)),
                structure=raw.get("structure", ""),
                pace=raw.get("pace", "normal"),
                narration_rate=float(raw.get("narration_rate", 0.0) or 0.0),
                rules=raw.get("rules", ""),
                landing_note=raw.get("landing_note", ""),
                brand_shots_min=int(raw.get("brand_shots_min", 0)),
                description=raw.get("description", ""),
                raw=raw,
            )

    def categories(self) -> list[str]:
        return sorted(self._templates)

    def get(self, category: Optional[str] = None) -> CategoryTemplate:
        """按品类返回模板;未知/缺省品类一律回退默认 tvc。"""
        key = category or DEFAULT_CATEGORY
        return self._templates.get(key, self._templates[DEFAULT_CATEGORY])


_default: Optional[TemplateRegistry] = None


def get_registry() -> TemplateRegistry:
    global _default
    if _default is None:
        _default = TemplateRegistry()
    return _default


def template(category: Optional[str] = None) -> CategoryTemplate:
    return get_registry().get(category)