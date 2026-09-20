#!/usr/bin/env bash
# ============================================================================
# supervise-eval.sh — claude 使用 shipin-platform 的监督验收器
#
# 验证目标（对应 AGENT_GUIDE 的核心承诺）：
#   A1 工具发现:   claude 自行读取 AGENT_GUIDE.md 或 GET /api/agent-guide
#   A2 正确调用:   intake/questions → intake/draft 结构与解析正确
#   A3 多提示词:   T1→T2→T3 三条独立提示词跨会话接续（以 trace 为接力棒）
#   A4 审查闭环:   review/iterate 多轮至 pass 或给出明确阻塞结论
#   A5 干净执行:   只访问 127.0.0.1，全部触点留痕到 trace.md
#
# 通道说明: 本脚本调用本机 `claude -p`（headless）。若你的 claude CLI
# 需要特殊模型/端点，用环境变量注入:
#   ANTHROPIC_BASE_URL / ANTHROPIC_API_KEY   (联邦通道，不写入任何文件)
# 凭据约束: 本脚本不读不写任何凭据字面量。
#
# 用法:
#   API_PORT=8767 ./scripts/supervise-eval.sh            # 通用 T1-T3 监督（默认端口 8766）
#   API_PORT=8767 ./scripts/supervise-eval.sh --tvc      # 联想 ThinkPad TVC 端到端实测
# 通道: 需要本机 `claude -p` 可达（桌面代理已监听，或注入 ANTHROPIC_API_KEY 等环境变量）
# ============================================================================
set -u

MODE="${1:-}"
API_PORT="${API_PORT:-8766}"
API="http://127.0.0.1:${API_PORT}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EVAL_DIR="${EVAL_DIR:-/tmp/shipin-eval}"
PROMPTS_DIR="$HERE/supervise-eval"
TRACE="$EVAL_DIR/trace.md"
MAX_TURNS="${MAX_TURNS:-60}"

mkdir -p "$EVAL_DIR"

say()  { printf '\n[supervise] %s\n' "$*"; }
fail() { printf '\n[supervise] FAIL: %s\n' "$*" >&2; exit 1; }

# --- 前置：API 必须活 -----------------------------------------------------
HEALTH="$(curl -s -m 3 "$API/api/health" || true)"
echo "$HEALTH" | grep -q '"status":"ok"' || fail "API 不可达（$API）——先启动: uvicorn src.api:app --port $API_PORT"
say "API 就绪: $API"

