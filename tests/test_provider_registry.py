"""Provider registry + safe outbound gateway tests (hypit runtime/credential
store adaptation; Mimosa security gate: only http/https, host validated,
loopback/private/reserved rejected, no redirects followed)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from shipin_platform.services import provider_registry as pr  # noqa: E402
from shipin_platform.services.provider_registry import (  # noqa: E402
    ProviderRegistry,
    ProviderSecurityError,
    ProviderUnavailableError,
    assert_safe_url,
    safe_fetch,
)


# ── profile / capability routing ──────────────────────────────────

class TestRegistryRouting:
    def test_loads_default_profile(self):
        reg = ProviderRegistry()
        caps = set(reg.capabilities())
        assert {"image.default", "image.flux", "image.openai", "image.pil",
                "video.default", "tts.default", "asr.default"} <= caps

    def test_resolve_image_default(self):
        ep = ProviderRegistry().resolve("image.default")
        assert ep.use == "agnes-image"
        assert ep.model == "agnes-image-2.5-flash"
        assert ep.provider == "agnes"
        assert ep.creds == ("agnes",)

    def test_resolve_local_needs_no_creds(self):
        ep = ProviderRegistry().resolve("image.pil")
        assert ep.use == "pil"
        assert ep.creds == ()

    def test_bindings_override(self):
        reg = ProviderRegistry()
        assert reg.resolve("image").use == "agnes-image"

    def test_unknown_capability_raises(self):
        with pytest.raises(ProviderUnavailableError):
            ProviderRegistry().resolve("image.nope")

    def test_missing_profile_raises(self, tmp_path):
        with pytest.raises(ProviderUnavailableError):
            ProviderRegistry(tmp_path / "nope.json")


class TestCredsCallTime:
    def test_creds_read_env_at_call_time(self, monkeypatch):
        reg = ProviderRegistry()
        monkeypatch.setenv("AGNES_KEY", "k-secret")
        assert reg.creds("agnes")["key"] == "k-secret"

    def test_creds_missing_env_raises(self, monkeypatch):
        monkeypatch.delenv("AGNES_KEY", raising=False)
        with pytest.raises(ProviderUnavailableError):
            ProviderRegistry().creds("agnes")

    def test_creds_unregistered_raises(self):
        with pytest.raises(ProviderUnavailableError):
            ProviderRegistry().creds("no-such-cred")


# ── security gateway ──────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "ftp://example.com/x",
    "data:text/plain,hi",
    "https://",            # no hostname
])
def test_assert_safe_url_bad_scheme_or_host(url):
    with pytest.raises(ProviderSecurityError):
        assert_safe_url(url)


@pytest.mark.parametrize("host", [
    "localhost", "127.0.0.1", "0.0.0.0", "::1",
    "10.0.0.5", "172.16.1.1", "192.168.1.1",
    "169.254.1.1",  # link-local
    "224.0.0.1",    # multicast
    "0.0.0.1",      # unspecified-ish
])
def test_assert_safe_rejects_loopback_private_reserved(host):
    with pytest.raises(ProviderSecurityError):
        assert_safe_url(f"https://{host}/x")


def test_assert_safe_accepts_public_host():
    assert_safe_url("https://apihub.agnes-ai.com/v1/images/generations")


def test_safe_fetch_rejects_loopback_before_connecting():
    with pytest.raises(ProviderSecurityError):
        safe_fetch("http://127.0.0.1:9/probe")


def test_safe_fetch_allowlist_rejects_unknown_host():
    with pytest.raises(ProviderSecurityError):
        safe_fetch("https://evil.example.com/x",
                   allow_hosts=frozenset({"apihub.agnes-ai.com"}))


def test_private_host_resolution_blocked_by_flag():
    # 域名解析到私网地址时在 connect 之前就被拒(SSRF 防护)
    assert pr._host_ok("localhost") is False
    assert pr._host_ok("127.0.0.1") is False
    assert pr._host_ok("10.1.2.3") is False
    assert pr._host_ok("apihub.agnes-ai.com") is True


# ── generate_assets 接入点保持安全语义 ──────────────────────────────

class TestGenerateAssetsSecurity:
    def test_generate_assets_safe_fetch_keeps_https_only(self):
        from shipin_platform.generation import generate_assets as ga
        with pytest.raises(ValueError, match="only https allowed"):
            ga._safe_fetch("http://x/images/1.png")

    def test_agnes_creds_reads_env_at_call_time(self, monkeypatch):
        from shipin_platform.generation import generate_assets as ga
        monkeypatch.delenv("AGNES_KEY", raising=False)
        key, _ = ga._agnes_creds()
        assert key == ""
        monkeypatch.setenv("AGNES_KEY", "k-abc")
        key, _ = ga._agnes_creds()
        assert key == "k-abc"