"""ProjectStageStore: SQLite-backed stage state machine for the pipeline.

Every project walks through stages in a fixed order.  A stage only becomes
PASS when its artifact hash is recorded; downstream operations call
``assert_stage_pass`` and get a ``StageGateError`` when the upstream is
missing, stale, or blocked.  This is the enforcement layer that used to be
"documented but not executed".
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Canonical stage order (must match Pipeline.STAGES).
STAGES = ["brief", "script", "storyboard", "image_prompt", "image_gen",
          "video_prompt", "video_gen", "post_production"]

STATUS = {
    "PENDING": "PENDING",
    "RUNNING": "RUNNING",
    "RETRY": "RETRY",
    "BLOCKED": "BLOCKED",
    "PASS": "PASS",
    "RELEASED": "RELEASED",
}


class StageGateError(Exception):
    """Raised when a stage gate refuses to let an operation proceed."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"[{code}] {message}")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    owner TEXT NOT NULL DEFAULT 'default'
);
CREATE TABLE IF NOT EXISTS stage_runs (
    project_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    artifact_hash TEXT,
    parent_hash TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (project_id, stage)
);
CREATE TABLE IF NOT EXISTS confirmations (
    project_id TEXT NOT NULL,
    gate TEXT NOT NULL,
    approved_by TEXT NOT NULL DEFAULT 'user',
    note TEXT,
    confirmed_at TEXT NOT NULL,
    PRIMARY KEY (project_id, gate)
);
CREATE TABLE IF NOT EXISTS clip_qc (
    project_id TEXT NOT NULL,
    shot_id TEXT NOT NULL,
    clip_path TEXT,
    verdict TEXT NOT NULL,
    report TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (project_id, shot_id)
);
CREATE TABLE IF NOT EXISTS pipeline_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    stage TEXT,
    summary TEXT NOT NULL,
    detail TEXT
);
"""

# 人工确认闸门：confirm gate -> 必须先确认才能做什么
CONFIRM_GATES = ("brief", "script", "storyboard")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dumps(data) -> str:
    import json
    return json.dumps(data, ensure_ascii=False, default=str)


class ProjectStageStore:
    """SQLite state store; thread-safe via a new connection per call."""

    def __init__(self, db_path: str = ":memory:"):
        self.db_path = db_path
        self._shared_conn: Optional[sqlite3.Connection] = None
        if db_path == ":memory:":
            # 内存库必须复用同一条连接——否则每次 _conn() 都是一个全新的
            # 空库，__init__ 里建的表全部丢失（Pipeline 默认路径就踩这里）。
            self._shared_conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._shared_conn.row_factory = sqlite3.Row
        else:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
            # 旧库无损迁移：projects 表补 owner 列（仅在缺列时执行一次）
            cols = {r[1] for r in conn.execute("PRAGMA table_info(projects)").fetchall()}
            if "owner" not in cols:
                conn.execute(
                    "ALTER TABLE projects ADD COLUMN owner TEXT "
                    "NOT NULL DEFAULT 'default'")

    def _conn(self) -> sqlite3.Connection:
        if self._shared_conn is not None:
            return self._shared_conn
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # -- project lifecycle -------------------------------------------------

    def create_project(self, project_id: str, owner: str = "default") -> None:
        if not project_id or not project_id.strip():
            raise StageGateError("EMPTY_PROJECT_ID", "project_id must be non-empty")
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO projects (project_id, created_at, owner) "
                "VALUES (?, ?, ?)",
                (project_id.strip(), _now(), owner or "default"))
        # 只在"新建成功"时留痕（已存在的幂等重入不重复记）
        if cur.rowcount:
            self.record_event(project_id, "project_created",
                              "项目创建，状态机就绪", stage="brief")

    def project_exists(self, project_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM projects WHERE project_id = ?",
                (project_id,)).fetchone()
        return row is not None

    def list_projects(self, limit: int = 50,
                      owner: Optional[str] = None) -> list[dict]:
        """项目列表（新→旧），带每个项目最新阶段状态与 PASS 数。
        前端项目页 / 外部 AI list_projects 共用；owner 过滤时只列本人项目。"""
        with self._conn() as conn:
            if owner:
                rows = conn.execute(
                    """SELECT p.project_id, p.created_at, p.owner,
                              (SELECT s2.status FROM stage_runs s2
                                WHERE s2.project_id = p.project_id
                                ORDER BY s2.rowid DESC LIMIT 1) AS latest_stage,
                              (SELECT COUNT(*) FROM stage_runs s3
                                WHERE s3.project_id = p.project_id
                                  AND s3.status = 'PASS') AS passed_stages
                       FROM projects p WHERE p.owner = ?
                       ORDER BY p.created_at DESC LIMIT ?""",
                    (owner, limit)).fetchall()
            else:
                rows = conn.execute(
                    """SELECT p.project_id, p.created_at, p.owner,
                              (SELECT s2.status FROM stage_runs s2
                                WHERE s2.project_id = p.project_id
                                ORDER BY s2.rowid DESC LIMIT 1) AS latest_stage,
                              (SELECT COUNT(*) FROM stage_runs s3
                                WHERE s3.project_id = p.project_id
                                  AND s3.status = 'PASS') AS passed_stages
                       FROM projects p
                       ORDER BY p.created_at DESC LIMIT ?""",
                    (limit,)).fetchall()
        return [dict(r) for r in rows]

    # -- stage lifecycle ----------------------------------------------------

    def begin_stage(self, project_id: str, stage: str,
                    parent_hash: Optional[str] = None) -> None:
        self._validate_stage(stage)
        self._require_project(project_id)
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO stage_runs (project_id, stage, status, artifact_hash,
                                            parent_hash, updated_at)
                   VALUES (?, ?, 'RUNNING', NULL, ?, ?)
                   ON CONFLICT (project_id, stage)
                   DO UPDATE SET status='RUNNING', parent_hash=excluded.parent_hash,
                                 updated_at=excluded.updated_at""",
                (project_id, stage, parent_hash, _now()))
        self.record_event(project_id, "stage_started",
                          f"阶段 {stage} 开始执行", stage=stage)

    def record_artifact(self, project_id: str, stage: str, artifact_hash: str,
                        parent_hash: Optional[str] = None,
                        status: str = "PASS") -> None:
        self._validate_stage(stage)
        self._require_project(project_id)
        if status not in STATUS:
            raise StageGateError("BAD_STATUS", f"unknown status {status!r}")
        if not artifact_hash or not artifact_hash.strip():
            raise StageGateError("EMPTY_HASH", f"artifact_hash required for {stage}")
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO stage_runs (project_id, stage, status, artifact_hash,
                                           parent_hash, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT (project_id, stage)
                   DO UPDATE SET status=excluded.status,
                                 artifact_hash=excluded.artifact_hash,
                                 parent_hash=excluded.parent_hash,
                                 updated_at=excluded.updated_at""",
                (project_id, stage, status, artifact_hash.strip(),
                 parent_hash, _now()))
        self.record_event(
            project_id, "stage_recorded",
            f"阶段 {stage} 产物已记录（{status}）",
            stage=stage,
            detail=f"artifact_hash={artifact_hash.strip()[:12]}…")

    def get_stage(self, project_id: str, stage: str) -> Optional[dict]:
        self._validate_stage(stage)
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM stage_runs WHERE project_id = ? AND stage = ?",
                (project_id, stage)).fetchone()
        return dict(row) if row else None

    def get_project_status(self, project_id: str) -> dict:
        self._require_project(project_id)
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT stage, status, artifact_hash, parent_hash, updated_at "
                "FROM stage_runs WHERE project_id = ? ORDER BY rowid",
                (project_id,)).fetchall()
        return {r["stage"]: dict(r) for r in rows}

    # -- gate enforcement ----------------------------------------------------

    def assert_stage_pass(self, project_id: str, stage: str,
                          artifact_hash: Optional[str] = None) -> dict:
        """Raise StageGateError unless the stage is PASS with a matching hash."""
        self._validate_stage(stage)
        self._require_project(project_id)
        row = self.get_stage(project_id, stage)
        if row is None:
            raise StageGateError(
                "STAGE_NOT_STARTED",
                f"stage '{stage}' has not been recorded for project '{project_id}'")
        if row["status"] != "PASS":
            raise StageGateError(
                "STAGE_NOT_PASS",
                f"stage '{stage}' status={row['status']} (expected PASS); "
                f"upstream must be re-run or unblocked first")
        if artifact_hash is not None and row["artifact_hash"] != artifact_hash:
            raise StageGateError(
                "HASH_MISMATCH",
                f"stage '{stage}' artifact hash changed: "
                f"stored={row['artifact_hash'][:12]}… incoming={artifact_hash[:12]}…; "
                f"downstream artifacts built on stale data must be regenerated")
        return row

    def invalidate_downstream(self, project_id: str, stage: str) -> int:
        """Mark every stage after ``stage`` as BLOCKED (stale lineage)."""
        self._validate_stage(stage)
        idx = STAGES.index(stage)
        downstream = STAGES[idx + 1:]
        if not downstream:
            return 0
        placeholders = ",".join("?" for _ in downstream)
        with self._conn() as conn:
            cur = conn.execute(
                f"UPDATE stage_runs SET status='BLOCKED', updated_at=? "
                f"WHERE project_id=? AND stage IN ({placeholders})",
                (_now(), project_id, *downstream))
        n = cur.rowcount
        if n > 0:
            self.record_event(
                project_id, "downstream_invalidated",
                f"用户/系统改写 {stage} 后，失效下游 {n} 个阶段",
                stage=stage, detail=",".join(downstream))
        return n

    # -- confirmation gates (人工确认闸门) -----------------------------------
    # ComfyUI 式硬约束：花生成钱的动作之前，必须有人确认过对应的闸门。
    # agent 无法绕过——生成端点会先查这里。

    def record_confirmation(self, project_id: str, gate: str,
                            approved_by: str = "user",
                            note: str = "") -> dict:
        if gate not in CONFIRM_GATES:
            raise StageGateError(
                "BAD_GATE", f"gate {gate!r} not in {CONFIRM_GATES}")
        self._require_project(project_id)
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO confirmations (project_id, gate, approved_by,
                                              note, confirmed_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT (project_id, gate)
                   DO UPDATE SET approved_by=excluded.approved_by,
                                 note=excluded.note,
                                 confirmed_at=excluded.confirmed_at""",
                (project_id, gate, approved_by, note, _now()))
        self.record_event(project_id, "gate_confirmed",
                          f"人工闸门 {gate} 已确认（{approved_by}）",
                          stage=gate, detail=note or "")
        return {"project_id": project_id, "gate": gate,
                "approved_by": approved_by, "confirmed": True}

    def get_confirmation(self, project_id: str, gate: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM confirmations WHERE project_id = ? AND gate = ?",
                (project_id, gate)).fetchone()
        return dict(row) if row else None

    def assert_confirmed(self, project_id: str, gate: str) -> dict:
        """Raise StageGateError unless the human confirmed this gate."""
        row = self.get_confirmation(project_id, gate)
        if row is None:
            raise StageGateError(
                "GATE_NOT_CONFIRMED",
                f"project '{project_id}': gate '{gate}' 未经用户确认——"
                f"先展示给用户并 POST /api/project/confirm (gate={gate})，"
                f"再执行花钱/进阶段操作")
        return row

    # -- per-clip QC ledger -------------------------------------------------

    def record_clip_qc(self, project_id: str, shot_id: str, clip_path: str,
                       verdict: str, report: dict) -> None:
        self._require_project(project_id)
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO clip_qc (project_id, shot_id, clip_path,
                                        verdict, report, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT (project_id, shot_id)
                   DO UPDATE SET clip_path=excluded.clip_path,
                                 verdict=excluded.verdict,
                                 report=excluded.report,
                                 updated_at=excluded.updated_at""",
                (project_id, shot_id, clip_path, verdict,
                 _json_dumps(report), _now()))

    def get_clip_qc(self, project_id: str, shot_id: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM clip_qc WHERE project_id = ? AND shot_id = ?",
                (project_id, shot_id)).fetchone()
        return dict(row) if row else None

    def list_clip_qc(self, project_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT shot_id, clip_path, verdict, updated_at FROM clip_qc "
                "WHERE project_id = ? ORDER BY shot_id",
                (project_id,)).fetchall()
        return [dict(r) for r in rows]

    # -- pipeline event ledger（执行轨迹，AI 每次调用自动留痕）----------------
    # 对标 n8n execution / Airflow TaskInstance 日志：阶段动作/人工介入/
    # 产物落点全部落到结构化事件，前端轮询即"AI 干了什么"变成可见面板。

    def record_event(self, project_id: str, kind: str, summary: str,
                     *, stage: Optional[str] = None,
                     detail: str = "") -> dict:
        """写一条执行事件。kind: project_created / stage_started /
        stage_recorded / gate_confirmed / downstream_invalidated /
        user_rewritten / gate_cleared / stage_reset / phase_started /
        phase_finished / budget_set / budget_exceeded / preflight_done。"""
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO pipeline_events "
                "(project_id, ts, kind, stage, summary, detail) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (project_id, _now(), kind, stage, summary, detail))
            seq = cur.lastrowid
        return {"seq": seq, "project_id": project_id, "kind": kind,
                "summary": summary, "stage": stage, "detail": detail}

    def list_events(self, project_id: str, limit: int = 50) -> list[dict]:
        """事件列表，新→旧（seq desc）。"""
        limit = max(1, min(int(limit), 500))
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT seq, ts, kind, stage, summary, detail "
                "FROM pipeline_events WHERE project_id = ? "
                "ORDER BY seq DESC LIMIT ?",
                (project_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def _events_rows(self, project_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT 1 FROM pipeline_events WHERE project_id = ? LIMIT 1",
                (project_id,)).fetchall()
        return rows

    def clear_confirmation(self, project_id: str, gate: str) -> bool:
        """用户改写产物后清除原确认——必须重新确认才能放行下游花钱。"""
        if gate not in CONFIRM_GATES:
            raise StageGateError(
                "BAD_GATE", f"gate {gate!r} not in {CONFIRM_GATES}")
        self._require_project(project_id)
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM confirmations WHERE project_id = ? AND gate = ?",
                (project_id, gate))
        if cur.rowcount:
            self.record_event(project_id, "gate_cleared",
                              f"产物改写：闸门 {gate} 确认已作废，需重新确认",
                              stage=gate)
        return cur.rowcount > 0

    def reset_stage(self, project_id: str, stage: str) -> None:
        """把单个阶段置回 PENDING（重跑前清态），不触碰上下游记录。"""
        self._validate_stage(stage)
        self._require_project(project_id)
        with self._conn() as conn:
            conn.execute(
                "UPDATE stage_runs SET status='PENDING', artifact_hash=NULL, "
                "parent_hash=NULL, updated_at=? "
                "WHERE project_id = ? AND stage = ?",
                (_now(), project_id, stage))
        self.record_event(project_id, "stage_reset",
                          f"阶段 {stage} 已置回 PENDING，等待重跑", stage=stage)

    # -- internal ------------------------------------------------------------

    def _validate_stage(self, stage: str) -> None:
        if stage not in STAGES:
            raise StageGateError("UNKNOWN_STAGE",
                                f"stage {stage!r} not in {STAGES}")

    def _require_project(self, project_id: str) -> None:
        if not self.project_exists(project_id):
            raise StageGateError(
                "PROJECT_NOT_FOUND",
                f"project '{project_id}' does not exist; create it first "
                f"via POST /api/project/create before touching stages")
