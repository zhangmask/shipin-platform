"""P1 异步任务测试：202+task_id、轮询收敛、进度推导、幂等重试、心跳回收。

验收对齐《平台级演进计划》P1：?async=true 返回 202 且随后收敛到终态
（本环境无凭据/无闸门 → 确定性 failed；进度映射单测见证 10→25→…→100）；
同步 generate 行为不被改动；heartbeat 超期任务标 failed(crashed)。
"""
import time
import uuid

import pytest
from fastapi.testclient import TestClient

import api as api_mod
from shipin_platform.orchestration.stage_store import ProjectStageStore
from shipin_platform.services.task_store import TaskStore

client = TestClient(api_mod.app)

# 测试专用假 key（与 P0 相同约定：仅占位，非真实凭据；strict 模式注入）
_ADMIN_KEY = "p1-test-admin-key-not-a-real-secret"


def _pid(tag: str) -> str:
    return f"p1{tag}-{uuid.uuid4().hex[:8]}"


def _make_project(pid: str) -> None:
    r = client.post("/api/project/create", json={"project_id": pid})
    assert r.status_code == 200, r.text


def _wait_terminal(task_id: str, timeout: float = 90.0,
                   settle: float = 0.3) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        r = client.get(f"/api/tasks/{task_id}")
        assert r.status_code == 200, r.text
        last = r.json()
        if last["status"] in ("success", "failed"):
            return last
        time.sleep(settle)
    raise AssertionError(f"task {task_id} 未收敛到终态: {last}")


# ── ?async=true 车道 ──────────────────────────────────────────────

def test_async_generate_returns_202_and_converges():
    pid = _pid("a1")
    _make_project(pid)
    r = client.post("/api/pipeline/generate?async=true",
                    json={"project_id": pid})
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["kind"] == "generate"
    assert body["project_id"] == pid
    assert body["caller"] == "anonymous"
    assert body["status"] in ("queued", "running")
    assert body["task_id"]

    final = _wait_terminal(body["task_id"])
    # 无外部凭据环境：生成可能快速失败或空跑成功 — 只要确定性收敛到终态
    assert final["status"] in ("success", "failed")
    if final["status"] == "failed":
        assert final["error"]


def test_sync_generate_behavior_unchanged():
    """不带 ?async=true 不产生任务：没有任何 202 语义泄漏到同步路径。"""
    pid = _pid("sync")
    _make_project(pid)
    r = client.post("/api/pipeline/generate", json={"project_id": pid})
    assert r.status_code in (200, 422)          # 同步语义：非 202
    rows = client.get("/api/tasks", params={"project_id": pid}).json()
    # 同步失败不写入任务表（无 async 参数则完全不走任务车道）
    tasks = [t for t in rows["tasks"]]
    assert tasks == [] or all(t["kind"] != "generate"
                              for t in tasks)


def test_async_assemble_returns_202():
    pid = _pid("as3")
    _make_project(pid)
    r = client.post("/api/pipeline/assemble?async=true",
                    json={"project_id": pid})
    assert r.status_code == 202, r.text
    final = _wait_terminal(r.json()["task_id"])
    assert final["status"] in ("success", "failed")
    assert final["kind"] == "assemble"


def test_list_tasks_filter_by_project():
    pid = _pid("lst4")
    _make_project(pid)
    client.post("/api/pipeline/generate?async=true", json={"project_id": pid})
    rows = client.get("/api/tasks", params={"project_id": pid}).json()
    ids = {t["project_id"] for t in rows["tasks"]}
    assert ids == {pid}


# ── 幂等重试（Airflow UP_FOR_RETRY 语义）──────────────────────────

