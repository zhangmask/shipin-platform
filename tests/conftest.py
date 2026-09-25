"""Pytest fixtures — make shipin_platform and the API module importable."""
import os
import sys
from pathlib import Path

import pytest

# P0: 测试默认关闭平台鉴权（SHIPIN_AUTH_MODE=off），保证既有 300+ 用例
# 无头运行不受影响；鉴权语义（strict 模式）由 test_platform_auth 单独
# 用 monkeypatch 开启验证。
os.environ.setdefault("SHIPIN_AUTH_MODE", "off")

# P7: 测试默认关闭限流（SHIPIN_RATE_MODE=off），避免全套件撞同一 IP 桶
# 触发 429；限流语义由 test_security_p7 单独 monkeypatch 开启验证。
os.environ.setdefault("SHIPIN_RATE_MODE", "off")

# 轮57(真实使用发现,子智能体实测 + 全套件交叉污染):测试默认钉死媒体
# 后端为 auto(云端)。.env 是本地 gitignored 配置,曾配成 local(DGX 节点
# 模式)——本机无本地 h3api 节点(127.0.0.1:9000 无监听),而 api 模块导入
# 时 load_dotenv 会把 .env 灌进进程环境,导致组合跑时后面导入的用例
# TTS/生图全切本地模式报「h3api 不可达」(单文件跑不导入 api,所以只有
# 组合跑挂——典型的顺序依赖污染)。setdefault 语义:显式要测 local 的
# 用例仍可自己 setenv。
os.environ.setdefault("SHIPIN_MEDIA_BACKEND", "auto")

# 轮62(全套件顺序依赖污染,实测根因):setdefault 类钉法救不了**晚于
# conftest 发生**的注入——test_api*/test_variants 在收集期 `import api`,
# api 模块导入时 load_dotenv 把 .env 的真 AGNES_KEY 灌进进程环境,之后
# 所有用例的 vlm_same_scene/vlm_morph_check 全部真打 AGNES:既在回归里
# 烧真钱,又让断言依赖外部服务状态——实测 VLM 可达时 testsrc2 假素材 vs
# 真实参考图必判 "different"(原文级差异)→ REF_VLM_MISMATCH critical →
# 定向重试永不触发 → test_critical_review_triggers_variant_regen 挂;
# VLM 恰好不可达时才过——单跑/文件跑/全套跑三套结果。修法:autouse
# 逐例 delenv(monkeypatch 语义,用例内自行 setenv 不受影响);真链路
# E2E 用 @pytest.mark.real_network 显式 opt-in 保留 key。
@pytest.fixture(autouse=True)
def _no_real_provider_credentials(monkeypatch, request):
    if request.node.get_closest_marker("real_network"):
        return
    monkeypatch.delenv("AGNES_KEY", raising=False)

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

for p in (str(SRC), str(ROOT.parent / "OpenMontage-main" / "OpenMontage-main")):
    if p not in sys.path:
        sys.path.insert(0, p)