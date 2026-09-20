"""P-2/P-4 MCP stdio 网关：16 工具硬面、JSON-RPC 握手、参数校验、错误透传、
subprocess 端到端 stdio 冒烟。

契约：外部 AI（Codex CLI 等）只能拿到这 16 个工具；没有任何代码执行入口；
确认闸门 / 花钱步骤 / 封印自检原样透传服务端结论（工具零判断）。
P4 新增 6 工具（list_events/get_artifact/rewrite_stage/set_budget/preflight/
preview_frames）+ project_status 富化（gates/artifacts/budget/events）。
"""
import json
import queue
import subprocess
import sys
import threading
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for p in (str(SRC), str(ROOT.parent / "OpenMontage-main" / "OpenMontage-main")):
    if p not in sys.path:
        sys.path.insert(0, p)

sys.path.insert(0, str(ROOT / "tools"))
from mcp_server import PROTOCOL_VERSION, ShipinMCP  # noqa: E402

import api as api_mod  # noqa: E402

EXPECTED_TOOLS = {
    "shipin_health", "shipin_list_projects", "shipin_project_status",
    "shipin_pipeline_text", "shipin_project_confirm",
    "shipin_pipeline_generate", "shipin_pipeline_assemble", "shipin_report",
    "shipin_ingest_reference", "shipin_integrity",
    # P4 增量
    "shipin_list_events", "shipin_get_artifact", "shipin_rewrite_stage",
    "shipin_set_budget", "shipin_preflight", "shipin_preview_frames",
}

NEW_TOOLS = {
    "shipin_list_events": ("project_id",),
    "shipin_get_artifact": ("project_id", "stage"),
    "shipin_rewrite_stage": ("project_id", "stage", "content"),
    "shipin_set_budget": (),
    "shipin_preflight": ("project_id",),
    "shipin_preview_frames": ("project_id",),
}

client = TestClient(api_mod.app)


def _call(handler: ShipinMCP, name: str, args: dict, _id: int = 1) -> dict:
    return handler.handle({"jsonrpc": "2.0", "id": _id,
                           "method": "tools/call",
                           "params": {"name": name, "arguments": args}})


def _result_text(resp: dict) -> str:
    return resp["result"]["content"][0]["text"]


def test_initialize_handshake():
    h = ShipinMCP()
    h.register_tools()
    r = h.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-06-18"}})
    assert r["result"]["protocolVersion"] == PROTOCOL_VERSION
    assert r["result"]["serverInfo"]["name"] == "shipin-platform-mcp"


def test_notification_no_response():
    h = ShipinMCP()
    h.register_tools()
    assert h.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) \
        is None


def test_tools_list_exactly_sixteen():
    h = ShipinMCP()
    h.register_tools()
    r = h.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in r["result"]["tools"]}
    assert names == EXPECTED_TOOLS
    assert len(names) == 16
    # 每个工具都带 JSON Schema，required 非空
    for t in r["result"]["tools"]:
        schema = t["inputSchema"]
        assert schema["type"] == "object"
        assert isinstance(schema.get("required"), list)
        assert t["description"]


def test_new_tools_schema_and_required():
    """P4 六个新工具骨架：必填参数符合契约，缺参报 -32602。"""
    h = ShipinMCP()
    h.register_tools()
    r = h.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    by_name = {t["name"]: t for t in r["result"]["tools"]}
    for name, required in NEW_TOOLS.items():
        schema = by_name[name]["inputSchema"]
        assert sorted(schema["required"]) == sorted(required), name
        for k in required:
            assert k in schema["properties"], (name, k)
    # 缺 content → 参数校验错误（不是服务端 4xx）
    r2 = _call(h, "shipin_rewrite_stage",
               {"project_id": "x", "stage": "script"})
    assert r2["error"]["code"] == -32602
    assert "content" in r2["error"]["message"]


def _new_project(tag: str) -> str:
    pid = f"mcp{tag}-{uuid.uuid4().hex[:8]}"
    r = client.post("/api/project/create", json={"project_id": pid})
    assert r.status_code == 200, r.text
    return pid


def test_call_health_and_integrity():
    h = ShipinMCP()
    h.register_tools()
    r = _call(h, "shipin_health", {})
    assert r["result"]["isError"] is False
    body = json.loads(_result_text(r))
    assert body["status"] == "ok"

    r2 = _call(h, "shipin_integrity", {}, _id=2)
    assert r2["result"]["isError"] is False
    seal = json.loads(_result_text(r2))
    assert "ok" in seal and "tampered" in seal


def test_missing_required_argument():
    h = ShipinMCP()
    h.register_tools()
    r = _call(h, "shipin_pipeline_text", {"project_id": "x"})   # 缺 brief
    assert "error" in r
    assert r["error"]["code"] == -32602
    assert "brief" in r["error"]["message"]


def test_unknown_tool_and_method():
    h = ShipinMCP()
    h.register_tools()
    r = _call(h, "shipin_nope", {})
    assert r["error"]["code"] == -32602
    r2 = h.handle({"jsonrpc": "2.0", "id": 2, "method": "nope"})
    assert r2["error"]["code"] == -32601


def test_http_business_refusal_transparent():
    """平台业务拒绝（闸门未确认）→ 200+ok:false 原样透传，AI 能看到
    被哪个闸门拦了（GATE_NOT_CONFIRMED 等），而不是吞掉。"""
    h = ShipinMCP()
    h.register_tools()
    r = _call(h, "shipin_pipeline_generate",
              {"project_id": "mcp_does_not_exist_xyz"})
    assert r["result"]["isError"] is False      # 业务拒绝≠传输错误
    body = json.loads(_result_text(r))
    assert body["ok"] is False
    assert "GATE_NOT_CONFIRMED" in body["reason"]


