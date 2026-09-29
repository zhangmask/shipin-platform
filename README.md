# Shipin Platform — AI 视频生成平台（服务端编排）

基于 OpenMontage / openshorts / Whisper / FFmpeg 等开源项目整合的 AI 视频生成平台，
以「文案 → 分镜 → 素材 → 合成 → 审查」流水线为核心，提供 **网页面板 / MCP 网关 / REST API**
三层入口；全流程产物版本化、任务异步化、成本全局可控、操作全量审计。

---

## 我们在解决什么问题

### 场景洞察

一个 AIGC 内容团队接到需求：明天交 50 条短剧草稿。他们不缺模型——市面上的生成模型随便挑。缺的是把 50 个需求变成 50 条「结构完整、人能接着改」的草稿的产能。于是全组开始人肉：写 prompt、等云端生成、逐条看结果、挑出能用的、坏的返工。一条 30 分钟，50 条就是 25 个小时——通宵。月底一看云 API 账单，比外包还贵。

**这个行业真正的成本从来不在模型，在两处：调用费，和人。**

### 现有现象

- **云端生成费用能上天。** 按次计费的模式下，草稿是「试错品」——十条里挑一条能用的，剩下九条的调用费照付。量越大，浪费越大，账单越离谱。
- **效率取决于人的成本。** 生成本身只要几分钟，卡住产能的是人等生成、人看结果、人做初级审核（时长对不对、有没有切镜、黑边、口型、跨镜换脸、品牌字写错没有）。**这些是最机械的环节，却吃着最贵的人力。**
- **产能被锁死在「人的分钟数」上。** 单点生成能力早已过剩，团队买的模型一个比一个强，但草稿产量没有翻倍——因为瓶颈根本不在模型，在流程和人。

### 需求

不是「又一个更好的生成模型」，而是**把草稿产能从人的分钟数里解放出来**：本地部署、批量并行、无人值守地出草稿；并且由机器完成低级审核，人只做最终判断。用一度电的成本，换一个人一小时。

### 我们的解决方案

一条**本地优先的视频草稿生产线**：一批需求/一句话进去，N 条结构完整、逐镜可改、自动过完质检的草稿出来——跑在自己的机器上，不按次付费，不需要人盯。

### 解决思路

1. **本地部署，把边际成本打到电费。** 草稿的本质是试错品，试错品的成本决定了你能试多少次。本地跑在自己的卡上，单条成本从「API 调用费」变成「电费」，批量越大，和云端的差距越离谱——**这才有可能「大量生成草稿和思路」，而不是十条里挑一条。**
2. **草稿自动化，无人值守。** 需求先被展开成可拍摄的分镜表（场景/景别/运动/旁白/台词），再逐镜生成，画面、配音、字幕三条轨各自分离、各自可替换。全程不需要人在旁边等。
3. **低级审核由质量门替代。** 时长、镜内切、运动量、黑边、首帧一致性、人物跨镜一致、品牌落成、旁白是否乱说话——这些原本靠人眼逐条看的项，由确定性规则 + 模型审查自动判定，并给出「哪一镜哪一项」的定位。能修的自动修（换 seed/换提示词/换引擎），不能修的显式拦下。**人从操作工升格为审稿人。**
4. **逐步取代，从最机械的两层开始。** 不推翻人的创作，先取代「草稿」和「初筛」这两层最占人时的工作——人保留创意方向和终审判断，中间那段机械劳动归机器。替换比例随部署规模线性上升。

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