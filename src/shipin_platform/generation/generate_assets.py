"""Image/video generation — SSRF-safe fetch, path-validated ffmpeg calls.

供应商接入统一走 provider_registry（对齐 hypit runtime profile 设计）：
- 端点/模型/供应商域在 config/providers.json 声明；
- 凭据调用时按 env 引用解析，绝不缓存、绝不落盘；
- 出网统一 safe_fetch：协议 + host + IP 三重校验，拒绝本地/私网/保留地址。
新增或切换模型服务 = 改 providers.json，保持本模块公开签名稳定。
"""
from __future__ import annotations

import json as _json
import os
import shutil
import subprocess
import urllib.request
from pathlib import Path
from typing import Optional

from shipin_platform.services.provider_registry import (
    creds as _registry_creds,
    safe_fetch as _registry_safe_fetch,
    safe_post as _registry_safe_post,
    assert_safe_url as _assert_safe_url,
    ProviderUnavailableError,
)

_IMG_HOSTS = frozenset({"cos-platform-outputs.agnes-ai.cn",
    "images.unsplash.com", "cdn.openai.com", "edge.mediastack.ai",
    "platform-outputs.agnes-ai.space",
    "platform-outputs.agnes-ai.cn",
    # 轮59(一句话驱动实测):API 主机自身——/v1/images 返回的产物 URL
    # 就落在 apihub.agnes-ai.com 上,白名单漏它 = 云端生图 100% 断
    # (换本地后端修好的镜像病:云端路径反而从没跑通过)
    "apihub.agnes-ai.com",
})


# ---------------------------------------------------------------------------
# 本地 DGX 后端分流（ZCode 集成）
#   SHIPIN_MEDIA_BACKEND=local|agnes|auto；model 传 "local:<引擎>" 也可强制本地。
# ---------------------------------------------------------------------------

def _local_backend(model: str = "") -> bool:
    """是否走本地 DGX 媒体后端（h3api）。"""
    if str(model or "").lower().startswith(("local:", "dgx:")):
        return True
    mode = os.environ.get("SHIPIN_MEDIA_BACKEND", "auto").strip().lower()
    if mode == "local":
        return True
    if mode == "agnes":
        return False
    try:
        from shipin_platform.generation import local_media
        return local_media.api_reachable()
    except Exception:
        return False


def _local_engine(model: str, kind: str) -> str:
    """从 "local:zimage" 或默认 env 解析本地引擎名。"""
    m = str(model or "").lower()
    if ":" in m:
        return m.split(":", 1)[1]
    env = {"image": "SHIPIN_LOCAL_IMAGE_ENGINE",
           "video": "SHIPIN_LOCAL_VIDEO_ENGINE",
           "tts": "SHIPIN_LOCAL_TTS_ENGINE",
           "music": "SHIPIN_LOCAL_MUSIC_ENGINE"}.get(kind, "")
    defaults = {"image": "zimage", "video": "h3", "tts": "vibevoice",
                "music": "music3"}
    return os.environ.get(env, defaults.get(kind, ""))


def _agnes_creds(capability: str = "") -> tuple[str, str]:
    """Read Agnes credentials at CALL time via provider registry.

    历史教训：这里曾经在模块导入时捕获 os.environ——uvicorn 从不同目录启动、
    或 dotenv 晚于本模块加载时，key 永远是空串，下游就会误报
    『AGNES_KEY 未配置』并静默降级成占位图。registry 固定"调用时解析"。

    传入 capability（video/image）时优先从 key 池挑选对应 key（见
    services/key_pool.py），池空回退 env——视频/图片可以配不同的 key。
    """
    try:
        c = _registry_creds("agnes", capability if capability else "")
        key = c["key"]
    except ProviderUnavailableError:
        key = ""
    url = os.environ.get("AGNES_BASE_URL", "https://apihub.agnes-ai.com/v1").rstrip("/")
    return key, url


