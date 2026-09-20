# Claude 通道直通指引（监督执行前唯一前置）

`scripts/supervise-eval.sh --tvc` 全部逻辑已就绪且验证过（健康检查 → claude -p 投喂 →
150s 挂起判定 → FINAL 核收）。**唯一缺口是 claude CLI 的模型通道**。本机 claude CLI 配置
（`~/.claude/settings.json`）写死 `ANTHROPIC_BASE_URL=http://127.0.0.1:15721`，但该端口
当前无进程监听；实测官方直连（403）、遗留第三方 key（kimi/tryai/tokenrouter，全部
INVALID_API_KEY）、空配置直连（挂死）都不通。完成监督只需其一：

## 方式 A：桌面拉起集成代理（推荐，不动凭据）
1. 打开 ZCode 桌面 → 找到 Claude Code / 组件集成入口（生成中心或设置里的
   “Claude Code” 启动项）→ 启动它。
2. 让它运行后确认代理端口出现：
   ```bash
   curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:15721/
   # 期望非 000；若为别的动态端口，用环境变量指过去：
   # ANTHROPIC_BASE_URL=http://127.0.0.1:<新端口> bash scripts/supervise-eval.sh --tvc
   ```

## 方法 B：注入有效 key（仅环境变量，勿写入文件）
```bash
export ANTHROPIC_API_KEY=<你的有效 key>
# 同时让 claude 忽略写死的 base url：
export ANTHROPIC_BASE_URL=https://api.anthropic.com
API_PORT=8767 bash scripts/supervise-eval.sh --tvc
```

## 验证通道（10 秒口径）
```bash
claude -p "回复OK" --max-turns 1 --dangerously-skip-permissions
# 输出 OK → 通道可用；挂起/ConnectionRefused → 未通
```

通道打通后，监督闭环自动完成：
- claude 执行 `tvc-main.md`（读 p1..p4，逐步走 brief→script→storyboard→prompts，每步过
  `/api/review/iterate` 闸门，产物落 `test_out/tvc_*.json`）
- trace 写 `/tmp/shipin-eval/tvc-trace.md`，末行 `FINAL: PASS/BLOCKED`
- README 的 A1–A5（TVC 版）按 trace/tcv.log 核收