# --- TVC 模式：一条总纲提示词驱动四阶段流水线 ---------------------------
if [ "$MODE" = "--tvc" ]; then
  TVCTRACE="$EVAL_DIR/tvc-trace.md"
  PROMPT="$PROMPTS_DIR/tvc-main.md"
  [ -f "$PROMPT" ] || fail "缺少总纲提示词 $PROMPT"
  REPO_ROOT="$(cd "$HERE/.." && pwd)"
  for f in tvc-p1 tvc-p2 tvc-p3 tvc-p4; do
    [ -f "$PROMPTS_DIR/$f.md" ] || fail "缺少阶段任务书 $PROMPTS_DIR/$f.md"
  done
  # 保留现场：上一轮 FINAL 行不清，只垫干净结尾
  [ -f "$TVCTRACE" ] && cp "$TVCTRACE" "$TVCTRACE.bak.$(date +%s)"
  : > "$TVCTRACE"
  echo "# shipin-agent TVC 监督 trace" >> "$TVCTRACE"
  echo "# 生成时间: $(date '+%F %T')   API: $API" >> "$TVCTRACE"
  CLAUDE_BIN="${CLAUDE_BIN:-claude}"
  CLAUDE_TIMEOUT="${CLAUDE_TIMEOUT:-1700}"
  say "TVC: 从 $REPO_ROOT 投喂 $PROMPT（$CLAUDE_BIN，最多 $MAX_TURNS 轮，$CLAUDE_TIMEOUT s 上限）..."
  ( cd "$REPO_ROOT" && timeout "$CLAUDE_TIMEOUT" "$CLAUDE_BIN" -p "$(cat "$PROMPT")" \
      --dangerously-skip-permissions --max-turns "$MAX_TURNS" ) > "$EVAL_DIR/tvc.log" 2>&1
  rc=$?
  case "$rc" in
    124) say "TVC: claude 超时（$CLAUDE_TIMEOUT s）被截断——以上一步留痕为准" ;;
    0)   say "TVC: claude 正常结束" ;;
    *)   say "TVC: claude 退出码 $rc（详见 $EVAL_DIR/tvc.log）" ;;
  esac
  FINAL="$(grep -E '^FINAL: (PASS|BLOCKED)' "$TVCTRACE" 2>/dev/null | tail -1)"
  if [ -n "$FINAL" ]; then
      echo "$FINAL"
      grep -q "FINAL: PASS" <<<"$FINAL" \
        && echo "TVC SUPERVISE PASS: claude 真跑通联想 ThinkPad TVC 四阶段流水线" \
        || echo "TVC SUPERVISE BLOCKED: claude 按要求给出阻塞结论（看 trace 原因）"
      echo "TVC 证据: $TVCTRACE   claude 现场: $EVAL_DIR/tvc.log"

      # ── 总导演验货：四阶段产物存在性 + 镜头数（纯 bash，不引入 python）─
      if grep -q "FINAL: PASS" <<<"$FINAL"; then
        echo "== 总导演验货 =="
        VERDICT="PASS"; REASONS=""
        TVC_OUT="$REPO_ROOT/test_out"
        for f in tvc_brief tvc_script tvc_storyboard tvc_image_prompt; do
          if [ -s "$TVC_OUT/$f.json" ]; then
            printf '  验货  %-20s 存在（%s 字节）\n' "$f.json" "$(wc -c < "$TVC_OUT/$f.json")"
          else
            printf '  验货  %-20s 缺失\n' "$f.json"
            VERDICT="存疑"; REASONS="$REASONS 缺$f.json;"
          fi
        done
        NS=$(grep -o '"shot' "$TVC_OUT/tvc_script.json" 2>/dev/null | wc -l)
        NB=$(grep -o '"shot' "$TVC_OUT/tvc_storyboard.json" 2>/dev/null | wc -l)
        printf '  验货  script镜头数=%s   storyboard镜头数=%s\n' "${NS:-0}" "${NB:-0}"
        [ "${NS:-0}" -ge 3 ] || { VERDICT="存疑"; REASONS="$REASONS script镜头<3;"; }
        [ "${NB:-0}" -ge 4 ] || { VERDICT="存疑"; REASONS="$REASONS storyboard镜头<4;"; }
        if [ "$VERDICT" = "存疑" ]; then
          echo "总导演验货: 存疑（$REASONS）——FINAL 虽 PASS，产物需人工复查"
        else
          echo "总导演验货: PASS（四产物齐全，镜头数达标）"
        fi
      fi
      exit 0
  fi
  say "致命: claude 未在 $TVCTRACE 写 FINAL: 行（没接住收尾协议）"
  echo "FINAL: MISSING"
  exit 1
fi

# --- 保留现场：已有 trace 不丢 ---------------------------------------
[ -f "$TRACE" ] && cp "$TRACE" "$TRACE.bak.$(date +%s)"
: > "$TRACE"
echo "# shipin-agent 监督 trace" >> "$TRACE"
echo "# 生成时间: $(date '+%F %T')   API: $API" >> "$TRACE"

