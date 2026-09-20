#!/usr/bin/env bash
# P6：本地一键启动（无 Docker 时）—— 构建前端 → 起 API（8766）
# 用法：./start.sh [--port 8766] [--reload]
set -e
cd "$(dirname "$0")"

PORT=8766
RELOAD=""
while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --reload) RELOAD="--reload"; shift ;;
    *) echo "未知参数: $1"; exit 2 ;;
  esac
done

echo "[start] 检查/构建前端（web/dist）…"
if [ ! -d web/node_modules ]; then
  echo "[start] 首次运行，安装前端依赖（npm ci）…"
  (cd web && npm ci --no-audit --no-fund)
fi
(cd web && npm run build >/dev/null) || { echo "前端构建失败，继续以旧产物启动"; }

export PYTHONPATH="${PYTHONPATH:-}:$(pwd)/src"
echo "[start] uvicorn http://127.0.0.1:${PORT} （/ui 面板 | /api 接口）"
echo "[start] 鉴权模式: SHIPIN_AUTH_MODE=${SHIPIN_AUTH_MODE:-strict}"
exec python -m uvicorn src.api:app --host 127.0.0.1 --port "${PORT}" ${RELOAD}