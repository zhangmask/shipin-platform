"""shipin_platform.guard — Agent 受控执行内核（约束壳）。

定位：薄薄一层"缰绳"，不重写任何生成/审查/合成逻辑——
真实能力一律通过 adapters 复用 shipin_platform 既有实现
（pipeline_runner / generate_assets / clip_qc / hard_gates）。

组成：
    Registry   平台函数注册表（白名单层）
    Ledger     执行台账（本地落盘、按运行可查询）
    WorkflowDefinition  步骤编排配置（顺序 / 每步白名单 / 审查门）
    Guard      受控执行引擎（状态机 + 门控 + 审查门）

Agent 唯一入口::

    guard.call(函数名, 参数)      # 白名单 + 当前步骤校验后执行
    guard.request_review()       # 审查门：通过才自动进入下一步
    guard.status()               # 我在哪一步、能调什么、审查结论
"""
from .errors import (
    CannotAdvanceError, GuardError, ManualPassDisabledError,
    NotAllowedInStepError, PlatformFunctionError, ReviewerNotCallableError,
    StepNotReadyError, UnknownFunctionError, WorkflowValidationError,
)
from .ledger import BLOCKED, COMPLETED, RUNNING, Ledger
from .registry import ACTION, REVIEWER, Registry
from .workflow import VIDEO_PIPELINE, WorkflowDefinition
from .engine import Guard
from .adapters import build_registry as build_real_registry

__all__ = [
    "Guard", "Registry", "Ledger", "WorkflowDefinition", "VIDEO_PIPELINE",
    "build_real_registry",
    "ACTION", "REVIEWER", "RUNNING", "COMPLETED", "BLOCKED",
    "GuardError", "UnknownFunctionError", "NotAllowedInStepError",
    "ReviewerNotCallableError", "StepNotReadyError", "ManualPassDisabledError",
    "PlatformFunctionError", "WorkflowValidationError", "CannotAdvanceError",
]
