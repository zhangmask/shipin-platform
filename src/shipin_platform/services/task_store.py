"""P1 异步任务：SQLite 任务表（零外部 broker）+ 进程内线程池。

调研结论（2026-09-18）：Celery/RQ/Dramatiq 都要求 Redis/RabbitMQ；Huey 支持
sqlite 后端但仍是外部依赖；Airflow 最值得借鉴的是「DB 即状态机 + heartbeat
心跳超时检测 + 幂等重试位」。视频生成任务不适合 fork/prefork（显存复制），
故用 ThreadPoolExecutor(max_workers=2)。进度百分比由轮询 stage_runs 自动推导：
brief 10 → script 25 → … → post_production 100，零侵入阶段执行代码。
"""
from __future__ import annotations

import json as _json
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from shipin_platform import roots
from typing import Callable, Optional

DEFAULT_STAGE_DB = (roots.data_root() / "data" / "stage_store.db").resolve()

# 阶段 → 进度百分比（与《平台级演进计划》P1 一致：brief 10 → post 100）
PROGRESS_BY_STAGE = {
    "brief": 10,
    "script": 25,
    "storyboard": 40,
    "image_prompt": 50,
    "image_gen": 60,
    "video_prompt": 75,
    "video_gen": 90,
    "post_production": 100,
}

_TASK_STATUS = ("queued", "running", "retry", "success", "failed")
_HEARTBEAT_INTERVAL_S = 15.0
_HEARTBEAT_TTL_S = 45.0


