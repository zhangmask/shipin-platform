"""节点画布后端测试：图 CRUD、校验（环/类型/重复边）、数据流执行
（上游输出经连线注入下游）、缓存哈希、以及安全（路径穿越拒绝）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from shipin_platform.graph import engine
from shipin_platform.graph.engine import (
    new_graph, save_graph, load_graph, validate_graph, run_node,
    _artifact_path, _owned_artifact,
)
from shipin_platform.graph.node_types import definitions_json


def _g(name="t") -> dict:
    return new_graph(name)


_make = _g  # 测试内「造一个默认空图」的别名


def _add_node(g, nid, ntype, x=0, y=0, params=None, edges=None):
    g["nodes"].append({"id": nid, "type": ntype, "x": x, "y": y,
                       "params": params or {}})
    for eds in edges or []:
        g["edges"].append(eds)
    return g


def test_crud_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "GRAPHS_DIR", tmp_path)
    g = new_graph("咖啡片")
    g = _add_node(g, "n1", "text", params={"text": "hello"})
    save_graph(g)
    g2 = load_graph(g["id"])
    assert g2["name"] == "咖啡片"
    assert g2["nodes"][0]["params"]["text"] == "hello"
    assert [x["id"] for x in engine.list_graphs()] == [g["id"]]


def test_validate_type_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "GRAPHS_DIR", tmp_path)
    g = _make()
    # image_gen 的 image 输出端口 kind=image，接到 text 节点（无输入端口）应报错
    g["nodes"] = [
        {"id": "a", "type": "image_gen", "x": 0, "y": 0, "params": {}},
        {"id": "b", "type": "text", "x": 100, "y": 0, "params": {}},
    ]
    # b 没有输入端；连线 a.output -> b.nonexist 会报端口不存在
    # 先试类型不匹配：text 输出（kind=text）连到 image_gen 的 first_frame（image）
    g["nodes"] = [
        {"id": "a", "type": "image_gen", "x": 0, "y": 0, "params": {}},
        {"id": "b", "type": "video_gen", "x": 100, "y": 0, "params": {}},
    ]
    g["edges"] = [{"from": "a", "from_port": "image", "to": "b",
                   "to_port": "first_frame"}]
    # image -> first_frame 是同类型，合法
    assert validate_graph(g) == []
    g["edges"] = [{"from": "a", "from_port": "image", "to": "b",
                   "to_port": "prompt"}]
    errs = validate_graph(g)
    assert any("类型不匹配" in e for e in errs)
    # 环
    g["edges"] = [{"from": "a", "from_port": "image", "to": "b",
                   "to_port": "first_frame"},
                  {"from": "b", "from_port": "video", "to": "a",
                   "to_port": "prompt"}]
    errs = validate_graph(g)
    assert any("环路" in e for e in errs)


def test_upstream_order_and_input_hash(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "GRAPHS_DIR", tmp_path)
    g = _make()
    g["nodes"] = [
        {"id": "text1", "type": "text", "x": 0, "y": 0,
         "params": {"text": "A"}},
        {"id": "img1", "type": "image_gen", "x": 0, "y": 100,
         "params": {"model": "pil"}},
        {"id": "vid1", "type": "video_gen", "x": 200, "y": 0,
         "params": {"model": "agnes-video-2.5-flash"}},
    ]
    g["edges"] = [
        {"from": "text1", "from_port": "text", "to": "vid1", "to_port": "prompt"},
        {"from": "img1", "from_port": "image", "to": "vid1",
         "to_port": "first_frame"},
    ]
    save_graph(g)
    nm = {n["id"]: n for n in g["nodes"]}
    order = engine._upstream_order(g, "vid1")
    assert set(order) == {"text1", "img1", "vid1"}
    assert order.index("text1") < order.index("vid1")
    h1 = engine.node_input_hash(g, nm["vid1"], nm)
    nm["text1"]["params"]["text"] = "B"
    h2 = engine.node_input_hash(g, nm["vid1"], nm)
    assert h1 != h2
    # 边值参与哈希：模拟 img1 输出变更
    nm["img1"]["state"] = {"ok": True,
                           "outputs": {"image": {"kind": "image", "value": "/x.png"}}}
    h3 = engine.node_input_hash(g, nm["vid1"], nm)
    assert h3 != h2


def test_run_cached_and_force(tmp_path, monkeypatch):
    """缓存：上游不变时再次运行不重跑（video_gen mock 计数器）。"""
    monkeypatch.setattr(engine, "GRAPHS_DIR", tmp_path)
    calls = {"n": 0}

    def fake_video(*args, **kwargs):
        calls["n"] += 1
        output_path = kwargs.get("output_path") or (args[2] if len(args) > 2 else "")
        Path(output_path).write_bytes(b"000mpeg")
        return {"ok": True, "model": kwargs.get("model", ""),
                "anchored": True, "warnings": [], "duration_sec": 5.0}

    monkeypatch.setattr(
        "shipin_platform.generation.generate_assets.generate_video_agnes",
        fake_video)
    g = _make()
    g["nodes"] = [
        {"id": "img1", "type": "image_gen", "x": 0, "y": 0,
         "params": {"model": "pil", "prompt": "蒸汽咖啡"}},
        {"id": "vid1", "type": "video_gen", "x": 200, "y": 0,
         "params": {"model": "agnes-video-2.5-flash", "duration": 5,
                    "prompt": "蒸汽上升，镜头缓缓推近"}},
    ]
    g["edges"] = [{"from": "img1", "from_port": "image", "to": "vid1",
                   "to_port": "first_frame"}]
    # img1 用 PIL 生成占位图
    from shipin_platform.generation.generate_assets import generate_image_pil
    monkeypatch.setattr(
        "shipin_platform.generation.generate_assets.generate_image_pil",
        lambda prompt, w, h, out: Path(out).write_bytes(b"png"))
    r1 = run_node(g, "vid1")
    assert r1["ok"] is True
    assert calls["n"] == 1
    r2 = run_node(g, "vid1")   # 未变化 → 缓存命中
    assert calls["n"] == 1
    r3 = run_node(g, "vid1")   # 再点一次运行，仍然命中
    assert calls["n"] == 1
    # 上游参数变化（改提示词）→ 缓存失效强制重跑
    g["nodes"][0]["params"]["prompt"] = "拿铁拉花"
    run_node(g, "vid1")
    assert calls["n"] == 2


def test_owned_artifact_rejects_external(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "GRAPHS_DIR", tmp_path)
    g = _make()
    save_graph(g)
    with pytest.raises(ValueError):
        _owned_artifact(g["id"], r"C:\Windows\system32\notepad.exe")
    ok = _artifact_path(g["id"], "n1", "mp4")
    assert _owned_artifact(g["id"], str(ok)) == ok.resolve()


def test_api_router():
    from api_graph import router
    paths = {getattr(r, "path", "") for r in router.routes}
    assert "/api/graphs/{gid}/run" in paths
    assert "/api/graphs/kit/definitions" in paths
    assert "/api/graphs" in paths


# ── 轮50(九审 P1-2/P1-4):画布车道 TTS 输出诚信 + 成本/预算 ─────────

class _FakeSeg:
    def __init__(self, sid: str):
        self.shot_id = sid
        self.output_path = ""
        self.error = ""
        self.text = "x"
        self.duration_sec = 1.0
        self.voice = "v"


class _FakeTts:
    """按真实命名({shot_id}_{uuid8}.mp3)落盘的假 TTS 服务。"""

    def __init__(self, work, db):
        self._work = Path(work)
        self._work.mkdir(parents=True, exist_ok=True)  # 真服务会建工作目录

    def build_segment(self, sid, text, role_code=None, voice=None,
                      rate="-6%"):
        return _FakeSeg(sid)

    def synthesize_segments_sync(self, segs):
        outs = []
        for s in segs:
            p = self._work / f"{s.shot_id}_deadbeef.mp3"
            p.write_bytes(b"mp3-" + s.shot_id.encode())
            s.output_path = str(p)
            outs.append(s)
        return outs


def _patch_fake_tts(monkeypatch):
    import shipin_platform.services.tts_service as ts_mod
    monkeypatch.setattr(ts_mod, "create_tts_service",
                        lambda work, db: _FakeTts(work, db))


class TestCanvasStorageIntegrity:
    """轮55(十审 P0-2/P1-1/P1-2):图存储完整性——
    (a) save_graph 原子写 + 按图 RMW 锁(并发 add_node 不丢节点,
        半截 JSON 不可见);
    (b) new_graph id 唯一(秒级时间戳同秒创建曾互相覆盖);
    (c) graph_asset 读侧不串台(n1 不得读到 n10 的产物)。"""

    def test_concurrent_add_node_loses_nothing(self, tmp_path,
                                               monkeypatch):
        """走 API 端点的并发 add_node(PUT/ POST nodes 现已持按图 RMW
        锁)——旧的无锁路径 8 线程只剩 1 个节点(十审实测)。"""
        import threading
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import api_graph
        monkeypatch.setattr(engine, "GRAPHS_DIR", tmp_path)
        app = FastAPI()
        app.include_router(api_graph.router)
        client = TestClient(app)
        g = engine.new_graph("race")
        errors = []

        def add(i):
            try:
                r = client.post(f"/api/graphs/{g['id']}/nodes",
                                json={"type": "text",
                                      "params": {"text": f"t{i}"}})
                if r.status_code != 200:
                    errors.append(r.text)
            except Exception as e:  # pragma: no cover - 防御
                errors.append(str(e))

        ts = [threading.Thread(target=add, args=(i,)) for i in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert not errors, errors
        final = engine.load_graph(g["id"])
        assert len(final["nodes"]) == 8, [n["id"] for n in final["nodes"]]

    def test_new_graph_ids_unique_same_second(self, tmp_path,
                                              monkeypatch):
        monkeypatch.setattr(engine, "GRAPHS_DIR", tmp_path)
        ids = {engine.new_graph(f"g{i}")["id"] for i in range(20)}
        assert len(ids) == 20, "同秒批量建图不得碰撞"

    def test_save_graph_leaves_no_tmp(self, tmp_path, monkeypatch):
        monkeypatch.setattr(engine, "GRAPHS_DIR", tmp_path)
        g = engine.new_graph("atomic")
        engine.add_node(g, "text", params={"text": "x"})
        d = engine._gdir(g["id"])
        assert (d / "graph.json").is_file()
        assert not list(d.glob("*.tmp")), "原子写不得残留临时文件"

    def test_asset_endpoint_no_cross_node(self, tmp_path, monkeypatch):
        """n1 与 n10 产物共存时,取 n1 必须拿 n1 的文件——旧的无分隔
        前缀 glob + 字典序首个小 bug('n10_x' < 'n1_y')。"""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import api_graph
        monkeypatch.setattr(engine, "GRAPHS_DIR", tmp_path)
        app = FastAPI()
        app.include_router(api_graph.router)
        client = TestClient(app)
        g = engine.new_graph("assets")
        art = engine.artifact_dir(g["id"])
        (art / "n1_aaaa.mp3").write_bytes(b"n1-audio")
        (art / "n10_bbbb.mp3").write_bytes(b"n10-audio")
        r = client.get(f"/api/graphs/{g['id']}/assets/n1")
        assert r.status_code == 200, r.text
        assert r.content == b"n1-audio", r.content
        r10 = client.get(f"/api/graphs/{g['id']}/assets/n10")
        assert r10.status_code == 200 and r10.content == b"n10-audio"

    def test_asset_exact_name_preferred(self, tmp_path, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import api_graph
        monkeypatch.setattr(engine, "GRAPHS_DIR", tmp_path)
        app = FastAPI()
        app.include_router(api_graph.router)
        client = TestClient(app)
        g = engine.new_graph("assets2")
        art = engine.artifact_dir(g["id"])
        (art / "n1.png").write_bytes(b"png-bytes")
        (art / "n1_zzzz.mp3").write_bytes(b"mp3-bytes")
        r = client.get(f"/api/graphs/{g['id']}/assets/n1.png")
        assert r.status_code == 200 and r.content == b"png-bytes"
    """轮50(九审 P1-2):_exec_tts 旧实现丢弃返回值 + 无 _ 分隔前缀 glob
    + 字典序首个 → 节点 n1 串到 n10 的音频('0'<'_')、重跑选中旧文件、
    失败返回旧音频且状态 ok。三类错随 assemble 流出且无声画一致性门。"""

    def test_two_nodes_do_not_cross_audio(self, tmp_path, monkeypatch):
        monkeypatch.setattr(engine, "GRAPHS_DIR", tmp_path)
        _patch_fake_tts(monkeypatch)
        g = _make()
        g["nodes"] = [
            {"id": "n1", "type": "tts", "x": 0, "y": 0,
             "params": {"text": "第一段"}},
            {"id": "n10", "type": "tts", "x": 0, "y": 100,
             "params": {"text": "第十段"}},
        ]
        # 先跑 n10(n10_*.mp3 先落盘),再跑 n1——旧实现的
        # sorted(glob("n1*"))[0] 此时会命中 'n10_deadbeef.mp3'
        # ('0'<'_' 字典序),节点 n1 拿到 n10 的音频
        r10 = run_node(g, "n10")
        r1 = run_node(g, "n1")
        v1 = r1["outputs"]["audio"]["value"]
        v10 = r10["outputs"]["audio"]["value"]
        assert Path(v1).read_bytes() == b"mp3-n1", v1
        assert Path(v10).read_bytes() == b"mp3-n10", v10

    def test_failure_raises_not_stale_audio(self, tmp_path, monkeypatch):
        """合成失败 + art 目录留有上一轮旧文件 → 必须抛,不得返回旧音频
        且节点 ok(轮31 同范式:静默失败必须显式)。"""
        import shipin_platform.services.tts_service as ts_mod

        class _FailTts(_FakeTts):
            def synthesize_segments_sync(self, segs):
                for s in segs:
                    s.error = "edge-tts timeout"
                return segs

        monkeypatch.setattr(engine, "GRAPHS_DIR", tmp_path)
        monkeypatch.setattr(ts_mod, "create_tts_service",
                            lambda work, db: _FailTts(work, db))
        g = _make()
        g["nodes"] = [{"id": "n1", "type": "tts", "x": 0, "y": 0,
                       "params": {"text": "第一段"}}]
        art = engine._gdir(g["id"]) / "artifacts"
        art.mkdir(parents=True)
        (art / "n1_oldbeef.mp3").write_bytes(b"stale")  # 上一轮旧音频
        with pytest.raises(RuntimeError) as ei:
            run_node(g, "n1")
        assert "tts 合成失败" in str(ei.value)
        st = g["nodes"][0]["state"]
        assert st["ok"] is False and "edge-tts timeout" in st["error"]


class TestCanvasCostAndBudget:
    """轮50(九审 P1-4):画布车道此前零记账零预算——image/video/tts
    直连付费供应商,同 key 在 pipeline 被 422、这里无限花钱。"""

    def test_spend_nodes_record_cost(self, tmp_path, monkeypatch):
        monkeypatch.setattr(engine, "GRAPHS_DIR", tmp_path)
        import shipin_platform.generation.generate_assets as ga
        monkeypatch.setattr(
            ga, "generate_image_agnes",
            lambda prompt, w, h, out, model="": (
                Path(out).write_bytes(b"png"),
                {"ok": True})[1])
        g = _make()
        g["nodes"] = [{"id": "img1", "type": "image_gen", "x": 0, "y": 0,
                       "params": {"model": "agnes-image-2.1-flash",
                                  "prompt": "x"}}]
        r = run_node(g, "img1")
        assert r["ok"] is True
        from shipin_platform.services.costing import cost_file
        fp = cost_file(f"graph-{g['id']}")
        assert fp.is_file(), "画布花钱节点必须入账(此前零记账)"
        rows = json.loads(fp.read_text(encoding="utf-8"))
        assert rows and rows[-1]["kind"] == "image"
        assert rows[-1]["usd"] >= 0.0

    def test_budget_blocks_spend_node(self, tmp_path, monkeypatch):
        monkeypatch.setattr(engine, "GRAPHS_DIR", tmp_path)
        import shipin_platform.generation.generate_assets as ga
        monkeypatch.setattr(
            ga, "generate_image_agnes",
            lambda prompt, w, h, out, model="": (
                Path(out).write_bytes(b"png"),
                {"ok": True})[1])
        g = _make()
        g["nodes"] = [{"id": "img1", "type": "image_gen", "x": 0, "y": 0,
                       "params": {"model": "agnes-image-2.1-flash",
                                  "prompt": "x"}}]
        run_node(g, "img1")  # 先花一笔入账
        # 配一个低于已用额的预算
        bfp = (engine.roots.data_dir() / "projects"
               / f"graph-{g['id']}" / "budget.json")
        bfp.parent.mkdir(parents=True, exist_ok=True)
        bfp.write_text(json.dumps({"max_budget_usd": 0.000001}),
                       encoding="utf-8")
        # 改参数让缓存失效 → 再跑必须被预算熔断
        g["nodes"][0]["params"]["prompt"] = "y"
        with pytest.raises(RuntimeError) as ei:
            run_node(g, "img1")
        assert "预算熔断" in str(ei.value)
        st = g["nodes"][0]["state"]
        assert st.get("blocked_by_budget") is True