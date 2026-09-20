# shipin-platform · Agent 监督与实战提示词包

这套文件用于验证“任意 Claude / LLM 都能按 AGENT_GUIDE.md 正确使用 shipin-platform 的工具、
并能接力多条提示词”，同时提供一个真实题目（联想 ThinkPad TVC）的完整实战提示词。

## 文件

| 文件 | 用途 |
|---|---|
| `supervise-eval.sh` | 一键监督器：T1→T2→T3 依次投喂提示词给 `claude -p`，最后按 A1–A5 自动核账 |
| `prompt-t1/2/3.md` | 通用工具使用测评：契约发现 → intake 立项 → review 闸门闭环 → 三段创作提示词接力 |
| `tvc-main.md` | **联想 ThinkPad TVC 总纲提示词**（一次投喂给 claude，驱动完整四阶段流水线） |
| `tvc-p1..p4.md` | 总纲引用的四个阶段任务书：brief / script / storyboard / 图像视频提示词，每阶段强制过审 |

## 用法

```bash
# 0) 前提：claude CLI 通道可用（桌面代理已拉起，或注入 ANTHROPIC_API_KEY 等环境变量）
# 1) 通用监督（三连提示词 + 自动验收，API 端口 8766 默认；API_PORT 可覆盖）
API_PORT=8767 bash scripts/supervise-eval.sh

# 2) TVC 实战（一条总纲提示词，claude 自己读四个阶段任务书并逐段执行，完成后核收 FINAL 行）
bash scripts/supervise-eval.sh --tvc        # 需要 API 已在 127.0.0.1:8767 运行，或由哨兵代劳
```

产物约定：四个阶段的修复后 data 落在 `test_out/tvc_*.json`，全程决策记录在
`/tmp/shipin-eval/tvc-trace.md`，最后一行给出 `FINAL: PASS / BLOCKED` 结论。

## 通道说明（重要）

`claude -p` 依赖本机 claude CLI 有可用的模型通道。若你的 CLI 被配置为指向本地代理
（如 `ANTHROPIC_BASE_URL=http://127.0.0.1:15721`）而代理未启动，会 ConnectionRefused。
两种处理：
- 在桌面端把 Claude Code 集成/代理拉起后再跑；
- 或注入可达通道：`ANTHROPIC_BASE_URL=<端点> ANTHROPIC_API_KEY=<密墙管理的key> claude …`
  （凭据只从环境变量读，本包不含任何凭据字面量）。

## 验收口径（A1–A5）

A1 自主发现契约 · A2 intake 调用正确 · A3 多提示词跨会话接续 · A4 review 闭环有结论 · A5 只走 127.0.0.1。