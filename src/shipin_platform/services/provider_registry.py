"""Provider registry — 供应商抽象层（对齐 hypit runtime/credential-store 设计）。

四层职责：Model(生成请求语义) → Provider(服务映射) → Endpoint(具体端点)
→ Credential(凭据引用，只读环境变量)。capability 路由：

    resolve("image.default") -> EndpointSpec(capability, use, model, provider,
                                             creds=("agnes",), params={...})

安全：所有出网请求统一走 safe_fetch —— 仅 http/https、校验 host、拒绝
localhost/环回/私有/保留地址、禁止跟随重定向。凭据只按 env 引用在调用时解析，
不落盘、不打印（对齐 hypit credential-store-env 的设计）。
"""
from __future__ import annotations

import ipaddress
import json
import os
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from shipin_platform import roots
from typing import Optional
from urllib.parse import urlparse

ROOT = roots.data_root()
DEFAULT_PROFILE = ROOT / "config" / "providers.json"


class ProviderError(Exception):
    """供应商层稳定错误(上层按 ok:false + code 返回)。"""
    code = "PROVIDER_ERROR"


class ProviderUnavailableError(ProviderError):
    """能力未注册 / 凭据缺失 / 端点不可用。"""
    code = "PROVIDER_UNAVAILABLE"


class ProviderSecurityError(ProviderError):
    """URL 被安全校验拦截(协议/host/IP 不合法)。"""
    code = "PROVIDER_SECURITY"


@dataclass(frozen=True)
class EndpointSpec:
    """一次解析的结果:端点 + 模型 + 凭据引用 + 端点参数。"""
    capability: str
    use: str                                   # 供应商实现名: agnes-image / agnes-video / whisper-local
    model: str                                 # 模型名
    provider: str                              # 供应商域: agnes-ai / openai / local
    creds: tuple[str, ...] = ()                # 凭据引用名（credentials.<name>.env）
    params: dict = field(default_factory=dict)  # 端点级默认参数（resolution 等）

    def __post_init__(self) -> None:
        object.__setattr__(self, "params", dict(self.params or {}))


# ---------------------------------------------------------------------------
# 安全出网网关
# ---------------------------------------------------------------------------

def _ip_unsafe(ip: "ipaddress.IPv4Address | ipaddress.IPv6Address") -> bool:
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def _host_ok(host: str) -> bool:
    """host 安全校验:拒绝 localhost / 环回 / 私有 / 保留 / 多播地址。

    支持字面 IP 与域名;域名解析到任意非法地址即拒绝。
    """
    if not host:
        return False
    low = host.lower().rstrip(".")
    if low in ("localhost", "0.0.0.0", "::1"):
        return False
    try:
        ip = ipaddress.ip_address(low)
        return not _ip_unsafe(ip)
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(low, None, type=socket.SOCK_STREAM)
    except OSError:
        return False
    for info in infos:
        if _ip_unsafe(ipaddress.ip_address(info[4][0])):
            return False
    return True


def assert_safe_url(url: str) -> None:
    """URL 出网前置校验(仅 http/https + host 安全);不通过抛 ProviderSecurityError。"""
    u = urlparse(url)
    if u.scheme not in ("http", "https"):
        raise ProviderSecurityError(f"仅允许 http/https,收到 scheme={u.scheme!r}")
    if not u.hostname:
        raise ProviderSecurityError(f"URL 缺少 hostname: {url!r}")
    if not _host_ok(u.hostname):
        raise ProviderSecurityError(f"host {u.hostname!r} 被安全网关拒绝(本地/私网/保留地址)")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def safe_fetch(url: str, timeout: int = 30,
               allow_hosts: Optional[frozenset] = None) -> bytes:
    """统一出站抓取:协议+host 双重校验,不跟随重定向。

    allow_hosts: 可选白名单(host 必须命中),例如现有 _IMG_HOSTS。
    """
    u = urlparse(url)
    if u.scheme not in ("http", "https"):
        raise ProviderSecurityError(f"仅允许 http/https,收到 {u.scheme!r}")
    if not u.hostname:
        raise ProviderSecurityError(f"URL 缺少 hostname: {url!r}")
    if allow_hosts is not None and u.hostname not in allow_hosts:
        raise ProviderSecurityError(
            f"host {u.hostname!r} 不在白名单内(allow_hosts)")
    if not _host_ok(u.hostname):
        raise ProviderSecurityError(
            f"host {u.hostname!r} 被安全网拒绝(non-routable IP)")
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": "shipin-platform/1.0"})
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError:
        raise
    except OSError as e:
        raise ProviderSecurityError(f"请求失败: {e}") from e


