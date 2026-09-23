"""P2 产物版本化测试：DVC 式内容哈希版本、n8n 式全量快照、restore 回滚。

验收：《平台级演进计划》P2 —— 改写后 versions 多一条；restore 后旧内容可见
且下游 BLOCKED/失效 + 闸门清空；同内容重复写不产生新版本（哈希去重）；
回归保持绿。
"""
import json
import uuid

from fastapi.testclient import TestClient

import api as api_mod
from shipin_platform.orchestration.pipeline_runner import (
    _load, _project_dir, _save)
from shipin_platform.services import artifact_store as av

client = TestClient(api_mod.app)


def _pid(tag: str) -> str:
    return f"v{tag}-{uuid.uuid4().hex[:8]}"


def _make_project(pid: str) -> None:
    r = client.post("/api/project/create", json={"project_id": pid})
    assert r.status_code == 200, r.text


# ── 快照与去重 ────────────────────────────────────────────────────

def test_save_snapshots_and_dedupes_by_hash():
    pid = _pid("snap")
    _make_project(pid)
    d = _project_dir(pid)
    _save(pid, "script.json", {"duration_sec": 8, "shots": [{"shot_id": "S1",
                                                             "narration": "开"}]})
    _save(pid, "script.json", {"duration_sec": 8, "shots": [{"shot_id": "S1",
                                                             "narration": "开"}]})
    assert len(av.list_versions(d, "script")) == 1      # 同内容 → 无新版本
    _save(pid, "script.json", {"duration_sec": 8, "shots": [{"shot_id": "S1",
                                                             "narration": "改"}]})
    v = av.list_versions(d, "script")
    assert len(v) == 2
    assert v[0]["version"] == 1 and v[1]["version"] == 2
    assert v[1]["hash"] != v[0]["hash"]
    # 快照本体真实存在（n8n 式全量快照）
    assert (d / "versions" / "script" / "v1.json").is_file()
    # 非追踪产物不产生版本目录
    _save(pid, "manifest.json", {"x": 1})
    assert not (d / "versions" / "manifest").exists()


def test_versions_endpoint_and_untracked():
    pid = _pid("vep")
    _make_project(pid)
    _save(pid, "storyboard.json", {"shots": [{"shot_id": "S1", "subject": "主"}]})
    _save(pid, "storyboard.json", {"shots": [{"shot_id": "S1", "subject": "A"}]})
    r = client.get(f"/api/pipeline/{pid}/versions")
    assert r.status_code == 200
    body = r.json()
    assert len(body["versions"]["storyboard"]) == 2
    assert body["versions"]["storyboard"][0]["version"] == 1
    # 不存在阶段 → 空
    r2 = client.get(f"/api/pipeline/{pid}/versions", params={"stage": "noise"})
    assert r2.json()["versions"] == {}


# ── restore 回滚（dvc checkout 语义 + rewrite 栅栏）────────────────

def _setup_storyboard(store, pid: str) -> None:
    store.record_artifact(pid, "script", "h-s")
    store.record_confirmation(pid, "script")
    for st in ("storyboard", "image_prompt", "video_gen"):
        store.begin_stage(pid, st)
        store.record_artifact(pid, st, "h0")


def test_restore_writes_old_content_and_blocks_downstream():
    pid = _pid("res")
    _make_project(pid)
    store = api_mod._stage_store()
    # v1(好) → v2(坏)
    v1 = {"duration_sec": 8, "shots": [{"shot_id": "S1", "narration": "好"}]}
    v2 = {"duration_sec": 8, "shots": [{"shot_id": "S1", "narration": "坏"}]}
    _save(pid, "script.json", v1)
    _save(pid, "script.json", v2)
    _setup_storyboard(store, pid)                       # 确认 + 下游已产物
    r = client.post(f"/api/pipeline/{pid}/restore",
                    json={"stage": "script", "version": 1})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["stage"] == "script"
    assert body["version"] == 1
    assert body["invalidated"] >= 1                      # 下游全部失效
    # 当前产物已回滚到旧内容
    assert _load(pid, "script.json") == v1
    # 闸门被清空 + 该阶段回到 PENDING
    assert store.get_confirmation(pid, "script") is None
    assert store.get_stage(pid, "script")["status"] == "PENDING" \
        or store.get_stage(pid, "script")["status"] == "RUNNING"
    # 事件留痕
    kinds = [e["kind"] for e in store.list_events(pid)]
    assert "stage_restored" in kinds
    ev = next(e for e in store.list_events(pid)
              if e["kind"] == "stage_restored")
    assert "v1" in ev["summary"]
    # 下游阶段 BLOCKED（陈旧血统，与 rewrite 一致——不能继续花钱）
    assert store.get_stage(pid, "image_prompt")["status"] == "BLOCKED"