# ── P4：6 个新工具按『模拟 Codex 工作流』各调一次，服务端落账 ──────────


def test_status_enriched_decision_context():
    """project_status 富化：gates_pending / artifacts / budget / recent_events
    全部出现且形状可读 —— AI 的决策上下文。"""
    pid = _new_project("ctx")
    h = ShipinMCP()
    h.register_tools()
    r = _call(h, "shipin_project_status", {"project_id": pid})
    assert r["result"]["isError"] is False
    body = json.loads(_result_text(r))
    assert body["gates_pending"] == ["brief", "script", "storyboard"]
    assert isinstance(body["artifacts"], list) and body["artifacts"]
    by_stage = {a["stage"]: a for a in body["artifacts"]}
    assert by_stage["script"]["exists"] is False
    assert "budget" in body and body["budget"]["max_budget_usd"] is None
    assert body["budget"]["exceeded"] is False
    assert isinstance(body["recent_events"], list)
    assert "stages" in body and "confirmations" in body


def test_rewrite_loop_lands_artifact_versions_and_events():
    """rewrite_stage → get_artifact → list_events：产物/版本/事件全部落账。"""
    pid = _new_project("rw")
    h = ShipinMCP()
    h.register_tools()
    script = {"shots": [{"shot_id": "s1", "narration": "P4 改写落账测试"}]}
    r = _call(h, "shipin_rewrite_stage",
              {"project_id": pid, "stage": "script", "content": script})
    assert r["result"]["isError"] is False
    body = json.loads(_result_text(r))
    assert body["ok"] is True and body["stage"] == "script"
    assert body["invalidated"] >= 0

    r2 = _call(h, "shipin_get_artifact",
               {"project_id": pid, "stage": "script"})
    art = json.loads(_result_text(r2))
    assert art["exists"] is True
    assert art["content"]["shots"][0]["shot_id"] == "s1"

    r3 = _call(h, "shipin_list_events", {"project_id": pid})
    events = json.loads(_result_text(r3))["events"]
    kinds = {e["kind"] for e in events}
    assert "user_rewritten" in kinds            # 服务端事件留痕

    r4 = _call(h, "shipin_project_status", {"project_id": pid})
    st = json.loads(_result_text(r4))
    script_art = next(a for a in st["artifacts"]
                      if a["stage"] == "script")
    assert script_art["exists"] is True
    assert script_art["versions"] >= 1          # P2 快照随 _save 落账
    assert "script" in st["gates_pending"]      # 改写后确认被清空


def test_set_budget_project_and_global():
    """预算调节：项目级 + 平台全局，null 解除；服务端预算事件落账。"""
    pid = _new_project("bud")
    h = ShipinMCP()
    h.register_tools()
    try:
        r = _call(h, "shipin_set_budget",
                  {"project_id": pid, "max_budget_usd": 12.5})
        body = json.loads(_result_text(r))
        assert body["budget"]["max_budget_usd"] == 12.5

        r2 = _call(h, "shipin_set_budget",
                   {"max_budget_usd": 999.0})   # 全局（admin 代理）
        gbody = json.loads(_result_text(r2))
        assert gbody["ok"] is True
        assert gbody["global_budget"]["max_monthly_usd"] == 999.0

        r3 = _call(h, "shipin_list_events", {"project_id": pid})
        kinds = {e["kind"] for e in json.loads(_result_text(r3))["events"]}
        assert "budget_set" in kinds
    finally:
        _call(h, "shipin_set_budget", {})            # 清全局预算（防串扰）


def test_preflight_readonly_checklist():
    """preflight：只读体检，不生成任何东西，返回凭据/产物/闸门/预算逐项。"""
    pid = _new_project("pf")
    h = ShipinMCP()
    h.register_tools()
    r = _call(h, "shipin_preflight", {"project_id": pid})
    body = json.loads(_result_text(r))
    assert "ok" in body and body["ok"] is False      # 新项目产物缺失 → 不可跑
    names = {c["name"] for c in body["checks"]}
    assert {"artifact:brief.json", "artifact:script.json",
            "artifact:storyboard.json"} <= names
    assert any(n.startswith("gate:") for n in names)


def test_preview_frames_empty_until_released():
    """preview_frames：未出片 = 空数组，不炸不糊。"""
    pid = _new_project("pv")
    h = ShipinMCP()
    h.register_tools()
    r = _call(h, "shipin_preview_frames", {"project_id": pid})
    body = json.loads(_result_text(r))
    assert body["frames"] == []


def test_subprocess_stdio_handshake():
    """端到端：子进程跑 mcp_server.py，走 newline JSON-RPC 握手。"""
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "tools" / "mcp_server.py")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, cwd=str(ROOT))
    q: queue.Queue = queue.Queue()

    def pump():
        for line in proc.stdout:
            q.put(line)

    threading.Thread(target=pump, daemon=True).start()

    def send(msg: dict) -> dict:
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()
        try:
            return json.loads(q.get(timeout=90))
        except queue.Empty:
            proc.kill()
            err = proc.stderr.read() if proc.stderr else ""
            raise AssertionError(
                f"MCP 子进程无响应（stdio 卡死）。exit={proc.poll()} "
                f"stderr={err[:500]!r}")

    def notify(msg: dict) -> None:
        """通知类消息：协议规定无响应，只写不等。"""
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    try:
        n = send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2024-11-05"}})
        assert n["result"]["protocolVersion"] == "2024-11-05"
        notify({"jsonrpc": "2.0", "method": "notifications/initialized"})
        lst = send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert {t["name"] for t in lst["result"]["tools"]} == EXPECTED_TOOLS
        hl = send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                   "params": {"name": "shipin_health", "arguments": {}}})
        assert json.loads(hl["result"]["content"][0]["text"])["status"] == "ok"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()