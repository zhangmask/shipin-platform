"""P-1 Web 前端挂载：/ui 同源静态点 + / 重定向（dist 未构建时跳过）。"""
from pathlib import Path

import api as api_mod
from fastapi.testclient import TestClient

client = TestClient(api_mod.app)

DIST = Path(api_mod.__file__).resolve().parent.parent / "web" / "dist"


def test_ui_mounted_when_built():
    if not DIST.is_dir():
        import pytest
        pytest.skip("web/dist 未构建——按需 npm run build 后回归")
    r = client.get("/ui/")
    assert r.status_code == 200
    html = r.text
    assert "<div id=\"root\">" in html
    assert "/ui/assets/" in html          # base=/ui/ 生效

    # 静态资源可由 /ui 同源取到
    css = [l for l in html.splitlines() if ".css" in l]
    assert css, "构建产物缺少样式引用"
    r2 = client.get(css[0].split('href="')[1].split('"')[0])
    assert r2.status_code == 200

    # 根路径引导到前端（API 前缀未受影响）
    r3 = client.get("/")
    assert r3.status_code in (200, 307, 308)
    assert client.get("/api/health").status_code == 200