def _media_arg(path) -> str:
    """Validate user-supplied media path (same pattern as ffmpeg_engine._media_arg)."""
    p = Path(str(path)).resolve()
    s = str(p)
    if s.startswith("-"):
        raise ValueError(f"path starts with '-': {s!r}")
    return s


def _safe_fetch(url: str, timeout: int = 30) -> bytes:
    """Fetch URL with protocol/host/IP allowlist (delegates to registry gateway)."""
    from urllib.parse import urlparse
    if urlparse(url).scheme != "https":
        raise ValueError("only https allowed")
    try:
        return _registry_safe_fetch(url, timeout=timeout, allow_hosts=_IMG_HOSTS)
    except ProviderUnavailableError:
        raise
    except Exception as e:  # ProviderSecurityError / ValueError 统一语义
        raise ValueError(str(e)) from e


def _safe_post(url: str, body: bytes, headers: dict,
               timeout: int = 120) -> bytes:
    """POST to generation API with same allowlist enforcement as _safe_fetch."""
    from urllib.parse import urlparse
    if urlparse(url).scheme != "https":
        raise ValueError("only https allowed")
    try:
        return _registry_safe_post(url, body, headers=headers, timeout=timeout,
                                   allow_hosts=_IMG_HOSTS)
    except ProviderUnavailableError:
        raise
    except Exception as e:
        raise ValueError(str(e)) from e


def generate_image_pil(prompt: str, width: int, height: int, out: str) -> dict:
    """PIL gradient placeholder image."""
    from PIL import Image, ImageDraw
    p = Path(out)
    p.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (width, height), color=(30, 30, 35))
    draw = ImageDraw.Draw(img)
    pl = prompt.lower()
    if "咖啡" in pl or "暖" in pl:
        c1, c2 = (62, 40, 30), (120, 75, 40)
    elif "蓝" in pl or "夜" in pl:
        c1, c2 = (25, 35, 60), (45, 60, 90)
    elif "光" in pl or "亮" in pl:
        c1, c2 = (80, 70, 50), (160, 130, 80)
    else:
        c1, c2 = (40, 40, 50), (80, 75, 85)
    for y in range(height):
        r = int(c1[0] + (c2[0] - c1[0]) * y / height)
        g = int(c1[1] + (c2[1] - c1[1]) * y / height)
        b = int(c1[2] + (c2[2] - c1[2]) * y / height)
        draw.line([(0, y), (width, y)], fill=(r, g, b))
    img.save(str(p), "JPEG", quality=92)
    return {"ok": True, "path": out, "mode": "pil_placeholder", "prompt": prompt}


def generate_image_agnes(prompt: str, width: int, height: int, out: str,
                         model: str = "agnes-image-2.5-flash") -> dict:
    """Generate image via Agnes AI (OpenAI-compatible /v1/images/generations).

    本地模式（SHIPIN_MEDIA_BACKEND=local 或 model 以 local:/dgx: 开头）时，
    改由同节点的 DGX 模型（Z-Image / Qwen-Image-2.1 / FLUX.2 / Krea-2）
    经 h3api 生成；auto 模式下本地不可达则回退云端。
    """
    if _local_backend(model):
        from shipin_platform.generation import local_media
        try:
            return local_media.local_image(
                prompt, width, height, out,
                engine=_local_engine(model, "image"))
        except local_media.LocalMediaError:
            if os.environ.get("SHIPIN_MEDIA_BACKEND", "auto") == "local":
                raise
    key, base = _agnes_creds("image")
    if not key:
        raise ValueError("AGNES_KEY not configured")
    _assert_safe_url(f"{base}/images/generations")
    body = _json.dumps({
        "model": model,
        "prompt": prompt,
        "n": 1,
        "size": f"{width}x{height}",
        "response_format": "b64_json",
    }).encode()
    resp_body = _safe_post(
        f"{base}/images/generations",
        body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        timeout=120,
    )
    data = _json.loads(resp_body)
    import base64
    b64 = data["data"][0].get("b64_json", "")
    if not b64:
        # fallback: fetch from URL
        img_url = data["data"][0].get("url", "")
        if img_url:
            Path(out).write_bytes(_safe_fetch(img_url))
            return {"ok": True, "path": out, "model": model}
        raise RuntimeError("no image data returned from Agnes")
    p = Path(out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(base64.b64decode(b64))
    return {"ok": True, "path": out, "model": model}


def generate_image_flux(prompt: str, width: int, height: int, out: str,
                        key: str) -> dict:
    url = "https://edge.mediastack.ai/api/v1/flux/pro"
    body = _json.dumps({"prompt": prompt, "width": width,
                         "height": height, "num_images": 1}).encode()
    resp_body = _safe_post(url, body, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"
    }, timeout=60)
    data = _json.loads(resp_body)
    img_url = data["data"][0]["url"]
    Path(out).write_bytes(_safe_fetch(img_url))
    return {"ok": True, "path": out, "mode": "flux"}


