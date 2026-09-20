"""P0 平台级鉴权与项目管理测试。

覆盖：X-API-Key 中间件（401/403/放行）、scope 分级（read/write/admin）、
项目绑定 key 的路径级隔离、key 签发/列表/撤销（明文只出现一次）、
owner 归属与 /api/projects?mine=1 过滤、auth_missing 全放行语义。
"""
import os

import pytest
from starlette.testclient import TestClient

from api import app, _shipin_root

ADMIN = "p0-test-admin-key-not-a-real-secret"


@pytest.fixture()
def strict_client(monkeypatch):
    monkeypatch.setenv("SHIPIN_AUTH_MODE", "strict")
    monkeypatch.setenv("SHIPIN_ADMIN_KEY", ADMIN)
    return TestClient(app)


def _admin_hdr():
    return {"X-API-Key": ADMIN}


# ── 401 / 放行 ────────────────────────────────────────────────────

def test_no_key_rejected_in_strict(strict_client):
    r = strict_client.get("/api/projects")
    assert r.status_code == 401
    assert "UNAUTHENTICATED" in r.text


def test_public_paths_bypass_auth(strict_client):
    assert strict_client.get("/api/health").status_code == 200
    assert strict_client.get("/api/health", headers=_admin_hdr()).status_code == 200


def test_off_mode_allows_anonymous(monkeypatch):
    monkeypatch.setenv("SHIPIN_AUTH_MODE", "off")
    c = TestClient(app)
    assert c.get("/api/projects").status_code == 200


# ── key 生命周期 ───────────────────────────────────────────────────

def test_issue_key_once_only(strict_client):
    r = strict_client.post("/api/platform/keys", headers=_admin_hdr(),
                           json={"label": "ro-cli", "scope": "read"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["once_only"] is True
    assert len(body["key"]) >= 30
    # 列表里没有明文，只有元数据
    keys = strict_client.get("/api/platform/keys",
                             headers=_admin_hdr()).json()["keys"]
    assert all("key" not in k and "hash" not in k for k in keys)


def test_bad_scope_rejected(strict_client):
    r = strict_client.post("/api/platform/keys", headers=_admin_hdr(),
                           json={"label": "bad", "scope": "root"})
    assert r.status_code == 422


def test_revoke_disables_key(strict_client):
    r = strict_client.post("/api/platform/keys", headers=_admin_hdr(),
                           json={"label": "temp", "scope": "read"})
    hdr = {"X-API-Key": r.json()["key"]}
    assert strict_client.get("/api/projects", headers=hdr).status_code == 200
    # 找到指纹并撤销
    keys = strict_client.get("/api/platform/keys",
                             headers=_admin_hdr()).json()["keys"]
    target = next(k for k in keys if k["label"] == "temp")
    # 通过列表拿不到 key_hash，直接按 label 查库撤销
    from shipin_platform.guard.api_auth import ApiKeyStore, hash_key
    store = ApiKeyStore(str(_shipin_root / "data" / "auth_keys.db"))
    assert store.revoke(hash_key(r.json()["key"])) is True
    assert strict_client.get("/api/projects", headers=hdr).status_code == 401


# ── scope 与项目隔离 ────────────────────────────────────────────

def test_read_key_cannot_write(strict_client):
    r = strict_client.post("/api/platform/keys", headers=_admin_hdr(),
                           json={"label": "ro", "scope": "read"})
    ro_hdr = {"X-API-Key": r.json()["key"]}
    resp = strict_client.post("/api/project/create",
                              json={"project_id": "p0-ro-1"}, headers=ro_hdr)
    assert resp.status_code == 403
    assert "FORBIDDEN_SCOPE" in resp.text


def test_bound_project_key_cannot_touch_other_projects(strict_client):
    strict_client.post("/api/project/create",
                       json={"project_id": "p0-own-1"}, headers=_admin_hdr())
    r = strict_client.post("/api/platform/keys", headers=_admin_hdr(),
                           json={"label": "bound", "scope": "write",
                                 "project_id": "p0-own-1"})
    b_hdr = {"X-API-Key": r.json()["key"]}
    # 绑定项目可写
    resp = strict_client.post("/api/project/create",
                              json={"project_id": "p0-own-1"}, headers=b_hdr)
    assert resp.status_code == 200  # 幂等重入
    # body 型端点绑定校验:其他项目被拒
    resp = strict_client.post("/api/project/create",
                              json={"project_id": "p0-other-1"}, headers=b_hdr)
    assert resp.status_code == 403
    assert "FORBIDDEN_PROJECT" in resp.text
    # 路径型端点同样被拒
    resp = strict_client.get("/api/project/p0-other-1/status", headers=b_hdr)
    assert resp.status_code == 403


def test_write_key_can_write(strict_client):
    r = strict_client.post("/api/platform/keys", headers=_admin_hdr(),
                           json={"label": "rw", "scope": "write"})
    rw_hdr = {"X-API-Key": r.json()["key"]}
    resp = strict_client.post("/api/project/create",
                              json={"project_id": "p0-rw-1"}, headers=rw_hdr)
    assert resp.status_code == 200
    assert resp.json()["owner"] == "rw/write"


def test_admin_required_for_platform_api(strict_client):
    r = strict_client.post("/api/platform/keys", headers=_admin_hdr(),
                                json={"label": "rw2", "scope": "write"})
    rw_hdr = {"X-API-Key": r.json()["key"]}
    resp = strict_client.post("/api/platform/keys",
                              json={"label": "x", "scope": "read"},
                              headers=rw_hdr)
    assert resp.status_code == 403


# ── owner 与 mine 过滤 ──────────────────────────────────────────

def test_project_owner_mine_filter(strict_client):
    strict_client.post("/api/project/create",
                       json={"project_id": "p0-mine-1"}, headers=_admin_hdr())
    r = strict_client.post("/api/platform/keys", headers=_admin_hdr(),
                           json={"label": "alice", "scope": "write"})
    a_hdr = {"X-API-Key": r.json()["key"]}
    strict_client.post("/api/project/create",
                       json={"project_id": "p0-alice-1"}, headers=a_hdr)
    # mine=1 以当前调用者 owner（caller 标识）过滤
    mine = strict_client.get("/api/projects?mine=1",
                             headers=a_hdr).json()["projects"]
    ids = {p["project_id"] for p in mine}
    assert "p0-alice-1" in ids
    assert all(p.get("owner") == "alice/write" for p in mine)


# ── 冒烟：管线入口在 strict 下带 key 可用 ─────────────────────────

def test_pipeline_text_requires_auth_when_strict(strict_client):
    r = strict_client.post("/api/pipeline/text",
                           json={"project_id": "nope", "brief": {}})
    assert r.status_code == 401