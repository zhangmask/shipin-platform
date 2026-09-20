"""B2-3 成本记账单元测试：定价回退 / 落账 / 汇总 / API 快照。"""
from pathlib import Path

from shipin_platform.services import costing

ROOT = Path(__file__).resolve().parents[2]   # tests/ → .. = shipin 项目根? 修正见下方


def _cleanup(project_id: str):
    fp = costing.cost_file(project_id)
    if fp.exists():
        fp.unlink()
    # 项目目录可能已是空壳，保留不影响


def test_default_pricing_loaded():
    assert costing.unit_usd("image") == 0.001
    assert costing.unit_usd("video") == 0.02
    assert costing.unit_usd("tts") == 0.0
    assert costing.unit_usd("unknown_kind") == 0.0


def test_record_and_summary_roundtrip():
    pid = "cost_test_roundtrip"
    _cleanup(pid)
    costing.record_cost(pid, "image", model="agnes-image", units=2.0,
                        note="S01 首帧")
    costing.record_cost(pid, "video", model="agnes-video", units=5.0,
                        note="S01")
    s = costing.cost_summary(pid)
    assert s["total_usd"] == round(0.001 * 2 + 0.02 * 5, 6)
    assert s["by_kind"]["image"] == 0.002
    assert s["by_kind"]["video"] == 0.1
    assert len(s["records"]) == 2
    assert s["records"][0]["seq"] == 1
    assert s["records"][1]["seq"] == 2
    _cleanup(pid)


def test_empty_queue_returns_zero_skeleton():
    pid = "cost_test_empty"
    _cleanup(pid)
    s = costing.cost_summary(pid)
    assert s["total_usd"] == 0.0
    assert s["by_kind"] == {}
    assert s["records"] == []


def test_pricing_reads_providers_config():
    pr = costing.pricing()
    assert isinstance(pr, dict)
    assert pr["image"]["usd_per_unit"] == 0.001   # 与 config/providers.json 一致
    assert "video" in pr
    assert "tts" in pr