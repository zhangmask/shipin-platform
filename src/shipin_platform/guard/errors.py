"""agent_guard.errors — 受控执行的异常体系。

所有异常 message 都是面向 Agent 的可读提示（中文），
并被 guard 记入台账（event=rejected）后抛出。
"""


class GuardError(Exception):
    """agent-guard 受控执行异常基类。"""

    code = "GUARD_ERROR"


class UnknownFunctionError(GuardError):
    """调用了未注册的平台函数 —— 白名单直接拒绝，不产生任何执行。"""

    code = "UNKNOWN_FUNCTION"


class NotAllowedInStepError(GuardError):
    """函数已注册，但不允许在当前步骤调用（乱序/跳步被阻断）。"""

    code = "NOT_ALLOWED_IN_STEP"


class ReviewerNotCallableError(GuardError):
    """审查函数只能由审查门在 request_review() 内部调用，Agent 不可直接调用。"""

    code = "REVIEWER_NOT_CALLABLE"


class StepNotReadyError(GuardError):
    """当前步骤状态不满足该操作（尚未执行任何函数 / 已通过待推进等）。"""

    code = "STEP_NOT_READY"


class CannotAdvanceError(GuardError):
    """审查未通过（且无人工放行）时禁止进入下一步。"""

    code = "CANNOT_ADVANCE"


class ManualPassDisabledError(GuardError):
    """该步骤未开启人工放行开关。"""

    code = "MANUAL_PASS_DISABLED"


class WorkflowValidationError(GuardError):
    """工作流配置与注册表不一致（缺函数/缺规则/审查员误用作动作等）。"""

    code = "WORKFLOW_INVALID"


class PlatformFunctionError(GuardError):
    """平台函数自身执行失败（非约束问题）。"""

    code = "PLATFORM_FUNCTION_FAILED"
