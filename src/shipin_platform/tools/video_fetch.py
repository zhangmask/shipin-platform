"""视频站搜索 / 分享链接解析 / 下载（参考视频复刻链路的取料层）。

安全模型（产品级）：
- 出网白名单：本模块**只允许**硬编码的公开视频平台域名，http 一律拒绝；
  URL 解析后 host 不在白名单直接抛 `VideoFetchError`。因此 127.0.0.1 /
  192.168.x / 内部域名之类不可能混进来（不依赖 DNS 嗅探，host 名精确匹配）。
- 下载走 yt-dlp 子进程，**参数数组**（shell=False），绝不拼 shell 字符串；
  输出目录必须位于 ref_root 之内（realpath 前缀围栏）——由调用方传入 ref 根。
- 搜索复用 yt-dlp 的 `bilisearchN:` / `ytsearchN:` / `douyinsN:` 提取器
  （免手调站内 API、免 cookie、走公网），单次最多 limit 条。
- 凭据：无——公开视频不需要任何密钥。VLM 反推在 analysis 层单独走 provider
  registry 网关，与本模块无关。

限时长默认 180s（TVC 参考片通常 < 2min；超时整片拒绝，不留半截文件）。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# 白名单：公开视频平台（唯一允许的下载源）
# ---------------------------------------------------------------------------
VIDEO_HOSTS = frozenset({
    # 哔哩哔哩（主域 + 短链域）
    "bilibili.com", "www.bilibili.com", "b23.tv",
    # 抖音（主域 + 分享短链域）
    "douyin.com", "www.douyin.com", "v.douyin.com",
    # YouTube（yt-dlp 覆盖）
    "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be",
    # 快手
    "kuaishou.com", "www.kuaishou.com",
})

_SHARE_URL_RE = re.compile(r"https?://[^\s，。（）\"'<>]+")


class VideoFetchError(ValueError):
    """取源失败（归类错误，可安全转 4xx）。"""


def safe_share_url(input_text: str) -> str:
    """对外入口：分享文本 / 裸链接 → 校验过的干净视频页 URL。

    - 从分享文本中先提取首个 http(s) 链接（覆盖
      「【标题】…b23.tv/xxxx」「0.35 复制… v.douyin.com/xxx」分享态）；
    - 强制 https + host 精确命中白名单；
    - 丢弃 query/fragment（消除签名参数与跟踪码），只保留 https://host/path。
    """
    m = _SHARE_URL_RE.search(input_text or "")
    if not m:
        raise VideoFetchError("未在分享文本中找到链接（请直接粘贴视频页链接）")
    raw = m.group(0).rstrip(".,;!?")
    p = urlparse(raw)
    if p.scheme != "https":
        raise VideoFetchError(f"仅允许 https 链接，收到: {raw[:60]}")
    if not p.hostname:
        raise VideoFetchError(f"链接缺少域名: {raw[:60]}")
    if p.username or p.password:
        raise VideoFetchError("链接不允许携带凭据（user:pass@）")
    host = p.hostname.lower().rstrip(".")
    if host not in VIDEO_HOSTS:
        raise VideoFetchError(
            f"该平台不在白名单（支持 bilibili/douyin/youtube/kuaishou），"
            f"收到: {host}")
    return f"https://{host}{p.path or ''}"


# ---------------------------------------------------------------------------
# 搜索：B站 / YouTube / 抖音关键词 → 视频候选列表
# ---------------------------------------------------------------------------

@dataclass
class VideoCandidate:
    id: str
    title: str
    url: str
    platform: str
    duration_sec: Optional[float] = None
    thumb: str = ""


def _ytdlp_bin() -> str:
    b = shutil.which("yt-dlp")
    if not b:
        raise VideoFetchError("未找到 yt-dlp（需在 PATH）。安装: pip install -U yt-dlp")
    return str(Path(b).resolve())


def _run(argv: list[str], timeout: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, capture_output=True, text=True,
                              shell=False, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise VideoFetchError(
            f"yt-dlp 超时（网络/平台不可达），请稍后重试或改用粘贴链接") from e
    except OSError as e:
        raise VideoFetchError(f"yt-dlp 子进程失败: {e}") from e


_SEARCH_PREFIX = {
    "bilibili": "bilisearch",
    "youtube": "ytsearch",
    "douyin": "douyins",
}


def search_videos(keyword: str, source: str = "bilibili",
                  limit: int = 10) -> list[VideoCandidate]:
    """站内搜索（yt-dlp flat-playlist 提取器）。

    返回候选列表（title/url/缩略图/时长）。候选 URL 仍再过一遍白名单
    （提取器产物也值得双门校验）。平台风控时抛 VideoFetchError，
    前端自动降级「粘贴链接」通道。
    """
    kw = (keyword or "").strip()
    if not kw:
        raise VideoFetchError("搜索关键词为空")
    prefix = _SEARCH_PREFIX.get(source)
    if not prefix:
        raise VideoFetchError(
            f"不支持的搜索源 {source!r}（bilibili/youtube/douyin）")
    n = min(max(int(limit or 10), 1), 20)
    proc = _run([_ytdlp_bin(), "--flat-playlist", "--no-warnings",
                 "-J", f"{prefix}{n}:{kw}"], timeout=45)
    if proc.returncode != 0:
        raise VideoFetchError(
            f"搜索失败（平台可能风控，请改用粘贴链接）: "
            f"{proc.stderr.strip()[-300:]}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise VideoFetchError(f"搜索返回非法 JSON: {e}") from e

    out: list[VideoCandidate] = []
    for ent in (data.get("entries") or [])[:n]:
        if not isinstance(ent, dict):
            continue
        vid = str(ent.get("id") or "")
        title = str(ent.get("title") or "").strip()
        if not vid or not title:
            continue
        page = str(ent.get("webpage_url") or "").strip()
        if not page:
            if source == "bilibili":
                page = f"https://www.bilibili.com/video/{vid}"
            elif source == "youtube":
                page = f"https://www.youtube.com/watch?v={vid}"
            else:
                continue
        try:
            page = safe_share_url(page)  # 双门校验：提取器产物同样过白名单
        except VideoFetchError:
            continue
        th = ent.get("thumbnails") or []
        thumb = ""
        if isinstance(th, list) and th:
            first = th[0]
            if isinstance(first, dict):
                thumb = str(first.get("url") or "")
        dur = ent.get("duration")
        out.append(VideoCandidate(
            id=vid, title=title, url=page, platform=source,
            duration_sec=None if dur in (None, "", 0) else float(dur),
            thumb=thumb))
    return out


# ---------------------------------------------------------------------------
# 下载
# ---------------------------------------------------------------------------

def _confine_dir(dest_dir: Path, ref_root: Path) -> Path:
    """路径围栏：dest_dir 必须位于 ref_root 之内（realpath 前缀校验）。"""
    d = Path(dest_dir).resolve()
    r = Path(ref_root).resolve()
    if not d.is_relative_to(r):
        raise VideoFetchError(f"下载目录越界被拒绝: {d}")
    d.mkdir(parents=True, exist_ok=True)
    return d


def download_video(url: str, dest_dir: Path, *,
                   max_duration: int = 180,
                   ref_root: Optional[Path] = None) -> dict:
    """下载单个视频到 dest_dir（白名单 URL + 时长上限 + 目录围栏）。

    返回 {ok, file, url, error?}。失败时 ok=False + error（不留半截文件）。
    """
    page = safe_share_url(url)                     # 再进一次白名单
    root = Path(ref_root) if ref_root else Path(dest_dir)
    d = _confine_dir(dest_dir, root)
    cap = min(max(int(max_duration or 180), 5), 3600)

    argv = [
        _ytdlp_bin(),
        "--no-warnings", "--no-playlist", "--max-downloads", "1",
        "-f", "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b",
        "--merge-output-format", "mp4",
        "--match-filter", f"duration <= {cap}",
        "--paths", str(d),
        "-o", "video.%(ext)s",
    ]
    proc = _run(argv + [page], timeout=900)

    meta = _probe_download(d)
    if proc.returncode != 0 or not meta:
        detail = (proc.stderr or "").strip()[-400:]
        raise VideoFetchError(
            f"下载失败（可能超过 {cap}s 上限或平台限制）: {detail}")
    return {"ok": True, "file": str(meta), "page": page}


def _probe_download(d: Path) -> Optional[Path]:
    """找 d 下的视频成品（video.mp4 或其变体）。"""
    for cand in sorted(Path(d).glob("video.*")):
        if cand.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov"):
            return cand
    return None


def probe_duration(path: str | Path) -> Optional[float]:
    """ffprobe 取时长（供展示/节奏分析；失败返回 None 不阻断）。"""
    try:
        from shipin_platform.analysis.reference import _ffprobe_meta
        meta = _ffprobe_meta(str(path))
        return float(meta.get("format", {}).get("duration", 0.0) or 0.0) or None
    except Exception:
        return None