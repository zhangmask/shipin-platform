"""组件配方注册表(方案"远期3·组件化"落地)。

把「转场拼接」「品牌落版卡 kenburns」「字幕烧录」「声音设计」这些每个
镜头重复的小行为,从 pipeline_runner 里的散落常量(defaults 即原行为)
升为幂等组件参数(配方),供默认管线与变体复用:

    component_registry.get("outro_card")     -> ComponentSpec(默认可覆盖)
    component_registry.apply("outro_card", {"zoom_to": 1.3}, src, dur, out)
        -> 与 params 合并 defaults 后调用注册的 apply 实现(确定性,幂等)

对标 hypit 的组件包体系:我们不搬 TS 插件生态,用"配方 + 默认参数"的
轻量等价物;默认值即原管线行为(行为零变化由回归测试兜底)。凭据不落
配置(本文件无任何密钥)。

安全:apply 只按 registry 里注册的实现点(静态白名单 dotted path)导入,
参数只透传给注册函数,不做任意代码执行。
"""
from __future__ import annotations

import importlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from shipin_platform import roots
from typing import Optional

ROOT = roots.data_root()
DEFAULT_PROFILE = ROOT / "config" / "components.json"


class ComponentError(ValueError):
    """组件未注册 / 参数非法 / 实现不可用。"""


@dataclass(frozen=True)
class ComponentSpec:
    id: str
    kind: str
    description: str
    apply_path: str            # dotted path,如 shipin_platform.assembly.kenburns
    defaults: dict             # 默认参数(原管线常量)
    variant_overridable: tuple[str, ...] = ()
    raw: dict = field(default_factory=dict)

    def params(self, overrides: Optional[dict] = None) -> dict:
        """合并默认参数;只允许显式 overrides(variant 白名单参数)。"""
        merged = dict(self.defaults)
        for k, v in (overrides or {}).items():
            if k not in self.defaults:
                raise ComponentError(
                    f"组件 {self.id!r} 无参数 {k!r}(可选: {sorted(self.defaults)})")
            merged[k] = v
        return merged

    def _func(self):
        mod_name, _, fn_name = self.apply_path.rpartition(".")
        try:
            mod = importlib.import_module(mod_name)
            fn = getattr(mod, fn_name)
        except (ImportError, AttributeError) as e:
            raise ComponentError(
                f"组件 {self.id!r} 实现不可用: {self.apply_path}({e})")
        return fn

    def apply(self, *args, overrides: Optional[dict] = None, **kwargs) -> dict:
        """以 defaults+overrides(显式 kwargs 优先)调用注册实现(幂等)。"""
        params = self.params(overrides)
        params.update(kwargs)
        return self._func()(*args, **params)


class ComponentRegistry:
    def __init__(self, profile_path: Optional[Path] = None):
        p = Path(profile_path or DEFAULT_PROFILE)
        if not p.exists():
            raise ComponentError(f"组件配方不存在: {p}")
        data = json.loads(p.read_text(encoding="utf-8"))
        components = data.get("components") or {}
        self._specs = {
            cid: ComponentSpec(
                id=cid,
                kind=raw.get("kind", ""),
                description=raw.get("description", ""),
                apply_path=raw.get("apply", ""),
                defaults=dict(raw.get("defaults") or {}),
                variant_overridable=tuple(raw.get("variant_overridable") or ()),
                raw=raw,
            )
            for cid, raw in components.items()
        }
        for cid, spec in self._specs.items():
            if not spec.apply_path:
                raise ComponentError(f"组件 {cid!r} 缺少 apply 实现路径")

    def list_components(self) -> list[str]:
        return sorted(self._specs)

    def get(self, component_id: str) -> ComponentSpec:
        spec = self._specs.get(component_id)
        if spec is None:
            raise ComponentError(
                f"未注册组件 {component_id!r}(可用: {self.list_components()})")
        return spec


_default: Optional[ComponentRegistry] = None


def get_registry() -> ComponentRegistry:
    global _default
    if _default is None:
        _default = ComponentRegistry()
    return _default


__all__ = ["ComponentError", "ComponentRegistry", "ComponentSpec", "get_registry"]