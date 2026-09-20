---
name: shipin-platform
description: >-
  Shipin 平台（AI 视频广告/宣传片制作平台）的完整工作流技能。
  触发词：用平台/做广告/做宣传片/做短视频/帮我跑平台/视频生成/过程回放/跑一遍流程。
  本技能用于：从零启动平台（服务/桌面窗口/MCP）、驱动完整制作流程
  （brief→剧本→分镜→生成→成片）、解读状态机闸门与 QC 拦截、管理预算与 key。
  ZCode：放在 skills 目录即可被识别；Codex：见 mcp/README.md 或本文件底部。
---

# Shipin Platform — AI 制作流水线技能

## 1. 平台是什么

FastAPI + SQLite 的受控制作平台（默认 `127.0.0.1:8766`）。流程：
brief → script → storyboard → 图/视频提示词 → generate（首帧图 → keyframes
视频 → TTS）→ assemble（成片 + 双层终验）。每一步都有状态机闸门、成本记账、
事件留痕；产物全部版本化可回滚。

## 2. 启动（三选一）

- **桌面版（像 exe 一样）**：双击 `start-desktop.bat`，或
  `python tools/desktop_app.py`（pywebview 原生窗口；无 WebView2 自动退回浏览器）。
- **Web**：`python tools/desktop_app.py --no-window` 后浏览器开
  `http://127.0.0.1:8766/ui/`。
- **MCP / REST**：见 `mcp/README.md`（Codex/ZCode 配置片段在 `mcp/`）。

桌面版窗口里能看到：项目列表、看板（阶段/闸门/产物/事件/版本 diff/成本）、
**过程回放**（`/projects/:pid/timeline`：里程碑 + 执行轨迹 + QC + 版本 + 成本）、
登录页（鉴权开时）。

## 3. 标准流程（AI 驱动时照此执行）

1. `shipin_health` / `shipin_preflight` → 确认凭据、闸门、预算、key 池。
2. `shipin_list_projects` 选定项目；新项目用 `shipin_pipeline_text` 交 brief。
3. 等闸门：script/storyboard 按 review 意见改写 → 用户确认（真实的
   `approved_by`）→ 推进。
4. `shipin_pipeline_generate`：先出首帧图，然后 keyframes 视频（首尾帧锚定，
   防镜内拆镜），TTS。生成结果逐镜 QC。
5. `shipin_pipeline_assemble` 成片；`shipin_report` 汇报产物与成本。
6. 用户没确认前不落任何闸门；花 key/钱前先 `preflight`。

## 4. 平台红线（违反=事故）

- 确认闸门必须由用户真实确认，AI 绝不能自己填 approved_by。
- **审过的剧本一锤定音**：分镜写死的单镜连续性不可被生成结果破坏；
  QC 拦截=证据，先把证据（帧差/时间点）给用户看，再谈放宽。
- 改写必经 `rewrite_stage`（仅 script/storyboard），改写后重确认三闸。
- key（AGNES_KEY / 平台 API key / SHIPIN_KEY_SEAL）永不进产物/日志/提示词；
  登记生成 key 一律走 `POST /api/platform/keypool`（密文落库，列表永不回显明文）。
- 封印一致性：`shipin_integrity` 非绿时，AI 修改平台源码需人重新封印。

## 5. 常见拦截与仲裁

- **生成被 QC 拦：「镜头内出现硬切」**：先抽帧核对（ffmpeg
  `select='gt(scene,0.3)'` + 相邻帧差），是模型私自拆镜还是真切换；
  证据确凿就如实报告，不静默改 QC。
- **video_prompt 审核不通过**：提示词与分镜不一致 → 走 rewrite（持久化），
  然后重新确认。
- **key 池 Empty / AGNES_KEY 未配置**：先看 `/api/health` 的 `agens` 和
  `key_pool` 段，别猜。

## 6. Codex / 其他客户端挂载

```bash
codex mcp add shipin -- python D:\aishipin\shipin-platform\tools\mcp_server.py
```

ZCode：在 MCP 配置新增 stdio server，命令 `python`、参数
`["D:/aishipin/shipin-platform/tools/mcp_server.py"]`。
然后把本技能目录加到你的 skills 目录即可在任意会话里唤醒。