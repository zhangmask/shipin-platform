# Shipin Platform — MCP 开放接入

平台对外部 AI（Codex CLI / Claude Code / ZCode 等）暴露的是 **16 个受控工具**
的 MCP stdio 网关（`<repo>/tools/mcp_server.py`）。外部 AI 只能通过这些工具
操作平台：没有代码执行、没有文件读写、没有命令行入口。

## 工具清单（16）

| 工具 | 作用 |
|------|------|
| `shipin_health` | 平台健康与门禁清单 |
| `shipin_list_projects` | 项目列表（新→旧） |
| `shipin_project_status` | 单项目状态（闸门/产物/预算/近期事件） |
| `shipin_list_events` | 执行事件账本（AI 干了什么） |
| `shipin_get_artifact` | 读阶段产物（brief/script/storyboard…） |
| `shipin_preview_frames` | 成片预览帧 |
| `shipin_pipeline_text` | 提交 brief，推进到 script/storyboard |
| `shipin_project_confirm` | 显式确认闸门（必须如实填 approved_by） |
| `shipin_rewrite_stage` | 仅 script/storyboard 可改写（改写后闸门重置） |
| `shipin_preflight` | 只读体检：能不能跑、缺什么 |
| `shipin_pipeline_generate` | 生成：首帧图 → 视频 → TTS（走 key 池选 key） |
| `shipin_pipeline_assemble` | 成片 + 双层终验 |
| `shipin_report` | 项目全量报告 |
| `shipin_set_budget` | 全局/项目预算（缺省=平台全局） |
| `shipin_ingest_reference` | 参考视频（素材/风格源） |
| `shipin_integrity` | 封印核对（平台文件被改即红） |

## 接入方式

### 方式 A：Codex CLI（推荐）

```bash
codex mcp add shipin -- python D:\aishipin\shipin-platform\tools\mcp_server.py
```

要求：Python 3.11+；仓库依赖已装（`pip install -r requirements.txt`）。
MCP 网关进程内直接驱动平台（无需先启动 8766 服务）。

### 方式 B：任何走 mcpServers JSON 的客户端（ZCode / Claude Code 等）

把 `shipin.mcp.json` 里的 `mcpServers.shipin` 合并进你自己的 MCP 配置：

```json
{
  "mcpServers": {
    "shipin": {
      "command": "python",
      "args": ["D:/aishipin/shipin-platform/tools/mcp_server.py"],
      "type": "stdio"
    }
  }
}
```

ZCode: 在 MCP 配置界面新增 stdio server，命令填 `python`，参数填上面的
`args` 数组即可。

### 方式 C：直接 REST（浏览器 / curl / 脚本）

服务起来后（`python tools/desktop_app.py --no-window` 或手动
`uvicorn src.api:app --port 8766`），全部 API 见
`http://127.0.0.1:8766/docs`（OpenAPI）。MCP 工具是这些 REST 的收口，
行为完全一致。

## 纪律（平台级铁律，AI 必须遵守）

1. **确认才落闸**：改内容或花钱的步骤，用户明确确认后调用 `project_confirm`，
   且 `approved_by` 必须如实填用户标识；AI 不能自己替用户确认。
2. **改写后重确认**：每次 `rewrite_stage` 后，三个 gate（brief/script/storyboard）
   需要重新确认，直到显示 green。
3. **审过的剧本一锤定音**：分镜 review 明确的镜头（单镜连续、无内部切换、
   首尾帧锚定）就是硬约束；生成结果不符合 → 报告给用户，不静默放行。
4. **花钱先体检**：生成/成本操作前先 `shipin_preflight`；`set_budget` 管全局硬闸。
5. **密钥红线**：任何 key（AGNES_KEY / 平台 API key / SHIPIN_KEY_SEAL）**永不**
   写入日志、产物、提示词；登记 key 走 `POST /api/platform/keypool`（密文落库）。

## 自检

```bash
python tools/mcp_server.py --smoke   # 存在 --smoke 时输出工具清单并退出
printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n' \
  | python tools/mcp_server.py
```