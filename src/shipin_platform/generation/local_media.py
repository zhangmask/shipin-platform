"""Local DGX media backend for shipin-platform.

All media generation (image / video / tts / music) is served by the h3api
service running on the same Spark node (ComfyUI + MiniMax-H3 + Z-Image +
Qwen-Image-2.1 + FLUX.2 + Krea-2 + Wan2.2 + VibeVoice + Music3 / ACE-Step).

Env:
    SHIPIN_LOCAL_API         h3api base url (default http://127.0.0.1:9000)
    SHIPIN_LOCAL_API_TOKEN   bearer token for h3api
    SHIPIN_MEDIA_BACKEND     auto (default) | local | agnes
                             local  => always use the DGX models
                             agnes  => always use the cloud API
                             auto   => use local when SHIPIN_LOCAL_API is
                                       reachable, else fall back to agnes
    SHIPIN_LOCAL_IMAGE_ENGINE   zimage (default) | qwen21 | flux2 | krea2
    SHIPIN_LOCAL_VIDEO_ENGINE   h3 (default) | wan22
    SHIPIN_LOCAL_TTS_ENGINE     vibevoice (default) | kokoro
    SHIPIN_LOCAL_MUSIC_ENGINE   music3 (default) | acestep
"""
from __future__ import annotations

import ipaddress
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_API = os.environ.get("SHIPIN_LOCAL_API", "http://127.0.0.1:9000").rstrip("/")
_TOKEN = os.environ.get("SHIPIN_LOCAL_API_TOKEN", "").strip()
# 额外放行的主机名/IP(逗号分隔;DGX 节点用 lan 主机名时配这里)
_EXTRA_HOSTS = {h.strip().lower() for h in os.environ.get(
    "SHIPIN_LOCAL_ALLOWED_HOSTS", "").split(",") if h.strip()}


