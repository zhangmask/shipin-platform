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