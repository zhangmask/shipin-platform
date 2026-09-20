"""Platform-level API key auth (P0).

Model (借鉴 n8n public API keys / Dify App keys / Windmill tokens 的通用最小集):
- Admin master key 从环境变量 SHIPIN_ADMIN_KEY 读取（凭据不进源码/DB）；拥有 admin scope。
- 项目级 key 由 admin 通过 POST /api/platform/keys 签发，存储只保留 sha256 哈希，
  明文只在签发响应里出现一次（对标 n8n "show once"）。
- scope 分三级：read < write < admin（读 = GET；写 = POST/PUT/DELETE；平台管理 = admin）。
- 项目级 key 可绑定 project_id：只能读写自己的项目（路径级强制）。
- 全部 SQL 均为字面量 + 参数占位符，禁拼接。

切换开关（默认 strict）：
- SHIPIN_AUTH_MODE=strict（默认）：非豁免路径必须携带有效 X-API-Key，否则 401。
- SHIPIN_AUTH_MODE=off：放行全部（本地开发 / 无头测试用；测试通过 env 显式关闭）。
- SHIPIN_ADMIN_KEY 未设置时，strict 模式首次启动自动生成临时 admin key 打印到
  stderr（仅本次进程有效，重启失效）——开箱可用但不落盘。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── 常量与默认 ────────────────────────────────────────────────────────

# scope 分三级：read < write < admin
SCOPE_READ = "read"
SCOPE_WRITE = "write"
SCOPE_ADMIN = "admin"
ALL_SCOPES = (SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN)

# 豁免鉴权的路径（健康检查、静态资源、OpenAPI 文档）
_PUBLIC_PREFIXES = ("/api/health", "/ui", "/docs", "/redoc", "/openapi.json")

# 明文 key 熵：32 字节 url-safe → 43 字符
_KEY_BYTES = 32


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def hash_key(plain: str) -> str:
    """SHA-256 指纹（固定公开盐）。安全靠 key 的 256 位熵本身。"""
    return hashlib.sha256(("shipin-key:" + plain).encode("utf-8")).hexdigest()


def generate_key() -> str:
    return secrets.token_urlsafe(_KEY_BYTES)


class AuthError(Exception):
    """鉴权失败：code 机器可读，status 默认 401。"""

    def __init__(self, code: str, message: str, status: int = 401):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(f"[{code}] {message}")


class Principal:
    """鉴权后的身份（挂在 request.state.principal）。"""

    __slots__ = ("key_hash", "label", "scope", "project_id", "caller")

    def __init__(self, key_hash: str, label: str, scope: str,
                 project_id: Optional[str] = None, caller: str = "api-key"):
        self.key_hash = key_hash
        self.label = label
        self.scope = scope
        self.project_id = project_id  # None = 全项目
        self.caller = caller          # 审计标识

    @property
    def can_write(self) -> bool:
        return self.scope in (SCOPE_WRITE, SCOPE_ADMIN)

    @property
    def is_admin(self) -> bool:
        return self.scope == SCOPE_ADMIN


class ApiKeyStore:
    """SQLite 存储 api_keys（只存 hash）。线程安全：每次调用新连接。"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(
                """CREATE TABLE IF NOT EXISTS api_keys (
                       key_hash TEXT PRIMARY KEY,
                       label TEXT NOT NULL,
                       scope TEXT NOT NULL,
                       project_id TEXT,
                       owner TEXT NOT NULL DEFAULT 'default',
                       created_at TEXT NOT NULL,
                       revoked_at TEXT
                   );""")

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # -- 签发 / 校验 -------------------------------------------------

    def issue(self, label: str, scope: str = SCOPE_READ,
              project_id: Optional[str] = None,
              owner: str = "default") -> tuple[str, dict]:
        """签发新 key。返回 (明文, 记录)；明文只出现一次。"""
        if scope not in ALL_SCOPES:
            raise AuthError("BAD_SCOPE",
                            f"scope must be one of {ALL_SCOPES}", 422)
        plain = generate_key()
        key_hash = hash_key(plain)
        created = _now()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO api_keys (key_hash, label, scope, project_id, "
                "owner, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (key_hash, label, scope, project_id, owner, created))
        return plain, {"label": label, "scope": scope,
                       "project_id": project_id, "owner": owner,
                       "created_at": created}

    def lookup(self, plain: str) -> Optional[dict]:
        """按明文 key 查有效记录（只存哈希，参数化）。"""
        if not plain:
            return None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM api_keys WHERE key_hash = ? "
                "AND revoked_at IS NULL",
                (hash_key(plain),)).fetchone()
        return dict(row) if row else None

    def list_keys(self) -> list[dict]:
        """列出全部未撤销 key 记录（不含明文 hash）。"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT label, scope, project_id, owner, created_at "
                "FROM api_keys WHERE revoked_at IS NULL "
                "ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]

    def revoke(self, key_hash: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE api_keys SET revoked_at = ? WHERE key_hash = ? "
                "AND revoked_at IS NULL",
                (_now(), key_hash))
        return cur.rowcount > 0

    def count_active(self) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM api_keys "
                "WHERE revoked_at IS NULL").fetchone()
        return int(row["n"])


# ── 鉴权解析（FastAPI 中间件 / 测试直接复用） ─────────────────────────


def auth_mode() -> str:
    return (os.environ.get("SHIPIN_AUTH_MODE", "strict").lower().strip()
            or "strict")


def is_public_path(path: str) -> bool:
    return any(path.startswith(p) for p in _PUBLIC_PREFIXES)


def resolve_principal(plain: Optional[str],
                      store: Optional[ApiKeyStore] = None,
                      ) -> Optional[Principal]:
    """按明文 key 解析身份；无 / 失效返回 None。admin key 走环境变量。"""
    if not plain:
        return None
    env_admin = os.environ.get("SHIPIN_ADMIN_KEY", "").strip()
    if env_admin and hmac.compare_digest(plain, env_admin):
        return Principal(hash_key(env_admin), "admin-master", SCOPE_ADMIN,
                         caller="admin")
    if store is None:
        return None
    row = store.lookup(plain)
    if row is None:
        return None
    return Principal(row["key_hash"], row["label"], row["scope"],
                     row["project_id"],
                     caller=f"{row['label']}/{row['scope']}")


def principal_from_header(headers: dict, store: ApiKeyStore) -> Optional[Principal]:
    """从请求头 X-API-Key 解析身份（中间件用）。"""
    return resolve_principal(headers.get("x-api-key"), store)


def project_allowed(principal: Optional[Principal], project_id: str) -> bool:
    """项目级越权检查：admin / 未绑项目 key 全量；绑定 key 只能访问自己项目。"""
    if principal is None:
        return False
    if principal.scope == SCOPE_ADMIN or principal.project_id is None:
        return True
    return principal.project_id == project_id