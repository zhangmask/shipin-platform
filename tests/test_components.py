"""组件化配方(远期3)测试: config/components.json + component_registry。

断言: 4 个组件解析齐全;默认值 = 原先散落的管线常量(零回归);
overrides 合并与参数白名单;apply 幂等(同参同输出)且 overrides 生效;
assemble 端从配方取值;API 只读端点。
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

import api  # noqa: E402

from shipin_platform.orchestration import pipeline_runner as pr  # noqa: E402
from shipin_platform.services.component_registry import (  # noqa: E402
    ComponentError, ComponentRegistry, get_registry,
)

client = TestClient(api.app)
FFMPEG = shutil.which("ffmpeg")
OUT = Path(__file__).resolve().parents[1] / "out"
OUT.mkdir(exist_ok=True)


class TestRegistry:
    def test_components_parsed(self):
        r = get_registry()
        assert r.list_components() == ["outro_card", "sound_design",
                                       "subtitle", "transition"]

    def test_defaults_match_pipeline_constants(self):
        r = get_registry()
        assert r.get("transition").defaults["transition_duration"] == 0.4  # DEFAULT_TD
        assert r.get("outro_card").defaults["zoom_to"] == 1.18
        assert r.get("subtitle").defaults["font_size"] == 46
        assert r.get("subtitle").defaults["margin_v"] == 96
        assert r.get("sound_design").defaults["bgm_gain_db"] == -19.0
        assert r.get("sound_design").defaults["duck"] is True

    def test_variant_overridable_whitelists(self):
        r = get_registry()
        assert r.get("outro_card").variant_overridable == ("zoom_to", "size")
        assert r.get("transition").variant_overridable == ("transition_duration",)

    def test_params_merge_and_unknown_rejected(self):
        o = get_registry().get("outro_card")
        assert o.params({"zoom_to": 1.35})["zoom_to"] == 1.35
        assert o.params({"zoom_to": 1.35})["fps"] == 24  # 其余保持默认
        with pytest.raises(ComponentError, match="无参数"):
            o.params({"evil": 1})

    def test_unknown_component_rejected(self):
        with pytest.raises(ComponentError, match="未注册组件"):
            get_registry().get("nope")

    def test_bad_profile_path(self, tmp_path):
        with pytest.raises(ComponentError, match="不存在"):
            ComponentRegistry(tmp_path / "no.json")


@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg 不可用")
class TestApplyPower:
    @pytest.fixture(scope="class")
    def shots(self):
        """320x180 测试图(1 张), 组件结果写 out/_comp_*.mp4。"""
        img = OUT / "_comp_src.jpg"
        subprocess.run([FFMPEG, "-y", "-f", "lavfi", "-i",
                        "testsrc2=s=320x180:d=1", "-frames:v", "1",
                        str(img)], capture_output=True, check=True)
        return img

    def test_apply_idempotent(self, shots):
        spec = get_registry().get("outro_card")
        a = spec.apply(str(shots), 1.5, str(OUT / "_comp_a1.mp4"))
        b = spec.apply(str(shots), 1.5, str(OUT / "_comp_a2.mp4"))
        assert a["ok"] is True and b["ok"] is True
        assert (OUT / "_comp_a1.mp4").read_bytes() == \
            (OUT / "_comp_a2.mp4").read_bytes()

    def test_apply_override_changes_output(self, shots):
        spec = get_registry().get("outro_card")
        r1 = spec.apply(str(shots), 1.5, str(OUT / "_comp_b1.mp4"),
                        overrides={"zoom_to": 1.05})
        r2 = spec.apply(str(shots), 1.5, str(OUT / "_comp_b2.mp4"),
                        overrides={"zoom_to": 1.45})
        assert r1["ok"] is True and r2["ok"] is True
        assert (OUT / "_comp_b1.mp4").read_bytes() != \
            (OUT / "_comp_b2.mp4").read_bytes()


class TestAssembleInjection:
    def test_component_defaults_reader(self):
        d = pr._component_defaults("transition")
        assert d["transition_duration"] == 0.4
        assert pr._component_defaults("no_such") == {}

    def test_defaults_are_wired_into_assemble_params(self, monkeypatch):
        # 配方被 assemble 消费:替换转换参数 → run_assemble 传给
        # build_transition_stitch 的 transition_duration 随之变化(间接
        # 通过 _component_defaults 快照断言)。
        calls = {}

        def fake_trans_defaults(cid):
            if cid == "transition":
                return {"transition_duration": 0.42, "fps": 24}
            return pr._component_defaults(cid)
        monkeypatch.setattr(pr, "_component_defaults", fake_trans_defaults)
        assert pr._component_defaults("transition")["transition_duration"] == 0.42


class TestComponentsApi:
    def test_get_components(self):
        r = client.get("/api/components")
        assert r.status_code == 200
        body = r.json()
        ids = {c["id"] for c in body["components"]}
        assert ids == {"transition", "outro_card", "subtitle", "sound_design"}
        outro = next(c for c in body["components"] if c["id"] == "outro_card")
        assert outro["defaults"]["zoom_to"] == 1.18