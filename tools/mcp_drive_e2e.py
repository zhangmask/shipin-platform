"""stdio MCP 端到端驱动：模拟外部 AI（Codex/ZCode）操作平台一遍。"""
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

pid = f"mcp-e2e-{uuid.uuid4().hex[:10]}"
env = dict(os.environ)
env["SHIPIN_AUTH_MODE"] = "off"
env["SHIPIN_RATE_MODE"] = "off"
proc = subprocess.Popen(
    [sys.executable, "tools/mcp_server.py"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    stderr=subprocess.DEVNULL, cwd=str(ROOT), env=env, text=True)


def rpc(method, params):
    req = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        req["params"] = params
    proc.stdin.write(json.dumps(req) + "\n")
    proc.stdin.flush()
    resp = json.loads(proc.stdout.readline())
    if "error" in resp:
        return {"_rpc_error": resp["error"]}
    txt = resp["result"]["content"][0]["text"]
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        return {"_text": txt}


steps = {}
steps["health"] = rpc("tools/call", {"name": "shipin_health", "arguments": {}})
steps["create"] = rpc("tools/call", {"name": "shipin_pipeline_text", "arguments": {
    "project_id": pid,
    "brief": {
        "content_type": "product",
        "product_info": "智能咖啡机，一键现磨，清晨第一杯",
        "target_platform": "douyin",
        "duration_sec": 15,
        "target_audience": "都市白领 25-35 岁",
        "tone": "温暖/品质感",
        "creative_direction": "清晨窗边咖啡机工作特写，蒸汽在逆光中升腾，"
                              "按键亮起，成品拉到杯口；结尾 logo 落版",
        "reference_materials": "",
        "special_requirements": "16:9，底部字幕",
        "style_anchor": "golden morning light, cinematic, shallow depth of field",
        "hook": "杯子自动推到你面前",
        "ending": "品牌落版 + 一句 slogan",
    }}})
steps["status"] = rpc("tools/call", {"name": "shipin_project_status", "arguments": {
    "project_id": pid}})
steps["confirm_script"] = rpc("tools/call", {"name": "shipin_project_confirm",
    "arguments": {"project_id": pid, "gate": "script", "approved_by": "e2e-user"}})
steps["confirm_storyboard"] = rpc("tools/call", {"name": "shipin_project_confirm",
    "arguments": {"project_id": pid, "gate": "storyboard", "approved_by": "e2e-user"}})
steps["events"] = rpc("tools/call", {"name": "shipin_list_events",
                      "arguments": {"project_id": pid, "limit": 10}})
steps["preflight"] = rpc("tools/call", {"name": "shipin_preflight",
                        "arguments": {"project_id": pid}})

proc.terminate()

print("PID:", pid)
for name, r in steps.items():
    if "_rpc_error" in r:
        print(f"[{name}] RPC-ERROR {r['_rpc_error']}")
        continue
    if "_text" in r:
        print(f"[{name}] TEXT {r['_text'][:200]}")
        continue
    if name == "create":
        print(f"[{name}] ok={r.get('ok')} phase={r.get('phase')} "
              f"reason={str(r.get('reason'))[:160]}")
        continue
    if name == "events":
        kinds = [e["kind"] for e in r.get("events", [])]
        print(f"[{name}] kinds={kinds}")
        continue
    if name == "status":
        gates = r.get("gates")
        print(f"[{name}] gates={gates}")
        continue
    if name == "preflight":
        ok = r.get("ok")
        bad = [c.get("name") for c in r.get("checks", []) if not c.get("ok")]
        print(f"[{name}] ok={ok} checks={len(r.get('checks', []))} "
              f"need={bad}")
        continue
    print(f"[{name}] ok={r.get('ok')} keys={list(r.keys())[:6]}")