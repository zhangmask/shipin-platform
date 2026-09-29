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


# 轮73:ASR sidecar 调用——与 _post 同一 SSRF 守卫范式(_assert_local_url
# 行内),目标是第一方 TTS sidecar(默认 127.0.0.1:8201)。WhisperService
# 经此走 faster-whisper,不在本模块外构造任何 URL。
_ASR_URL = os.environ.get("SHIPIN_TTS_SIDECAR_URL",
                          "http://127.0.0.1:8201/asr").rstrip("/")


def sidecar_asr(audio_path: str, language: str = "zh",
                initial_prompt: str = "") -> list[dict]:
    """调 TTS sidecar /asr,返回 whisper 同构 segments [{start,end,text}]。

    initial_prompt(轮73):预期文本解码偏置,压掉短句 ASR 噪声。
    失败向上抛(LocalMediaError/网络/协议)——调用方负责 skip 语义,
    本层绝不吞(静默 skip 冒充审过正是轮45 修掉的病)。
    """
    _assert_local_url(_ASR_URL)  # 轮58/73:守卫行内,紧贴请求
    req = urllib.request.Request(
        _ASR_URL,
        data=_json_dumps({"path": str(audio_path),
                          "language": language,
                          "initial_prompt": initial_prompt}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST")
    try:
        with _OPENER.open(req, timeout=600) as r:
            data = _json_loads(r.read().decode("utf-8"))
    except Exception as e:
        raise LocalMediaError(f"sidecar asr 不可达: {e}")
    segs = data.get("segments")
    if not isinstance(segs, list):
        raise LocalMediaError("sidecar asr 返回无 segments")
    return segs


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
                engine: str = "", steps: int = 0,
                width: int = 0, height: int = 0,
                ref_images: Optional[list[str]] = None) -> dict:
    """Image/text-to-video via the DGX (MiniMax-H3 / Wan 2.2).

    width/height 必须由调用方按项目画布传入——h3api 默认 1344x768 横屏，
    竖屏项目(720x1280)不传就会得到横屏素材,之后被 _normalize_canvas 加
    黑边缩成小横条(有效画面只剩约三分之一),成片比例观感前后不一。

    ref_images(2026-09-26 一致性升级):非空时走 H3 Ref2VA 参考模式——
    业界「金参考」做法的原生对应(Seedance Omni-Reference 同型):把角色
    金参考(及本镜首帧)作为参考图喂进去,让跨镜主体身份不再只靠首帧
    锚定。ref2va 与 i2v 是两套权重,首尾帧字段不再适用。
    """
    eng = engine or os.environ.get("SHIPIN_LOCAL_VIDEO_ENGINE", "h3")
    body = {"prompt": prompt, "duration_s": duration}
    if first_frame:
        body["first_frame"] = _upload(first_frame)
    if last_frame:
        body["last_frame"] = _upload(last_frame)
    if steps:
        body["steps"] = steps
    if width and height:
        body["width"] = int(width)
        body["height"] = int(height)
    if eng == "wan22":
        # 轮67:wan22 I2V(双 expert 14B + lightx2v 4 步)作人物运动镜的
        # 第二引擎——H3 对连续人物位移属上限区(轮60/61/63 实证)。
        # width/height 必须透传:server 端 wan22 分支的默认画幅不是项目
        # 画布,竖屏项目不传就得到横屏,后续 normalize 又要裁剪重编码。
        if not first_frame:
            raise LocalMediaError("wan22 引擎需要首帧图")
        body = {"mode": "wan22", "image": _upload(first_frame),
                "prompt": prompt, "duration_s": duration, "length": 81,
                "negative": os.environ.get("SHIPIN_LOCAL_VIDEO_NEGATIVE", "")}
        if steps:
            body["steps"] = steps
        if width and height:
            body["width"] = int(width)
            body["height"] = int(height)
    elif eng == "wan21-flf2v":
        # 轮67:Wan2.1-FLF2V 首尾帧生视频——端点(start+end image)在
        # VAE 编码后进 latent 采样,模型只做「两点间的受限插值」。
        # 专治两类已实证病根:H3 fl2v 的锚点-运动错拍/场景断裂、wan22
        # i2v 4 步 81 帧尾部运动死亡(静止重复)。首尾帧二者缺一不可。
        if not (first_frame and last_frame):
            raise LocalMediaError("wan21-flf2v 引擎需要首帧和尾帧图")
        body = {"mode": "flf2v",
                "first_frame": _upload(first_frame),
                "last_frame": _upload(last_frame),
                "prompt": prompt, "duration_s": duration, "length": 81,
                "negative": os.environ.get("SHIPIN_LOCAL_VIDEO_NEGATIVE", "")}
        if steps:
            body["steps"] = steps
        if width and height:
            body["width"] = int(width)
            body["height"] = int(height)
    elif ref_images:
        # Ref2VA:参考图模式(权重/协议与 i2v 不同,不带 first/last_frame)
        refs = [_upload(p) for p in ref_images]
        body.pop("first_frame", None)
        body.pop("last_frame", None)
        body["mode"] = "ref2v"
        body["ref_images"] = refs
    else:
        body["mode"] = "i2v" if first_frame else "t2v"
    job = _post("/v1/videos/generations", body)
    done = _wait_job(job["job_id"], max_wait=7200.0)
    if done.get("status") != "succeeded":
        raise LocalMediaError(f"本地视频失败: {done.get('error')}")
    path = _download(_first_file(done), out)
    return {"ok": True, "provider": "local", "engine": eng, "path": path,
            "duration_sec": duration,
            "mode": body.get("mode"),
            "width": body.get("width"), "height": body.get("height")}


def local_dub(frame_path: str, audio_path: str, out: str,
              duration: float = 0.0, engine: str = "",
              width: int = 0, height: int = 0,
              prompt: str = "", steps: int = 6) -> dict:
    """音频驱动配音(照帧重说):InfiniteTalk(Wan2.1-I2V 基座)拿一张静态帧
    + 一条音频重新生成嘴型/表情/头部动作,音频决定嘴怎么动。

    轮73 音视频分离架构的生成侧:本地视频画面不够用或音频换掉时,不再整
    镜重生,而是只重生"照着音频演"的这条画面轨,音频轨由平台 TTS/混音
    管线另行管理(模型输出不带音频,分轨在平台侧合)。24G 显存档位下这是
    必选项:480p 档(最大边 ≤832)+ fp8 基座 + 6 步蒸馏。

    frame_path: 首帧(人物近照,决定长相/姿态);audio_path: 驱动音频;
    duration: 音频秒数(0=ffprobe 自读);width/height: 输出画幅(按调用方
    项目画布传,超过 832 的边等比压回 480p 档)。
    """
    eng = engine or os.environ.get("SHIPIN_LOCAL_DUB_ENGINE", "infinitetalk")
    if eng != "infinitetalk":
        raise LocalMediaError(f"未知配音引擎 {eng!r}(仅 infinitetalk)")
    if not frame_path or not Path(frame_path).is_file():
        raise LocalMediaError(f"配音首帧不存在: {frame_path}")
    if not audio_path or not Path(audio_path).is_file():
        raise LocalMediaError(f"配音音频不存在: {audio_path}")
    sec = duration or _tts_duration(audio_path)
    if sec <= 0.2:
        raise LocalMediaError(f"配音音频时长异常: {sec}s({audio_path})")
    fps = 25
    length = int(sec * fps) - 1
    length = ((length - 1) // 4) * 4 + 1  # Wan latent 需 4n+1
    w, h = int(width or 0), int(height or 0)
    if w and h and max(w, h) > 832:
        k = 832.0 / max(w, h)
        w, h = int(w * k) // 16 * 16, int(h * k) // 16 * 16  # 16 对齐
    body = {"mode": "dub", "image": _upload(frame_path),
            "audio": _upload(audio_path), "length": length, "fps": fps,
            "steps": steps, "cfg": 1.0,
            "prompt": prompt or (
                "A person is talking to the camera, natural lip movements "
                "synchronized with the speech, stable face, static camera, "
                "smooth motion"),
            "negative": os.environ.get("SHIPIN_LOCAL_VIDEO_NEGATIVE", "")}
    if w and h:
        body["width"], body["height"] = w, h
    job = _post("/v1/videos/generations", body)
    done = _wait_job(job["job_id"], max_wait=7200.0)
    if done.get("status") != "succeeded":
        raise LocalMediaError(f"配音失败: {done.get('error')}")
    path = _download(_first_file(done), out)
    return {"ok": True, "provider": "local", "engine": eng, "path": path,
            "duration_sec": round(length / fps, 2),
            "mode": "dub", "width": w or None, "height": h or None}


def _tmp_path(path: str, tag: str) -> str:
    # ffmpeg infers the muxer from the extension: "<f>.mp3.trim" fails with
    # "Unable to choose an output format", so keep the original suffix.
    # 轮63c:候选路径是 "<out>.aN"(非媒体扩展名)——原样保留会让 ffmpeg
    # 无法推断输出封装,trim/atempo 全部静默失败(rc≠0 无异常),坏种
    # 未经加工原样晋升(2026-09-26 两次事故的根因)。非媒体后缀一律
    # 回落 .mp3(libmp3lame 可用)。
    import os
    base, ext = os.path.splitext(path)
    if ext.lower() not in ('.mp3', '.flac', '.wav', '.m4a', '.ogg', '.aac'):
        ext = '.mp3'
    return f"{base}.{tag}{ext or '.mp3'}"


def _tts_duration(path: str) -> float:
    import subprocess
    out = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                          'format=duration', '-of',
                          'default=noprint_wrappers=1:nokey=1', path],
                         capture_output=True, text=True).stdout.strip()
    try:
        return float(out)
    except Exception:
        return 0.0


