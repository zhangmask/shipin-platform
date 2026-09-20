"""agent_guard.registry — 平台函数注册表（白名单层）。

一切 Agent 可调用的能力都必须先在这里注册；
清单之外的调用会被直接拒绝（UnknownFunctionError），
不产生任何实际执行。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from .errors import GuardError, PlatformFunctionError, UnknownFunctionError

ACTION = "action"      # 动作函数：Agent 在步骤内可调用（生图、写剧本、后期……）
REVIEWER = "reviewer"  # 审查函数：只能由审查门在 request_review 内部调用


@dataclass
class RegisteredFunction:
    name: str
    fn: Callable[..., Any]
    category: str
    description: str = ""
    params_hint: str = ""


class Registry:
    """统一注册入口 + 调用拦截。

    用法::

        registry = Registry()
        registry.register("generate_keyframes", generate_keyframes,
                          description="为首尾帧提示词生成图片",
                          params_hint="frame_prompts, resolution, subject_drift")
        registry.register_reviewer("review_keyframes", review_keyframes)
    """

    def __init__(self) -> None:
        self._functions: Dict[str, RegisteredFunction] = {}
        self.call_count: Dict[str, int] = {}

    # ── 注册 ────────────────────────────────────────────────
    def register(self, name: str, fn: Callable[..., Any], category: str = ACTION,
                 description: str = "", params_hint: str = "") -> "Registry":
        if category not in (ACTION, REVIEWER):
            raise ValueError(f"category 只能是 {ACTION} 或 {REVIEWER}，收到 {category!r}")
        if name in self._functions:
            raise ValueError(f"平台函数「{name}」已注册，不可重复注册")
        if not callable(fn):
            raise ValueError(f"注册「{name}」失败：fn 必须可调用")
        self._functions[name] = RegisteredFunction(
            name=name, fn=fn, category=category,
            description=description, params_hint=params_hint or "")
        self.call_count[name] = 0
        return self

    def register_reviewer(self, name: str, fn: Callable[..., Any],
                          description: str = "") -> "Registry":
        """审查函数注册入口（category 固定为 reviewer）。"""
        return self.register(name, fn, category=REVIEWER, description=description)

    # ── 查询 ────────────────────────────────────────────────
    def has(self, name: str) -> bool:
        return name in self._functions

    def get(self, name: str) -> RegisteredFunction:
        """取函数；未注册时抛 UnknownFunctionError（消息含可用清单）。"""
        rf = self._functions.get(name)
        if rf is None:
            actions = [f.name for f in self._functions.values() if f.category == ACTION]
            reviewers = [f.name for f in self._functions.values() if f.category == REVIEWER]
            raise UnknownFunctionError(
                f"未注册的平台函数「{name}」，已拒绝执行。"
                f"当前注册的动作函数：{actions or '（无）'}；"
                f"审查函数：{reviewers or '（无）'}。"
                f"Agent 只能调用预先注册的平台函数；"
                f"新增函数请按 HANDOVER.md《注册新的平台函数》操作。")
        return rf

    def list_functions(self, category: Optional[str] = None) -> List[RegisteredFunction]:
        return [f for f in self._functions.values()
                if category is None or f.category == category]

    def describe(self) -> Dict[str, Any]:
        """生成面向 Agent / 文档的函数清单。"""
        return {
            "actions": [
                {"name": f.name, "description": f.description, "params": f.params_hint}
                for f in self.list_functions(ACTION)],
            "reviewers": [
                {"name": f.name, "description": f.description}
                for f in self.list_functions(REVIEWER)],
        }

    # ── 执行（引擎内部使用）─────────────────────────────────
    def invoke(self, name: str, params: Optional[dict] = None) -> Any:
        """执行已注册函数。只做存在性检查与异常包装；
        步骤/顺序/审查门控由 Guard 负责。
        """
        rf = self.get(name)
        try:
            self.call_count[name] = self.call_count.get(name, 0) + 1
            kwargs = dict(params or {})
            return rf.fn(**kwargs)
        except GuardError:
            raise
        except Exception as exc:  # 平台函数自身的失败
            raise PlatformFunctionError(
                f"平台函数「{name}」执行失败：{exc}") from exc