def generate_image_openai(prompt: str, width: int, height: int, out: str,
                          key: str) -> dict:
    import openai
    client = openai.OpenAI(api_key=key)
    resp = client.images.generate(model="dall-e-3", prompt=prompt,
                                   size=f"{width}x{height}", n=1)
    img_url = resp.data[0].url
    Path(out).write_bytes(_safe_fetch(img_url))
    return {"ok": True, "path": out, "mode": "openai_dalle"}


def _image_to_data_url(path: str | Path) -> str:
    """Local image → data URL (self-contained; no upload step to drift on)."""
    import base64
    p = Path(path)
    if not p.exists():
        raise ValueError(f"keyframe image not found: {p}")
    mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(p.read_bytes()).decode()


def generate_video_agnes(prompt: str, model: str = "agnes-video-2.5-flash",
                          duration: int = 5, resolution: str = "720p",
                          work_dir: Path | None = None,
                          first_frame: str | None = None,
                          last_frame: str | None = None,
                          output_path: str | None = None,
                          negative_prompt: str = "",
                          seed: int | None = None,
                          poll_interval: int = 5,
                          poll_attempts: int = 60,
                          width: int = 0, height: int = 0,
                          ref_images: Optional[list[str]] = None) -> dict:
    """Generate video via Agnes AI, first/last-frame anchored when possible.

    Agnes /videos 接口两代协议：
    - v2.5-flash（新，默认）：mode ∈ {reference, image_to_video, ...}；
      images=[首帧, 尾帧] 数组（≥2 张即首尾帧锚定）；服务端固定 5s/720p，
      不接受 duration/resolution 字段——平台拿回后自行裁到请求时长。
      实测（2026-09-19）：双图 reference 出片 internal_cuts=0、一次过 QC，
      对比 v2.0 的三档重试全部被切镜门拦截——这是默认切到 2.5 的原因。
    - v2.0（旧）：mode ∈ {ti2vid, keyframes, multi_reference}；
      keyframes 要求 image 数组 ≥2 张图（首帧+尾帧）。
    首尾帧锚定是『单镜头内不再私开子镜头』的关键约束——分镜的每个镜头
    必须优先双图锚定；只有用户显式豁免（allow_unanchored）才允许纯
    文生视频，且结果里会带 unanchored 警告。

    本地模式（SHIPIN_MEDIA_BACKEND=local 或 model 以 local:/dgx: 开头）时，
    改由同节点的 DGX 模型（MiniMax-H3 / Wan 2.2）经 h3api 生成；auto 模式
    下本地不可达则回退云端。

    seed（轮62）：2.5 协议实测接受该字段（2026-09-25 探测：带 seed 提交
    与不带均 200，无 unknown-field 拒绝）。定向重生成换 seed 是与换
    prompt 正交的采样维度——同一 prompt/首帧下换 seed 拿不同实现，
    用于终审 finding（如 S03 侧面按键）在 prompt 已到边际后的采样面
    重试。v2.0 协议不接受则忽略（不留痕）。
    """
    if _local_backend(model):
        from shipin_platform.generation import local_media
        try:
            return local_media.local_video(
                prompt=prompt,
                out=output_path or str((work_dir or Path(".")) / "local_video.mp4"),
                first_frame=first_frame or "",
                last_frame=last_frame or "",
                duration=int(duration),
                engine=_local_engine(model, "video"),
                width=width, height=height,
                # 一致性升级(2026-09-26):ref_images 非空 → H3 Ref2VA 参考模式
                # (角色金参考+本镜首帧),见 local_media.local_video 注释
                ref_images=ref_images)
        except local_media.LocalMediaError:
            if os.environ.get("SHIPIN_MEDIA_BACKEND", "auto") == "local":
                raise

    import time
    import requests as _req
    key, base = _agnes_creds("video")
    if not key:
        raise ValueError("AGNES_KEY not configured")
    _assert_safe_url(f"{base}/videos")
    is_v25 = model in ("agnes-video-2.5-flash", "agnes-video-2.5")
    body: dict = {"model": model, "prompt": prompt}
    if not is_v25:
        body["duration"] = duration
        body["resolution"] = resolution
    if negative_prompt and not is_v25:
        body["negative_prompt"] = negative_prompt
    if seed is not None and is_v25:
        # 轮62：2.5 协议实测接受；v2.0 不加（协议不认）
        body["seed"] = int(seed)
    warnings: list[str] = []
    anchored = False
    anchor_imgs = [p for p in (first_frame, last_frame) if p]
    if is_v25:
        # 2.5 协议：reference 模式 + images 数组。双图=首尾帧锚定（实证过
        # QC 无内切）；单图退化为图生视频参考；无图=纯文生（未锚定警告）。
        body["mode"] = "reference"
        if len(anchor_imgs) >= 2:
            body["images"] = [_image_to_data_url(anchor_imgs[0]),
                              _image_to_data_url(anchor_imgs[1])]
            anchored = True
        elif len(anchor_imgs) == 1:
            body["images"] = [_image_to_data_url(anchor_imgs[0])]
            anchored = False
            warnings.append("2.5-flash reference 模式只给了首帧、缺尾帧："
                            "单图参考出片，无首尾帧锚定（镜内可能出现子镜头切换）")
        else:
            warnings.append("未提供首帧图：纯文生视频（未锚定），镜内可能出现"
                            "模型自开的子镜头切换")
    else:
        if anchor_imgs:
            body["mode"] = "keyframes"
            body["image"] = [_image_to_data_url(a) for a in anchor_imgs]
            anchored = len(anchor_imgs) >= 2
        else:
            body["mode"] = "ti2vid"

    def _submit(payload: dict):
        return _req.post(f"{base}/videos", json=payload,
                         headers={"Authorization": f"Bearer {key}"},
                         timeout=120)

    resp = _submit(body)
    if resp.status_code == 400 and not is_v25 and body["mode"] == "keyframes":
        # keyframes 提交即被拒（参数/内容审核）：显式降级为 ti2vid 重投一次，
        # 绝不静默——结果里 anchored=false + warnings，QC 与终验都能看到。
        warnings.append(f"keyframes 提交被拒（{resp.text[:120]}），"
                        f"降级为未锚定 ti2vid 重试")
        body = {k: v for k, v in body.items() if k != "image"}
        body["mode"] = "ti2vid"
        anchored = False
        resp = _submit(body)
    resp.raise_for_status()
    data = resp.json()
    task_id = data.get("task_id", "")
    if not task_id:
        raise RuntimeError(f"no task_id in response: {data}")
    out_dir = Path(work_dir or Path(".").resolve())
    out_dir.mkdir(parents=True, exist_ok=True)
    for _ in range(poll_attempts):
        time.sleep(poll_interval)
        status_resp = _req.get(
            f"{base}/videos/{task_id}",
            headers={"Authorization": f"Bearer {key}"},
            timeout=60,
        )
        status_resp.raise_for_status()
        status = status_resp.json()
        st = status.get("status") or status.get("internal_status", "")
        if st in ("completed", "done", "success"):
            url = (status.get("url") or status.get("video_url")
                   or (status.get("metadata") or {}).get("url") or "")
            if not url:
                raise RuntimeError(f"no url in completed response: {status}")
            final_path = Path(output_path) if output_path else \
                out_dir / f"video_{task_id[:8]}.mp4"
            final_path.parent.mkdir(parents=True, exist_ok=True)
            final_path.write_bytes(_safe_fetch(url))
            # 保留未裁剪 master：保时长转场需要从镜头首尾各借 transition_duration
            # 的帧（xfade 重叠消耗），只有裁剪后的成品不够用。
            master_path = final_path.with_name(final_path.stem + "_master.mp4")
            master_path.write_bytes(final_path.read_bytes())
            # Agnes 只出 5s/10s 粒度（2.5 协议固定 5s，不接受 duration 请求字段）：
            # 平台负责把素材裁到请求时长——否则逐镜 QC 的时长门永远误报，
            # 且长出的尾巴里藏着模型私开的子镜头。
            try:
                dur_actual = float(status.get("seconds", duration) or duration)
            except (TypeError, ValueError):
                dur_actual = float(duration)
            trimmed_note = None
            if duration and dur_actual - float(duration) > 0.3:
                trimmed = final_path.with_suffix(".trim.mp4")
                subprocess.run(
                    ["ffmpeg", "-y", "-i", str(final_path),
                     "-t", str(float(duration)),
                     "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an",
                     str(trimmed)],
                    capture_output=True, text=True)
                if trimmed.exists() and trimmed.stat().st_size > 1000:
                    trimmed.replace(final_path)
                    trimmed_note = f"已从 {dur_actual:.2f}s 裁剪到请求时长 {float(duration):.1f}s"
                    dur_actual = float(duration)
                else:
                    warnings.append("时长裁剪失败，素材保留原始时长（QC 时长门会拦截）")
            result = {"ok": True, "path": str(final_path),
                      "master_path": str(master_path),
                      "model": model,
                      "task_id": task_id, "anchored": anchored,
                      "mode": body["mode"],
                      "duration_sec": dur_actual,
                      **({"trimmed": trimmed_note} if trimmed_note else {}),
                      **({"warnings": warnings} if warnings else {})}
            return result
        if st in ("failed", "error"):
            raise RuntimeError(f"video generation failed: {status.get('message', status)}")
    raise RuntimeError(f"video generation timeout after "
                       f"{poll_interval * poll_attempts}s for task {task_id}")