def _trim_silence(path: str) -> str:
    # VibeVoice pads clips with long silence; trim head/tail.
    import os
    import subprocess
    tmp = _tmp_path(path, 'trim')
    af = ('silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.05,'
          'areverse,silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.05,'
          'areverse')
    r = subprocess.run(['ffmpeg', '-y', '-v', 'error', '-i', path,
                        '-af', af, tmp], capture_output=True)
    if r.returncode == 0:
        os.replace(tmp, path)
    return path


def _atempo_fit(path: str, dur: float, max_sec: float) -> str:
    """把已知时长 dur 的音频时间压缩到 max_sec 内(≤2.0x)。

    dur 由调用方显式传入:不要在这里重新探测——候选项路径带 .aN
    后缀(内容 mp3/flac、扩展名怪异),ffprobe 偶发探测失败返回 0.0,
    旧代码 `dur <= 0` 直接跳过压缩,坏种原样晋升、一路无人发现
    (2026-09-26 实测:9.07s 坏种被当终选,对齐门才拦下)。
    """
    import subprocess
    if dur <= max_sec or dur <= 0:
        return path
    tempo = min(2.0, dur / max_sec)
    tmp = _tmp_path(path, 'fit')
    r = subprocess.run(['ffmpeg', '-y', '-v', 'error', '-i', path,
                        '-filter:a', 'atempo=%.3f' % tempo, tmp],
                       capture_output=True)
    if r.returncode == 0:
        os.replace(tmp, path)
    return path


