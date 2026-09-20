"""P7 请求审计：对 /api 受保护路径的 request_audit 表。

只使用完全静态的 SQL：每条语句在 execute() 调用处内联字面量（与
stage_store.py 既有风格一致，通过 Mimosa 审计），参数全部走 ? 占位。

每行记录：调用者（key 指纹或 anonymous）、IP、方法、路由、HTTP 状态、
scope、kind（request/rate_limited 等）与 detail。中间件在 call_next 返回后
落一行（含 401/403 拒绝），导出由 admin 端点 `GET /api/platform/events`
提供 CSV/JSON（见 src/api.py），满足 OWASP API10 的记录与监测要求。
"""

from __future__ import annotations

import csv
import io
import os
import sqlite3
import threading
import time
from typing import List, Optional

# 数据根目录可被 SHIPIN_DATA_DIR 覆盖（与其余 store 一致）
DB_FILE = os.path.join(os.environ.get("SHIPIN_DATA_DIR", "data"),
                       "audit_log.db")
# 保留上限：超过后按 seq 截断最旧行，防无界增长
MAX_ROWS = 10_000

_LOCAL = threading.local()

_COLUMNS = ["seq", "ts", "caller", "ip", "method", "route", "status",
            "scope", "kind", "detail"]


def _conn() -> sqlite3.Connection:
    conn = getattr(_LOCAL, "conn", None)
    if conn is None:
        os.makedirs(os.path.dirname(DB_FILE) or ".", exist_ok=True)
        conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS request_audit ("
            "seq INTEGER PRIMARY KEY AUTOINCREMENT,"
            "ts TEXT NOT NULL,"
            "caller TEXT NOT NULL,"
            "ip TEXT NOT NULL,"
            "method TEXT NOT NULL,"
            "route TEXT NOT NULL,"
            "status INTEGER NOT NULL,"
            "scope TEXT NOT NULL,"
            "kind TEXT NOT NULL,"
            "detail TEXT NOT NULL DEFAULT '')"
        )
        conn.commit()
        _LOCAL.conn = conn
    return conn


def record(
    ts: Optional[str] = None, caller: str = "anonymous", ip: str = "",
    method: str = "", route: str = "", status: int = 0, scope: str = "",
    kind: str = "request", detail: str = "",
) -> None:
    conn = _conn()
    conn.execute(
        "INSERT INTO request_audit"
        " (ts, caller, ip, method, route, status, scope, kind, detail)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (ts or time.strftime("%Y-%m-%dT%H:%M:%S%z"),
         caller[:200], ip[:128], method[:16], route[:64],
         int(status), scope[:16], kind[:32], detail[:1024]),
    )
    conn.commit()
    conn.execute(
        "DELETE FROM request_audit WHERE seq NOT IN"
        " (SELECT seq FROM request_audit ORDER BY seq DESC LIMIT ?)",
        (MAX_ROWS,),
    )
    conn.commit()


def list_all(limit: int = 500, ip: Optional[str] = None,
             caller: Optional[str] = None) -> List[dict]:
    """按时间倒序取最近 limit 行；ip/caller 为可选精确过滤。"""
    conn = _conn()
    limit = int(limit)
    if ip and caller:
        rows = conn.execute(
            "SELECT * FROM request_audit"
            " WHERE ip = ? AND caller = ?"
            " ORDER BY seq DESC LIMIT ?",
            (ip, caller, limit),
        ).fetchall()
    elif ip:
        rows = conn.execute(
            "SELECT * FROM request_audit WHERE ip = ?"
            " ORDER BY seq DESC LIMIT ?",
            (ip, limit),
        ).fetchall()
    elif caller:
        rows = conn.execute(
            "SELECT * FROM request_audit WHERE caller = ?"
            " ORDER BY seq DESC LIMIT ?",
            (caller, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM request_audit ORDER BY seq DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(zip(_COLUMNS, r)) for r in rows]


def count() -> int:
    return _conn().execute("SELECT COUNT(*) FROM request_audit").fetchone()[0]


def to_csv(rows: List[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(_COLUMNS)
    for r in rows:
        w.writerow([r[c] for c in _COLUMNS])
    return buf.getvalue()