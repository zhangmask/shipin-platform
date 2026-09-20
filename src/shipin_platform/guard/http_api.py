"""shipin_platform.guard.http_api — guard 的 HTTP 接入层。

Agent 通过 HTTP 与平台交互，因此约束必须在 HTTP 层同样生效。
本模块把 Guard 暴露为 6 个端点，全部挂在 /api/guard 前缀下：

    POST /api/guard/start            开始一次受控运行（intent: project_id + brief?）
    POST /api/guard/call             调用当前步骤白名单内的平台函数（唯一执行入口）
    POST /api/guard/review           提交当前步骤审查门（通过→自动进入下一步）
    POST /api/guard/manual-pass      人工放行（仅开启开关的步骤、且已有失败审查）
    GET  /api/guard/{run_id}/status  当前步骤/可调函数/审查结论
    GET  /api/guard/{run_id}/events  该运行的台账事件流

设计：
- Guard 实例按 run_id 惰性重建（状态全部落在 Ledger/meta，进程重启不丢）；
- threading.Lock 对每个 run 串行化（与“单 Agent 顺序执行”的边界一致）；
- GuardError 及其子类映射为 4xx，并在响应体里带 code/message，
  前端/Agent 可直接把 message 转述给用户。
"""
from __future__ import annotations

import threading
from pathlib import Path
from shipin_platform import roots
from typing import Any, Dict, Optional

from pydantic import BaseModel

from shipin_platform.guard import (
    Guard, GuardError, Ledger, ManualPassDisabledError, Registry,
    StepNotReadyError, UnknownFunctionError, WorkflowDefinition,
)
from shipin_platform.guard.adapters import build_registry

_ROOT = roots.data_root()
_WORKFLOW = _ROOT / "config" / "guard_workflow_video.json"
_LEDGER_ROOT = _ROOT / "data" / "guard_ledger"

# 进程内缓存：run_id -> (Guard, Lock)。状态本体在 Ledger，重建无损失。
_RUNS: Dict[str, tuple] = {}
_RUNS_LOCK = threading.Lock()


def _load_guard(run_id: str) -> tuple:
    with _RUNS_LOCK:
        hit = _RUNS.get(run_id)
        if hit:
            return hit
        wf = WorkflowDefinition.from_json_file(_WORKFLOW)
        guard = Guard(wf, build_registry(), Ledger(_LEDGER_ROOT))
        # 把已存在的运行状态接回来（台账为准）
        meta = None
        try:
            meta = guard.ledger.get_meta(run_id)
        except ValueError:
            raise StepNotReadyError(f"运行 {run_id} 不存在；请先 POST /api/guard/start")
        guard.run_id = run_id
        guard._intent = dict(meta.get("intent") or {})
        steps = meta.get("steps") or []
        guard._step_states = [
            {"status": s.get("state", "active"), "artifacts": {},
             "review_attempts": s.get("review_attempts", 0),
             "functions_called": [], "last_review": None}
            for s in steps]
        guard._idx = next((i for i, s in enumerate(steps)
                           if s.get("state") == "active"), len(steps) - 1)
        guard.run_status = meta.get("status", "running")
        pair = (guard, threading.Lock())
        _RUNS[run_id] = pair
        return pair


def _run_guard(run_id: str, fn):
    """串行化单运行的所有操作。"""
    guard, lock = _load_guard(run_id)
    with lock:
        return fn(guard)


# ── 请求模型 ────────────────────────────────────────────────

class GuardStartRequest(BaseModel):
    project_id: str
    brief: Optional[dict] = None


class GuardCallRequest(BaseModel):
    run_id: str
    function: str
    params: dict = {}


class GuardReviewRequest(BaseModel):
    run_id: str
    attempt_note: str = ""


class GuardManualPassRequest(BaseModel):
    run_id: str
    reason: str
    approver: str = "人工"


# ── 端点 ────────────────────────────────────────────────────

def register_guard_endpoints(app) -> None:
    """把 guard 端点挂到 FastAPI app 上（api.py 里 include 一次即可）。"""

    @app.post("/api/guard/start")
    def guard_start(req: GuardStartRequest):
        wf = WorkflowDefinition.from_json_file(_WORKFLOW)
        guard = Guard(wf, build_registry(), Ledger(_LEDGER_ROOT))
        run_id = guard.start(intent={"project_id": req.project_id,
                                     "brief": req.brief or {}})
        with _RUNS_LOCK:
            _RUNS[run_id] = (guard, threading.Lock())
        return {"ok": True, "run_id": run_id, "status": guard.status(),
                "allowed_functions": guard.allowed_functions()}

    @app.post("/api/guard/call")
    def guard_call(req: GuardCallRequest):
        def op(g: Guard):
            result = g.call(req.function, req.params)
            return {"ok": True, "function": req.function,
                    "result_summary": result, "status": g.status()}
        try:
            return _run_guard(req.run_id, op)
        except GuardError as exc:
            return _err(exc)

    @app.post("/api/guard/review")
    def guard_review(req: GuardReviewRequest):
        def op(g: Guard):
            report = g.request_review()
            return {"ok": True, "review": report, "status": g.status()}
        try:
            return _run_guard(req.run_id, op)
        except GuardError as exc:
            return _err(exc)

    @app.post("/api/guard/manual-pass")
    def guard_manual_pass(req: GuardManualPassRequest):
        def op(g: Guard):
            g.manual_pass(reason=req.reason, approver=req.approver)
            return {"ok": True, "status": g.status()}
        try:
            return _run_guard(req.run_id, op)
        except GuardError as exc:
            return _err(exc)

    @app.get("/api/guard/{run_id}/status")
    def guard_status(run_id: str):
        try:
            return _run_guard(run_id, lambda g: {"ok": True, "status": g.status(),
                                                 "allowed_functions": g.allowed_functions()})
        except GuardError as exc:
            return _err(exc)

    @app.get("/api/guard/{run_id}/events")
    def guard_events(run_id: str):
        try:
            led = Ledger(_LEDGER_ROOT)
            return {"ok": True, "meta": led.get_meta(run_id),
                    "events": led.get_events(run_id)}
        except (ValueError, GuardError) as exc:
            return {"ok": False, "code": "LEDGER_LOOKUP_FAILED", "message": str(exc)}


def _err(exc: GuardError) -> dict:
    """约束拒绝统一 200 + ok:false（Agent 可编程处理；错误码稳定可分支）。
    平台函数自身异常（PLATFORM_FUNCTION_FAILED）由 FastAPI 500 兜底。"""
    return {"ok": False, "code": exc.code, "message": str(exc)}
