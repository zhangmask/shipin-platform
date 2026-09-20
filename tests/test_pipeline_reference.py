"""B2-2 reference→brief 预填接线：/api/pipeline/text 带 reference_id 时
参考报告的 filled/suggested 维度注入 brief 空槽，已有值不被覆盖，并溯源。"""
import json
from pathlib import Path

import api as api_mod
from fastapi.testclient import TestClient

client = TestClient(api_mod.app)

REF_DIR = Path(api_mod.__file__).resolve().parents[1] / "data" / "reference_reports"
FID = "_pipe_prefill_test"


def _write_ref(fid: str, prefill: dict):
    REF_DIR.mkdir(parents=True, exist_ok=True)
    (REF_DIR / f"{fid}.json").write_text(
        json.dumps({"profile": "shipin.reference@1", "brief_prefill": prefill}),
        encoding="utf-8")


def _cleanup(fid: str):
    fp = REF_DIR / f"{fid}.json"
    fp.unlink(missing_ok=True)


def test_pipeline_text_injects_reference_prefill(monkeypatch):
    fid = "pipe_prefill_test"
    _write_ref(fid, {
        "content_type": {"value": "product", "state": "filled", "note": ""},
        "tone": {"value": "说服冷峻", "state": "suggested", "note": ""},
        "duration_sec": {"value": 15, "state": "filled", "note": ""},
        "product_info": {"value": "咖啡豆", "state": "pending", "note": ""},
    })
    seen = {}

    def fake_run_text(project_id, brief, store, category=None):
        seen["brief"] = brief
        return {"ok": True, "phase": "text", "brief": brief}

    monkeypatch.setattr(api_mod, "run_text_phase", fake_run_text)
    try:
        r = client.post("/api/pipeline/text", json={
            "project_id": "ptest_1",
            "brief": {"product_info": "手工指定", "content_type": "抖音"},
            "reference_id": "pipe_prefill_test",
        })
    finally:
        _cleanup(fid)
    assert r.status_code == 200, r.text
    b = seen["brief"]
    assert b["content_type"] == "抖音"             # 用户已有值不被预填覆盖
    assert b["tone"] == "说服冷峻"                 # suggested → 填空
    assert b["duration_sec"] == 15                # filled → 填空
    assert b["product_info"] == "手工指定"         # 用户已有值保留
    assert b["_reference_id"] == fid               # 溯源


def test_pipeline_text_missing_reference_404():
    r = client.post("/api/pipeline/text", json={
        "project_id": "ptest2", "brief": {},
        "reference_id": "no_such_ref_zzz",
    })
    assert r.status_code == 404


def test_pipeline_text_without_reference_no_op():
    seen = {}

    def fake_run_text(project_id, brief, store, category=None):
        seen["brief"] = brief
        return {"ok": True, "phase": "text", "brief": brief}

    import api as api_mod2
    original = api_mod2.run_text_phase
    api_mod2.run_text_phase = fake_run_text
    try:
        r = client.post("/api/pipeline/text", json={
            "project_id": "ptest3", "brief": {"tone": "燃"},
        })
    finally:
        api_mod2.run_text_phase = original
    assert r.status_code == 200, r.text
    assert "content_type" not in seen["brief"]    # 无 reference 时零注入
    assert seen["brief"]["tone"] == "燃"