def test_restore_bad_version_and_stage():
    pid = _pid("bad")
    _make_project(pid)
    _save(pid, "script.json", {"shots": [{"shot_id": "S1", "narration": "a"}]})
    r = client.post(f"/api/pipeline/{pid}/restore",
                    json={"stage": "script", "version": 99})
    assert r.status_code == 404
    r2 = client.post(f"/api/pipeline/{pid}/restore",
                     json={"stage": "manifest", "version": 1})
    assert r2.status_code == 422
    assert "版本回滚" in r2.json()["detail"]


def test_restore_of_existing_version_makes_new_head():
    """回滚本身也是一次写：v1 → v2 → 回滚到 v1 → 生成 v3（内容与 v1 同 hash）。"""
    pid = _pid("roll")
    _make_project(pid)
    v1 = {"narration": "旧"}
    v2 = {"narration": "新"}
    _save(pid, "storyboard.json", v1)
    _save(pid, "storyboard.json", v2)
    r = client.post(f"/api/pipeline/{pid}/restore",
                    json={"stage": "storyboard", "version": 1})
    assert r.status_code == 200
    v = av.list_versions(_project_dir(pid), "storyboard")
    assert len(v) == 3
    assert v[-1]["hash"] == v[0]["hash"]        # 内容哈希一致
    assert _load(pid, "storyboard.json") == v1


# ── P5 版本内容读取（前端 diff 视图数据源）─────────────────────────

def test_version_content_endpoint():
    pid = _pid("vce")
    _make_project(pid)
    v1 = {"duration_sec": 8, "shots": [{"shot_id": "S1", "narration": "旧文案"}]}
    v2 = {"duration_sec": 8, "shots": [{"shot_id": "S1", "narration": "新文案"}]}
    _save(pid, "script.json", v1)
    _save(pid, "script.json", v2)
    r = client.get(f"/api/pipeline/{pid}/versions/script/1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 1
    assert body["content"] == v1
    assert body["hash"]
    # 空版本 / 不听凭 exotic 阶段
    assert client.get(f"/api/pipeline/{pid}/versions/script/99").status_code == 404
    assert client.get(f"/api/pipeline/{pid}/versions/manifest/1").status_code == 422
    assert client.get(f"/api/pipeline/{pid}/versions/script/1") \
        .status_code == 200


# ── 轮56(十审 P1-3):并发快照的版本唯一性与索引完整 ────────────────
# 旧 snapshot 读-改-写无锁:两个并发快照都读到同一 prev、都算
# version=1 → 索引出现两条 v1、磁盘只剩一个 v1.json;read_version 按
# version 字典取最后一条 entry → hash 与内容错位,restore 静默回滚到
# 错内容;索引 RMW 还会整份丢条目(实测 10 并发只剩 2 条)。

def test_concurrent_snapshots_version_uniqueness(tmp_path):
    import threading
    proj = tmp_path / "p"
    proj.mkdir()
    errs = []

    def snap(i):
        try:
            av.snapshot(proj, "script", {"v": i})
        except Exception as e:  # pragma: no cover - 防御
            errs.append(e)

    ts_ = [threading.Thread(target=snap, args=(i,)) for i in range(10)]
    for t in ts_:
        t.start()
    for t in ts_:
        t.join()
    assert not errs, errs
    entries = av.list_versions(proj, "script")
    versions = [e["version"] for e in entries]
    assert len(entries) == 10, versions          # 索引不丢条目
    assert sorted(versions) == list(range(1, 11)), versions  # 版本唯一递增
    # 每个 entry 的 hash 与磁盘 v{n}.json 内容一致(不错位)
    for e in entries:
        entry2, content = av.read_version(proj, "script", e["version"])
        assert entry2 is not None and content is not None, e
        assert av._content_hash(content) == e["hash"], e
    # 无临时文件残留(原子写)
    assert not list(proj.rglob("*.tmp"))


def test_snapshot_dedup_still_holds_under_concurrency(tmp_path):
    """并发写同一内容:只产一个版本(哈希去重语义不被锁破坏)。"""
    import threading
    proj = tmp_path / "p2"
    proj.mkdir()
    errs = []

    def snap():
        try:
            av.snapshot(proj, "brief", {"same": "content"})
        except Exception as e:  # pragma: no cover - 防御
            errs.append(e)

    ts_ = [threading.Thread(target=snap) for _ in range(6)]
    for t in ts_:
        t.start()
    for t in ts_:
        t.join()
    assert not errs, errs
    assert len(av.list_versions(proj, "brief")) == 1