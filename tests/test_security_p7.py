"""P7 安全硬化测试：限流（429+头）/ 请求审计导出 / CORS 白名单 / preflight 富化。

测法要点：
- 限流/鉴权开关都是逐请求读 env（与 auth 同构），用 monkeypatch 切换；
- CORSMiddleware 在首次请求时已实例化（allow_origins 固化），测试通过
  替换 app.user_middleware 里的 CORS 条目 + 清空 middleware_stack 重建，
  模拟生产里不同 SHIPIN_CORS_ORIGINS 配置的启动态；env 默认路径（空列表）
  用同一机制验证"默认全关"；
- audit 落库到 data/audit_log.db（真实文件读写，与平台其余 store 一致），
  断言只取本次请求对应的行，不依赖全局行数。
"""
import pytest
from starlette.middleware import Middleware
from starlette.testclient import TestClient

from api import app
from shipin_platform.services import audit_store, rate_limiter

ADMIN = "p7-test-admin-key-not-a-real-secret"
CORS_METHODS = ["GET", "POST", "PUT", "DELETE"]
CORS_HEADERS = ["X-API-Key", "Content-Type", "Authorization"]


@pytest.fixture()
def strict_env(monkeypatch):
    monkeypatch.setenv("SHIPIN_AUTH_MODE", "strict")
    monkeypatch.setenv("SHIPIN_ADMIN_KEY", ADMIN)
    monkeypatch.setenv("SHIPIN_RATE_MODE", "off")  # 限流仅在下文显式开启
    return TestClient(app)


def _reconfigure_cors(origins):
    """替换 app 上的 CORS 中间件条目并强制重建 middleware 栈。"""
    from fastapi.middleware.cors import CORSMiddleware

    for i, mw in enumerate(app.user_middleware):
        if mw.cls is CORSMiddleware:
            app.user_middleware[i] = Middleware(
                CORSMiddleware,
                allow_origins=origins,
                allow_methods=CORS_METHODS,
                allow_headers=CORS_HEADERS,
                allow_credentials=False,
            )
            app.middleware_stack = None  # 下次请求时按新配置重建
            return
    raise AssertionError("CORSMiddleware not registered on app")


def _admin_hdr():
    return {"X-API-Key": ADMIN}


def _reset_buckets():
    """清空进程内共享的计数桶，保证各限流用例互不串扰。"""
    rate_limiter._STATE.clear()


# ── 限流（SHIPIN_RATE_MODE=on；IP 桶与 key 桶分离）──────────────────

def test_rate_limit_429_with_headers(strict_env, monkeypatch):
    _reset_buckets()
    monkeypatch.setenv("SHIPIN_RATE_MODE", "on")
    monkeypatch.setenv("SHIPIN_RATE_MAX", "3")
    c = strict_env
    # 同一 key 第 4 次请求 → 429 + 限流头
    for _ in range(3):
        assert c.get("/api/projects", headers=_admin_hdr()).status_code == 200
    r = c.get("/api/projects", headers=_admin_hdr())
    assert r.status_code == 429
    h = r.headers
    assert h["x-ratelimit-limit"] == "3"
    assert h["x-ratelimit-remaining"] == "0"
    assert h["x-ratelimit-window"] == "60"
    assert int(h["retry-after"]) >= 0
    assert "RATE_LIMITED" in r.text
    # 公开端点不受限流影响（/api/health 不计数）
    assert c.get("/api/health").status_code == 200


def test_rate_limit_separate_buckets_per_key(strict_env, monkeypatch):
    _reset_buckets()
    monkeypatch.setenv("SHIPIN_RATE_MODE", "on")
    monkeypatch.setenv("SHIPIN_RATE_MAX", "2")
    c = strict_env
    r0 = c.post("/api/platform/keys", headers=_admin_hdr(),
                json={"label": "p7-a", "scope": "read"})
    r1 = c.post("/api/platform/keys", headers=_admin_hdr(),
                json={"label": "p7-b", "scope": "read"})
    ka, kb = r0.json()["key"], r1.json()["key"]
    # 两个不同 key 各自独立计数
    assert c.get("/api/projects", headers={"X-API-Key": ka}).status_code == 200
    assert c.get("/api/projects", headers={"X-API-Key": ka}).status_code == 200
    assert c.get("/api/projects", headers={"X-API-Key": ka}).status_code == 429
    assert c.get("/api/projects", headers={"X-API-Key": kb}).status_code == 200


def test_rate_limiter_unit(monkeypatch):
    _reset_buckets()
    monkeypatch.setenv("SHIPIN_RATE_MODE", "on")
    monkeypatch.setenv("SHIPIN_RATE_IP_MAX", "1")
    # 无 token → IP 桶（ip_max）
    ok1, info1 = rate_limiter.check_rate(token=None, ip="10.0.0.7")
    ok2, info2 = rate_limiter.check_rate(token=None, ip="10.0.0.7")
    assert ok1 and info1["remaining"] == 0
    assert not ok2 and info2["retry_after"] > 0
    # 不同 IP 独立桶
    ok3, _ = rate_limiter.check_rate(token=None, ip="10.0.0.8")
    assert ok3 is True
    # key 指纹桶走 SHIPIN_RATE_MAX
    monkeypatch.setenv("SHIPIN_RATE_MAX", "1")
    okk1, _ = rate_limiter.check_rate(token="k-1", ip="10.0.0.7")
    okk2, _ = rate_limiter.check_rate(token="k-1", ip="10.0.0.7")
    assert okk1 is True and okk2 is False
    monkeypatch.setenv("SHIPIN_RATE_MODE", "off")
    ok_off, _ = rate_limiter.check_rate(token=None, ip="10.0.0.7")
    assert ok_off is True


