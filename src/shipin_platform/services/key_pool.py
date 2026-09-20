"""提供方 key 池 — 图片/视频生成时按 capability、provider 挑选 API key。

设计红线（对齐项目 audit_store 的参数化纪律）：
- 所有 SQL 均在 execute() 处内联完整字面量，输入一律 ? 参数绑定，
  不使用任何拼接、format、f-string 组装 SQL；
- 密钥只以密文落库（AES-256-GCM，信封密钥来自环境变量 SHIPIN_KEY_SEAL，
  缺失即拒绝登记，杜绝明文入库）；
- 列表/审计回显永不出现明文 key，只有 sha256 短摘要用于识别轮换；
- pick() 只在生成链路被调用，返回明文 key 后立即使用、不断言、不落盘。

用法（生成链路换 key）：
    pool = get_pool()
    rec = pool.pick("agnes", "video")   # -> {"key": ..., "id": ...} 或 None
"""
from __future__ import annotations

import base64
import hashlib
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from shipin_platform import roots
from typing import Optional

ROOT = roots.data_root()
DEFAULT_DB = ROOT / "data" / "key_pool.db"
SEAL_ENV = "SHIPIN_KEY_SEAL"

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM as _AESGCM
    _HAS_CRYPTO = True
except Exception:  # pragma: no cover
    _HAS_CRYPTO = False


class KeyPoolError(Exception):
    """key 池稳定错误（上层按 ok:false + code 返回）。"""


class SealMissingError(KeyPoolError):
    """信封密钥缺失——拒绝任何明文写入。"""


def _seal_key() -> bytes:
    raw = (os.environ.get(SEAL_ENV) or "").encode("utf-8")
    if len(raw) < 32:
        raise SealMissingError(
            f"{SEAL_ENV} 未配置或短于 32 字节，拒绝 key 密文落库")
    return hashlib.sha256(raw).digest()


def _encrypt(plain: str) -> dict:
    if not _HAS_CRYPTO:  # pragma: no cover
        raise KeyPoolError("cryptography 未安装，无法安全落库")
    nonce = os.urandom(12)
    ct = _AESGCM(_seal_key()).encrypt(nonce, plain.encode("utf-8"), b"shipin")
    return {
        "cipher": base64.b64encode(ct).decode(),
        "nonce": base64.b64encode(nonce).decode(),
        "sha256": hashlib.sha256(plain.encode("utf-8")).hexdigest(),
    }


def _decrypt(cipher_b64: str, nonce_b64: str) -> str:
    if not _HAS_CRYPTO:  # pragma: no cover
        raise KeyPoolError("cryptography 未安装")
    ct = base64.b64decode(cipher_b64)
    nonce = base64.b64decode(nonce_b64)
    return _AESGCM(_seal_key()).decrypt(nonce, ct, b"shipin").decode("utf-8")


class KeyPoolStore:
    """SQLite 密钥池：登记/选取/启停/删除，全部参数化、不落明文。"""

    def __init__(self, db: Path = DEFAULT_DB):
        self.db = Path(db)
        self.db.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(str(self.db), timeout=15)
        c.row_factory = sqlite3.Row
        return c

    def _init_schema(self) -> None:
        with self._conn() as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS provider_keys ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " provider TEXT NOT NULL,"
                " capability TEXT NOT NULL,"
                " service TEXT NOT NULL DEFAULT '',"
                " cipher TEXT NOT NULL,"
                " nonce TEXT NOT NULL,"
                " sha256 TEXT NOT NULL,"
                " priority INTEGER NOT NULL DEFAULT 0,"
                " enabled INTEGER NOT NULL DEFAULT 1,"
                " created_at TEXT NOT NULL,"
                " last_used_at TEXT)")
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_pk_lookup ON "
                " provider_keys(provider, capability)")

    # -- 写 -------------------------------------------------------------

    def add(self, provider: str, capability: str, key: str,
            service: str = "", priority: int = 0) -> dict:
        if not provider or not capability or not key:
            raise ValueError("provider/capability/key 不能为空")
        enc = _encrypt(key)  # seal 缺则抛错，绝不明文落库
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._conn() as c:
            cur = c.execute(
                "INSERT INTO provider_keys"
                " (provider, capability, service, cipher, nonce, sha256,"
                "  priority, enabled, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)",
                (provider, capability, service, enc["cipher"], enc["nonce"],
                 enc["sha256"], priority, now))
        return {"id": cur.lastrowid, "provider": provider,
                "capability": capability, "service": service,
                "priority": priority,
                "sha256_short": enc["sha256"][:8], "created_at": now}

    # -- 选取（生成链路内部：返回明文 key 到调用栈）----------------------

    def pick(self, provider: str, capability: str) -> Optional[dict]:
        """最高优先级、最久未用的启用 key。找不到返回 None。"""
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT id, provider, capability, service, cipher, nonce"
                " FROM provider_keys"
                " WHERE provider = ? AND capability = ? AND enabled = 1"
                " ORDER BY priority DESC, last_used_at ASC, id ASC"
                " LIMIT 1",
                (provider, capability)).fetchone()
        if row is None:
            return None
        plain = _decrypt(row["cipher"], row["nonce"])
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._conn() as c:
            c.execute("UPDATE provider_keys SET last_used_at = ? WHERE id = ?",
                      (now, row["id"]))
        return {"id": row["id"], "provider": row["provider"],
                "capability": row["capability"], "service": row["service"],
                "key": plain}

    # -- 管理 -----------------------------------------------------------

    def list(self, provider: str = "", capability: str = "") -> list[dict]:
        with self._conn() as c:
            if provider and capability:
                rows = c.execute(
                    "SELECT id, provider, capability, service, priority, enabled,"
                    " created_at, last_used_at, substr(sha256, 1, 8) AS sha256_short"
                    " FROM provider_keys"
                    " WHERE provider = ? AND capability = ? ORDER BY id",
                    (provider, capability)).fetchall()
            elif provider:
                rows = c.execute(
                    "SELECT id, provider, capability, service, priority, enabled,"
                    " created_at, last_used_at, substr(sha256, 1, 8) AS sha256_short"
                    " FROM provider_keys WHERE provider = ? ORDER BY id",
                    (provider,)).fetchall()
            elif capability:
                rows = c.execute(
                    "SELECT id, provider, capability, service, priority, enabled,"
                    " created_at, last_used_at, substr(sha256, 1, 8) AS sha256_short"
                    " FROM provider_keys WHERE capability = ? ORDER BY id",
                    (capability,)).fetchall()
            else:
                rows = c.execute(
                    "SELECT id, provider, capability, service, priority, enabled,"
                    " created_at, last_used_at, substr(sha256, 1, 8) AS sha256_short"
                    " FROM provider_keys ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def set_enabled(self, key_id: int, enabled: bool) -> bool:
        with self._lock, self._conn() as c:
            cur = c.execute("UPDATE provider_keys SET enabled = ? WHERE id = ?",
                            (1 if enabled else 0, key_id))
            return cur.rowcount > 0

    def remove(self, key_id: int) -> bool:
        with self._lock, self._conn() as c:
            cur = c.execute("DELETE FROM provider_keys WHERE id = ?",
                            (key_id,))
            return cur.rowcount > 0


_default_pool: Optional[KeyPoolStore] = None


def get_pool() -> KeyPoolStore:
    global _default_pool
    if _default_pool is None:
        _default_pool = KeyPoolStore()
    return _default_pool