# -*- coding: utf-8 -*-
"""诊断：pytest 环境下 /api/guard/start 的真实响应。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import api  # noqa: E402
import shipin_platform.guard.http_api as http_api  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(api.app)


def test_probe_response_shape(tmp_path, monkeypatch):
    monkeypatch.setattr(http_api, "_LEDGER_ROOT", tmp_path / "gl")
    monkeypatch.setattr(http_api, "_RUNS", {})
    r = client.post("/api/guard/start", json={"project_id": "probe-pytest"})
    print("STATUS:", r.status_code)
    d = r.json()
    print("KEYS:", list(d.keys()))
    print("STATUS FIELD:", repr(d.get("status"))[:200])
    assert False  # 强制输出
