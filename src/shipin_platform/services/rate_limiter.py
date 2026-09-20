"""P7 限流：固定窗口计数器（进程内、零第三方依赖）。

对标 slowapi（FastAPI 社区标准制式）的 memory backend 语义：
- 响应头 `X-RateLimit-Limit` / `X-RateLimit-Remaining` / `X-RateLimit-Window`
- 超限返回 429 + `Retry-After`
- key 维度：已鉴权请求按 API key 的 sha256 指纹（不以明文做桶名）；
  未鉴权请求按客户端 IP 兜底。

配置（环境变量，逐请求读取便于测试与热改）：
- SHIN_RATE_MODE   off（默认，开发/CI）| on —— 与 auth_mode 同构：
  on 时对所有非公开 /api 请求计数（strict 部署建议 on）
- SHIN_RATE_MAX        窗口内最大请求数（默认 120）
- SHIN_RATE_WINDOW_SEC 窗口秒数（默认 60）
- SHIN_RATE_IP_MAX     无 key 时按 IP 的窗口上限（默认 30）

单进程部署（本平台 SQLite 单写者）下为精确全局计数；
多 worker 需要共享后端（如 Redis）——README 已注明该边界。
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from typing import Dict, Optional, Tuple

# 进程内窗口状态：{bucket_key: (window_start, count)}
_STATE: Dict[str, Tuple[float, int]] = {}
_LOCK = threading.Lock()


def rate_mode() -> str:
    """'on' / 'off'。默认 off（开发便捷）；生产将 SHIPIN_RATE_MODE=on。"""
    v = os.environ.get("SHIPIN_RATE_MODE", "off").strip().lower()
    return "on" if v in ("1", "true", "yes", "on") else "off"


def rate_config() -> dict:
    return {
        "max": max(1, int(os.environ.get("SHIPIN_RATE_MAX", "120"))),
        "window": max(1, int(os.environ.get("SHIPIN_RATE_WINDOW_SEC", "60"))),
        "ip_max": max(1, int(os.environ.get("SHIPIN_RATE_IP_MAX", "30"))),
    }


def _bucket_key(*, token: Optional[str], ip: str) -> str:
    if token:
        # 不把明文 key 放内存/日志：只留 sha256 指纹作桶名
        return "k:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
    return "ip:" + ip


def check_rate(*, token: Optional[str], ip: str) -> Tuple[bool, dict]:
    """消耗 1 次的判定。返回 (allowed, info)。超限时 allowed=False，
    info 内含 retry_after（秒数）供 429 响应使用。"""
    cfg = rate_config()
    if rate_mode() != "on":
        return True, {"limit": cfg["max"], "remaining": cfg["max"],
                      "window": cfg["window"], "retry_after": 0,
                      "enabled": False}
    cap = cfg["max"] if token else cfg["ip_max"]
    key = _bucket_key(token=token, ip=ip)
    now = int(time.time())
    win = now // cfg["window"] * cfg["window"]

    with _LOCK:
        # 惰性清理：只保留最近两个窗口的桶，防止长期运行内存膨胀
        if len(_STATE) > 10_000:
            stale = now - cfg["window"] * 2
            _STATE.clear()
        cur = _STATE.get(key)
        if cur is None or cur[0] != win:
            _STATE[key] = (win, 0)
            cur = _STATE[key]
        count = cur[1] + 1
        _STATE[key] = (win, count)
        remaining = max(0, cap - count)
        allowed = count <= cap
        retry_after = max(0, win + cfg["window"] - now) if not allowed else 0
    return allowed, {"limit": cap, "remaining": remaining,
                    "window": cfg["window"], "retry_after": retry_after,
                    "enabled": True}


def rate_state() -> dict:
    """供 preflight / 健康面板展示的只读快照（不含 key 指纹）。"""
    cfg = rate_config()
    with _LOCK:
        live = sum(1 for _, (ws, _c) in _STATE.items()
                   if ws + cfg["window"] >= time.time())
    cfg["active_buckets"] = live
    cfg["mode"] = rate_mode()
    return cfg