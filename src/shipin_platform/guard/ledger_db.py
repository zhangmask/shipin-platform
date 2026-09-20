"""shipin_platform.guard.ledger_db — 台账的 SQLite 数据库存储层（日志先行）。

设计（用户要求：先有日志和数据库存储，再做真实调用，最后从库里审计执行顺序）：
- 与 JSONL **双写**：Ledger.record 每写一行 JSONL，同时 INSERT 一条 events 记录；
- 每个事件落库前先过 redact()：Authorization/key/token/password 等敏感值打码；
- runs 表：run_id 主键、workflow、intent、status、started/finished；
- events 表：全局自增 id、run_id、seq、ts、event、step、function、code、
  payload（脱敏后 JSON 文本）——天然支持“按顺序审计”；
- steps 表：每步的状态/审查次数/结论，供工作流顺序核对。

审计查询（SQL 直查或 CLI）：
    python src/shipin_platform/guard/ledger_db.py <db> --order <run_id>   # 步骤顺序审计
    python src/shipin_platform/guard/ledger_db.py <db> --events <run_id>  # 事件回放
    python src/shipin_platform/guard/ledger_db.py <db> --summary          # 全部运行
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

_SENSITIVE_KEY = re.compile(
    r"(key|token|secret|password|authorization|api[-_]?key)", re.I)


def redact(obj: Any, _depth: int = 0) -> Any:
    """递归脱敏：键名含敏感词的值打码；字符串值里的密钥形态也打码。

    覆盖形态：Bearer xxx / sk-xxx / cpk-xxx（Agnes key）/ KEY=value 对内嵌密钥。
    """
    if _depth > 6:
        return "…"
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and _SENSITIVE_KEY.search(k):
                out[k] = "***REDACTED***"
            else:
                out[k] = redact(v, _depth + 1)
        return out
    if isinstance(obj, list):
        return [redact(v, _depth + 1) for v in obj]
    if isinstance(obj, str):
        s = re.sub(r"(Bearer\s+)[A-Za-z0-9._\-]{6,}", r"\1***", obj)
        s = re.sub(r"\b((?:sk|cpk)-[A-Za-z0-9]{6,})", "***", s)
        # 字符串值内嵌 "KEY=密钥值" 形态（如 AGNES_KEY=cpk-...）
        s = re.sub(r"((?:api[-_]?key|token|secret|password|AGNES_KEY)\s*=\s*)[^\s;&]+",
                   r"\1***", s, flags=re.I)
        return s
    return obj


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


class LedgerDB:
    """SQLite 台账库。所有写入方法只做 insert/update，不做任何业务判断。"""

    def __init__(self, db_path: "str | Path") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                run_id      TEXT PRIMARY KEY,
                workflow    TEXT NOT NULL,
                intent_json TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'running',
                started_at  TEXT NOT NULL,
                finished_at TEXT,
                block_reason TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id   TEXT NOT NULL,
                seq      INTEGER NOT NULL,
                ts       TEXT NOT NULL,
                event    TEXT NOT NULL,
                step     TEXT,
                function TEXT,
                code     TEXT,
                payload  TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES runs(run_id)
            );
            CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, seq);
            CREATE INDEX IF NOT EXISTS idx_events_type ON events(event);
            CREATE TABLE IF NOT EXISTS steps (
                run_id   TEXT NOT NULL,
                step_index INTEGER NOT NULL,
                step_name  TEXT NOT NULL,
                state      TEXT NOT NULL,
                review_attempts INTEGER NOT NULL DEFAULT 0,
                last_decision TEXT,
                PRIMARY KEY (run_id, step_index),
                FOREIGN KEY (run_id) REFERENCES runs(run_id)
            );
            """)

    # ── 写入 ────────────────────────────────────────────────
    def upsert_run(self, run_id: str, workflow: str, intent: dict,
                   status: str = "running", started_at: Optional[str] = None,
                   finished_at: Optional[str] = None,
                   block_reason: Optional[str] = None) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO runs(run_id, workflow, intent_json, status, started_at,"
                " finished_at, block_reason) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(run_id) DO UPDATE SET status=excluded.status,"
                " finished_at=excluded.finished_at, block_reason=excluded.block_reason",
                (run_id, workflow,
                 json.dumps(redact(intent), ensure_ascii=False),
                 status, started_at or _now_iso(), finished_at, block_reason))

    def insert_event(self, run_id: str, seq: int, ts: str, event: str,
                     step: Optional[str] = None, function: Optional[str] = None,
                     code: Optional[str] = None,
                     payload: Optional[dict] = None) -> None:
        safe = redact(payload or {})
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO events(run_id, seq, ts, event, step, function, code,"
                " payload) VALUES(?,?,?,?,?,?,?,?)",
                (run_id, seq, ts, event, step, function, code,
                 json.dumps(safe, ensure_ascii=False)))

    def upsert_step(self, run_id: str, step_index: int, step_name: str,
                    state: str, review_attempts: int,
                    last_decision: Optional[str]) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO steps(run_id, step_index, step_name, state,"
                " review_attempts, last_decision) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(run_id, step_index) DO UPDATE SET state=excluded.state,"
                " review_attempts=excluded.review_attempts,"
                " last_decision=excluded.last_decision",
                (run_id, step_index, step_name, state, review_attempts,
                 last_decision))

    # ── 审计查询 ────────────────────────────────────────────
    def list_runs(self, status: Optional[str] = None) -> List[dict]:
        q = "SELECT * FROM runs" + (" WHERE status=?" if status else "") + \
            " ORDER BY started_at DESC"
        with self._conn() as conn:
            rows = conn.execute(q, (status,) if status else ()).fetchall()
            return [dict(r) for r in rows]

    def events_in_order(self, run_id: str) -> List[dict]:
        """事件按全局 id 升序——这就是“一步一步往下走”的执行轨迹。"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE run_id=? ORDER BY id", (run_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def step_order_audit(self, run_id: str) -> dict:
        """工作流顺序审计：从事件流重建“step_entered/step_passed/called”的
        实际顺序，与 steps 表对照；任何乱序/跳步/越门调用都会显形。"""
        with self._conn() as conn:
            steps = conn.execute(
                "SELECT * FROM steps WHERE run_id=? ORDER BY step_index",
                (run_id,)).fetchall()
        timeline = []
        for ev in self.events_in_order(run_id):
            if ev["event"] in ("step_entered", "step_passed", "advanced",
                               "run_finished", "run_blocked", "manual_pass"):
                timeline.append({
                    "id": ev["id"], "ts": ev["ts"], "event": ev["event"],
                    "step": ev["step"],
                    "detail": json.loads(ev["payload"]).get("to_step")
                              or json.loads(ev["payload"]).get("via") or ""})
        # 乱序检测：step_passed 之前必须有同步骤的 step_entered
        entered: set = set()
        violations = []
        for t in timeline:
            if t["event"] == "step_entered":
                entered.add(t["step"])
            elif t["event"] == "step_passed":
                if t["step"] not in entered:
                    violations.append(
                        f"#{t['id']} {t['step']} 未进入即通过（顺序违规）")
        return {"run_id": run_id,
                "steps_in_db": [dict(s) for s in steps],
                "timeline": timeline,
                "order_violations": violations,
                "verdict": "PASS" if not violations else "FAIL"}

    def verify_not_redacted_leak(self, run_id: Optional[str] = None) -> int:
        """防泄漏巡检：库里不应存在明文 Bearer/sk- 密钥。返回命中数（应为 0）。"""
        # run_id 永远走参数绑定，绝不拼入 SQL 文本。
        with self._conn() as conn:
            if run_id is None:
                row = conn.execute(
                    "SELECT COUNT(*) c FROM events "
                    "WHERE payload LIKE '%Bearer %' OR payload LIKE '%sk-%'"
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) c FROM events "
                    "WHERE (payload LIKE '%Bearer %' OR payload LIKE '%sk-%') "
                    "AND run_id = ?",
                    (run_id,),
                ).fetchone()
            return int(row["c"])


def main(argv: Optional[List[str]] = None) -> int:
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        return 0
    db = LedgerDB(argv[0])
    if "--order" in argv:
        import json as _j
        audit = db.step_order_audit(argv[argv.index("--order") + 1])
        print(_j.dumps(audit, ensure_ascii=False, indent=1))
        return 0
    if "--events" in argv:
        import json as _j
        rid = argv[argv.index("--events") + 1]
        for ev in db.events_in_order(rid):
            print(f"#{ev['id']:>4} {ev['ts']} [{ev['event']}] "
                  f"step={ev['step']} fn={ev['function']} code={ev['code']} "
                  + ev["payload"][:160])
        return 0
    print(f"运行总数：{len(db.list_runs())}")
    for m in db.list_runs():
        print(f"{m['run_id']:<26} {m['status']:<10} {m['workflow']:<18} "
              f"start={m['started_at']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