def _make_shot_clip(img_path: str, dur: float, fps: int, out: str) -> None:
    """Create a single-video clip from an image (no audio)."""
    subprocess.run([
        "ffmpeg", "-y", "-loop", "1", "-i", img_path,
        "-t", str(int(dur)), "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-framerate", str(fps), out
    ], capture_output=True, text=True, shell=False, timeout=60)


def generate_video(shots: list, output_path: str, fps: int = 24) -> dict:
    """Generate video from image shots, concat via OpenMontage VideoStitch."""
    import sys
    from shipin_platform import roots as _roots
    _shipin_root = _roots.data_root()
    sys.path.insert(0, str(_shipin_root / "src"))
    from shipin_platform.tools.ffmpeg_engine import FFmpegEngine

    out = Path(output_path).resolve()
    work_dir = out.parent
    work_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = work_dir / "_gen_tmp"
    tmp_dir.mkdir(exist_ok=True)
    clips: list = []
    engine = FFmpegEngine()

    try:
        for shot in shots:
            img_path = _media_arg(shot.get("image_path", ""))
            dur = float(shot.get("duration_sec", 3.0))
            clip_out = str(tmp_dir / f"{shot.get('shot_id', 'clip')}.mp4")
            _make_shot_clip(img_path, dur, fps, clip_out)
            clips.append(Path(clip_out))

        result = engine.stitch(clips, out, transition="cut")
        if not result["ok"]:
            raise RuntimeError(f"stitch failed: {result.get('error')}")
    finally:
        for c in clips:
            c.unlink(missing_ok=True)
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return {"ok": True, "path": str(out), "clips_count": len(clips)}
