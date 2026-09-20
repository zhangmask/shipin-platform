"""C5 阶段协同与人工介入：中间产物可见 / 人工改写重跑 / 预算硬闸 / 执行轨迹。

对标来源见 docs/2026-09-17-C5-阶段协同与人工介入-调研与设计.md
（n8n/Dify 执行轨迹、Airflow 任务产物与清态重跑、ComfyUI 改 prompt 重排、
windmill 审批前置、MoneyPrinterTurbo preflight）。
"""

import json
import uuid

import api as api_mod
from fastapi.testclient import TestClient

from shipin_platform.orchestration.pipeline_runner import _load, _save
from shipin_platform.services.costing import record_cost

client = TestClient(api_mod.app)


def _pid(tag: str) -> str:
    return f"c5{tag}-{uuid.uuid4().hex[:8]}"


def _make_project(pid: str) -> None:
    r = client.post("/api/project/create", json={"project_id": pid})
    assert r.status_code == 200, r.text


def _put_generated_stages(store, pid: str) -> None:
    """造出下游已有行的项目：script/storyboard PASS + 确认 + 下游各行。"""
    _save(pid, "script.json", {"duration_sec": 12, "shots": [
        {"shot_id": "S01", "scene": "桌面", "narration": "开"},
        {"shot_id": "S02", "scene": "窗外", "narration": "尾"}]})
    _save(pid, "storyboard.json", {"hero_shot": "S01", "shots": [
        {"shot_id": "S01", "duration_sec": 3, "subject": "主角"},
        {"shot_id": "S02", "duration_sec": 3, "subject": "主角"}]})
    for stage in ("script", "storyboard"):
        store.record_artifact(pid, stage, "h0")
    store.record_confirmation(pid, "script")
    store.record_confirmation(pid, "storyboard")
    # 下游已有行（模拟跑到 generate 一半）
    store.begin_stage(pid, "image_prompt")
    store.record_artifact(pid, "image_prompt", "h0")
    store.begin_stage(pid, "video_gen")
    store.record_artifact(pid, "video_gen", "h0")


# ── 执行轨迹（事件留痕）────────────────────────────────────────────

def test_events_auto_trace_on_state_changes():
    pid = _pid("ev")
    _make_project(pid)
    store = api_mod._stage_store()
    store.begin_stage(pid, "script")
    store.record_artifact(pid, "script", "h1")
    store.record_confirmation(pid, "script")
    kinds = [e["kind"] for e in store.list_events(pid)]
    assert "project_created" in kinds
    assert "stage_started" in kinds
    assert "stage_recorded" in kinds
    assert "gate_confirmed" in kinds
    # 新→旧排列：最后发生的最前
    assert kinds[0] == "gate_confirmed"
    # 闸门作废/重置也有留痕
    store.clear_confirmation(pid, "script")
    assert store.list_events(pid)[0]["kind"] == "gate_cleared"
    store.reset_stage(pid, "script")
    assert store.list_events(pid)[0]["kind"] == "stage_reset"


def test_events_endpoint_and_limit():
    pid = _pid("evapi")
    _make_project(pid)
    store = api_mod._stage_store()
    for i in range(5):
        store.record_event(pid, "stage_started", f"fake {i}", stage="script")
    r = client.get(f"/api/pipeline/{pid}/events", params={"limit": 50})
    assert r.status_code == 200
    evs = r.json()["events"]
    seqs = [e["seq"] for e in evs]
    assert seqs == sorted(seqs, reverse=True)          # 新→旧
    r = client.get(f"/api/pipeline/{pid}/events", params={"limit": 3})
    assert len(r.json()["events"]) == 3
    assert r.json()["events"][0]["summary"] == "fake 4"
    # 未知项目 404
    assert client.get("/api/pipeline/nope/events").status_code == 404


# ── 中间产物可见 ───────────────────────────────────────────────────

def test_artifact_missing_then_present():
    pid = _pid("art")
    _make_project(pid)
    r = client.get(f"/api/pipeline/{pid}/artifact", params={"stage": "script"})
    assert r.status_code == 200
    assert r.json()["exists"] is False

    _save(pid, "script.json", {"duration_sec": 12,
                               "shots": [{"shot_id": "S01", "scene": "桌面"}]})
    r = client.get(f"/api/pipeline/{pid}/artifact", params={"stage": "script"})
    assert r.json()["exists"] is True
    assert r.json()["content"]["shots"][0]["scene"] == "桌面"

    # 未知产物名 422 / 未知项目 404
    assert client.get(f"/api/pipeline/{pid}/artifact",
                      params={"stage": "nope"}).status_code == 422
    assert client.get("/api/pipeline/nope/artifact",
                      params={"stage": "script"}).status_code == 404


# ── 人工改写 → 清确认 → 失效下游 → 重跑前置 ─────────────────────────

def test_rewrite_rejects_bad_input():
    pid = _pid("rw")
    _make_project(pid)
    _put_generated_stages(api_mod._stage_store(), pid)
    # 非法 stage（提示词由分镜确定性派生，不能手改）
    r = client.post(f"/api/pipeline/{pid}/rewrite",
                    json={"stage": "image_prompt", "content": {}})
    assert r.status_code == 422
    assert "分镜确定性派生" in r.json()["detail"]
    # content 非 dict
    r = client.post(f"/api/pipeline/{pid}/rewrite",
                    json={"stage": "script", "content": "hello"})
    assert r.status_code == 422
    # shots 空
    r = client.post(f"/api/pipeline/{pid}/rewrite",
                    json={"stage": "script", "content": {"shots": []}})
    assert r.status_code == 422


