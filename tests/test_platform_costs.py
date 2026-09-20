"""P3 全局成本测试：跨项目聚合（OpenCost 式 showback）+ 全局月度硬闸。

验收：《平台级演进计划》P3 —— 造 3 个项目不同成本，聚合正确；
全局预算设为小于当前累计 → 下一个 generate 被 422；清除全局预算 → 解闸。
"""
import json
import time
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

import api as api_mod
from shipin_platform.services import costing

client = TestClient(api_mod.app)

BUDGET_FILE = costing.GLOBAL_BUDGET_PATH


def _pid(tag: str) -> str:
    return f"g3{tag}-{uuid.uuid4().hex[:8]}"


def _make_project(pid: str) -> None:
    r = client.post("/api/project/create", json={"project_id": pid})
    assert r.status_code == 200, r.text


def _blat(pid: str, usd: float, ts: float) -> None:
    """直写成本台账（避开 record_cost 的计价模型，只测试聚合）。"""
    fp = costing.cost_file(pid)          # 已确保父目录存在
    rows = json.loads(fp.read_text(encoding="utf-8")) if fp.exists() else []
    rows.append({"seq": len(rows) + 1, "ts": ts, "kind": "image",
                 "model": "t3", "units": 1.0, "usd": usd, "note": "p3-fixture"})
    fp.write_text(json.dumps(rows, ensure_ascii=False, indent=1),
                  encoding="utf-8")


def _clear_global_budget() -> None:
    costing.set_global_budget(None)
    assert (not BUDGET_FILE.exists()
            or BUDGET_FILE.read_text(encoding="utf-8") == "{}")


# ── 聚合正确性 ───────────────────────────────────────────────────

def test_global_aggregation_over_three_projects():
    _clear_global_budget()
    now = time.time()
    week_ago = now - 3 * 86400      # 仍在本月、近 7 日
    month_ago = now - 40 * 86400    # 上月（不属本月窗口）
    p1, p2, p3 = _pid("a1"), _pid("a2"), _pid("a3")
    for p in (p1, p2, p3):
        _make_project(p)
    before = costing.global_cost_summary()
    _blat(p1, 1.0, now)
    _blat(p2, 2.0, week_ago)
    _blat(p3, 0.5, month_ago)
    _blat(p3, 0.2, now)
    after = costing.global_cost_summary()

    # 总额增量 = 1 + 2 + 0.5 + 0.2
    assert round(after["total_usd"] - before["total_usd"], 6) == 3.7
    # 本月窗口增量 = 1 + 2 + 0.2（40 天前的 0.5 排除）
    assert round(after["month_usd"] - before["month_usd"], 6) == 3.2
    # 近 7 日增量同样 = 3.2
    assert round(after["week_usd"] - before["week_usd"], 6) == 3.2
    assert after["projects"] - before["projects"] >= 3
    # 每项目台账独立正确（top 列表可能被其他项目挤占，不做全局排序断言）
    assert costing.cost_summary(p1)["total_usd"] == 1.0
    assert costing.cost_summary(p2)["total_usd"] == 2.0
    assert costing.cost_summary(p3)["total_usd"] == 0.7


def test_platform_costs_endpoint():
    _clear_global_budget()
    r = client.get("/api/platform/costs")
    assert r.status_code == 200
    body = r.json()
    assert "total_usd" in body and "month_usd" in body
    assert "top_projects" in body


# ── 全局月度硬闸 ─────────────────────────────────────────────────

def test_global_budget_gate_blocks_generate():
    _clear_global_budget()
    pid = _pid("blk")
    _make_project(pid)
    _blat(pid, 0.9, time.time())
    # 设上限 0.5 → 已用 0.9 超限
    r = client.post("/api/platform/budget", json={"max_monthly_usd": 0.5})
    assert r.status_code == 200
    assert r.json()["global_budget"]["max_monthly_usd"] == 0.5
    # 同步车道被 422，且消息指明全局
    r2 = client.post("/api/pipeline/generate", json={"project_id": pid})
    assert r2.status_code == 422
    assert "全局月度预算超限" in r2.json()["detail"]
    # async 车道同样被拦（闸在入队前）
    r3 = client.post("/api/pipeline/generate?async=true",
                     json={"project_id": pid})
    assert r3.status_code == 422
    # 事件留痕（budget_exceeded 全局）
    store = api_mod._stage_store()
    evs = [e for e in store.list_events(pid) if e["kind"] == "budget_exceeded"]
    assert evs and "全局" in evs[0]["summary"]
    # 全局总览暴露超限状态
    costs = client.get("/api/platform/costs").json()
    assert costs["exceeded"] is True
    assert costs["overrun_usd"] > 0
    # 清除预算 → 解闸（项目无单闸时不再拦）
    r4 = client.post("/api/platform/budget", json={"max_monthly_usd": None})
    assert r4.status_code == 200
    r5 = client.post("/api/pipeline/generate", json={"project_id": pid})
    assert r5.status_code != 422


def test_global_budget_rejects_negative():
    r = client.post("/api/platform/budget", json={"max_monthly_usd": -1})
    # 负值视为清除（回到无上限）
    assert r.status_code == 200
    assert costing.read_global_budget() == {}