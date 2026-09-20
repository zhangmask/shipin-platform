# Shipin Platform — AI 视频生成平台（服务端编排）

基于 OpenMontage / openshorts / Whisper / FFmpeg 等开源项目整合的 AI 视频生成平台，
以「文案 → 分镜 → 素材 → 合成 → 审查」流水线为核心，提供 **网页面板 / MCP 网关 / REST API**
三层入口；全流程产物版本化、任务异步化、成本全局可控、操作全量审计。

---

## 一、三层入口

| 层 | 地址 | 适用对象 | 说明 |
|---|---|---|---|
| 🌐 网页面板 | `http://<host>:8765/ui/`（本机模式 `:8766`） | 人 | 项目看板：阶段卡片与闸门状态、生成/合成进度条（异步轮询）、中间产物 diff、改写、版本回滚、事件瀑布流、成本总览、导出物预览 |
| 🤖 MCP 网关 | stdio：`python tools/mcp_server.py` | 外部 AI Agent | JSON-RPC 2.0，**16 个工具**（会话/观察/编辑/推进/预算/参考），Agent 只能通过工具推进流水线，无任何代码执行/文件读写入口；所有写操作落台账，失败返回 `ok:false` + 稳定错误码 |
| 🔌 REST API | `http://<host>:8766/api/*` | 程序化集成 | FastAPI 全端点；健康检查 / 项目 / 流水线 / 版本 / 成本 / 审计全开放，鉴权见下 |

> 首次接入的 AI Agent 请先读 `AGENT_GUIDE.md`（或 `GET /api/agent-guide`）：
> 「查看状态 → 评审 → 确认闸 → 生成」的标准推进纪律由服务端状态机强制。

## 二、鉴权

```
SHIPIN_AUTH_MODE   # strict（默认）| off（仅本机开发/演示）
SHIPIN_ADMIN_KEY   # off 模式下签发 API key 用的管理员口令
```

- **默认 strict**：`/api/*` 全部要求请求头 `X-API-Key`；服务端只存 key 的 **sha256 指纹**，明文仅在签发时返回一次。
- `POST /api/platform/keys`（admin scope）签发：`{label, scopes?}`，scopes ∈ `read / write / admin`。
- 前端登录页输入 key 后仅存浏览器 `localStorage`（键 `shipin.api_key`），可随时清除（设置 → 清除键 / 页面"退出登录"）。
- `tests/conftest.py` 强制 off，CI 与开发一致；无 key 的请求在 strict 下返回 401。

## 三、部署

### 方式 A：Docker（推荐）

```bash
docker compose up -d --build            # 多阶段镜像：node 构建前端 → python3.12+ffmpeg 运行时
curl -s http://127.0.0.1:8765/api/health   # {"status":"ok", ...}
# 打开 http://127.0.0.1:8765/ui/
```

- 命名卷 `shipin-data:/app/data` 持久化 SQLite 三库 + 项目产物 ledger；
- cloud/认证 env（`OPENAI_API_KEY`、`WHISPER_MODEL`、`SHIPIN_AUTH_MODE`、`SHIPIN_ADMIN_KEY`）由宿主机透传，**不写进镜像**；
- 容器 HEALTHCHECK 每 30s 探 `/api/health`，`restart: unless-stopped`。

### 方式 B：本地一键脚本（无 Docker）

```bash
./start.sh            # Linux/macOS：npm 构建前端 → PYTHONPATH=src → uvicorn :8766（--port 可改）
.\start.ps1          # Windows PowerShell 等效（-Port / -Reload）
```

手动拆解：

```bash
pip install -r requirements.txt          # 运行依赖（系统需装 ffmpeg）
cd web && npm ci && npm run build && cd ..  # 产物 web/dist（FastAPI 挂载 /ui）
PYTHONPATH=src python -m uvicorn src.api:app --host 0.0.0.0 --port 8766
```

## 四、MCP 接入（外部 AI Agent）

```bash
# 示例：注册到支持 MCP 的客户端
codex mcp add shipin -- python D:\aishipin\shipin-platform\tools\mcp_server.py
```

| 组 | 工具 |
|---|---|
| 会话/观察 | `shipin_health` `shipin_list_projects` `shipin_project_status`（返回 gates_pending / artifacts / budget / recent_events）`shipin_list_projects` `shipin_report` |
| 推进 | `shipin_pipeline_text` `shipin_project_confirm` `shipin_pipeline_generate` `shipin_pipeline_assemble` `shipin_preflight` |
| 编辑/版本 | `shipin_rewrite_stage`（改写文案/分镜，内容哈希版本化）`shipin_make_version_snapshot` `shipin_restore_version` |
| 预算/参考 | `shipin_cost_summary` `shipin_set_budget` `shipin_ingest_reference` `shipin_integrity` `shipin_preview_frames` `shipin_list_events` `shipin_get_artifact` |

（共 16 个工具，工具清单与缺失参数校验由 `tests/test_mcp.py` 钉死为契约，变更即红。）

## 五、数据与产物

```
data/
├── projects/<project_id>/     # 各阶段产物 JSON ledger + versions/ 内容寻址快照 + versions.json 索引
├── stage_store.db             # 阶段状态机（闸门/确认/下游 BLOCKED）
├── auth_keys.db               # API key 指纹 + scopes
├── tasks.db                   # 异步任务（心跳 + zombie 扫描 + retry）
└── audit.db                   # 操作事件流（caller/IP/舞台）
```

版本相关：`GET /api/pipeline/<id>/versions` 列历史；`POST /api/pipeline/<id>/restore` 回滚
（自动清确认 → 重置闸门 → 下游 BLOCKED → `stage_restored` 事件）。下载产物 `GET /api/pipeline/<id>/artifact?stage=...`。

## 六、开发与配套

```bash
PYTHONPATH=src python -m pytest -q -p no:cacheprovider    # 全量回归（exit 0）
python tools/platform_integrity.py --build && ... --verify  # 封印：src/config/tools 全部 .py/.json 的 sha256 清单，防 AI/代码改平台级工具
```

平台级演进分批落地，执行表见 `docs/2026-09-18-平台级演进计划.md`（P0 鉴权 → P1 异步任务 → P2 产物版本化 → P3 全局成本 → P4 MCP → P5 前端产品化 → P6 部署 → P7 安全硬化）。

## 七、安全基线

- 凭据只存指纹（sha256，明文不落盘）；测试用 `-not-a-real-secret` 占位。
- 全部 SQL 静态语句 + Python 侧过滤；不用 MD5；外联仅 https 且 host 白名单（localhost/loopback/private 屏蔽）。
- 鉴权默认 strict（`SHIPIN_AUTH_MODE=off` 仅测试/开发）；限流 `SHIPIN_RATE_MODE=on` 时按 key 指纹/IP 桶计数，超限 429 + `X-RateLimit-*`/`Retry-After`（生产建议开启，多 worker 需共享后端）。
- CORS 默认仅同源（`SHIPIN_CORS_ORIGINS` 逗号分隔白名单，永不当 `*`）；OPTIONS 预检旁路鉴权。
- 审计日志（caller / IP / method / route / status）：`GET /api/platform/events`（JSON）与 `/api/platform/events.csv`（CSV 全量导出，仅 admin）；`preflight` 附带 rate/disk/audit 资源健康。