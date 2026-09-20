"""统一根目录解析：源码模式与 PyInstaller 冻结模式共用。

冻结（打包发布）时：
- 只读资源（web/dist 前端、config 默认值）在 sys._MEIPASS 解包目录；
- 可写数据（data/、.env、用户生成的全部产物）在 exe 同级的
  shipin-data/ 目录（PORTABLE：拷文件夹即迁移，不写注册表/用户目录）；
- 本机工具路径（ffmpeg/ffprobe，随包分发在 _MEIPASS/ffmpeg）由
  prepend_tools_path() 注入 PATH——子进程按名字找到，无需用户装任何东西。

源码模式保持原样：一切相对项目根 D:\\aishipin\\shipin-platform。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def is_frozen() -> bool:
    """当前是否运行在 PyInstaller 打包后的环境。"""
    return _frozen()


def resource_root() -> Path:
    """只读资源根：冻结=解包目录；源码=项目根。"""
    if _frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    # src/shipin_platform/roots.py → 项目根
    return Path(__file__).resolve().parents[2]


def data_root() -> Path:
    """可写数据根（data/ 的父目录）。冻结时 = exe 旁 shipin-data。"""
    if _frozen():
        return Path(sys.executable).resolve().parent / "shipin-data"
    return Path(__file__).resolve().parents[2]


def data_dir() -> Path:
    d = data_root() / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ensure_data_dirs() -> None:
    (data_dir() / "graphs").mkdir(parents=True, exist_ok=True)
    (data_dir() / "projects").mkdir(parents=True, exist_ok=True)


def config_dir() -> Path:
    """可写配置目录：源码=项目根/config；打包=shipin-data/config。

    providers.json / global_budget.json 等需用户可编辑、预算可写，
    不能留在只读解包目录里。
    """
    d = data_root() / "config"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ensure_config_dir() -> None:
    """打包首启：把随包默认配置复制到可写 config/（已存在则不覆盖）。

    源码模式 no-op（config_dir 即资源本身）。
    """
    if not _frozen():
        return
    src = resource_root() / "config"
    dst = config_dir()
    if not src.is_dir():
        return
    for p in src.iterdir():
        if p.is_file() and not (dst / p.name).exists():
            try:
                (dst / p.name).write_bytes(p.read_bytes())
            except OSError:
                pass


def prepend_tools_path() -> None:
    """把随包工具目录（ffmpeg/ffprobe 等）放入 PATH（幂等）。"""
    if not _frozen():
        return
    extra = [str(resource_root() / "ffmpeg")]
    for p in extra:
        if os.path.isdir(p) and p not in os.environ.get("PATH", ""):
            os.environ["PATH"] = p + os.pathsep + os.environ.get("PATH", "")