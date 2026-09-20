"""桌面版启动器测试：真实拉起服务 + 健康检查 + SSRF 护栏 + 回退逻辑。"""
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import desktop_app  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_local_get_guard_blocks_bad_inputs():
    with pytest.raises(ValueError):
        desktop_app._local_get(0, "/api/health")
    with pytest.raises(ValueError):
        desktop_app._local_get(65536, "/api/health")
    with pytest.raises(ValueError):
        desktop_app._local_get(8766, "api/health")  # 不以 / 开头


def test_server_boot_and_live_health():
    """真实冒烟：拉起 uvicorn（自带子进程），健康检查通过，然后关闭。"""
    port = _free_port()
    proc = desktop_app.start_server(port)
    assert proc is not None, "应新拉起服务（端口为空闲）"
    try:
        assert desktop_app._server_alive(port)
        status, body = desktop_app._local_get(port, "/api/health", timeout=5)
        assert status == 200 and b'"status":"ok"' in body
        status, html = desktop_app._local_get(port, "/ui/", timeout=5)
        assert status == 200 and b'id="root"' in html
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_health_check_routine_reports_ok(monkeypatch):
    """health_check 通过时只输出版本/健康标志（不 assert 失败路径的 fd）。"""
    port = _free_port()
    proc = desktop_app.start_server(port)
    assert proc is not None
    try:
        desktop_app.health_check(port)  # 不抛异常即通过
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_open_window_falls_back_to_browser_without_pywebview(monkeypatch):
    """无 pywebview 时退化为系统浏览器打开（不崩溃）。"""
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "webview":
            raise ImportError("not installed")
        return real_import(name, *a, **k)

    opened = []
    monkeypatch.setattr(builtins, "__import__", fake_import)
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url))
    desktop_app.open_window("http://127.0.0.1:9999/ui/")
    assert opened == ["http://127.0.0.1:9999/ui/"]