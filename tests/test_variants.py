"""B3: 变体/重跑(同源换参数重跑、独立产物目录)测试。

覆盖四层:
  - 派生语义: 白名单覆盖合并、源文件落盘、media_root 引用、manifest 深拷贝;
  - 覆盖防线: 非白名单键/坏类型/重名/相同 id/路径穿越 → VariantError;
  - 隔离: base 字节不变、两变体阶段状态互不污染;
  - 端到端成片(真实 ffmpeg): 从仓库基准 coffee-v7 派生 2 个变体, 各自
    generate + assemble 产出 final.mp4(无 AGNES key 时终验门禁如实
    返回 released=False, 成片产物与阶段状态各自独立)。
"""
import json
import shutil
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

import api  # noqa: E402

from shipin_platform.contracts import stable_artifact_hash  # noqa: E402
from shipin_platform.orchestration.pipeline_runner import PROJECTS_DIR  # noqa: E402
from shipin_platform.orchestration.stage_store import ProjectStageStore  # noqa: E402
from shipin_platform.variants.variant_runner import (  # noqa: E402
    VARIANT_OVERRIDE_KEYS, VariantError, derive_variant, list_variants,
    run_variant_phases, variant_status,
)

client = TestClient(api.app)

COFFEE = PROJECTS_DIR / "coffee-v7"
FFMPEG = shutil.which("ffmpeg")


@pytest.fixture()
def projects_dir(tmp_path) -> Path:
    return tmp_path / "projects"


@pytest.fixture()
def synthetic_base(projects_dir: Path) -> Path:
    """最小成片源基准(tmp):brief + storyboard + manifest(引用媒体路径)。"""
    b = projects_dir / "base1"
    b.mkdir(parents=True)
    (b / "brief.json").write_text(json.dumps({
        "product_info": "测试咖啡", "duration_sec": 30,
        "style_anchor": "soft light", "brand_name": "测试牌",
    }, ensure_ascii=False), encoding="utf-8")
    (b / "storyboard.json").write_text(json.dumps({
        "hero_shot": "S01",
        "shots": [{"shot_id": "S01", "duration_sec": 3, "narration": "好",
                   "beat": "hook", "scene": "门口", "subject": "杯",
                   "motion": "steam rises slowly", "spatial": "center",
                   "camera": "dolly in", "shot_size": "cu"}],
    }, ensure_ascii=False), encoding="utf-8")
    (b / "manifest.json").write_text(json.dumps({
        "shots": {"S01": {"first_frame": str(b / "S01.jpg"), "qc": "ok",
                          "clip": str(b / "S01_clip.mp4")}},
        "keyframe_plan": [], "align": {"verdict": "ok",
                                       "timeline": [{"shot_id": "S01",
                                                     "window_sec": 3}]},
    }, ensure_ascii=False), encoding="utf-8")
    return b


def _uid() -> str:
    return uuid.uuid4().hex[:8]


# ── 派生语义 ─────────────────────────────────────────────────────

