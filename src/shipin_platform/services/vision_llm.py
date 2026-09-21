"""多模态视觉反推服务 —— 自动探测可用视觉 LLM，逐帧反推广告分镜。

产品决策（用户三问结论）：不搞学术方案、不用本地小模型 —— 用多模态大模型，
**自动探测**：优先 AGNES hub（AGNES_KEY + AGNES_BASE_URL，平台已配置），
没有视觉模型时回退 OpenAI（gpt-4o 系）；VISION_MODEL 环境变量可显式指定。
密钥只从环境变量按 provider_registry 惯例读取（key 池 → env），不落盘不打印。

安全：
- 所有出网请求走 provider_registry.safe_post / safe_fetch（协议+host 校验+
  白名单+不跟随重定向）。
- 帧以 base64 data URL 内联（不产生公网图片 URL），不入日志。
- 反推结果 JSON 由调用方 confined 到参考包目录。
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
from pathlib import Path
from typing import Optional

from shipin_platform.services import provider_registry as pr

DEFAULT_OPENAI_BASE = "https://api.openai.com/v1"
OPENAI_HOSTS = frozenset({"api.openai.com"})

# 视觉模型名字模式（探测过滤）
_VISION_HINTS = ("vl", "vision", "4o", "omni", "gpt-4o", "gpt-4.1")


class VisionError(ValueError):
    """VLM 反推失败（归类错误，可安全转 4xx）。"""


def _agnes_base() -> str:
    return os.environ.get("AGNES_BASE_URL", "https://apihub.agnes-ai.com/v1").rstrip("/")


def _host_allowlist(base: str) -> frozenset:
    from urllib.parse import urlparse
    h = (urlparse(base).hostname or "").lower()
    if h.endswith("openai.com"):
        return OPENAI_HOSTS
    return frozenset({h}) if h else frozenset()


def _fetch_json(url: str, headers: dict, allow_hosts: frozenset,
                timeout: int = 30) -> dict:
    try:
        b = pr.safe_fetch(url, timeout=timeout,
                          allow_hosts=allow_hosts or None,
                          headers={"User-Agent": "shipin-platform/1.0",
                                   **(headers or {})})
    except pr.ProviderSecurityError as e:
        raise VisionError(f"模型探测被安全网关拦截: {e}") from e
    except pr.ProviderUnavailableError:
        raise
    except OSError as e:
        raise VisionError(f"模型探测请求失败: {e}") from e
    try:
        return json.loads(b.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise VisionError(f"模型探测返回非法 JSON: {e}") from e


def _post_json(url: str, payload: dict, headers: dict,
               allow_hosts: frozenset, timeout: int = 300) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        b = pr.safe_post(url, body, headers={"User-Agent": "shipin-platform/1.0",
                                             **(headers or {})},
                         timeout=timeout, allow_hosts=allow_hosts or None)
    except pr.ProviderSecurityError as e:
        raise VisionError(f"VLM 调用被安全网关拦截: {e}") from e
    except pr.ProviderUnavailableError:
        raise
    except OSError as e:
        raise VisionError(f"VLM 请求失败: {e}") from e
    try:
        return json.loads(b.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise VisionError(f"VLM 返回非法 JSON: {e}") from e


def _read_key(env_name: str) -> str:
    """从 provider registry 拿密钥（key 池优先，env 回退）——与生成层一致。"""
    try:
        c = pr.creds("agnes") if env_name == "AGNES_KEY" else {"key": ""}
        if env_name == "AGNES_KEY":
            return c.get("key", "") or ""
    except Exception:
        pass
    return os.environ.get(env_name, "").strip()


def _probe_agnes_vision(key: str) -> str:
    """AGNES hub /v1/models → 过滤视觉模型名。"""
    base = _agnes_base()
    try:
        data = _fetch_json(base + "/models",
                           headers={"Authorization": f"Bearer {key}"},
                           allow_hosts=_host_allowlist(base), timeout=20)
    except Exception:
        return ""
    for m in (data.get("data") or []):
        mid = str(m.get("id") or "")
        low = mid.lower()
        if any(h in low for h in _VISION_HINTS):
            return mid
    return ""


def _probe_openai(key: str) -> str:
    try:
        data = _fetch_json(DEFAULT_OPENAI_BASE + "/models",
                           headers={"Authorization": f"Bearer {key}"},
                           allow_hosts=OPENAI_HOSTS, timeout=20)
        for m in (data.get("data") or []):
            low = str(m.get("id") or "").lower()
            if "gpt-4o" in low or "gpt-4.1" in low:
                return str(m.get("id"))
    except Exception:
        return ""
    return ""


def resolve_vision() -> dict:
    """探测可用视觉端点，返回 {base, model, key, source}。

    优先级：VISION_MODEL 显式指定 → AGNES 视觉模型（hub 探测）→
    OpenAI gpt-4o 系。全不可用时抛 VisionError（带修复指引，不让任务卡死）。
    """
    explicit = os.environ.get("VISION_MODEL", "").strip()

    agnes_key = os.environ.get("AGNES_KEY", "").strip()
    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()

    if explicit:
        base = _agnes_base()
        if agnes_key:
            if explicit.startswith("openai/"):
                return {"base_url": DEFAULT_OPENAI_BASE, "model": explicit.split("/", 1)[1],
                        "key": openai_key, "source": "explicit-openai"}
            return {"base_url": base, "model": explicit,
                    "key": agnes_key, "source": "explicit"}
        if openai_key:
            return {"base_url": DEFAULT_OPENAI_BASE, "model": explicit,
                    "key": openai_key, "source": "explicit-openai"}
        raise VisionError(
            "已设置 VISION_MODEL，但 AGNES_KEY / OPENAI_API_KEY 均未配置，"
            "请在 .env 配置密钥")

    if agnes_key:
        model = _probe_agnes_vision(agnes_key)
        if model:
            return {"base_url": _agnes_base(), "model": model,
                    "key": agnes_key, "source": "agnes"}

    if openai_key:
        model = _probe_openai(openai_key) or "gpt-4o-mini"
        return {"base_url": DEFAULT_OPENAI_BASE, "model": model,
                "key": openai_key, "source": "openai"}

    raise VisionError(
        "没有可用的多模态大模型。请在 .env 配置 AGNES_KEY 或 OPENAI_API_KEY"
        "（多模态模型需支持 image_url 输入）；或用 VISION_MODEL 显式指定。")


_SYSTEM_PROMPT = (
    "你是一位顶级广告分镜师和提示词工程师。我会给你一帧参考广告画面，"
    "请先用中文观察并描述（机位、景别、主体、运动、光线、色调、构图、氛围），"
    "然后用**英文**给出可直接用于文生图/图生视频的两段提示词："
    "image_prompt 是静态画面描述；video_prompt 描述镜头内运动"
    "（主体动作、运镜、时间感）。不要编造画面里不存在的内容。"
    "只输出 JSON 对象："
    '{"scene": "一句话中文场景概述", "subject": "中文主体",'
    '"camera": "中文机位/景别", "lighting": "中文光线",'
    '"tone_and_palette": "中文色调/氛围", "composition": "中文构图",'
    '"image_prompt": "English static frame prompt",'
    '"video_prompt": "English motion prompt"}'
)

_KEYS = ("scene", "subject", "camera", "lighting", "tone_and_palette",
         "composition", "image_prompt", "video_prompt")


def frame_to_data_url(frame_path: str, max_side: int = 1024) -> str:
    """读帧 → 缩到 max_side → base64 data URL（控制 token 体积）。"""
    try:
        from PIL import Image
    except ImportError as e:
        raise VisionError("需要 Pillow 读帧（pip install pillow）") from e
    img = Image.open(str(frame_path)).convert("RGB")
    w, h = img.size
    scale = min(1.0, max_side / max(w, h))
    if scale < 1.0:
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                         Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=86)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


def _extract_json(text: str) -> dict:
    """从模型回复尽力提取 JSON 对象（剥 ```json 围栏）。"""
    t = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", t, re.S)
    if m:
        t = m.group(1)
    m = re.search(r"\{.*\}", t, re.S)
    if m:
        t = m.group(0)
    try:
        obj = json.loads(t)
    except json.JSONDecodeError as e:
        raise VisionError(f"VLM 输出不是合法 JSON（已重试）: {e}") from e
    if not isinstance(obj, dict):
        raise VisionError("VLM 输出不是 JSON 对象")
    return obj


def analyze_frame(frame_path: str | Path, *,
                  context: str = "",
                  cfg: Optional[dict] = None,
                  retries: int = 2) -> dict:
    """单帧反推。cfg 可复用 resolve_vision() 结果（避免每帧都探测）。

    返回 {scene, subject, camera, ..., image_prompt, video_prompt, _raw?}。
    """
    vcfg = cfg or resolve_vision()
    data_url = frame_to_data_url(frame_path)
    prompt = "分析这帧广告参考画面。" + (
        f"\n参考上下文：{context}" if context else "") + "\n只输出 JSON。"
    last = None
    for attempt in range(retries + 1):
        try:
            payload = {
                "model": vcfg["model"],
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": [
                        {"type": "image_url",
                         "image_url": {"url": data_url}},
                        {"type": "text", "text": prompt},
                    ]},
                ],
                "temperature": 0.4,
                "max_tokens": 1200,
            }
            data = _post_json(vcfg["base_url"] + "/chat/completions", payload,
                              headers={"Authorization": f"Bearer {vcfg['key']}"},
                              allow_hosts=_host_allowlist(vcfg["base_url"]))
            try:
                content = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                raise VisionError("VLM 响应缺少 choices")
            obj = _extract_json(content)
            out = {k: str(obj.get(k) or "").strip() for k in _KEYS}
            if not out["image_prompt"] and not out["subject"]:
                raise VisionError("VLM 输出缺少关键字段（image_prompt/subject）")
            out["model"] = vcfg["model"]
            out["source"] = vcfg["source"]
            return out
        except VisionError as e:
            last_err = e
            if attempt >= retries:
                raise
        except (KeyError, ValueError) as e:
            last_err = e
            if attempt >= retries:
                raise VisionError(f"VLM 反推失败: {e}") from e
    raise VisionError(f"VLM 反推失败（重试 {retries} 次）：{last_err}")