"""Pytest fixtures — make shipin_platform and the API module importable."""
import os
import sys
from pathlib import Path

# P0: 测试默认关闭平台鉴权（SHIPIN_AUTH_MODE=off），保证既有 300+ 用例
# 无头运行不受影响；鉴权语义（strict 模式）由 test_platform_auth 单独
# 用 monkeypatch 开启验证。
os.environ.setdefault("SHIPIN_AUTH_MODE", "off")

# P7: 测试默认关闭限流（SHIPIN_RATE_MODE=off），避免全套件撞同一 IP 桶
# 触发 429；限流语义由 test_security_p7 单独 monkeypatch 开启验证。
os.environ.setdefault("SHIPIN_RATE_MODE", "off")

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

for p in (str(SRC), str(ROOT.parent / "OpenMontage-main" / "OpenMontage-main")):
    if p not in sys.path:
        sys.path.insert(0, p)