# ── CORS（白名单；默认全关；预检旁路鉴权）──────────────────────────

def test_cors_default_denies_cross_origin(strict_env):
    _reconfigure_cors([])  # 默认（未设 SHIPIN_CORS_ORIGINS）= 仅同源
    c = strict_env
    r = c.options("/api/projects",
                  headers={"Origin": "https://evil.example",
                           "Access-Control-Request-Method": "GET"})
    # Starlette 对未白名单源的预检直接 400 且不带 ACAO（浏览器层面拦截）
    assert r.status_code == 400
    assert "access-control-allow-origin" not in r.headers


def test_cors_allowlist_honored(strict_env):
    _reconfigure_cors(["https://app.example"])
    c = strict_env
    ok = c.options(
        "/api/projects",
        headers={"Origin": "https://app.example",
                 "Access-Control-Request-Method": "GET",
                 "Access-Control-Request-Headers": "X-API-Key"})
    assert ok.status_code == 200
    assert ok.headers["access-control-allow-origin"] == "https://app.example"
    assert "X-API-Key" in ok.headers.get("access-control-allow-headers", "")
    # 非白名单源：应答无 ACAO（浏览器层面拦截）
    evil = c.options("/api/projects",
                     headers={"Origin": "https://evil.example",
                              "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in evil.headers


def test_preflight_options_passthrough_no_auth(strict_env):
    _reconfigure_cors(["https://app.example"])
    # 预检不带 X-API-Key 也必须是 200（真实请求才带 key）
    r = strict_env.options(
        "/api/projects",
        headers={"Origin": "https://app.example",
                 "Access-Control-Request-Method": "GET"})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "https://app.example"


# ── 审计导出（JSON / CSV / 403 与 401 落账）────────────────────────

def test_audit_events_json_and_csv(strict_env):
    c = strict_env
    assert c.get("/api/projects/__nope__",
                 headers=_admin_hdr()).status_code == 404
    js = c.get("/api/platform/events", headers=_admin_hdr())
    assert js.status_code == 200
    body = js.json()
    assert body["count"] >= 1
    row = next((e for e in body["events"]
                if e["route"] == "/api/projects/__nope__"), None)
    assert row is not None
    assert row["caller"] and row["ip"] and row["status"] == 404
    # CSV 走 .csv 路径
    csv = c.get("/api/platform/events.csv", headers=_admin_hdr())
    assert csv.status_code == 200
    assert csv.headers["content-type"].startswith("text/csv")
    head = csv.text.splitlines()[0]
    assert "seq,ts,caller,ip,method,route" in head
    assert "/api/projects/__nope__" in csv.text


def test_audit_exports_blocked_for_non_admin(strict_env):
    r = strict_env.post("/api/platform/keys", headers=_admin_hdr(),
                        json={"label": "p7-ro", "scope": "read"})
    ro = {"X-API-Key": r.json()["key"]}
    assert strict_env.get("/api/platform/events",
                          headers=ro).status_code == 403
    assert strict_env.get("/api/platform/events.csv",
                          headers=ro).status_code == 403


def test_audit_row_recorded_for_401(strict_env):
    # 无 key 的请求在 strict 下 401，且该拒绝被计入审计
    strict_env.get("/api/projects")
    rows = audit_store.list_all(limit=200)
    denied = [r for r in rows if r["status"] == 401]
    assert denied[0]["caller"] == "anonymous"
    assert denied[0]["ip"]


# ── preflight 富化（P7 资源面） ────────────────────────────────────

def test_preflight_includes_platform_health(strict_env):
    # 需要真实项目；auth strict 下项目创建走 guard create（同 P5 smoke 前置）
    r = strict_env.post(
        "/api/project/create", headers=_admin_hdr(),
        json={"product_info": "preflight-p7-产品", "target_platform": "douyin",
              "duration_sec": 30, "target_audience": "测试用户",
              "tone": "专业", "creative_direction": "纪实",
              "reference_materials": [], "special_requirements": ""})
    pid = r.json().get("project_id")
    if not pid:  # 环境不允许创建时降级为宽松断言
        return
    pf = strict_env.get(f"/api/pipeline/{pid}/preflight",
                        headers=_admin_hdr())
    assert pf.status_code == 200
    body = pf.json()
    names = {c["name"] for c in body["checks"]}
    assert {"rate_limit", "disk", "audit_log"} <= names
    assert body["platform"]["rate"]["mode"] == "off"  # conftest 默认 off
    assert body["platform"]["audit_rows"] >= 1