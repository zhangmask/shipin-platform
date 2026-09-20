"""key 池测试：密文落库、参数化 SQL、按 capability 挑选、管理 API 脱敏契约。

验收口径（平台级演进计划 P3-key pool）：
- 明文 key 绝不落盘：DB 文件字节级检查 secret 不在其中；
- list/管理接口永不回显明文 key / cipher / nonce；
- pick() 按 priority DESC + last_used_at ASC（启用 key 内轮换）;
- 停用（enabled=0）后 pick 不再命中；
- seal 缺失时拒绝任何明文写入（SealMissingError / 503）;
- provider_registry 顺序：池命中 → env 回退。
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

SEAL = "test-seal-" + "k" * 40  # >= 32 字节


@pytest.fixture(autouse=True)
def _seal(monkeypatch):
    monkeypatch.setenv("SHIPIN_KEY_SEAL", SEAL)
    yield


@pytest.fixture()
def pool(tmp_path):
    from shipin_platform.services.key_pool import KeyPoolStore
    return KeyPoolStore(tmp_path / "pool.db")


@pytest.fixture()
def client(monkeypatch, pool):
    """API 测试隔离：keypool 端点打 tmp 池，绝不触碰真实 data/key_pool.db。"""
    import shipin_platform.services.key_pool as kp_mod
    monkeypatch.setattr(kp_mod, "get_pool", lambda: pool)
    import api as api_mod
    return TestClient(api_mod.app)


# ── 落库安全性 ──────────────────────────────────────────────────


def test_register_never_writes_plaintext(pool, tmp_path):
    secret = "sk-ag-video-" + "a" * 60
    rec = pool.add("agnes", "video", secret, service="t1", priority=5)
    assert rec["sha256_short"] == rec["sha256_short"]  # str present
    blob = (tmp_path / "pool.db").read_bytes()
    assert secret.encode() not in blob
    assert b"cipher" in blob  # ciphertext field确实落库


def test_list_never_returns_secret_fields(pool):
    pool.add("agnes", "video", "sk-secret-video-" + "b" * 40)
    rows = pool.list("agnes", "video")
    assert len(rows) == 1
    row = rows[0]
    for forbidden in ("cipher", "nonce", "key", "secret"):
        assert forbidden not in row
    assert len(row["sha256_short"]) == 8


# ── 选取 / 轮换 / 停用 ──────────────────────────────────────────


def test_pick_cleartext_roundtrip(pool):
    secret = "sk-pool-" + "c" * 40
    pool.add("agnes", "video", secret, priority=1)
    got = pool.pick("agnes", "video")
    assert got is not None
    assert got["key"] == secret
    assert pool.pick("agnes", "image") is None  # capability 维度隔离


def test_pick_priority_then_lru(pool):
    pool.add("agnes", "video", "sk-lo-" + "d" * 40, priority=0)
    hi_id = pool.add("agnes", "video", "sk-hi-" + "e" * 40, priority=9)["id"]
    # 高优先级一直占先（priority 主宰，LRU 只在同优先级内生效）
    for _ in range(3):
        assert pool.pick("agnes", "video")["id"] == hi_id
    # 同优先级 → last_used_at ASC：两条 key 轮流轮换
    pool.add("agnes", "image", "sk-a-" + "f" * 40, priority=0)
    pool.add("agnes", "image", "sk-b-" + "g" * 40, priority=0)
    ids = [pool.pick("agnes", "image")["id"] for _ in range(4)]
    assert len(set(ids)) == 2  # a,b,a,b… 轮换而非永远第一条


def test_disabled_key_excluded(pool):
    rec = pool.add("agnes", "video", "sk-off-" + "g" * 40)
    assert pool.set_enabled(rec["id"], False)
    assert pool.pick("agnes", "video") is None
    assert pool.set_enabled(rec["id"], True)
    assert pool.pick("agnes", "video") is not None


def test_remove_and_idempotent(pool):
    rec = pool.add("agnes", "image", "sk-del-" + "h" * 40)
    assert pool.remove(rec["id"])
    assert not pool.remove(rec["id"])
    assert pool.list("agnes", "image") == []


def test_list_filters(pool):
    pool.add("agnes", "image", "sk-im-" + "i" * 40)
    pool.add("agnes", "video", "sk-vd-" + "j" * 40, priority=2, service="s1")
    assert len(pool.list(provider="agnes")) == 2
    assert len(pool.list(capability="image")) == 1
    assert len(pool.list(provider="agnes", capability="video")) == 1
    row = pool.list(provider="agnes", capability="video")[0]
    assert row["priority"] == 2 and row["service"] == "s1"


def test_seal_missing_rejects_write(pool, monkeypatch):
    monkeypatch.delenv("SHIPIN_KEY_SEAL", raising=False)
    from shipin_platform.services.key_pool import SealMissingError
    with pytest.raises(SealMissingError):
        pool.add("agnes", "video", "sk-noseal-" + "k" * 20)


# ── registry 集成：池命中 → env 回退 ────────────────────────────


def test_registry_falls_back_to_env_when_pool_empty(monkeypatch, pool):
    import shipin_platform.services.key_pool as kp_mod
    from shipin_platform.services.provider_registry import get_registry
    monkeypatch.setattr(kp_mod, "get_pool", lambda: pool)
    monkeypatch.setenv("AGNES_KEY", "sk-env-fallback-" + "m" * 20)
    c = get_registry().creds("agnes", "video")
    assert c["key"] == "sk-env-fallback-" + "m" * 20
    assert "keypool" not in c.get("source", "")


def test_registry_picks_pool_key_by_capability(monkeypatch, pool):
    import shipin_platform.services.key_pool as kp_mod
    from shipin_platform.services.provider_registry import get_registry
    pool_key = "sk-pool-video-" + "n" * 30
    pool.add("agnes", "video", pool_key, priority=1)
    monkeypatch.setattr(kp_mod, "get_pool", lambda: pool)
    monkeypatch.setenv("AGNES_KEY", "sk-env-should-not-" + "o" * 10)
    c = get_registry().creds("agnes", "video")
    assert c["key"] == pool_key
    assert c["source"].startswith("keypool:")
    # image 无池内登记 → env 回退（避免用的是其它 capability 的 key）
    c2 = get_registry().creds("agnes", "image")
    assert c2["key"] == "sk-env-should-not-" + "o" * 10


# ── 管理 API 契约 ───────────────────────────────────────────────


def test_api_register_never_echoes_secret(client):
    secret = "sk-api-secret-" + "q" * 50
    r = client.post("/api/platform/keypool", json={
        "provider": "test-provider-1", "capability": "video",
        "key": secret, "service": "acc-a", "priority": 3})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["registered"]["sha256_short"]
    assert secret not in json.dumps(body)
    assert "cipher" not in json.dumps(body["registered"])


def test_api_list_and_toggle_and_delete(client):
    r = client.post("/api/platform/keypool", json={
        "provider": "test-provider-2", "capability": "image",
        "key": "sk-api-2-" + "r" * 40})
    assert r.status_code == 200
    kid = r.json()["registered"]["id"]

    rows = client.get("/api/platform/keypool",
                      params={"provider": "test-provider-2"}).json()["keys"]
    assert rows and rows[0]["capability"] == "image"
    for row in rows:
        assert "key" not in row and "cipher" not in row

    # 停用后 pick 不再命中（经由 registry 断言）→ 这里只验状态更新
    r = client.patch(f"/api/platform/keypool/{kid}", json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    rows = client.get("/api/platform/keypool",
                      params={"provider": "test-provider-2"}).json()["keys"]
    assert rows[0]["enabled"] == 0

    assert client.delete(f"/api/platform/keypool/{kid}").status_code == 200
    assert client.delete(f"/api/platform/keypool/{kid}").status_code == 404


def test_api_seal_missing_conflicts_503(client, monkeypatch):
    monkeypatch.delenv("SHIPIN_KEY_SEAL", raising=False)
    r = client.post("/api/platform/keypool", json={
        "provider": "test-provider-3", "capability": "video",
        "key": "sk-noseal-api-" + "s" * 20})
    assert r.status_code == 503