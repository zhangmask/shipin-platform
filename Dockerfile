# ── P6 多阶段构建：node:20 构建前端 → python:3.12 运行平台 ──────────
# 产物：单镜像 shipin-platform:latest，`docker compose up` 即用。

# 阶段 1：React 构建（产物复制进 runtime，不携带 node）
FROM node:20-alpine AS web
WORKDIR /app/web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# 阶段 2：运行时（python 3.12 + ffmpeg + 平台源码）
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    PORT=8766
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY pyproject.toml README.md ./
COPY src ./src
COPY tools ./tools
COPY config ./config
COPY AGENT_GUIDE.md ./
# 前端构建产物挂在 /ui（与本地开发同构）
COPY --from=web /app/web/dist ./web/dist
EXPOSE 8765
HEALTHCHECK --interval=15s --timeout=5s --start-period=25s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=3).status == 200 else 1)"
CMD ["python", "-m", "uvicorn", "src.api:app", "--host", "0.0.0.0", "--port", "8765"]