class LocalMediaError(RuntimeError):
    """Local backend failure (caller falls back to the cloud provider)."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _assert_local_url(url: str) -> None:
    """出网前置校验(与 provider_registry.assert_safe_url 同范式,行内
    紧贴请求调用,静态安检可见)。

    本模块的唯一合法目标是**第一方本地节点**(Spark/DGX 同机或 lan),
    因此白名单语义与云端相反:协议 http/https,主机必须是 loopback /
    私网(RFC1918) / link-local / ULA / env 显式放行(SHIPIN_LOCAL_ALLOWED_HOSTS)。
    公网主机一律拒绝——防止被配置错误/环境注入时充当公网代理(SSRF)。
    """
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http", "https"):
        raise LocalMediaError(
            f"仅允许 http/https,收到 scheme={u.scheme!r}")
    host = (u.hostname or "").lower().rstrip(".")
    if not host:
        raise LocalMediaError(f"URL 缺少 hostname: {url!r}")
    if host in _EXTRA_HOSTS or host in ("localhost", "::1"):
        return
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # 非字面 IP 的主机名:不在 env 放行名单就拒绝(不做 DNS 解析,
        # 也不跟随——解析放行只会扩大面, lan 名请显式登记)
        raise LocalMediaError(
            f"host {host!r} 非本地/私网地址且未登记 SHIPIN_LOCAL_ALLOWED_HOSTS")
    if ip.is_loopback or ip.is_private or ip.is_link_local:
        return
    raise LocalMediaError(f"host {host!r}({ip}) 非本地/私网地址,已拒绝")


def api_reachable(timeout: float = 3.0) -> bool:
    try:
        _assert_local_url(f"{_API}/v1/health")
        req = urllib.request.Request(f"{_API}/v1/health")
        with _OPENER.open(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def use_local() -> bool:
    """Dispatch decision for media generation (cached per call)."""
    mode = os.environ.get("SHIPIN_MEDIA_BACKEND", "auto").strip().lower()
    if mode == "local":
        return True
    if mode == "agnes":
        return False
    return api_reachable()


def _post(path: str, body: dict, timeout: int = 60) -> dict:
    if not _TOKEN:
        raise LocalMediaError("SHIPIN_LOCAL_API_TOKEN 未配置")
    url = f"{_API}{path}"
    _assert_local_url(url)  # 轮58:出网前 SSRF 守卫(行内,紧贴请求)
    req = urllib.request.Request(
        url,
        data=_json_dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {_TOKEN}"},
        method="POST")
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return _json_loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise LocalMediaError(f"h3api {path} HTTP {e.code}: {detail}")
    except Exception as e:
        raise LocalMediaError(f"h3api {path} 不可达: {e}")


def _get(path: str, timeout: int = 30) -> dict:
    sep = "&" if "?" in path else "?"
    if _TOKEN and "token=" not in path:
        path = f"{path}{sep}token={_TOKEN}"
    url = f"{_API}{path}"
    _assert_local_url(url)  # 轮58:出网前 SSRF 守卫(行内,紧贴请求)
    req = urllib.request.Request(url)
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return _json_loads(r.read().decode("utf-8"))
    except Exception as e:
        raise LocalMediaError(f"h3api {path} 查询失败: {e}")


def _download(url_path: str, out: str, timeout: int = 300) -> str:
    sep = "&" if "?" in url_path else "?"
    if _TOKEN and "token=" not in url_path:
        url_path = f"{url_path}{sep}token={_TOKEN}"
    url = f"{_API}{url_path}"
    _assert_local_url(url)  # 轮58:出网前 SSRF 守卫(行内,紧贴请求)
    req = urllib.request.Request(url)
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            data = r.read()
    except Exception as e:
        raise LocalMediaError(f"下载产物失败: {e}")
    dst = Path(out)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(data)
    return str(dst)


def _wait_job(job_id: str, poll_interval: float = 4.0,
              max_wait: float = 3600.0,
              on_event=None) -> dict:
    """Poll a h3api job until it settles; returns the job dict."""
    t0 = time.time()
    while time.time() - t0 < max_wait:
        job = _get(f"/v1/jobs/{job_id}")
        st = job.get("status")
        if on_event and job.get("progress"):
            on_event(job["progress"])
        if st in ("succeeded", "failed", "cancelled"):
            return job
        time.sleep(poll_interval)
    raise LocalMediaError(f"任务 {job_id} 超时({max_wait:.0f}s)")


def _first_file(job: dict) -> str:
    files = job.get("files") or job.get("result", {}).get("files") or []
    if not files:
        raise LocalMediaError("任务成功但没有产物文件")
    f = files[0]
    return f"/v1/files/{f['filename']}?subfolder={f.get('subfolder', '')}"


# ------------------------------------------------------------------ public API
def local_image(prompt: str, width: int, height: int, out: str,
                engine: str = "", steps: int = 0) -> dict:
    """Text-to-image via the DGX (Z-Image-Turbo / Qwen-Image-2.1 / FLUX.2 / Krea-2)."""
    eng = engine or os.environ.get("SHIPIN_LOCAL_IMAGE_ENGINE", "zimage")
    body = {"engine": eng, "prompt": prompt, "width": width, "height": height}
    if steps:
        body["steps"] = steps
    job = _post("/v1/images/generations", body)
    done = _wait_job(job["job_id"])
    if done.get("status") != "succeeded":
        raise LocalMediaError(f"本地出图失败: {done.get('error')}")
    path = _download(_first_file(done), out)
    return {"ok": True, "provider": "local", "engine": eng, "path": path}


def local_video(prompt: str, out: str, first_frame: str = "",
                last_frame: str = "", duration: int = 5,
                engine: str = "", steps: int = 0) -> dict:
    """Image/text-to-video via the DGX (MiniMax-H3 / Wan 2.2)."""
    eng = engine or os.environ.get("SHIPIN_LOCAL_VIDEO_ENGINE", "h3")
    body = {"prompt": prompt, "duration_s": duration}
    if first_frame:
        body["first_frame"] = _upload(first_frame)
    if last_frame:
        body["last_frame"] = _upload(last_frame)
    if steps:
        body["steps"] = steps
    if eng == "wan22":
        if not first_frame:
            raise LocalMediaError("wan22 引擎需要首帧图")
        body = {"mode": "wan22", "image": _upload(first_frame),
                "prompt": prompt, "duration_s": duration, "length": 81}
    else:
        body["mode"] = "i2v" if first_frame else "t2v"
    job = _post("/v1/videos/generations", body)
    done = _wait_job(job["job_id"], max_wait=7200.0)
    if done.get("status") != "succeeded":
        raise LocalMediaError(f"本地视频失败: {done.get('error')}")
    path = _download(_first_file(done), out)
    return {"ok": True, "provider": "local", "engine": eng, "path": path,
            "duration_sec": duration}


def local_tts(text: str, out: str, engine: str = "") -> dict:
    """Text-to-speech via the DGX (VibeVoice / Kokoro)."""
    eng = engine or os.environ.get("SHIPIN_LOCAL_TTS_ENGINE", "vibevoice")
    job = _post("/v1/audio/tts", {"engine": eng, "text": text})
    done = _wait_job(job["job_id"])
    if done.get("status") != "succeeded":
        raise LocalMediaError(f"本地配音失败: {done.get('error')}")
    path = _download(_first_file(done), out)
    return {"ok": True, "provider": "local", "engine": eng, "path": path}


def local_music(caption: str, out: str, lyrics: str = "",
                duration: int = 60, engine: str = "") -> dict:
    """Music generation via the DGX (MiniMax Music 3 / ACE-Step 1.5)."""
    eng = engine or os.environ.get("SHIPIN_LOCAL_MUSIC_ENGINE", "music3")
    body = {"engine": eng, "caption": caption, "max_duration": duration}
    if lyrics:
        body["lyrics"] = lyrics
    job = _post("/v1/audio/music", body)
    done = _wait_job(job["job_id"], max_wait=3600.0)
    if done.get("status") != "succeeded":
        raise LocalMediaError(f"本地作曲失败: {done.get('error')}")
    path = _download(_first_file(done), out)
    return {"ok": True, "provider": "local", "engine": eng, "path": path}


def _upload(path: str) -> str:
    """Upload a local file to h3api; returns the server-side filename."""
    p = Path(path)
    if not p.is_file():
        raise LocalMediaError(f"待上传文件不存在: {path}")
    boundary = "----shipin" + os.urandom(8).hex()
    body = b""
    body += f"--{boundary}\r\n".encode()
    body += f'Content-Disposition: form-data; name="file"; filename="{p.name}"\r\n'.encode()
    body += b"Content-Type: application/octet-stream\r\n\r\n"
    body += p.read_bytes()
    body += f"\r\n--{boundary}\r\n".encode()
    body += b'Content-Disposition: form-data; name="kind"\r\n\r\n'
    kind = "video" if p.suffix.lower() in (".mp4", ".mov", ".webm", ".mkv") else \
           ("audio" if p.suffix.lower() in (".wav", ".mp3", ".flac", ".ogg") else "image")
    body += kind.encode() + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    url = f"{_API}/v1/uploads"
    _assert_local_url(url)  # 轮58:出网前 SSRF 守卫(行内,紧贴请求)
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "Authorization": f"Bearer {_TOKEN}"},
        method="POST")
    try:
        with _OPENER.open(req, timeout=300) as r:
            res = _json_loads(r.read().decode("utf-8"))
    except Exception as e:
        raise LocalMediaError(f"上传 {path} 失败: {e}")
    return res.get("filename", "")


# ------------------------------------------------------------------ json helpers
def _json_dumps(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)


def _json_loads(s: str):
    import json
    return json.loads(s)