# --- T1/T2/T3: 每条提示词独立一次 claude -p -------------------------
run_t() {
  local label="$1" prompt="$2"
  local log="$EVAL_DIR/$label.log"
  say "$label: claude -p 执行中（最多 $MAX_TURNS 轮；连接挂起 150s 自动判定通道不可达）..."
  timeout 150 claude -p "$(cat "$prompt")" --dangerously-skip-permissions --max-turns "$MAX_TURNS" \
    > "$log" 2>&1
  local rc=$?
  [ $rc -ne 0 ] && say "$label 退出码 $rc（详见$log；124=通道不可达）"
}

if [ "$MODE" = "--tvc" ]; then
  # TVC 场景：一条总纲提示词驱动四阶段（brief→script→storyboard→prompts），
  # 阶段任务书由 claude 自己读取；核账关注 FINAL 结论是否给出。
  TRACE="$EVAL_DIR/tvc-trace.md"
  [ -f "$PROMPTS_DIR/tvc-main.md" ] || fail "缺少 tvc-main.md"
  run_t "tvc" "$PROMPTS_DIR/tvc-main.md"
  say "核账(TVC):"
  if grep -qE "FINAL: (PASS|BLOCKED)" "$TRACE" 2>/dev/null; then
    echo "TVC RESULT   PASS - 有 FINAL 结论（见 $TRACE）"
    grep -E "FINAL:" "$TRACE" | tail -1
  else
    echo "TVC RESULT   MISS - trace 无 FINAL 行（见 $EVAL_DIR/tvc.log）"
  fi
  [ -f "$EVAL_DIR/tvc.log" ] && grep -cE "intake/(draft|questions)|review/iterate" \
    "$EVAL_DIR/tvc.log" | xargs echo "API 触达次数(日志):"
  echo "trace: $TRACE"
  exit 0
fi

for t in t1 t2 t3; do
  [ -f "$PROMPTS_DIR/prompt-$t.md" ] || fail "缺少提示词 $PROMPTS_DIR/prompt-$t.md"
  run_t "$t" "$PROMPTS_DIR/prompt-$t.md"
done

# --- 核账：A1-A5 ------------------------------------------------------
say "核账:"
declare -i score=0
printf '%-44s %s\n' "检查项" "结果"
check() { # $1 名称  $2 判定命令  $3 证据文件
  if eval "$2" >/dev/null 2>&1; then
    printf '%-44s %s\n' "$1" "PASS"; score=$((score+1))
  else
    printf '%-44s %s\n' "$1" "MISS（证据: $3）"
  fi
}

A1=$(cat "$EVAL_DIR/t1.log" 2>/dev/null | grep -cE "AGENT_GUIDE|agent-guide")
check "A1 自主发现契约" "[ ${A1:-0} -ge 1 ]" "${EVAL_DIR}/t1.log"
A2=$(cat "$EVAL_DIR/t1.log" 2>/dev/null | grep -cE "intake/(questions|draft)")
check "A2 正确调用 intake" "[ ${A2:-0} -ge 2 ]"
A3=$(grep -cE "^## T[123] 步" "$TRACE" 2>/dev/null)
check "A3 三条提示词留痕" "[ ${A3:-0} -ge 3 ]"
A4=$(grep -cE "T2 RESULT: (PASS|BLOCKED)" "$TRACE" 2>/dev/null)
check "A4 审查闭环有结论" "[ ${A4:-0} -ge 1 ]"
EXT=$(cat "$EVAL_DIR"/*.log 2>/dev/null | grep -oE "https?://[^ ]+" | grep -vcE "127\.0\.0\.1|localhost|docs\.pytest|pypi|github\.com/pro" || true)
check "A5 未访问外网" "[ ${EXT:-0} -eq 0 ]"

echo "--------------------------------------------------------------"
echo "评分: $score/5  （A1-A5 全部 PASS 才算监督通过）"
echo "trace: $TRACE   各步日志: $EVAL_DIR/t{1,2,3}.log"
[ "$score" -eq 5 ] && echo "SUPERVISE PASS: claude 能正确使用工具并接力多条提示词" \
                  || echo "SUPERVISE INCOMPLETE: 见上表 MISS 项，重新跑或人工复查"