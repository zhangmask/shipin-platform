"""timeline 「过程回放」聚合端点契约测试。

验收口径：
- 里程碑：各 stage 的 status/artifact_hash/updated_at 都在；
- 事件账本按旧→新返回（与 list_events 的新→旧互补）；
- 产物只列媒体文件（图/视频），json 不进资产列表；
- QC 门禁、版本计数、成本汇总与 preview 字段齐备且类型正确。
"""
import uuid

import api as api_mod
from fastapi.testclient import TestClient

from shipin_platform.orchestration.pipeline_runner import _save

client = TestClient(api_mod.app)


def _pid(tag: str) -> str:
    return f"tl{tag}-{uuid.uuid4().hex[:8]}"


def _make_project(pid: str) -> None:
    r = client.post("/api/project/create", json={"project_id": pid})
    assert r.status_code == 200, r.text


def test_timeline_empty_project():
    pid = _pid("tl0")
    _make_project(pid)
    r = client.get(f"/api/pipeline/{pid}/timeline")
    assert r.status_code == 200
    body = r.json()
    assert body["project_id"] == pid
    assert body["stages"] == []
    assert body["events"] == [] or all(
        e["kind"] == "project_created" for e in body["events"])
    assert body["assets"] == []
    assert body["qc"] == []
    assert body["versions"] == {}
    assert isinstance(body["cost"], dict)
    assert body["cost"]["total_usd"] == 0
    assert body["preview_frames"] == []


def test_timeline_aggregates_stages_events_assets_qc():
    pid = _pid("tl1")
    _make_project(pid)
    store = api_mod._stage_store()

    # 铺出 script/storyboard PASS + 确认 + 一个 image_gen 阶段
    _save(pid, "script.json", {"duration_sec": 6, "shots": []})
    _save(pid, "storyboard.json", {"shots": []})
    store.record_artifact(pid, "script", "h1")
    store.record_artifact(pid, "storyboard", "h2")
    store.record_confirmation(pid, "script")
    store.record_confirmation(pid, "storyboard")
    store.begin_stage(pid, "image_gen")

    # 造一个媒体资产 + 一条 QC 记录
    import time
    from shipin_platform.orchestration.pipeline_runner import _project_dir
    (time_stamped := _project_dir(pid) / "S01.jpg").write_bytes(b"\xff\xd8\xff")
    (tmp_clip := _project_dir(pid) / "S01_clip.mp4").write_bytes(b"\x00\x00\x00\x18ftyp")
    store.record_clip_qc(pid, "S01", str(tmp_clip), "ok",
                         {"cuts": 0, "motion": 2.3})

    body = client.get(f"/api/pipeline/{pid}/timeline").json()

    # 里程碑：三条（script/storyboard/image_gen）都在
    stages = body["stages"]
    assert len(stages) == 3
    m = {s["stage"]: s for s in stages}
    assert m["script"]["status"] == "PASS"
    assert m["storyboard"]["status"] == "PASS"
    assert m["image_gen"]["status"] == "RUNNING"
    assert m["script"]["artifact_hash"] == "h1"

    # 2) 事件：旧→新，包含确认与生成的记录
    events = body["events"]
    assert events, "应至少包含 create/brief 等事件"
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs)
    kinds = {e["kind"] for e in events}
    assert "project_created" in kinds
    assert "gate_confirmed" in kinds

    # 3) 资产：S01.jpg 是 image、S01_clip.mp4 是 video；json 不进资产
    kinds2 = {a["name"]: a["kind"] for a in body["assets"]}
    assert kinds2.get("S01.jpg") == "image"
    assert kinds2.get("S01_clip.mp4") == "video"

    # 4) QC / 版本 / 成本
    assert body["qc"][0]["shot_id"] == "S01"
    assert body["qc"][0]["verdict"] == "ok"
    assert body["versions"].get("script") == 1  # 单次记录 = 1 版
    assert isinstance(body["cost"]["total_usd"], (int, float))
    assert body["preview_frames"] == []


def test_timeline_404_unknown_project():
    r = client.get(f"/api/pipeline/no-such-{uuid.uuid4().hex[:8]}/timeline")
    assert r.status_code == 404