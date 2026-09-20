"""桌面版 App：像 Windows 程序一样打开的平台图形界面。

行为：
1. 若服务未在运行，自动拉起（127.0.0.1:8766，环境变量透传，含
   SHIPIN_AUTH_MODE / AGNES_KEY / SHIPIN_KEY_SEAL 等）；
2. 打开原生桌面窗口（pywebview 内嵌 Chromium WebView2），加载平台
   Web 界面（/ui React SPA）；
3. 点「×」不退出：窗口最小化/隐藏进系统托盘（Windows 通知区），
   进程继续常驻（对外部 AI 的调用保持响应）；
4. 托盘菜单：左键/「显示平台」还原窗口，「退出平台」真正结束；
5. 没有托盘能力（pystray 缺失）时退化为「关闭需确认」（confirm_close），
   确认后退出；没有 pywebview / WebView2 时退化系统浏览器。

打包（PyInstaller，frozen）行为全部相同，且：
- 不再 spawn `python -m uvicorn` 子进程（机器上可能没有 Python），
  改为进程内直接跑 uvicorn.Server 线程；
- 随包 ffmpeg/ffprobe 目录注入 PATH（无需用户安装任何运行环境）；
- 可写数据在 exe 同级 shipin-data/（源码头则在项目根 data/）。

参数：
    python tools/desktop_app.py             # 弹桌面窗（源码开发模式）
    tools/dist/shipin-platform.exe          # 打包版：同样打开窗口+托盘
    python tools/desktop_app.py --no-window # 只拉起服务+健康检查（无头验证）
    python tools/desktop_app.py --port 8765 # 自定义端口
"""
from __future__ import annotations

import argparse
import http.client
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

# 源码模式（python tools/desktop_app.py）注入 src/；打包后 roots 在包内
if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from shipin_platform import roots  # noqa: E402

ROOT = roots.data_root()
DEFAULT_PORT = 8766
LOCAL_HOST = "127.0.0.1"  # 固定字面量：任何请求只允许打本地环回


def _local_get(port: int, path: str, timeout: float = 2.0) -> tuple[int, bytes]:
    """对本地服务的只读 GET。主机是常量，端口做范围校验——
    不解析任何外部主机，也无重定向跟随（http.client 语义）。"""
    if not (1 <= int(port) <= 65535):
        raise ValueError(f"port out of range: {port}")
    if not path.startswith("/"):
        raise ValueError(f"path must start with '/': {path!r}")
    conn = http.client.HTTPConnection(LOCAL_HOST, int(port), timeout=timeout)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def _server_alive(port: int) -> bool:
    try:
        status, body = _local_get(port, "/api/health")
        return status == 200 and b'"status":"ok"' in body
    except Exception:
        return False


def _start_in_process(port: int) -> threading.Thread:
    """打包模式下进程内跑 uvicorn（机器无 Python 也能常驻服务）。

    独立守护线程 + webview/浏览器主循环并存的典型编排：
    服务线程 setDaemon，主进程退出即随之结束。
    """
    import uvicorn

    config = uvicorn.Config(
        "src.api:app" if not roots.is_frozen() else "api:app",
        host=LOCAL_HOST, port=int(port), log_level="info")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True, name="uvicorn")
    t.start()
    return t


def start_server(port: int = DEFAULT_PORT) -> subprocess.Popen | threading.Thread | None:
    """服务未启动才拉起，返回子进程/线程（复用现有时返回 None）。"""
    if _server_alive(port):
        print(f"[desktop] 服务已在运行: http://{LOCAL_HOST}:{port}")
        return None
    # 与源码模式子进程相同默认：用户没配 .env 时本地免鉴权
    os.environ.setdefault("SHIPIN_AUTH_MODE", "off")
    os.environ.setdefault("SHIPIN_RATE_MODE", "off")
    roots.ensure_data_dirs()
    (ROOT / "data").mkdir(parents=True, exist_ok=True)
    # 打包态：不依赖 python 环境，进程内直接起 uvicorn 线程
    if roots.is_frozen():
        print(f"[desktop] 进程内启动服务: http://{LOCAL_HOST}:{port}")
        _start_in_process(port)
        for _ in range(60):  # 最多等 10s
            if _server_alive(port):
                print(f"[desktop] 服务已启动: http://{LOCAL_HOST}:{port}")
                return threading.current_thread()
            time.sleep(0.5)
        print("[desktop] 服务启动超时（进程内）")
        return None
    env = dict(os.environ)
    log = open(ROOT / "data" / "desktop_server.log", "ab", buffering=0)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "src.api:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT), env=env, stdout=log, stderr=log,
        creationflags=(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                       | 0x00000200))
    for _ in range(60):  # 最多等 10s
        if _server_alive(port):
            print(f"[desktop] 服务已启动(pid={proc.pid}): "
                  f"http://{LOCAL_HOST}:{port}")
            return proc
        time.sleep(0.5)
    print("[desktop] 服务启动超时，日志: data/desktop_server.log")
    return proc


def _build_tray_icon(window) -> object | None:
    """系统托盘（可选依赖 pystray）：返回 icon 对象；不可用时返回 None。

    icon.run_detached() 不阻塞 webview 主循环；icon.stop() 由退出菜单触发。
    """
    try:
        import pystray
        from PIL import Image
    except Exception:
        return None
    img = Image.new("RGB", (64, 64), (38, 66, 133))
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    d.ellipse([14, 14, 50, 50], fill=(104, 180, 255))
    d.polygon([(24, 36), (44, 36), (34, 26)], fill=(20, 28, 50))
    icon = pystray.Icon(
        "shipin-platform", img, "Shipin 平台（后台运行中）",
        menu=pystray.Menu(
            pystray.MenuItem("显示平台窗口", lambda: window.show()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出平台", lambda: (
                icon.stop(), window.destroy()))))
    return icon