class TestDerive:
    def test_derive_two_variants_independent(self, synthetic_base, projects_dir):
        a = derive_variant("base1", "base1-a", {"category": "drama",
                                                "duration_sec": 24},
                           projects_dir=projects_dir)
        b = derive_variant("base1", "base1-b", {"brand_name": "晨光"},
                           projects_dir=projects_dir)
        assert a["ok"] and b["ok"]
        assert a["media_root"] == b["media_root"] == str(synthetic_base)
        da, db = projects_dir / "base1-a", projects_dir / "base1-b"
        ma = json.loads((da / "variant.json").read_text(encoding="utf-8"))
        mb = json.loads((db / "variant.json").read_text(encoding="utf-8"))
        assert ma["overrides"]["category"] == "drama"
        assert mb["overrides"] == {"brand_name": "晨光"}
        assert ma["phases"] == ["text", "generate", "assemble"]
        # 两变体各自独立目录, brief 文件各自存在
        assert (da / "brief.json").is_file() and (db / "brief.json").is_file()

    def test_merge_brief_keeps_base_fields(self, synthetic_base, projects_dir):
        r = derive_variant("base1", "base1-c", {"duration_sec": 24,
                                                "style_anchor": "cool"},
                           projects_dir=projects_dir)
        brief = json.loads((projects_dir / "base1-c" / "brief.json")
                           .read_text(encoding="utf-8"))
        assert brief["duration_sec"] == 24
        assert brief["style_anchor"] == "cool"
        assert brief["product_info"] == "测试咖啡"   # 未覆盖的原字段保留
        assert "category" not in brief               # category 不进 brief 正文
        assert r["brief_merged"]["duration_sec"] == 24

    def test_manifest_deep_copy_align_cleared(self, synthetic_base, projects_dir):
        derive_variant("base1", "base1-m", {}, projects_dir=projects_dir)
        mf = json.loads((projects_dir / "base1-m" / "manifest.json")
                        .read_text(encoding="utf-8"))
        assert "align" not in mf                    # 留给 generate 重新对齐
        assert mf["shots"]["S01"]["first_frame"].endswith("S01.jpg")
        # storyboard 独立拷贝存在(变体自带创制源)
        assert (projects_dir / "base1-m" / "storyboard.json").is_file()

    def test_override_does_not_touch_base(self, synthetic_base, projects_dir):
        before = {p.name: p.read_bytes() for p in synthetic_base.glob("*.json")}
        derive_variant("base1", "base1-x", {"duration_sec": 12},
                       projects_dir=projects_dir)
        after = {p.name: p.read_bytes() for p in synthetic_base.glob("*.json")}
        assert before == after

    def test_list_variants_only_base_derived(self, synthetic_base, projects_dir):
        derive_variant("base1", "base1-a", {}, projects_dir=projects_dir)
        derive_variant("base1", "base1-b", {}, projects_dir=projects_dir)
        other = projects_dir / "unrelated"
        other.mkdir(exist_ok=True)
        (other / "variant.json").write_text(json.dumps(
            {"base_project_id": "someone-else"}), encoding="utf-8")
        ids = [v["variant_id"] for v in list_variants("base1",
                                                       projects_dir=projects_dir)]
        assert set(ids) == {"base1-a", "base1-b"}

    def test_variant_id_same_as_base_rejected(self, synthetic_base,
                                              projects_dir):
        with pytest.raises(VariantError):
            derive_variant("base1", "base1", {}, projects_dir=projects_dir)

    def test_duration_override_lands_in_brief(self, synthetic_base,
                                              projects_dir):
        derive_variant("base1", "base1-d", {"duration_sec": 45},
                       projects_dir=projects_dir)
        brief = json.loads((projects_dir / "base1-d" / "brief.json")
                           .read_text(encoding="utf-8"))
        assert brief["duration_sec"] == 45


# ── 覆盖保镖 ─────────────────────────────────────────────────────

class TestOverridesSafety:
    def test_unknown_key_rejected(self, synthetic_base, projects_dir):
        with pytest.raises(VariantError, match="不在白名单"):
            derive_variant("base1", "bad1", {"evil_param": 1},
                           projects_dir=projects_dir)

    def test_bad_duration_rejected(self, synthetic_base, projects_dir):
        with pytest.raises(VariantError, match="正整数"):
            derive_variant("base1", "bad2", {"duration_sec": -3},
                           projects_dir=projects_dir)

    def test_already_exists_requires_force(self, synthetic_base, projects_dir):
        derive_variant("base1", "dup", {}, projects_dir=projects_dir)
        with pytest.raises(VariantError, match="已存在"):
            derive_variant("base1", "dup", {}, projects_dir=projects_dir)
        r = derive_variant("base1", "dup", {}, projects_dir=projects_dir,
                           force=True)
        assert r["ok"] is True

    def test_base_missing(self, projects_dir):
        with pytest.raises(VariantError, match="不存在"):
            derive_variant("nope", "a", {}, projects_dir=projects_dir)

    def test_path_traversal_rejected(self, synthetic_base, projects_dir):
        for evil in ("../escape", "a/../b", "a\\b"):
            with pytest.raises(VariantError):
                derive_variant("base1", evil, {}, projects_dir=projects_dir)

    def test_whitelist_full_set(self):
        assert VARIANT_OVERRIDE_KEYS == frozenset({
            "category", "duration_sec", "style_anchor", "pacing",
            "brand_name", "slogan", "product_info"})


# ── 阶段状态隔离 ─────────────────────────────────────────────────

class TestStageIsolation:
    def test_variant_store_isolation(self, synthetic_base, projects_dir):
        store = ProjectStageStore(":memory:")
        store.create_project("base1")
        store.record_artifact("base1", "script", "abc")
        derive_variant("base1", "base1-i", {}, projects_dir=projects_dir)
        assert store.get_stage("base1-i", "script") is None
        store.create_project("base1-i")
        store.record_artifact("base1-i", "script", "xyz")
        assert store.get_stage("base1", "script")["artifact_hash"] == "abc"
        assert store.get_stage("base1-i", "script")["artifact_hash"] == "xyz"

    def test_variant_status_before_run(self, synthetic_base, projects_dir):
        derive_variant("base1", "base1-s", {}, projects_dir=projects_dir)
        store = ProjectStageStore(":memory:")
        st = variant_status("base1-s", store, projects_dir=projects_dir)
        assert st["ok"] is True
        assert st["base_project_id"] == "base1"
        assert st["final_exists"] is False
        assert "variant.json" in st["files"]