def safe_post(url: str, body: bytes, headers: Optional[dict] = None,
              timeout: int = 30,
              allow_hosts: Optional[frozenset] = None) -> bytes:
    """统一出站 POST:与 safe_fetch 相同的协议+host+白名单校验,不跟随重定向。

    body 由调用方序列化(JSON/表单均可);返回响应体 bytes。
    """
    u = urlparse(url)
    if u.scheme not in ("http", "https"):
        raise ProviderSecurityError(f"仅允许 http/https,收到 {u.scheme!r}")
    if not u.hostname:
        raise ProviderSecurityError(f"URL 缺少 hostname: {url!r}")
    if allow_hosts is not None and u.hostname not in allow_hosts:
        raise ProviderSecurityError(
            f"host {u.hostname!r} 不在白名单内(allow_hosts)")
    if not _host_ok(u.hostname):
        raise ProviderSecurityError(
            f"host {u.hostname!r} 被安全网拒绝(non-routable IP)")
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(
        url, data=body,
        headers={"User-Agent": "shipin-platform/1.0",
                 **(headers or {})},
        method="POST")
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError:
        raise
    except OSError as e:
        raise ProviderSecurityError(f"请求失败: {e}") from e


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class ProviderRegistry:
    """加载 config/providers.json;按 capability 路由 EndpointSpec。"""

    def __init__(self, profile_path: Optional[Path] = None):
        p = profile_path or DEFAULT_PROFILE
        if not Path(p).exists():
            raise ProviderUnavailableError(f"provider profile 不存在: {p}")
        self._profile = json.loads(Path(p).read_text(encoding="utf-8"))

    # -- 查询 ---------------------------------------------------------------
    @property
    def profile(self) -> dict:
        return self._profile

    def capabilities(self) -> list[str]:
        return list((self._profile.get("endpoints") or {}).keys())

    def credential_names(self) -> list[str]:
        return list((self._profile.get("credentials") or {}).keys())

    def endpoint_names(self) -> list[str]:
        return self.capabilities()

    def resolve(self, capability: str) -> EndpointSpec:
        """按能力名解析端点;bindings 表优先于默认端点名。"""
        binds = self._profile.get("bindings") or {}
        cap = binds.get(capability, capability)
        ep = (self._profile.get("endpoints") or {}).get(cap)
        if not ep:
            raise ProviderUnavailableError(
                f"未注册能力 {capability!r}(可用: {', '.join(self.capabilities())})")
        # 凭据引用:显式 credentials_ref 优先；否则沿用 provider 域同名凭据
        # （local 供应商是纯本地实现，不需要凭据）
        creds_raw = ep.get("credentials_ref")
        if not creds_raw:
            prov = ep.get("provider", "")
            creds_raw = (prov,) if prov and prov != "local" else ()
        return EndpointSpec(
            capability=capability,
            use=ep.get("use") or cap,
            model=ep.get("model", ""),
            provider=ep.get("provider", ""),
            creds=tuple(c for c in creds_raw if c),
            params={k: v for k, v in ep.items()
                    if k not in ("use", "model", "provider",
                                 "credentials", "credentials_ref", "credential")},
        )

    def resolvable_bindings(self) -> list[str]:
        return list((self._profile.get("bindings") or {}).keys())

    def creds(self, name: str, capability: str = "") -> dict:
        """调用时凭据解析:绝不缓存,绝不打印。

        传入 capability（如 "video"/"image"/"tts"）时优先走 key 池：
        池中命中返回明文 key（source=keypool:{id}:{capability}），
        池空或 key 池不可用(SHIPIN_KEY_SEAL 缺失/DB 损坏)时回退 env。
        顺序:key 池 → env，两者都空才报错。
        """
        c = (self._profile.get("credentials") or {}).get(name)
        if not c:
            raise ProviderUnavailableError(f"未登记凭据 {name!r}")
        env_key = c.get("env") or c.get("ref") or ""
        if not env_key:
            raise ProviderUnavailableError(
                f"凭据 {name!r} 缺少 env 引用")

        if capability:
            try:
                from shipin_platform.services.key_pool import get_pool
                rec = get_pool().pick(name, capability)
                if rec is not None:
                    return {"name": name, "env": env_key, "key": rec["key"],
                            "source": f"keypool:{rec['id']}:{capability}"}
            except Exception:
                # key 池故障不阻断:回退 env(seal 未配/加密库缺失/DB 损坏)
                pass

        value = os.environ.get(env_key, "").strip()
        if not value:
            raise ProviderUnavailableError(
                f"凭据 {name!r} 未配置:环境变量 {env_key} 为空")
        return {"name": name, "env": env_key, "key": value}


# -- 进程级默认实例(与 generate_assets 的接入点) ---------------------------
_default: Optional[ProviderRegistry] = None


def get_registry() -> ProviderRegistry:
    global _default
    if _default is None:
        _default = ProviderRegistry()
    return _default


def resolve(capability: str) -> EndpointSpec:
    return get_registry().resolve(capability)


def creds(name: str, capability: str = "") -> dict:
    return get_registry().creds(name, capability)