#!/usr/bin/env python3
"""Shipin Platform —— MCP stdio 网关（外部 AI 的受控入口）。

开放给 Codex / Claude Code 等外部 AI 的 16 个受控工具：
health / list_projects / project_status / list_events / get_artifact /
preview_frames / pipeline_text / project_confirm / rewrite_stage / preflight /
pipeline_generate / pipeline_assemble / report / set_budget / ingest_reference /
integrity。

铁律与平台级约束一致：
  · AI 只有这 16 个工具可用——没有代码执行 / 文件 / 命令入口；
  · 一切落到服务端受控编排（状态机闸门 + 人工确认 + 成本记账），工具零判断；
  · confirm 必须显式如实填写 approved_by——只有用户明确确认后才能调用；
  · set_budget 不传 project_id = 平台全局预算（admin 代理）；null 解除上限；
  · 平台工具的修改只有人（root）能通过 tools/platform_integrity.py 重新
    封印，AI 全程没有任何重建入口。

协议：stdlib newline-delimited JSON-RPC 2.0（零第三方依赖）。
接入（Codex CLI）：
    codex mcp add shipin -- python D:\\aishipin\\shipin-platform\\tools\\mcp_server.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)                       # 无论从哪启动，数据路径都锚定仓库根

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT.parent / "OpenMontage-main" / "OpenMontage-main"))
from fastapi.testclient import TestClient  # noqa: E402
import api as api_mod                # noqa: E402

PROTOCOL_VERSION = "2025-06-18"


class ToolError(Exception):
    """HTTP 4xx/5xx 或平台业务拒绝 → MCP error 透传给 AI。"""

    def __init__(self, message: str):
        super().__init__(message)


class ShipinMCP:
    """把平台 REST API 收口成 10 个 MCP 工具（服务端是唯一权威）。"""

    def __init__(self) -> None:
        self._client = TestClient(api_mod.app)
        self.version = api_mod.app.version
        self._tools: list[dict] = []
        # P0: 平台鉴权接管后，MCP 网关作为管理员代理，持 SHIPIN_ADMIN_KEY
        # 调用服务端（凭据只从环境变量读，绝不落源码）。未配置时在
        # strict 模式下服务端会 401 —— 属预期行为，AI 收到明确错误；
        # off 模式（本地开发）直接放行。
        _admin = os.environ.get("SHIPIN_ADMIN_KEY", "").strip()
        self._headers = {"X-API-Key": _admin} if _admin else {}

    # ── HTTP 桥：平台响应原样透传；4xx/5xx 翻成 MCP error ──────────────
    def _http(self, method: str, path: str, **kw) -> Any:
        headers = dict(kw.pop("headers", {}) or {})
        headers.update(self._headers)   # admin-key 代理头优先于调用方
        try:
            r = self._client.request(method, path, headers=headers, **kw)
        except Exception as e:       # 服务端内部异常（TestClient raise）
            raise ToolError(f"platform server error: {e}")
        try:
            body = r.json()
        except Exception:
            body = {"_text": r.text[:500]}
        if r.status_code >= 400:
            detail = body.get("detail", body) if isinstance(body, dict) else body
            raise ToolError(f"HTTP {r.status_code}: "
                            f"{json.dumps(detail, ensure_ascii=False) if not isinstance(detail, str) else detail}")
        return body

    def _tool(self, name: str, description: str, props: dict,
              required: list[str], fn: Callable[[dict], Any]) -> None:
        self._tools.append({
            "name": name,
            "description": description,
            "inputSchema": {
                "type": "object",
                "properties": props,
                "required": required,
            },
            "_fn": fn,
        })

    # ── 工具注册 ────────────────────────────────────────────────────────
    def register_tools(self) -> None:
        self._tool(
            "shipin_health",
            "平台健康与凭据状态（只报有无，不回显 key）。任何会话开始先调这个。",
            {}, [],
            lambda a: self._http("GET", "/api/health"))

        self._tool(
            "shipin_list_projects",
            "列出全部项目（新→旧），含最新阶段状态与 PASS 数。",
            {}, [],
            lambda a: self._http("GET", "/api/projects"))

        self._tool(
            "shipin_project_status",
            "项目详情：各阶段状态 + 确认闸门 + 逐镜 QC。",
            {"project_id": {"type": "string", "description": "项目 ID"}},
            ["project_id"],
            lambda a: self._http("GET",
                                 f"/api/project/{a['project_id']}/status"))

        self._tool(
            "shipin_pipeline_text",
            "阶段一（brief→剧本→分镜→提示词，服务端生成+审核循环）。"
            "可带 reference_id 注入参考视频画像预填 brief 空维度。"
            "返回的剧本/分镜必须展示给用户", 
            {
                "project_id": {"type": "string", "description": "项目 ID"},
                "brief": {"type": "object",
                          "description": "brief 维度（product_info/tone/"
                          "duration_sec/content_type…）"},
                "reference_id": {"type": "string",
                                 "description": "可选：参考视频报告名"
                                 "（先去 shipin_ingest_reference）"},
                "category": {"type": "string", "description": "可选品类"},
            },
            ["project_id", "brief"],
            lambda a: self._http("POST", "/api/pipeline/text",
                                 json={k: a[k] for k in
                                       ("project_id", "brief", "reference_id",
                                        "category") if k in a}))

        self._tool(
            "shipin_project_confirm",
            "人工确认闸门（brief/script/storyboard）。铁律：先把产物展示给"
            "用户、只有用户明确确认后才能调用；approved_by 必须如实填写"
            "（'user' 或实际代确认人）。",
            {
                "project_id": {"type": "string"},
                "gate": {"type": "string", "enum": ["brief", "script",
                                                    "storyboard"]},
                "approved_by": {"type": "string",
                                "description": "谁确认的（如实，默认 user）"},
                "note": {"type": "string", "description": "备注"},
            },
            ["project_id", "gate"],
            lambda a: self._http("POST", "/api/project/confirm",
                                 json={"project_id": a["project_id"],
                                       "gate": a["gate"],
                                       "approved_by": a.get("approved_by",
                                                            "user"),
                                       "note": a.get("note", "")}))

        self._tool(
            "shipin_pipeline_generate",
            "阶段二：首帧图→链式锚定视频→逐镜 QC→TTS（花钱步骤，"
            "闸门：script/storyboard 双确认）。",
            {"project_id": {"type": "string"}},
            ["project_id"],
            lambda a: self._http("POST", "/api/pipeline/generate",
                                 json={"project_id": a["project_id"]}))

        self._tool(
            "shipin_pipeline_assemble",
            "阶段三：对齐→落版→转场→调色→字幕→声音设计→终验→RELEASED。",
            {"project_id": {"type": "string"}},
            ["project_id"],
            lambda a: self._http("POST", "/api/pipeline/assemble",
                                 json={"project_id": a["project_id"]}))

        self._tool(
            "shipin_report",
            "项目全量快照：阶段状态/确认闸/manifest/QC/成本，轮询终态用。",
            {"project_id": {"type": "string"}},
            ["project_id"],
            lambda a: self._http("GET",
                                 f"/api/pipeline/{a['project_id']}/report"))

        self._tool(
            "shipin_ingest_reference",
            "参考视频纵深剖析（只吃本地路径、不发网络请求）→ "
            "reference_reports/<name>.json + 9 维 brief 预填。"
            "之后 pipeline_text 传同 name 作为 reference_id。",
            {
                "video_path": {"type": "string", "description": "本地视频路径"},
                "name": {"type": "string", "description": "报告名（作 reference_id）"},
                "scene_threshold": {"type": "number"},
                "max_shots": {"type": "integer", "minimum": 1},
            },
            ["video_path"],
            lambda a: self._http("POST", "/api/ingest/reference",
                                 json={k: a[k] for k in
                                       ("video_path", "name",
                                        "scene_threshold", "max_shots")
                                       if k in a}))

        self._tool(
            "shipin_integrity",
            "平台封印自检：src/config/tools 与 integrity.json 清单比对。"
            "ok=false 说明平台级工具已被改动（AI 无任何修改入口）。",
            {}, [],
            lambda a: self._http("GET", "/api/platform/integrity"))

        # ── P4 扩展：轨迹 / 产物 / 改写 / 预算 / 体检 / 预览 ────────────
        # 全部同源 REST（服务端唯一权威），AI 只做"读→按闸决策→写→确认"。

        self._tool(
            "shipin_list_events",
            "项目执行轨迹（新→旧，默认 50 条）：阶段动作/人工介入/AI 调用"
            "全部留痕。改任何东西前后都先看这里回放。",
            {
                "project_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            ["project_id"],
            lambda a: self._http(
                "GET", f"/api/pipeline/{a['project_id']}/events",
                params={"limit": a.get("limit", 50)}))

        self._tool(
            "shipin_get_artifact",
            "读取阶段中间产物 JSON（brief/script/storyboard/image_prompt/"
            "video_prompt/manifest/stitch/final_review）。展示给用户前先取这里。",
            {
                "project_id": {"type": "string"},
                "stage": {"type": "string",
                          "enum": ["brief", "script", "storyboard",
                                   "image_prompt", "video_prompt", "manifest",
                                   "stitch", "final_review"]},
            },
            ["project_id", "stage"],
            lambda a: self._http("GET",
                                 f"/api/pipeline/{a['project_id']}/artifact",
                                 params={"stage": a["stage"]}))

        self._tool(
            "shipin_rewrite_stage",
            "人工改写 script/storyboard 产物（服务端写回→清确认→下游全部"
            "失效→事件留痕）。改完必须重新 project_confirm 再跑 generate，"
            "杜绝『改完旧链条继续花钱』。",
            {
                "project_id": {"type": "string"},
                "stage": {"type": "string", "enum": ["script", "storyboard"]},
                "content": {"type": "object",
                            "description": "整份产物 JSON（script/storyboard "
                            "至少含非空 shots 数组）"},
            },
            ["project_id", "stage", "content"],
            lambda a: self._http("POST",
                                 f"/api/pipeline/{a['project_id']}/rewrite",
                                 json={"stage": a["stage"],
                                       "content": a["content"]}))

        self._tool(
            "shipin_set_budget",
            "预算调节：传 project_id = 项目预算上限；不传 project_id = 平台"
            "全局月度预算（admin 代理）。max_budget_usd 传 null 表示解除上限。"
            "generate/assemble 由服务端硬闸（409/422），AI 无法绕过。",
            {
                "project_id": {"type": "string",
                               "description": "可选；缺省=平台全局预算"},
                "max_budget_usd": {"type": ["number", "null"],
                                   "description": "上限金额；null=解除"},
            },
            [],
            lambda a: (self._http(
                "POST", f"/api/pipeline/{a['project_id']}/budget",
                json={"max_budget_usd": a.get("max_budget_usd")})
                if a.get("project_id") else
                self._http("POST", "/api/platform/budget",
                           json={"max_monthly_usd": a.get("max_budget_usd")})))

        self._tool(
            "shipin_preflight",
            "只读体检：凭据（只报有/无）/前置产物/闸门/预算/目录，一个都不"
            "生成，只回答『现在能不能跑』。花钱步骤前必须调用。",
            {"project_id": {"type": "string"}},
            ["project_id"],
            lambda a: self._http("POST",
                                 f"/api/pipeline/{a['project_id']}/preflight",
                                 json={}))

        self._tool(
            "shipin_preview_frames",
            "成片预览帧列表（assemble 后自动抽 3 帧；未出片 = 空数组）。"
            "给用户看成片效果前调用。",
            {"project_id": {"type": "string"}},
            ["project_id"],
            lambda a: self._http(
                "GET", f"/api/pipeline/{a['project_id']}/preview"))

    # ── JSON-RPC 2.0 消息处理 ──────────────────────────────────────────
    def handle(self, msg: dict) -> Optional[dict]:
        msg_id = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params") or {}

        if method == "initialize":
            return self._jsonrpc(msg_id, {
                "protocolVersion": (params.get("protocolVersion")
                                    or PROTOCOL_VERSION),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "shipin-platform-mcp",
                               "version": api_mod.app.version},
            })
        if method == "notifications/initialized":
            return None                     # 通知无响应
        if method == "ping":
            return self._jsonrpc(msg_id, {})
        if method == "tools/list":
            return self._jsonrpc(msg_id, {
                "tools": [{
                    "name": t["name"], "description": t["description"],
                    "inputSchema": t["inputSchema"],
                } for t in self._tools]})
        if method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            tool = next((t for t in self._tools if t["name"] == name), None)
            if tool is None:
                return self._jsonrpc_error(msg_id, -32602,
                                           f"unknown tool: {name}")
            # 必填校验（与服务端契约一致）
            missing = [k for k in tool["inputSchema"].get("required", [])
                       if args.get(k) in (None, "")]
            if missing:
                return self._jsonrpc_error(
                    msg_id, -32602,
                    f"missing required argument(s): {', '.join(missing)}")
            try:
                result = tool["_fn"](args)
                return self._jsonrpc(msg_id, {
                    "content": [{"type": "text",
                                 "text": json.dumps(result, ensure_ascii=False,
                                                    indent=2)}],
                    "isError": False,
                })
            except ToolError as e:
                return self._jsonrpc(msg_id, {
                    "content": [{"type": "text", "text": str(e)}],
                    "isError": True,
                })
            except Exception as e:          # 不应发生：平台异常也要如实报
                return self._jsonrpc(msg_id, {
                    "content": [{"type": "text",
                                 "text": f"internal error: {e!r}"}],
                    "isError": True,
                })
        return self._jsonrpc_error(msg_id, -32601,
                                   f"method not found: {method}")

    @staticmethod
    def _jsonrpc(msg_id: Optional[Any], result: Any) -> dict:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    @staticmethod
    def _jsonrpc_error(msg_id: Optional[Any], code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": msg_id,
                "error": {"code": code, "message": message}}


def main() -> int:
    handler = ShipinMCP()
    handler.register_tools()
    # newline-delimited JSON-RPC over stdio
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            sys.stdout.write(json.dumps({
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32700, "message": f"parse error: {e}"}
            }) + "\n")
            sys.stdout.flush()
            continue
        resp = handler.handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())