def test_rewrite_clears_confirmation_invalidates_downstream():
    pid = _pid("rwok")
    _make_project(pid)
    store = api_mod._stage_store()
    _put_generated_stages(store, pid)

    new_scene = "改后的场景"
    r = client.post(f"/api/pipeline/{pid}/rewrite",
                    json={"stage": "storyboard",
                          "content": {"shots": [{"shot_id": "S01",
                                                 "scene": new_scene}]}})
    assert r.status_code == 200, r.text
    assert r.json()["stage"] == "storyboard"
    assert r.json()["invalidated"] >= 2      # image_prompt/video_gen 已有行被失效

    # 文件已写回新内容
    got = _load(pid, "storyboard.json")
    assert got["shots"][0]["scene"] == new_scene

    # 确认已清除；本阶段 PENDING；下游 BLOCKED；script 不受影响
    assert store.get_confirmation(pid, "storyboard") is None
    assert store.get_stage(pid, "storyboard")["status"] == "PENDING"
    assert store.get_stage(pid, "image_prompt")["status"] == "BLOCKED"
    assert store.get_stage(pid, "video_gen")["status"] == "BLOCKED"
    assert store.get_confirmation(pid, "script") is not None
    assert store.get_stage(pid, "script")["status"] == "PASS"
    # 轨迹可见
    kinds = [e["kind"] for e in store.list_events(pid)]
    assert kinds[0] == "user_rewritten"
    assert "gate_cleared" in kinds


# ── 预算硬闸────────────────────────────────────────────────────────

def test_budget_over_limit_blocks_generate_and_assemble():
    pid = _pid("bg")
    _make_project(pid)
    assert client.post(f"/api/pipeline/{pid}/budget",
                       json={"max_budget_usd": 0.005}).status_code == 200
    record_cost(pid, "image", model="t", units=10.0)   # 0.001*10 = $0.01 > 0.005
    assert api_mod.cost_summary(pid)["total_usd"] > 0.005
    r = client.post("/api/pipeline/generate", json={"project_id": pid})
    assert r.status_code == 422
    assert "预算超限" in r.json()["detail"]
    # 事件留痕
    kinds = [e["kind"] for e in api_mod._stage_store().list_events(pid)]
    assert "budget_exceeded" in kinds
    # assemble 同样被拦
    assert client.post("/api/pipeline/assemble",
                       json={"project_id": pid}).status_code == 422


def test_budget_removal_unblocks():
    pid = _pid("bclr")
    _make_project(pid)
    record_cost(pid, "image", model="t", units=10)
    client.post(f"/api/pipeline/{pid}/budget", json={"max_budget_usd": 0.0001})
    assert client.post("/api/pipeline/generate",
                       json={"project_id": pid}).status_code == 422
    client.post(f"/api/pipeline/{pid}/budget", json={"max_budget_usd": None})
    r = client.post("/api/pipeline/generate", json={"project_id": pid})
    # 不再因预算被 422（无 key 环境走 phase 正常返回；其他门禁错误不算预算）
    if r.status_code == 422:
        assert "预算超限" not in r.json()["detail"]


def test_budget_via_pipeline_text_and_report():
    pid = _pid("bgt")
    r = client.post("/api/pipeline/text",
                    json={"project_id": pid, "brief": {"product_info": "x"},
                          "max_budget_usd": 2.5})
    assert r.status_code == 200, r.text
    fp = api_mod._project_dir(pid) / "budget.json"
    assert fp.is_file()
    assert json.loads(fp.read_text(encoding="utf-8"))["max_budget_usd"] == 2.5
    rep = client.get(f"/api/pipeline/{pid}/report").json()
    assert rep["budget"]["max_budget_usd"] == 2.5
    # 负预算被拒
    r = client.post(f"/api/pipeline/{pid}/budget", json={"max_budget_usd": -1})
    assert r.status_code == 422


# ── 体检 / 预览 ────────────────────────────────────────────────────

def test_preflight_structured_checks():
    pid = _pid("pf")
    _make_project(pid)
    r = client.post(f"/api/pipeline/{pid}/preflight")
    assert r.status_code == 200
    body = r.json()
    names = [c["name"] for c in body["checks"]]
    for want in ("stage:script", "gate:storyboard", "budget", "project_dir"):
        assert want in names
    assert any(n.startswith("credential:") for n in names)
    for c in body["checks"]:
        assert isinstance(c["ok"], bool)
        assert isinstance(c["detail"], str)


def test_preview_frames_served_with_path_guard():
    pid = _pid("pv")
    _make_project(pid)
    # 无帧 → 空列表（不依赖真 ffmpeg/成片）
    assert client.get(f"/api/pipeline/{pid}/preview").json()["frames"] == []
    # 造帧文件（模拟 assemble 后抽帧产物）
    d = api_mod._project_dir(pid) / "preview_frames"
    d.mkdir(parents=True, exist_ok=True)
    (d / "frame_1.jpg").write_bytes(b"\xff\xd8\xff\xe0FAKEJPEG")
    (d / "frame_2.jpg").write_bytes(b"\xff\xd8\xff\xe0FAKEJPEG")
    frames = client.get(f"/api/pipeline/{pid}/preview").json()["frames"]
    assert len(frames) == 2
    assert all(f.startswith(f"/api/pipeline/{pid}/preview/") for f in frames)
    r = client.get(frames[0])
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    # 白名单：非 jpg 与路径穿越一律 404
    assert client.get(
        f"/api/pipeline/{pid}/preview/..%2F..%2Fbudget.json").status_code in (404, 422)
    assert client.get(f"/api/pipeline/{pid}/preview/evil.txt").status_code == 404
    assert client.get(f"/api/pipeline/{pid}/preview/frame_9.jpg").status_code == 404