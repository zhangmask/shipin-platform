# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec —— 把整个 Shipin 平台打成免 Python 的 Windows 可执行档。

  python -m PyInstaller tools/shipin_platform.spec --noconfirm

产物 dist/shipin-platform/ 整目录即「绿色发布包」：
  shipin-platform.exe                 服务+桌面托盘入口 (双击即用)
  _internal/web/dist/                 内置前端 (免 node_modules/免 Python)
  _internal/ffmpeg/                   ffmpeg+ffprobe（进 PATH，无需安装）
  （首次运行在 exe 旁自动创建 shipin-data/：产品数据、.env 密钥位、
    config 配置均可写 —— 一键迁移）

打包不含 pywebview/pythonnet（体积与生命周期风险），桌面窗口
退化为系统浏览器打开；托盘（pystray）保留：关闭浏览器后服务常驻，
托盘可随时「打开平台 / 退出」。
"""
import os
import sys
from pathlib import Path

# spec 位于 tools/ 下；所有路径显式锚定项目根，避免解析歧义
_SPEC_DIR = Path(SPECPATH)
_PROJ = _SPEC_DIR.parent

block_cipher = None

datas = [
    (str(_PROJ / "web" / "dist"), "web/dist"),              # React SPA
    (str(_PROJ / "config"), "config"),                      # 默认配置（首启复制到可写区）
    (str(_SPEC_DIR / "portable" / "ffmpeg"), "ffmpeg"),     # 内置 ffmpeg/ffprobe
]

hiddenimports = [
    # 服务入口（frozen 下 uvicorn 以 "api:app" 字符串方式导入，需显式收集）
    "api",
    # uvicorn 动态加载的子系统都要显式收
    "uvicorn",
    "uvicorn.config",
    "uvicorn.main",
    "uvicorn.server",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.logging",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "uvicorn.middleware",
    "uvicorn.middleware.wsgi",
    "uvicorn.middleware.proxy_headers",
    "uvicorn.middleware.message_logger",
    "starlette",
    "starlette.applications",
    "starlette.routing",
    "starlette.middleware",
    "starlette.middleware.cors",
    "starlette.middleware.errors",
    "starlette.staticfiles",
    "starlette.responses",
    "starlette.requests",
    "starlette.websockets",
    "multipart",
    "yaml",
    "dotenv",
    "requests",
    "http.client",
    "pystray",
]

excludes = [
    # 不打包桌面窗口后端（免 pythonnet/.NET 依赖），退化为浏览器模式
    "pywebview",
    "webview",
    "clr",
    "pythonnet",
    "proxy",
    # 平台不依赖的重量级可选项
    "openai-whisper",
    "whisper",
    "torch",
    "tkinter",
    "matplotlib",
    "PyQt5",
    "PySide6",
]

a = Analysis(
    [str(_SPEC_DIR / "desktop_app.py")],
    pathex=[str(_PROJ / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="shipin-platform",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,      # 双击无黑窗；日志落 shipin-data/data/*.log
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(_SPEC_DIR / "portable" / "icon.ico"),
    version=None,
)