def _promote_final(src: str, out: str, cands: list) -> str:
    """终稿落回调用方期望的 out 路径,并清掉重试残留的 .aN 副本。

    调用方(tts_service→glob_tts/align)只认 out 路径;最佳 take 若留在
    `.aN` 副本上会被 glob 绕过,对齐读到的是未加工的第 0 次尝试原件。
    """
    import os
    import shutil
    if os.path.abspath(src) != os.path.abspath(out):
        shutil.copyfile(src, out)
    for c in cands:
        if os.path.abspath(c) != os.path.abspath(out) and os.path.exists(c):
            os.remove(c)
    return out


def local_tts(text: str, out: str, engine: str = "", voice: str = "") -> dict:
    # Text-to-speech via the DGX. VibeVoice pacing on short lines is
    # stochastic: a take can come back far too long for the shot budget,
    # so retry with fresh seeds, then time-stretch as a last resort.
    import os
    import time
    eng = engine or os.environ.get("SHIPIN_LOCAL_TTS_ENGINE", "vibevoice")
    # 轮63b:单句预算按字数线性放宽。固定 2.9s 硬顶对 8-10 字合法长句
    # 是物理不可能(实测 vibevoice 最慢 ~0.41s/字,10 字 ≈ 3.7s)——合法
    # 长句连抽 8 次全部「超预算」后显式失败,而它们根本不是坏种。
    # 环境值降为下限,实际上限 = 字数*0.45+0.3(观测最慢速率+ slack),
    # 硬顶 7s;真正压不进的坏种(如 9s 级)仍会在下方显式失败。
    _floor = float(os.environ.get("SHIPIN_LOCAL_TTS_MAX_SEC", "9.5"))
    max_sec = min(7.0, max(_floor, len(text.strip()) * 0.45 + 0.3))
    seed = int(os.environ.get("SHIPIN_LOCAL_TTS_SEED", "42"))
    # 轮63:种子基线按调用时刻加盐——固定基线下同一条坏句每次重跑抽
    # 同一批坏种(seed+attempt*977 恒定),「重跑一次就好了」不成立。
    seed += int(time.time() * 7) % 100000
    best_path, best_dur, attempts_used = "", float("inf"), 0
    cands: list = []
    for attempt in range(8):
        attempts_used = attempt + 1
        # 轮65:voice(role_code)只对 voxcpm 有意义(克隆参考选择);
        # vibevoice 忽略该字段
        _body = {"engine": eng, "text": text, "seed": seed + attempt * 977}
        if voice:
            _body["voice"] = voice
        job = _post("/v1/audio/tts", _body)
        done = _wait_job(job["job_id"])
        if done.get("status") != "succeeded":
            raise LocalMediaError(f"local tts failed: {done.get('error')}")
        cand = _download(_first_file(done), out if attempt == 0
                         else f"{out}.a{attempt}")
        cands.append(cand)
        _trim_silence(cand)
        dur = _tts_duration(cand)
        if dur <= max_sec:  # 完美命中:原速不加工
            _promote_final(cand, out, cands)
            return {"ok": True, "provider": "local", "engine": eng,
                    "path": out, "attempts": attempts_used}
        if dur < best_dur:
            best_path, best_dur = cand, dur
    # 8 次都没原速命中:拿最短的那条时间压缩补齐(max 2.0x 内可救)
    _atempo_fit(best_path, best_dur, max_sec)
    final_dur = _tts_duration(best_path)
    if final_dur > max_sec + 0.15:
        # 压不进预算必须显式失败——旧代码无条件 promote 坏种并返回
        # ok=True,超长旁白无声流入 assemble,直到对齐门才炸且报错指错
        # 方向(2026-09-26 实测 9.07s 坏种晋升事故)。残留 .aN 副本不在此
        # 删:下次成功 promote 时 _promote_final 统一清理,且按 mtime 排
        # 在终选之后不会被误取。
        raise LocalMediaError(
            f"local tts 8 次抽样+压缩后仍超预算: 最短 {best_dur:.2f}s / "
            f"压后 {final_dur:.2f}s > 上限 {max_sec}s——请缩短该句文案")
    _promote_final(best_path, out, cands)
    return {"ok": True, "provider": "local", "engine": eng,
            "path": out, "attempts": attempts_used, "fit": True,
            "raw_sec": round(best_dur, 2)}


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