class TaskConflict(RuntimeError):
    """轮56(十审 P1-4):同项目已有活跃任务——调用方翻译 409。"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json_dumps(data) -> str:
    return _json.dumps(data, ensure_ascii=False, default=str)


class TaskStore:
    """线程池派发 + tasks 表持久化；同一实例内完成任务可幂等 retry。"""

    def __init__(self, db_path: str, stage_db_path: str = "",
                 max_workers: int = 2, max_retries: int = 2) -> None:
        self.db_path = db_path
        self.stage_db_path = stage_db_path or str(DEFAULT_STAGE_DB)
        self.max_retries = max_retries
        self._pool = ThreadPoolExecutor(max_workers=max_workers)
        self._fns: dict[str, Callable[[], dict]] = {}
        self._lock = threading.Lock()
        self._init_schema()
        self._hb_stop = threading.Event()
        self._hb_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True, name="task-hb")
        self._hb_thread.start()

    # -- 基础设施 -----------------------------------------------------------

    def _init_schema(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    caller TEXT NOT NULL DEFAULT 'anonymous',
                    status TEXT NOT NULL DEFAULT 'queued',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_retries INTEGER NOT NULL DEFAULT 2,
                    heartbeat_at TEXT NOT NULL,
                    error TEXT,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT
                )""")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_project "
                "ON tasks (project_id, created_at)")

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def close(self) -> None:
        self._hb_stop.set()
        self._pool.shutdown(wait=False)

    # -- 生命周期 -----------------------------------------------------------

    _ACTIVE_STATUS = ("queued", "running", "retry")

    def active_task_for(self, project_id: str,
                        kinds: Optional[tuple] = None) -> Optional[dict]:
        """轮56(十审 P1-4):项目的活跃任务(queued/running/retry)。
        旧 submit_task 只按 task_id 去重、不看项目——连续两次
        POST /api/pipeline/{id}/assemble 会让两个 worker 同时
        _save/_promote_final/record_artifact 同一 final.mp4(哈希
        快照竞态、中间文件互相覆盖)。recover_stale 只清 heartbeat
        超期的,不清正在跑的。"""
        if not project_id:
            return None
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE project_id = ? "
                "AND status IN (?,?,?) ORDER BY created_at DESC",
                (project_id, *self._ACTIVE_STATUS)).fetchall()
        for row in rows:
            task = dict(row)
            if kinds is None or task.get("kind") in kinds:
                return task
        return None

    def submit_task(self, kind: str, project_id: str, caller: str,
                    fn: Callable[[], dict],
                    allow_project_overlap: bool = False) -> dict:
        task_id = uuid.uuid4().hex
        now = _now_iso()
        # 轮56(十审 P1-4):同项目互斥——已有活跃任务时拒绝(TaskConflict,
        # 调用方翻译 409),不让两个 generate/assemble 并发写同一项目
        # 目录。allow_project_overlap 给确需并发的内部场景显式逃逸。
        if not allow_project_overlap:
            active = self.active_task_for(project_id)
            if active is not None:
                raise TaskConflict(
                    f"项目 {project_id} 已有进行中的任务 "
                    f"{active['task_id']}({active['kind']}/"
                    f"{active['status']})——同项目互斥,等它结束后再提交")
        with self._lock:
            self._fns[task_id] = fn
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO tasks (task_id, kind, project_id, caller,
                                      status, max_retries, heartbeat_at,
                                      created_at)
                   VALUES (?, ?, ?, ?, 'queued', ?, ?, ?)""",
                (task_id, kind, project_id, caller, self.max_retries,
                 now, now))
        self._pool.submit(self._execute, task_id)
        return self.task_status(task_id)

    def _execute(self, task_id: str) -> None:
        fn = self._fns.get(task_id)
        if fn is None:
            self._fail(task_id, "worker lost task function")
            return
        while True:
            self._mark_running(task_id)
            try:
                result = fn()
            except Exception as exc:  # noqa: BLE001 - 任务失败要落库，不能炸线程
                attempts = self._bump_attempts(task_id)
                if attempts < self.max_retries:
                    self._mark(task_id, "retry", error=repr(exc))
                    time.sleep(0.5)
                    continue
                self._fail(task_id, repr(exc))
                return
            self._succeed(task_id, result)
            return

    def _row(self, task_id: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        return dict(row) if row else None

    def _update(self, task_id: str, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._conn() as conn:
            conn.execute(
                f"UPDATE tasks SET {cols} WHERE task_id = ?",
                (*fields.values(), task_id))

    def _mark_running(self, task_id: str) -> None:
        self._update(task_id, status="running", heartbeat_at=_now_iso())

    def _mark(self, task_id: str, status: str, error: Optional[str] = None
              ) -> None:
        fields = {"status": status, "heartbeat_at": _now_iso()}
        if error is not None:
            fields["error"] = error
        self._update(task_id, **fields)

    def _bump_attempts(self, task_id: str) -> int:
        with self._conn() as conn:
            conn.execute(
                "UPDATE tasks SET attempts = attempts + 1 WHERE task_id = ?",
                (task_id,))
        row = self._row(task_id)
        return row.get("attempts", 0) if row else self.max_retries

    def _succeed(self, task_id: str, result: dict) -> None:
        self._update(task_id, status="success", error=None,
                     heartbeat_at=_now_iso(), finished_at=_now_iso(),
                     result_json=_json_dumps(result))

    def _fail(self, task_id: str, error: str) -> None:
        self._update(task_id, status="failed", error=error,
                     heartbeat_at=_now_iso(), finished_at=_now_iso())

    def _heartbeat_loop(self) -> None:
        while not self._hb_stop.is_set():
            try:
                with self._conn() as conn:
                    conn.execute(
                        "UPDATE tasks SET heartbeat_at = ? "
                        "WHERE status IN ('running', 'retry')",
                        (_now_iso(),))
            except Exception:  # noqa: BLE001 - DB 暂时不可用则下轮再试
                pass
            self._hb_stop.wait(_HEARTBEAT_TTL_S * 0.3)

    def recover_stale(self, ttl_s: float = _HEARTBEAT_TTL_S) -> int:
        """启动/惰性扫描：heartbeat 超期的 running/retry → failed(crashed)，
        可被幂等 retry。返回本次标为 failed 的数量。"""
        cutoff = datetime.now(timezone.utc).timestamp() - ttl_s
        stale = []
        with self._conn() as conn:
            for row in conn.execute(
                "SELECT task_id, heartbeat_at FROM tasks "
                "WHERE status IN ('running', 'retry')").fetchall():
                try:
                    ts = datetime.fromisoformat(row["heartbeat_at"]).timestamp()
                except (ValueError, TypeError):
                    ts = 0.0
                if ts < cutoff:
                    stale.append(row["task_id"])
        for task_id in stale:
            self._update(task_id, status="failed",
                         error="heartbeat timeout (worker crashed)",
                         finished_at=_now_iso())
        return len(stale)

    # -- 查询 ---------------------------------------------------------------

    def _derive_progress(self, project_id: str) -> dict:
        """从 stage_store 的 stage_runs 推导进度：PASS 阶段的最高百分比 +
        当前 RUNNING 阶段名。零侵入：只读 SQL。"""
        progress, current = 0, ""
        try:
            path = self.stage_db_path
            if path == ":memory:":
                return {"progress": progress, "current": current}
            with sqlite3.connect(
                    f"file:{path}?mode=ro", uri=True,
                    timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT stage, status FROM stage_runs "
                    "WHERE project_id = ?", (project_id,)).fetchall()
        except sqlite3.Error:
            return {"progress": progress, "current": current}
        for row in rows:
            stage = row["stage"]
            if stage not in PROGRESS_BY_STAGE:
                continue
            pct = PROGRESS_BY_STAGE[stage]
            if row["status"] in ("PASS", "DONE", "SUCCESS"):
                progress = max(progress, pct)
            elif row["status"] == "RUNNING":
                current = stage
                progress = max(progress, pct)
        return {"progress": progress, "current": current}

    def task_status(self, task_id: str) -> Optional[dict]:
        row = self._row(task_id)
        if row is None:
            return None
        row = dict(row)
        extra = self._derive_progress(row["project_id"])
        row["progress"] = extra["progress"]
        row["current_stage"] = extra["current"]
        if row["status"] == "success":
            row["progress"] = 100
        if row.get("result_json"):
            row["result"] = _json.loads(row.pop("result_json"))
        else:
            row.pop("result_json", None)
        return row

    def get_task(self, task_id: str) -> Optional[dict]:
        return self._row(task_id)

    def list_tasks(self, project_id: Optional[str] = None,
                   limit: int = 200) -> list[dict]:
        limit = max(1, min(int(limit), 500))
        if project_id:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM tasks WHERE project_id = ? "
                    "ORDER BY rowid DESC LIMIT ?", (project_id, limit)
                ).fetchall()
        else:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM tasks ORDER BY rowid DESC LIMIT ?",
                    (limit,)).fetchall()
        return [dict(r) for r in rows]

    def retry_task(self, task_id: str) -> Optional[dict]:
        """幂等重试：仅 failed / retry 可重投；运行中/成功/队列中 → None。
        进程重启后内存函数表丢失 → 也返回 None（需重新提交）。"""
        with self._lock:
            row = self._row(task_id)
            if row is None:
                return None
            if row["status"] not in ("failed", "retry"):
                return None
            fn = self._fns.get(task_id)
            if fn is None:
                return None
            self._update(task_id, status="queued", error=None,
                         finished_at=None, heartbeat_at=_now_iso())
        self._pool.submit(self._execute, task_id)
        return self.task_status(task_id)