def open_window(url: str) -> None:
    """首选 pywebview 原生窗口；不可用则回退系统浏览器。

    点「×」：有托盘 → 隐藏到托盘常驻；无托盘 → 弹确认关（confirm_close）。

    打包（frozen）的进程生命周期：服务跑在 daemon 线程，主线程必须常驻
    才有服务——pywebview 窗口（webview.start 阻塞）或 pystray 托盘
    （icon.run 阻塞）都满足；两样都缺时回退阻塞循环（等待一次 Enter/无限
    sleep 会鬼祟地养着服务，直到用户杀进程）。这里明确交给托盘：
    打包版没有 pywebview 一定走浏览器模式，但托盘「退出平台」= 真正退出。
    """
    try:
        import webview  # 桌面可选依赖
    except Exception:
        import webbrowser
        print(f"[desktop] 未安装 pywebview，改用系统浏览器: {url}")
        webbrowser.open(url)
        if roots.is_frozen() and _tray_blocking(url) is None:
            # 托盘也失败：sleep 循环保持服务存活（用户 Ctrl+C / 杀进程退出）
            import threading
            stop = threading.Event()
            stop.wait()

        return
    try:
        window = webview.create_window(
            "Shipin 平台", url, width=1440, height=900,
            min_size=(1024, 700), confirm_close=False)
        icon = _build_tray_icon(window)
        if icon is not None:
            icon.run_detached()

            def _closing():
                # 点「×」不退出：藏进托盘，进程与后端服务继续在线
                print("[desktop] 窗口关闭 → 最小化到托盘（托盘「退出平台」结束）")
                try:
                    window.hide()
                except Exception:
                    pass
                return False  # 阻止真实关闭

            window.events.closing += _closing
        webview.start()
    except Exception as e:
        import webbrowser
        print(f"[desktop] 桌面窗口不可用（{e}），改用系统浏览器")
        webbrowser.open(url)


def _tray_blocking(url: str) -> object | None:
    """打包版托盘：常驻进程 + 「打开平台 / 退出」菜单；失败返回 None。"""
    try:
        import webbrowser
        import pystray
        from PIL import Image, ImageDraw
    except Exception as e:
        print(f"[desktop] 托盘不可用（{e}）")
        return None
    img = Image.new("RGB", (64, 64), (38, 66, 133))
    d = ImageDraw.Draw(img)
    d.ellipse([14, 14, 50, 50], fill=(104, 180, 255))
    d.polygon([(24, 36), (44, 36), (34, 26)], fill=(20, 28, 50))
    icon = pystray.Icon(
        "shipin-platform", img, "Shipin 平台（后台运行中）",
        menu=pystray.Menu(
            pystray.MenuItem("打开平台", lambda: webbrowser.open(url)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出平台", lambda: icon.stop())))
    icon.run()  # 阻塞：托盘常驻直到点退出
    return icon


def health_check(port: int) -> None:
    status, body = _local_get(port, "/api/health", timeout=5)
    assert status == 200 and b'"status":"ok"' in body, body[:200]
    status, html = _local_get(port, "/ui/", timeout=5)
    assert status == 200 and b'id="root"' in html or b"index.html" in html[:300]
    print(f"[desktop] 健康检查 OK（/api/health 200、/ui HTML 就绪）")


def main() -> int:
    ap = argparse.ArgumentParser(description="Shipin 平台桌面版入口")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--page", choices=["home", "graph"], default="home",
                    help="默认打开页面：home=项目列表 / graph=节点画布")
    ap.add_argument("--no-window", action="store_true",
                    help="只拉起服务+健康检查，不弹窗口（无头验证）")
    args = ap.parse_args()

    if roots.is_frozen():
        roots.prepend_tools_path()  # 随包 ffmpeg/ffprobe 进 PATH
        roots.ensure_config_dir()   # 首启复制默认配置到可写区
        # windowed exe 无控制台：把启动日志落到 shipin-data/data/ 便于排障
        try:
            (ROOT / "data").mkdir(parents=True, exist_ok=True)
            _logf = open(ROOT / "data" / "desktop_server.log", "a",
                         buffering=1, encoding="utf-8")
            sys.stdout = sys.stderr = _logf
        except OSError:
            pass

    start_server(args.port)
    try:
        health_check(args.port)
    except Exception as e:
        print(f"[desktop] 健康检查失败: {e}")
        return 1
    if args.no_window:
        print("[desktop] --no-window：服务就绪，本轮不弹窗")
        if roots.is_frozen():
            # 打包版服务跑在 daemon 线程，主线程一旦返回，进程退出、服务即死。
            # 无头模式必须驻留（Ctrl+C / taskkill 结束），否则健康检查完即宕。
            print("[desktop] 无头常驻中（Ctrl+C 或 taskkill 结束）")
            try:
                threading.Event().wait()
            except KeyboardInterrupt:
                pass
        return 0
    page = "/ui/#/graph" if args.page == "graph" else "/ui/"
    open_window(f"http://{LOCAL_HOST}:{args.port}{page}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())