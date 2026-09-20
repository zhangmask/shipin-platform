"""P-3 密封封印：构建/校验/篡改检出/清单自封条/新增文件/API 暴露。

核心保证（用户需求"AI 不能改平台级工具"的可验证形式）：
  · 任何受保护文件被改动 → verify().ok == False（TAMPERED）
  · 受保护文件被删除 → MISSING；被新增 → UNLISTED
  · 清单自身被改动 → SELF_TAMPERED（self 封条）
  · 真实仓库必须可封（build_manifest → verify 全过）
"""
import json
import sys
from pathlib import Path

import api as api_mod
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for p in (str(SRC), str(ROOT.parent / "OpenMontage-main" / "OpenMontage-main")):
    if p not in sys.path:
        sys.path.insert(0, p)

from shipin_platform import integrity  # noqa: E402

client = TestClient(api_mod.app)


def _seed(root: Path) -> None:
    (root / "src" / "svc").mkdir(parents=True)
    (root / "config").mkdir(parents=True)
    (root / "src" / "svc" / "core.py").write_text("def f(): return 1\n",
                                                  encoding="utf-8")
    (root / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (root / "config" / "providers.json").write_text('{"pricing": {}}\n',
                                                    encoding="utf-8")


def _built(tmp_path: Path) -> Path:
    _seed(tmp_path)
    mp = tmp_path / "config" / "integrity.json"
    integrity.build_manifest(root=tmp_path, manifest_path=mp)
    return mp


def test_build_and_verify_clean(tmp_path):
    mp = _built(tmp_path)
    rep = integrity.verify(root=tmp_path, manifest_path=mp)
    assert rep["ok"] is True
    assert rep["clean"] == 3            # core.py + main.py + providers.json
    assert rep["tampered"] == rep["missing"] == 0
    # 清单自身不混进 files（self 封条已覆盖），也不是 UNLISTED
    assert all(f["status"] == "OK" for f in rep["files"])


def test_tamper_detected(tmp_path):
    mp = _built(tmp_path)
    f = tmp_path / "src" / "svc" / "core.py"
    f.write_text("def load(): return 2\n", encoding="utf-8")
    rep = integrity.verify(root=tmp_path, manifest_path=mp)
    assert rep["ok"] is False
    statuses = {x["path"]: x["status"] for x in rep["files"]}
    assert statuses["src/svc/core.py"] == "TAMPERED"
    assert rep["clean"] == 2


def test_missing_detected(tmp_path):
    mp = _built(tmp_path)
    (tmp_path / "src" / "main.py").unlink()
    rep = integrity.verify(root=tmp_path, manifest_path=mp)
    assert rep["ok"] is False
    assert rep["missing"] == 1


def test_unlisted_detected(tmp_path):
    mp = _built(tmp_path)
    (tmp_path / "src" / "sneaky.json").write_text("{}", encoding="utf-8")
    rep = integrity.verify(root=tmp_path, manifest_path=mp)
    assert rep["ok"] is False
    assert "src/sneaky.json" in {x["path"] for x in rep["files"]}
    assert any(x["status"] == "UNLISTED" for x in rep["files"])


def test_manifest_self_tamper_detected(tmp_path):
    mp = _built(tmp_path)
    data = json.loads(mp.read_text(encoding="utf-8"))
    data["files"]["src/main.py"] = "0" * 64      # 直接改清单里的 hash
    mp.write_text(json.dumps(data), encoding="utf-8")
    rep = integrity.verify(root=tmp_path, manifest_path=mp)
    assert rep["ok"] is False
    assert rep["reason"] == "SELF_TAMPERED"


def test_missing_manifest(tmp_path):
    _seed(tmp_path)
    rep = integrity.verify(root=tmp_path,
                           manifest_path=tmp_path / "config" / "integrity.json")
    assert rep["ok"] is False
    assert rep["reason"] == "MANIFEST_MISSING"


def test_real_repo_is_sealable(tmp_path):
    """真实仓库可复现出一致性封条（临时清单，不动真库）。"""
    mp = tmp_path / "integrity.json"
    integrity.build_manifest(manifest_path=mp)      # default root = 本仓库
    rep = integrity.verify(manifest_path=mp)       # 传了 manifest_path → 用临时清单
    # 未重新写 MANIFEST —— verify 用传入的 manifest_path
    assert rep["ok"] is True
    assert rep["clean"] > 30                       # src/config/tools 规模化


def test_api_integrity_and_projects():
    r = client.get("/api/platform/integrity")
    assert r.status_code == 200
    body = r.json()
    for key in ("ok", "clean", "tampered", "missing", "files"):
        assert key in body
    # 端点上没有重建/解锁入口（只读自检）
    r2 = client.post("/api/platform/integrity", json={})
    assert r2.status_code == 405

    r3 = client.get("/api/projects")
    assert r3.status_code == 200
    assert "projects" in r3.json()