# ── API 契约(真实基准 coffee-v7, 用完自清理)───────────────────

@pytest.fixture()
def coffee_base():
    if not (COFFEE / "brief.json").is_file():
        pytest.skip("仓库基准 coffee-v7 不存在")
    return "coffee-v7"


class TestVariantApi:
    def test_derive_and_status_api(self, coffee_base):
        vid = f"api-{_uid()}"
        try:
            r = client.post("/api/variant/derive", json={
                "base_project_id": coffee_base, "variant_id": vid,
                "overrides": {"category": "drama", "style_anchor": "neon"}})
            assert r.status_code == 200
            body = r.json()
            assert body["ok"] is True
            assert body["brief_merged"]["style_anchor"] == "neon"
            rs = client.get(f"/api/variant/{vid}/status")
            assert rs.status_code == 200
            st = rs.json()
            assert st["base_project_id"] == coffee_base
            assert st["final_exists"] is False
            assert st["overrides"]["category"] == "drama"
        finally:
            shutil.rmtree(PROJECTS_DIR / vid, ignore_errors=True)

    def test_derive_bad_override_422(self, coffee_base):
        vid = f"api-bad-{_uid()}"
        try:
            r = client.post("/api/variant/derive", json={
                "base_project_id": coffee_base, "variant_id": vid,
                "overrides": {"hack": 1}})
            assert r.status_code == 422
            assert "白名单" in r.json()["detail"]
        finally:
            shutil.rmtree(PROJECTS_DIR / vid, ignore_errors=True)

    def test_status_unknown_422(self):
        r = client.get("/api/variant/does-not-exist-xyz/status")
        assert r.status_code == 422


# ── 端到端成片(真实 ffmpeg) ──────────────────────────────────────

@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg 不可用")
class TestVariantE2E:
    def _prime_text(self, vid: str, store: ProjectStageStore) -> None:
        """文本阶段前置状态(真实流程由 text 阶段落在同一 store)。"""
        store.create_project(vid)
        data = json.loads((PROJECTS_DIR / vid / "storyboard.json")
                          .read_text(encoding="utf-8"))
        h = stable_artifact_hash(data)
        store.record_confirmation(vid, "script")
        store.record_confirmation(vid, "storyboard")
        store.record_artifact(vid, "script", h)
        store.record_artifact(vid, "storyboard", h)

    def test_two_variants_each_final_video(self, coffee_base):
        vids = (f"e2e-{_uid()}", f"e2e-{_uid()}")
        try:
            store = ProjectStageStore(":memory:")
            for vid, ov in ((vids[0], {"category": "drama",
                                       "style_anchor": "teal tones"}),
                            (vids[1], {"category": "tutorial",
                                       "brand_name": "晨光咖啡"})):
                r = derive_variant(coffee_base, vid, ov)
                assert r["ok"] is True
                self._prime_text(vid, store)
                g = run_variant_phases(vid, store, phases="generate")
                assert g["generate"]["ok"] is True, g["generate"].get("reason")
                a = run_variant_phases(vid, store, phases="assemble")["assemble"]
                assert a["ok"] is True, a.get("reason")
                # 轮23:通道级结论汇总必须出现在响应里(终审 fix 时
                # 运营按它定位「哪一镜的哪个通道」出的问题)
                assert "review_channels" in a, sorted(a.keys())
                for ch in a["review_channels"]:
                    assert ch.get("channel") and "critical" in ch, ch
                assert (PROJECTS_DIR / vid / "final.mp4").is_file()
                st = variant_status(vid, store)
                assert st["final_exists"] is True
                assert st["stages"]["post_production"]["status"] in ("PASS",
                                                                     "RELEASED")
            # 两变体产物互不相同(各自独立重跑)
            fa = (PROJECTS_DIR / vids[0] / "final.mp4").read_bytes()
            fb = (PROJECTS_DIR / vids[1] / "final.mp4").read_bytes()
            assert fa != fb and len(fa) > 1000
        finally:
            for vid in vids:
                shutil.rmtree(PROJECTS_DIR / vid, ignore_errors=True)