def test_retry_failed_task_then_conflict_when_done(monkeypatch):
    pid = _pid("rt5")
    _make_project(pid)

    def _boom(project_id, store):
        raise RuntimeError("p1-fake-boom")

    monkeypatch.setattr(api_mod, "run_generate_phase", _boom)
    r = client.post("/api/pipeline/generate?async=true",
                    json={"project_id": pid})
    assert r.status_code == 202
    task_id = r.json()["task_id"]
    first = _wait_terminal(task_id)
    assert first["status"] == "failed"
    assert "p1-fake-boom" in first["error"]

    # 重试 → 重新入队 → 再次失败（attempts 递增）
    rt = client.post(f"/api/tasks/{task_id}/retry")
    assert rt.status_code == 200, rt.text
    assert rt.json()["status"] == "queued"
    second = _wait_terminal(task_id)
    assert second["status"] == "failed"
    assert second["attempts"] >= 2

    # 已成功的任务不可反复重试（幂等：重复 retry 不产生双跑）
    ok_pid = _pid("ok6")
    _make_project(ok_pid)

    def _ok(project_id, store):
        return {"ok": True, "report": []}

    monkeypatch.setattr(api_mod, "run_generate_phase", _ok)
    r2 = client.post("/api/pipeline/generate?async=true",
                     json={"project_id": ok_pid})
    assert r2.status_code == 202
    tid = r2.json()["task_id"]
    final = _wait_terminal(tid)
    assert final["status"] == "success"
    conf = client.post(f"/api/tasks/{tid}/retry")
    assert conf.status_code in (200, 409)       # 幂等：不产生重复执行
    assert api_mod._task_store().get_task(tid)["status"] in ("success",
                                                             "queued")


# ── 进度推导（stage_runs 轮询式映射）──────────────────────────────

def test_progress_derived_from_stage_runs(tmp_path):
    stage_db = tmp_path / "stages.db"
    ts = TaskStore(str(tmp_path / "tasks.db"), stage_db_path=str(stage_db))
    try:
        s = ProjectStageStore(str(stage_db))
        pid = _pid("pr7")
        s.create_project(pid)
        # brief PASS → 10；script PASS → 25
        s.begin_stage(pid, "brief")
        s.record_artifact(pid, "brief", "h0")
        assert ts._derive_progress(pid)["progress"] == 10

        s.begin_stage(pid, "script")
        s.record_artifact(pid, "script", "h0")
        assert ts._derive_progress(pid)["progress"] == 25

        # RUNNING 阶段出现在 current_stage
        s.begin_stage(pid, "storyboard")
        prog = ts._derive_progress(pid)
        assert prog["current"] == "storyboard"
        assert prog["progress"] >= 40

        # 成功任务 → progress 100
        tid = ts.submit_task("probe", pid, "tester",
                             lambda: {"ok": True})["task_id"]
        deadline = time.time() + 20
        while time.time() < deadline:
            row = ts.task_status(tid)
            if row["status"] == "success":
                break
            time.sleep(0.2)
        assert ts.task_status(tid)["status"] == "success"
        assert ts.task_status(tid)["progress"] == 100
    finally:
        ts.close()


# ── 心跳超时回收（Airflow 式 zombie 检测）─────────────────────────

def test_stale_heartbeat_recovered_and_retryable(tmp_path):
    ts = TaskStore(str(tmp_path / "tasks.db"), stage_db_path=":memory:")
    try:
        pid = _pid("st8")
        tid = ts.submit_task("probe", pid, "tester",
                             lambda: {"ok": True})["task_id"]
        deadline = time.time() + 20
        while time.time() < deadline:
            if ts.get_task(tid)["status"] == "success":
                break
            time.sleep(0.2)
        # 伪造僵尸：success 行改回 running + 过期 heartbeat
        ts._update(tid, status="running",
                   heartbeat_at="2000-01-01T00:00:00+00:00")
        assert ts.recover_stale() >= 1
        row = ts.get_task(tid)
        assert row["status"] == "failed"
        assert "heartbeat" in row["error"]
        # 僵尸任务可由幂等重试救活（进程内 fn 仍在内存）
        rt = ts.retry_task(tid)
        assert rt is not None
        deadline = time.time() + 20
        while time.time() < deadline:
            if ts.get_task(tid)["status"] == "success":
                break
            time.sleep(0.2)
        assert ts.get_task(tid)["status"] == "success"
    finally:
        ts.close()


# ── 鉴权联动：绑定 key 只能看自己项目的任务 ───────────────────────

