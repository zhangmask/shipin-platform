"""agent_guard.engine — 受控执行引擎（状态机 + 门控 + 审查门）。

Agent 唯一的执行入口是 ``guard.call(函数名, 参数)``，引擎按固定顺序校验：

    1. 函数是否已注册（白名单）          → 未注册：拒绝 + 台账留痕
    2. 是否为动作函数（审查函数不可直调） → 违规：拒绝 + 台账留痕
    3. 是否属于当前步骤的允许清单        → 乱序/跳步：拒绝 + 台账留痕
    4. 执行并记录台账（函数、参数、结果摘要）

步骤推进只有一个出口：``request_review()`` → 审查门自动执行审查函数 →
通过才自动进入下一步；不通过则停留在当前步骤（重做后可再审），
连续失败达到上限将运行标记为 blocked（可人工放行，若该步骤开启）。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from .errors import (
    GuardError, ManualPassDisabledError, NotAllowedInStepError,
    PlatformFunctionError, ReviewerNotCallableError, StepNotReadyError,
    WorkflowValidationError,
)
from .ledger import BLOCKED, COMPLETED, RUNNING, Ledger
from .registry import ACTION, Registry
from .workflow import WorkflowDefinition

MAX_TEXT = 400

# 大产物不截断入库（保留全量可审计）；超出部分存项目文件本身，
# 台账记录路径与哈希 —— real-guard-15 续跑时发现 400 字符截断丢 storyboard。
FULL_PAYLOAD_EVENTS = {"called"}
FULL_PAYLOAD_FUNCS = {"pipeline.text", "pipeline.generate", "pipeline.assemble"}


def _now_iso() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="milliseconds")


def _clip(obj: Any, full: bool = False) -> str:
    """参数/结果的台账摘要。full=True 时保留全量（大产物可审计）。"""
    try:
        s = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        s = str(obj)
    if full or len(s) <= MAX_TEXT:
        return s
    return s[:MAX_TEXT] + f"…(截断，共{len(s)}字符)"


def _filter_kwargs(fn: Any, params: Optional[dict], ctx: Optional[dict] = None) -> Dict[str, Any]:
    """按函数签名过滤参数（多余参数忽略，防注入式误传）；
    函数若声明 ctx 参数，则自动注入运行时数据流（上游产物 + intent）。"""
    import inspect
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return dict(params or {})
    filtered = {k: v for k, v in (params or {}).items() if k in sig.parameters}
    if "ctx" in sig.parameters and "ctx" not in filtered:
        filtered["ctx"] = ctx or {}
    return filtered


class Guard:
    """受控执行引擎。一个 Guard 实例 = 一次受控运行。"""

    def __init__(self, workflow: "WorkflowDefinition | dict", registry: Registry,
                 ledger: Ledger) -> None:
        self.wf = workflow if isinstance(workflow, WorkflowDefinition) \
            else WorkflowDefinition(workflow)
        self.registry = registry
        self.ledger = ledger
        self._check_workflow_registry()
        self.run_id: Optional[str] = None
        self.run_status = RUNNING
        self._intent: Dict[str, Any] = {}
        self._idx = 0
        self._step_states: List[Dict[str, Any]] = []
        self._block_reason: Optional[str] = None

    # ── 初始化校验 ──────────────────────────────────────────
    def _check_workflow_registry(self) -> None:
        for i, step in enumerate(self.wf.steps, 1):
            for fn_name in step.get("entry_actions", []):
                if not self.registry.has(fn_name):
                    raise WorkflowValidationError(
                        f"步骤「{step['name']}」entry_actions 引用未注册函数「{fn_name}」")
            for fn_name in step["actions"]:
                if not self.registry.has(fn_name):
                    raise WorkflowValidationError(
                        f"步骤「{step['name']}」actions 引用未注册函数「{fn_name}」")
                if self.registry.get(fn_name).category != ACTION:
                    raise WorkflowValidationError(
                        f"步骤「{step['name']}」actions 中的「{fn_name}」不是动作函数")
            rev = step["reviewer"]
            if not self.registry.has(rev):
                raise WorkflowValidationError(
                    f"步骤「{step['name']}」reviewer「{rev}」未注册")
            if self.registry.get(rev).category == ACTION:
                raise WorkflowValidationError(
                    f"步骤「{step['name']}」reviewer「{rev}」不是审查函数")

    # ── 生命周期 ────────────────────────────────────────────
    def start(self, intent: Optional[Dict[str, Any]] = None) -> str:
        if self.run_id is not None:
            raise StepNotReadyError("本次运行已启动，不可重复 start()")
        self._intent = dict(intent or {})
        self._validate_intent(self._intent)
        self.run_id = self.ledger.start_run(self.wf.name, self._intent)
        self._step_states = [
            {"status": "active", "artifacts": {}, "review_attempts": 0,
             "functions_called": [], "last_review": None}
            for _ in self.wf.steps]
        self._enter_step(0)
        return self.run_id

    def _validate_intent(self, intent: Dict[str, Any]) -> None:
        fields = self.wf.intent_schema.get("fields", [])
        if fields and not any(intent.get(f) for f in fields):
            raise StepNotReadyError(
                f"intent 至少需要提供以下之一：{fields}"
                f"（用户的一段文字 / 上传的素材 / 想法）")

    def _dataflow(self) -> Dict[str, Any]:
        """运行时数据流：intent + 各步骤已产出产物（含当前步骤已调用的部分）。"""
        steps = {self.wf.step(i)["name"]: dict(self._step_states[i]["artifacts"])
                 for i in range(len(self.wf.steps))}
        return {"intent": dict(self._intent), "run_id": self.run_id,
                "steps": steps}

    def _enter_step(self, index: int) -> None:
        self._idx = index
        st = self._step_states[index]
        st.update({"status": "active", "artifacts": {}, "review_attempts": 0,
                   "functions_called": [], "last_review": None})
        step = self.wf.step(index)
        self.ledger.record(self.run_id, "step_entered", step_index=index + 1,
                           step=step["name"], title=step.get("title", ""))
        self._sync_meta()
        for fn_name in step.get("entry_actions", []):
            rf = self.registry.get(fn_name)
            params = _filter_kwargs(rf.fn, self._intent, ctx=self._dataflow())
            result = self.registry.invoke(fn_name, params)
            st["artifacts"][fn_name] = result
            st["functions_called"].append(fn_name)
            self.ledger.record(self.run_id, "called", step_index=index + 1,
                               step=step["name"], function=fn_name,
                               auto=True, params=_clip(params),
                               result_summary=_clip(result, full=(function_name in FULL_PAYLOAD_FUNCS)))

    # ── Agent 唯一执行入口 ──────────────────────────────────
    def call(self, function_name: str, params: Optional[dict] = None) -> Any:
        self._ensure_active()
        try:
            rf = self.registry.get(function_name)
        except GuardError as exc:
            self._reject(exc, function=function_name)
            raise
        if rf.category != ACTION:
            exc = ReviewerNotCallableError(
                f"「{function_name}」是审查函数，只能由审查门在 request_review() "
                f"内部调用，Agent 不可直接调用审查函数。")
            self._reject(exc, function=function_name)
            raise exc
        step = self.wf.step(self._idx)
        st = self._step_states[self._idx]
        if function_name not in step["actions"]:
            owner = next((s["name"] for s in self.wf.steps
                          if function_name in s["actions"]), None)
            owner_pos = next((i + 1 for i, s in enumerate(self.wf.steps)
                              if function_name in s["actions"]), None)
            exc = NotAllowedInStepError(
                f"当前处于第 {self._idx + 1}/{len(self.wf.steps)} 步"
                f"「{step['name']}」（状态：{st['status']}），"
                f"不允许调用「{function_name}」"
                + (f"——它属于第 {owner_pos} 步「{owner}」。"
                   if owner else "（它不属于任何步骤）")
                + f"本步骤允许调用：{step['actions']}。"
                f"必须完成当前步骤且审查通过后，才能进入下一步。")
            self._reject(exc, function=function_name)
            raise exc
        filtered = _filter_kwargs(rf.fn, params, ctx=self._dataflow())
        try:
            result = self.registry.invoke(function_name, filtered)
        except PlatformFunctionError as exc:
            self.ledger.record(self.run_id, "function_failed",
                               step_index=self._idx + 1, step=step["name"],
                               function=function_name, params=_clip(filtered),
                               error=str(exc))
            raise
        st["artifacts"][function_name] = result
        st["functions_called"].append(function_name)
        self.ledger.record(self.run_id, "called", step_index=self._idx + 1,
                           step=step["name"], function=function_name,
                           params=_clip(filtered), result_summary=_clip(result, full=(function_name in FULL_PAYLOAD_FUNCS)))
        return result

    # ── 审查门 ──────────────────────────────────────────────
    def request_review(self) -> Dict[str, Any]:
        self._ensure_active()
        step = self.wf.step(self._idx)
        st = self._step_states[self._idx]
        if st["status"] == "passed":
            raise StepNotReadyError(
                f"步骤「{step['name']}」已通过审查。")
        if not st["artifacts"]:
            raise StepNotReadyError(
                f"步骤「{step['name']}」尚未执行任何平台函数，没有可审查的产物；"
                f"请先调用：{step['actions']}")
        attempt = st["review_attempts"] + 1
        st["review_attempts"] = attempt
        self.ledger.record(self.run_id, "review_requested",
                           step_index=self._idx + 1, step=step["name"],
                           attempt=attempt)
        context = {
            "step": step["name"], "step_index": self._idx + 1,
            "review_focus": step.get("review_focus", ""),
            "params": step.get("review_params", {}),
            "previous_artifacts": {
                self.wf.step(i)["name"]: dict(self._step_states[i]["artifacts"])
                for i in range(self._idx)},
            "run_id": self.run_id,
        }
        try:
            report = self.registry.invoke(
                step["reviewer"],
                {"artifacts": dict(st["artifacts"]), "context": context})
        except PlatformFunctionError as exc:
            self.ledger.record(self.run_id, "review_function_failed",
                               step_index=self._idx + 1, step=step["name"],
                               reviewer=step["reviewer"], error=str(exc))
            raise
        verdict = report.get("verdict")
        if verdict not in ("pass", "fail"):
            raise PlatformFunctionError(
                f"审查函数「{step['reviewer']}」返回了非法 verdict：{verdict!r}"
                f"（只允许 'pass' / 'fail'）")
        rules = report.get("rules", [])
        st["last_review"] = report
        self.ledger.record(self.run_id, "review_finished",
                           step_index=self._idx + 1, step=step["name"],
                           attempt=attempt, verdict=verdict,
                           rules=rules, summary=_clip(report.get("summary", "")))
        if verdict == "pass":
            self._pass_step(via="review")
        else:
            max_attempts = int(step.get("max_review_attempts", 3))
            if attempt >= max_attempts:
                self._block_run(
                    f"步骤「{step['name']}」连续 {attempt} 次审查未通过："
                    f"{report.get('summary', '')}")
        return report

    def _manual_unlockable(self) -> None:
        """manual_pass 允许在 RUNNING 或 BLOCKED 状态调用（放行即解锁阻断）。"""
        if self.run_id is None:
            raise StepNotReadyError("运行尚未启动：请先 guard.start(intent)。")
        if self.run_status == COMPLETED:
            raise StepNotReadyError("运行已完成，无需人工放行。")

    def manual_pass(self, reason: str = "", approver: str = "人工") -> None:
        """人工放行：仅在步骤开启 manual_pass_enabled 且已至少审查失败一次后可用；
        运行被阻断（blocked）时，人工放行是唯一解锁通道。
        """
        self._manual_unlockable()
        step = self.wf.step(self._idx)
        st = self._step_states[self._idx]
        if st["status"] == "passed":
            raise StepNotReadyError(f"步骤「{step['name']}」已通过，无需人工放行。")
        if not step.get("manual_pass_enabled", False):
            exc = ManualPassDisabledError(
                f"步骤「{step['name']}」未开启人工放行"
                f"（manual_pass_enabled=false）。"
                f"如需人工放行通道，请在该步骤配置中开启后重跑。")
            self._reject(exc)
            raise exc
        if st["review_attempts"] == 0:
            exc = StepNotReadyError(
                f"步骤「{step['name']}」尚未执行过自动审查；"
                f"人工放行仅用于审查未通过后的兜底，请先 request_review()。")
            self._reject(exc)
            raise exc
        self.ledger.record(self.run_id, "manual_pass",
                           step_index=self._idx + 1, step=step["name"],
                           approver=approver, reason=reason)
        if self.run_status == BLOCKED:
            self.run_status = RUNNING
            self._block_reason = None
        self._pass_step(via="manual")

    def _pass_step(self, via: str) -> None:
        step = self.wf.step(self._idx)
        st = self._step_states[self._idx]
        st["status"] = "passed"
        self.ledger.record(self.run_id, "step_passed", step_index=self._idx + 1,
                           step=step["name"], via=via)
        self._sync_meta()
        if self._idx == len(self.wf.steps) - 1:
            self.run_status = COMPLETED
            final = st["artifacts"]
            finished_at = _now_iso()
            self.ledger.record(self.run_id, "run_finished", status=COMPLETED,
                               final_summary=_clip(final))
            self.ledger.update_meta(self.run_id, status=COMPLETED,
                                    current_step=None,
                                    finished_at=finished_at,
                                    final_summary=_clip(final))
        else:
            self.ledger.record(self.run_id, "advanced",
                               from_step=step["name"],
                               to_step=self.wf.step(self._idx + 1)["name"])
            self._enter_step(self._idx + 1)

    def _block_run(self, reason: str) -> None:
        self.run_status = BLOCKED
        self._block_reason = reason
        step = self.wf.step(self._idx)
        self.ledger.record(self.run_id, "run_blocked", step_index=self._idx + 1,
                           step=step["name"], reason=reason)
        self.ledger.update_meta(self.run_id, status=BLOCKED, block_reason=reason)
        self._sync_meta()

    def _reject(self, exc: GuardError, function: Optional[str] = None) -> None:
        """拒绝也要留痕：写入台账后再抛出。"""
        if self.run_id:
            step = self.wf.step(self._idx)["name"] if self._step_states else None
            self.ledger.record(self.run_id, "rejected", step=step,
                               function=function, code=exc.code,
                               message=str(exc))

    def _ensure_active(self) -> None:
        if self.run_id is None:
            raise StepNotReadyError(
                "运行尚未启动：请先 guard.start(intent)。")
        if self.run_status == COMPLETED:
            raise StepNotReadyError(
                f"运行 {self.run_id} 已完成，不能再调用平台函数；"
                f"如需新的制作任务请另起一次运行。")
        if self.run_status == BLOCKED:
            step = self.wf.step(self._idx)
            hint = ""
            if step.get("manual_pass_enabled", False):
                hint = "该步骤已开启人工放行，可调用 manual_pass(reason, approver) 解锁。"
            raise StepNotReadyError(
                f"运行 {self.run_id} 已被阻断：{self._block_reason}。{hint}"
                f"台账可用 python agent_guard/ledger.py <root> --run {self.run_id} 查看。")

    # ── 状态与自描述 ────────────────────────────────────────
    def _sync_meta(self) -> None:
        if not self.run_id:
            return
        cur = self.wf.step(self._idx)
        self.ledger.update_meta(
            self.run_id,
            status=self.run_status,
            current_step={"index": self._idx + 1, "name": cur["name"]},
            steps=[{"name": s["name"], "state": st["status"],
                    "review_attempts": st["review_attempts"]}
                   for s, st in zip(self.wf.steps, self._step_states)])

    def status(self) -> Dict[str, Any]:
        step = self.wf.step(self._idx)
        st = self._step_states[self._idx] if self._step_states else {}
        return {
            "run_id": self.run_id,
            "workflow": self.wf.name,
            "status": self.run_status,
            "current_step": {
                "index": self._idx + 1,
                "total": len(self.wf.steps),
                "name": step["name"],
                "state": st.get("status"),
                "review_attempts": st.get("review_attempts", 0),
                "functions_called": list(st.get("functions_called", [])),
            },
            "steps": [{"name": s["name"], "state": t["status"],
                       "review_attempts": t["review_attempts"]}
                      for s, t in zip(self.wf.steps, self._step_states)],
            "block_reason": self._block_reason,
        }

    def allowed_functions(self) -> List[str]:
        """当前步骤允许 Agent 调用的平台函数清单。"""
        return list(self.wf.step(self._idx)["actions"])

    def step_config(self) -> Dict[str, Any]:
        return dict(self.wf.step(self._idx))
