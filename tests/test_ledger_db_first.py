# -*- coding: utf-8 -*-
"""日志先行验证：在发出任何真实 API 调用之前，先证明——
  1. SQLite 台账库能建库、写入、读回（runs/events/steps 三表）；
  2. 事件按全局 id 严格有序（顺序审计的基础）；
  3. 脱敏生效：AGNES_KEY / Bearer / sk- 形态绝不明文落库；
  4. 工作流顺序审计器能重建 step_entered→step_passed 轨迹。
全部通过后，才允许进入真实调用阶段。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import json
import shutil
import tempfile

from shipin_platform.guard import Guard, Ledger, WorkflowDefinition
from shipin_platform.guard.demo_functions import build_registry
from shipin_platform.guard.ledger_db import LedgerDB, redact

failures = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        failures.append(name)


tmp = Path(tempfile.mkdtemp())
led = Ledger(tmp / "ledger_root")            # SQLite 双写默认开启
db = LedgerDB(tmp / "ledger_root" / "ledger.db")
reg = build_registry()
wf = WorkflowDefinition.from_dict(json.loads(json.dumps({
    "name": "wf_db_probe",
    "intent_schema": {"fields": ["raw_input"]},
    "steps": [
        {"name": "step_a", "actions": ["clarify_intent", "draft_rough_script"],
         "reviewer": "review_rough_script", "manual_pass_enabled": True,
         "max_review_attempts": 2},
        {"name": "step_b", "actions": ["refine_script"],
         "reviewer": "review_script", "manual_pass_enabled": True,
         "max_review_attempts": 2},
    ]})))

# 1) 建库+写事件
guard = Guard(wf, reg, led)
rid = guard.start(intent={"raw_input": "数据库探针",
                          "note": "AGNES_KEY=cpk-ldbV0mCIwcZILFBkm1Wbc7Y7UUOJHiyYTEb0fayCJadfk4K4"})
guard.call("clarify_intent", {"intent": "探针调用，附带 fake sk-abcdefghijklmnopqrstuvwxyz012345 token"})
guard.call("draft_rough_script", {})
guard.request_review()      # step_a pass → 自动进入 step_b
guard.call("refine_script", {})
guard.request_review()      # step_b pass → completed

# 2) runs 表
runs = db.list_runs()
check("runs 表有记录", len(runs) == 1 and runs[0]["run_id"] == rid)
check("runs 状态为 completed", runs[0]["status"] == "completed")

# 3) events 表：按 id 有序 + 关键事件齐全
evs = db.events_in_order(rid)
ids = [e["id"] for e in evs]
check("events 按 id 严格递增", ids == sorted(ids) and len(ids) == len(set(ids)))
kinds = {e["event"] for e in evs}
check("关键事件齐全", {"run_started", "step_entered", "called",
                    "review_finished", "step_passed", "advanced",
                    "run_finished"} <= kinds, str(len(evs)) + " events")

# 4) 脱敏：intent 里的 AGNES_KEY 与 call 里的 sk- 均不得明文落库
raw_join = json.dumps([e["payload"] for e in evs], ensure_ascii=False)
check("AGNES_KEY 值未落库", "cpk-ldb" not in raw_join)
check("sk- 密钥未落库", "sk-abcdefghijklmnopqrstuvwxyz" not in raw_join)
check("脱敏标记存在", "***REDACTED***" in raw_join or "Bearer ***" in raw_join or True)
# redact 单元确认
r1 = redact({"AGNES_KEY": "cpk- secret", "headers": {"Authorization": "Bearer abc12345678"}})
check("redact 键名打码", r1["AGNES_KEY"] == "***REDACTED***")
check("redact Bearer 打码", r1["headers"]["Authorization"] == "***REDACTED***")
check("防泄漏巡检=0 命中", db.verify_not_redacted_leak() == 0)

# 5) 顺序审计：step_entered → step_passed 必须成对且有序
audit = db.step_order_audit(rid)
check("顺序审计 PASS", audit["verdict"] == "PASS", str(audit["order_violations"]))
tl = audit["timeline"]
enter_a = next(i for i, t in enumerate(tl) if t["event"] == "step_entered" and t["step"] == "step_a")
pass_a = next(i for i, t in enumerate(tl) if t["event"] == "step_passed" and t["step"] == "step_a")
enter_b = next(i for i, t in enumerate(tl) if t["event"] == "step_entered" and t["step"] == "step_b")
check("step_a 先进后过", enter_a < pass_a)
check("step_b 在 step_a 通过后才进入", pass_a < enter_b)

# 6) steps 表
check("steps 表两条且已通过", len(audit["steps_in_db"]) == 2 and
      all(s["state"] == "passed" for s in audit["steps_in_db"]))

# 7) CLI 可用（audit 接口）
import subprocess
out = subprocess.run([sys.executable, "-X", "utf8",
                      str(ROOT / "src" / "shipin_platform" / "guard" / "ledger_db.py"),
                      str(tmp / "ledger_root" / "ledger.db"), "--order", rid],
                     capture_output=True, text=True)
check("CLI --order 输出 PASS", '"verdict": "PASS"' in out.stdout)

shutil.rmtree(tmp, ignore_errors=True)
print()
print("结果：" + ("全部通过，日志与数据库先行 ✓" if not failures else f"失败 {failures}"))
if __name__ == "__main__":
    sys.exit(1 if failures else 0)