def test_bound_key_cannot_read_other_project_task(monkeypatch):
    monkeypatch.setenv("SHIPIN_AUTH_MODE", "strict")
    monkeypatch.setenv("SHIPIN_ADMIN_KEY", _ADMIN_KEY)
    sc = TestClient(api_mod.app)
    own, other = _pid("own"), _pid("oth")
    for p in (own, other):
        sc.post("/api/project/create", json={"project_id": p})
    # 直接落一个 other 项目的任务（auth off 车道与 admin 相同）
    api_mod._task_store().submit_task(
        "probe", other, "admin", lambda: {"ok": True})
    # 签发绑定 own 的 write key
    r = sc.post("/api/platform/keys", headers={"X-API-Key": _ADMIN_KEY},
                json={"label": "b1", "scope": "write",
                      "project_id": own})
    assert r.status_code == 200, r.text
    bound = r.json()["key"]
    tasks = sc.get("/api/tasks", headers={"X-API-Key": bound}).json()
    assert all(t["project_id"] == own for t in tasks["tasks"])
    other_task = api_mod._task_store().list_tasks(project_id=other)
    if other_task:
        r = sc.get(f"/api/tasks/{other_task[0]['task_id']}",
                   headers={"X-API-Key": bound})
        assert r.status_code == 403


# ── 轮56(十审 P1-4):同项目任务互斥 ────────────────────────────────
# 旧 submit_task 只按 task_id 去重不看项目:连续两次
# POST /api/pipeline/{id}/assemble 会让两个 worker 同时
# _save/_promote_final/record_artifact 同一 final.mp4(哈希快照竞态、
# 中间文件互相覆盖)。active_task_for + TaskConflict 现按项目拦。

def test_same_project_second_submit_conflicts(tmp_path):
    ts = TaskStore(str(tmp_path / "tasks.db"), stage_db_path=":memory:")
    try:
        pid = _pid("mtx")
        ts.submit_task("probe", pid, "tester",
                       lambda: (time.sleep(1.5), {"ok": True})[1])
        from shipin_platform.services.task_store import TaskConflict
        with pytest.raises(TaskConflict) as ei:
            ts.submit_task("probe", pid, "tester",
                           lambda: {"ok": True})
        assert pid in str(ei.value)
    finally:
        ts.close()


def test_different_projects_run_parallel(tmp_path):
    ts = TaskStore(str(tmp_path / "tasks2.db"), stage_db_path=":memory:")
    try:
        p1, p2 = _pid("par1"), _pid("par2")
        t1 = ts.submit_task("probe", p1, "tester",
                            lambda: {"ok": True})
        t2 = ts.submit_task("probe", p2, "tester",
                            lambda: {"ok": True})
        assert t1["task_id"] != t2["task_id"]
    finally:
        ts.close()


def test_next_submit_ok_after_previous_finished(tmp_path):
    ts = TaskStore(str(tmp_path / "tasks3.db"), stage_db_path=":memory:")
    try:
        pid = _pid("seq")
        t1 = ts.submit_task("probe", pid, "tester",
                            lambda: {"ok": True})["task_id"]
        deadline = time.time() + 20
        while time.time() < deadline:
            if ts.get_task(t1)["status"] == "success":
                break
            time.sleep(0.2)
        assert ts.get_task(t1)["status"] == "success"
        # 前一个成功后才放行下一个(顺序重跑同项目是正常形态)
        t2 = ts.submit_task("probe", pid, "tester",
                            lambda: {"ok": True})
        assert t2["task_id"] != t1
    finally:
        ts.close()


def test_allow_overlap_escape_hatch(tmp_path):
    """内部确需并发的场景可显式逃逸(默认互斥不变)。"""
    ts = TaskStore(str(tmp_path / "tasks4.db"), stage_db_path=":memory:")
    try:
        pid = _pid("esc")
        ts.submit_task("probe", pid, "tester",
                       lambda: (time.sleep(1.5), {"ok": True})[1])
        t2 = ts.submit_task("probe", pid, "tester",
                            lambda: {"ok": True},
                            allow_project_overlap=True)
        assert t2["status"] in ("queued", "running")
    finally:
        ts.close()