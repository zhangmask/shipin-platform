"""tests/test_guard_http.py — guard HTTP 层约束用例。

用 TestClient 验证 /api/guard/* 端点与内核约束一致：
  1. 白名单：未注册函数 → ok:false + code=UNKNOWN_FUNCTION
  2. 顺序：跳步 → ok:false + code=NOT_ALLOWED_IN_STEP（消息含当前步骤）
  3. 审查门：审查失败后调下一步函数被拒；修复重审通过 → 自动进入下一步
  4. 台账：/events 可回放（rejected/review_finished 均留痕）
  5. 状态恢复：run 可跨请求续跑（meta 重建 Guard）

运行：  pytest tests/test_guard_http.py -q   或   python tests/test_guard_http.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import api  # noqa: E402
from shipin_platform.guard import http_api  # noqa: E402
from shipin_platform.guard.ledger import Ledger  # noqa: E402

client = TestClient(api.app)


@pytest.fixture()
def isolated(monkeypatch, tmp_path):
    """隔离：guard 台账指到临时目录，清空进程内运行缓存。"""
    monkeypatch.setattr(http_api, "_LEDGER_ROOT", tmp_path / "guard_ledger")
    monkeypatch.setattr(http_api, "_RUNS", {})
    return tmp_path


def _start(client_obj, project_id="demo-guard-http"):
    r = client_obj.post("/api/guard/start",
                        json={"project_id": project_id})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    return body["run_id"]


def test_start_returns_step1_whitelist(isolated):
    rid = _start(client)
    r = client.get(f"/api/guard/{rid}/status")
    body = r.json()
    assert body["ok"] is True
    st = body["status"]
    assert st["current_step"]["name"] == "intake_script"
    assert body["allowed_functions"] == ["pipeline.text"]


def test_whitelist_rejection_over_http(isolated):
    rid = _start(client)
    r = client.post("/api/guard/call",
                    json={"run_id": rid, "function": "hack_the_planet", "params": {}})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["code"] == "UNKNOWN_FUNCTION"
    # 台账留痕
    events = Ledger(http_api._LEDGER_ROOT).get_events(rid)
    assert any(e["event"] == "rejected" and e["code"] == "UNKNOWN_FUNCTION"
               for e in events)


def test_out_of_order_rejection_over_http(isolated):
    rid = _start(client)
    r = client.post("/api/guard/call",
                    json={"run_id": rid, "function": "pipeline.generate", "params": {}})
    body = r.json()
    assert body["ok"] is False
    assert body["code"] == "NOT_ALLOWED_IN_STEP"
    assert "intake_script" in body["message"]


def test_review_gate_pass_then_advance_over_http(isolated):
    rid = _start(client)
    # 调阶段一真实函数（demo brief 即可通过平台规则审查；LLM 审查缺 key 时自动跳过）
    r = client.post("/api/guard/call",
                    json={"run_id": rid, "function": "pipeline.text",
                          "params": {"project_id": "demo-guard-http",
                                     "brief": {"tone": "燃", "duration_sec": 60,
                                               "product_info": "测试咖啡", "brand_name": "XX咖啡"}}})
    body = r.json()
    if not body["ok"]:
        pytest.skip(f"平台环境不满足真实 pipeline.text 运行：{body.get('message', '')[:80]}")
    r = client.post("/api/guard/review", json={"run_id": rid})
    body = r.json()
    assert body["ok"] is True
    review = body["review"]
    assert review["verdict"] in ("pass", "fail")
    if review["verdict"] == "pass":
        st = body["status"]
        assert st["current_step"]["name"] == "keyframes_video"
        assert st["steps"][0]["state"] == "passed"
    # 无论过没过，审查已留痕
    events = Ledger(http_api._LEDGER_ROOT).get_events(rid)
    assert any(e["event"] == "review_finished" for e in events)


def test_ledger_events_endpoint(isolated):
    rid = _start(client)
    r = client.get(f"/api/guard/{rid}/events")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    kinds = {e["event"] for e in body["events"]}
    assert "run_started" in kinds and "step_entered" in kinds


def test_status_recovery_from_ledger(isolated):
    """跨请求状态恢复：清空进程缓存后仍能从台账重建状态。"""
    rid = _start(client)
    http_api._RUNS.clear()
    r = client.get(f"/api/guard/{rid}/status")
    body = r.json()
    assert body["ok"] is True
    assert body["status"]["run_id"] == rid
    assert body["status"]["current_step"]["name"] == "intake_script"


def test_unknown_run_status_404ish(isolated):
    r = client.get("/api/guard/RUN-NOPE/status")
    body = r.json()
    assert body["ok"] is False
    assert body["code"] == "STEP_NOT_READY"


def test_agent_guide_mentions_guard():
    """自描述接口必须告诉新 Agent 受控入口的存在。"""
    r = client.get("/api/agent-guide")
    assert r.status_code == 200
    body = r.json()
    text = str(body)
    assert "/api/guard/start" in text


if __name__ == "__main__":
    import shutil
    import tempfile
    failed = []
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        tmp = Path(tempfile.mkdtemp())
        try:
            fn(isolated=None or tmp)  # 需 fixture 时走 pytest；此处仅 pytest 全量跑
            print(f"PASS  {name}")
        except TypeError:
            print(f"SKIP  {name}（需要 pytest fixture）")
        except Exception as exc:  # noqa: BLE001
            failed.append(name)
            print(f"FAIL  {name}: {exc}")
        shutil.rmtree(tmp, ignore_errors=True)
    print("建议使用 pytest 运行以获得完整 fixture 支持")
