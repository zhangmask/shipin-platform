"""agent_guard.ledger — 执行台账（本地落盘，按运行可查询）。

目录结构::

    <root>/runs/<run_id>/meta.json     运行摘要（状态/当前步骤/各步骤结论）
    <root>/runs/<run_id>/ledger.jsonl  事件流（时间戳 + 序号 + 逐条事件）

事件类型：run_started / step_entered / called / review_requested /
review_finished / rejected / manual_pass / step_passed / advanced /
run_finished / run_blocked

查询方式：Python API（Ledger.list_runs / get_run）或命令行::

    python agent_guard/ledger.py <root> --list
    python agent_guard/ledger.py <root> --run RUN-XXXX
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

RUNNING = "running"
COMPLETED = "completed"
BLOCKED = "blocked"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


class Ledger:
    """台账：一次运行 = runs/<run_id>/ 目录（meta + jsonl）+ SQLite 双写。

    数据库层（ledger_db.LedgerDB）默认启用（<root>/ledger.db），每条 JSONL
    事件同步落库（脱敏后），支持 SQL 审计与工作流顺序核对。
    """

    def __init__(self, root: "str | Path", db_path: "str | Path | None" = None,
                 use_db: bool = True) -> None:
        self.root = Path(root)
        self.runs_dir = self.root / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self._seq: Dict[str, int] = {}
        self._counter_file = self.root / "counter.txt"
        self.db = None
        if use_db:
            from shipin_platform.guard.ledger_db import LedgerDB
            self.db = LedgerDB(db_path or (self.root / "ledger.db"))

    # ── 生命周期 ────────────────────────────────────────────
    def start_run(self, workflow_id: str, intent: Dict[str, Any]) -> str:
        run_id = self._next_run_id()
        run_dir = self.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "run_id": run_id,
            "workflow_id": workflow_id,
            "intent": intent,
            "started_at": _now_iso(),
            "updated_at": _now_iso(),
            "status": RUNNING,
            "current_step": None,
            "steps": [],
            "finished_at": None,
            "final_summary": None,
        }
        _write_json(run_dir / "meta.json", meta)
        if self.db:
            self.db.upsert_run(run_id, workflow_id, intent, status=RUNNING,
                               started_at=meta["started_at"])
        self.record(run_id, "run_started", workflow=workflow_id, intent=intent)
        return run_id

    def _next_run_id(self) -> str:
        try:
            n = int(self._counter_file.read_text(encoding="utf-8").strip() or "0")
        except (OSError, ValueError):
            n = 0
        n += 1
        self._counter_file.write_text(str(n), encoding="utf-8")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return f"RUN-{stamp}-{n:04d}"

    # ── 记录 ────────────────────────────────────────────────
    def record(self, run_id: str, event: str, **fields: Any) -> None:
        run_dir = self.runs_dir / run_id
        if not run_dir.exists():
            raise ValueError(f"台账中不存在运行 {run_id}")
        seq = self._seq.get(run_id, 0) + 1
        self._seq[run_id] = seq
        entry = {"seq": seq, "ts": _now_iso(), "run_id": run_id,
                 "event": event, **fields}
        with (run_dir / "ledger.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if self.db:  # SQLite 双写（脱敏在 ledger_db.redact 内完成）
            self.db.insert_event(run_id, seq, entry["ts"], event,
                                 step=fields.get("step"),
                                 function=fields.get("function"),
                                 code=fields.get("code"),
                                 payload=fields)

    def update_meta(self, run_id: str, **fields: Any) -> Dict[str, Any]:
        meta = self.get_meta(run_id)
        meta.update(fields)
        meta["updated_at"] = _now_iso()
        _write_json(self.runs_dir / run_id / "meta.json", meta)
        if self.db:  # runs/steps 表与 meta.json 保持同步
            steps = meta.get("steps") or []
            for i, s in enumerate(steps, 1):
                self.db.upsert_step(run_id, i, s.get("name", f"step{i}"),
                                    s.get("state", "active"),
                                    s.get("review_attempts", 0), None)
            self.db.upsert_run(
                run_id, meta.get("workflow_id", ""), meta.get("intent", {}),
                status=meta.get("status", "running"),
                started_at=meta.get("started_at"),
                finished_at=meta.get("finished_at"),
                block_reason=meta.get("block_reason"))
        return meta

    # ── 查询 ────────────────────────────────────────────────
    def get_meta(self, run_id: str) -> Dict[str, Any]:
        p = self.runs_dir / run_id / "meta.json"
        if not p.exists():
            raise ValueError(f"台账中不存在运行 {run_id}")
        return json.loads(p.read_text(encoding="utf-8"))

    def get_events(self, run_id: str) -> List[Dict[str, Any]]:
        p = self.runs_dir / run_id / "ledger.jsonl"
        if not p.exists():
            return []
        return [json.loads(line) for line in
                p.read_text(encoding="utf-8").splitlines() if line.strip()]

    def get_run(self, run_id: str) -> Dict[str, Any]:
        return {"meta": self.get_meta(run_id), "events": self.get_events(run_id)}

    def list_runs(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        metas = []
        for d in sorted(self.runs_dir.iterdir(), reverse=True):
            mp = d / "meta.json"
            if mp.exists():
                meta = json.loads(mp.read_text(encoding="utf-8"))
                if status is None or meta.get("status") == status:
                    metas.append(meta)
        return metas


def _fmt_run_line(meta: Dict[str, Any]) -> str:
    step = meta.get("current_step") or {}
    cur = f"{step.get('index', '?')}.{step.get('name', '-')}" if step else "-"
    return (f"{meta['run_id']:<24} {meta['status']:<10} "
            f"workflow={meta['workflow_id']:<18} 当前步骤={cur:<22} "
            f"开始={meta['started_at']}")


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        return 0
    root = Path(argv[0])
    led = Ledger(root)
    if "--run" in argv:
        rid = argv[argv.index("--run") + 1]
        run = led.get_run(rid)
        print(json.dumps(run["meta"], ensure_ascii=False, indent=2))
        print("── 事件流 ──")
        for ev in run["events"]:
            print(f"#{ev['seq']:>3} {ev['ts']} [{ev['event']}] " +
                  json.dumps({k: v for k, v in ev.items()
                              if k not in ("seq", "ts", "run_id", "event")},
                             ensure_ascii=False))
        return 0
    status = None
    if "--status" in argv:
        status = argv[argv.index("--status") + 1]
    runs = led.list_runs(status)
    print(f"运行总数：{len(runs)}" + (f"（status={status}）" if status else ""))
    for meta in runs:
        print(_fmt_run_line(meta))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
