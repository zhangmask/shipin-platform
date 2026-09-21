"""reference_apply 测试：参考包 → 画布节点链注入。

验证产物（script/shots/brief JSON）被正确转换成
text→script→storyboard→(frame_prompts|video_prompt) 链：
- 节点类型/标题/参数内容正确（剧本每镜一行、分镜含反推字段）
- 连线端口按 kind 匹配，validate_graph 通过
- graph_id 复用已有画布（追加不覆盖）
"""
from __future__ import annotations

import json

import pytest

from shipin_platform.graph import engine
from shipin_platform.graph.reference_apply import (
    ReferenceApplyError, build_reference_graph)


def _write_pack(ref_dir) -> None:
    """写一份最小但完整的参考包（pipeline 产物三件套）。"""
    ref_dir.mkdir(parents=True, exist_ok=True)
    (ref_dir / "script.json").write_text(json.dumps({
        "title": "咖啡TVC",
        "total_duration": 16.0,
        "shot_count": 2,
        "lines": [
            {"shot": 1, "time": "0.0-4.0", "duration": 4.0,
             "dialogue": "第一句台词", "frame": "f001_m.jpg",
             "image_prompt": "barista pouring coffee, warm light",
             "video_prompt": "slow push-in, steam rising"},
            {"shot": 2, "time": "4.0-14.0", "duration": 10.0,
             "dialogue": "第二句台词在这镜", "frame": "f002_m.jpg",
             "image_prompt": "cup on counter, sunlight",
             "video_prompt": "pan right, latte art"},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    (ref_dir / "shots.json").write_text(json.dumps([
        {"idx": 1, "start": 0.0, "end": 4.0, "duration": 4.0,
         "frames": ["f001_m.jpg", "f001_j.jpg"],
         "dialogue": "第一句台词",
         "scene": "咖啡店吧台", "subject": "咖啡师", "camera": "中景",
         "lighting": "暖光", "tone_and_palette": "暖棕", "composition": "居中",
         "image_prompt": "barista pouring coffee, warm light",
         "video_prompt": "slow push-in"},
        {"idx": 2, "start": 4.0, "end": 14.0, "duration": 10.0,
         "frames": ["f002_m.jpg"],
         "dialogue": "第二句台词在这镜", "scene": "吧台", "subject": "咖啡杯",
         "camera": "特写", "lighting": "侧逆光", "tone_and_palette": "暖棕",
         "composition": "三分线",
         "image_prompt": "cup, steam, sunlight", "video_prompt": "un right"}],
        ensure_ascii=False), encoding="utf-8")
    (ref_dir / "brief.json").write_text(json.dumps({
        "brief_prefill": {"style": "参考: 咖啡暖调", "platform": "竖屏"},
        "vlm_used": True, "vlm_error": "",
    }, ensure_ascii=False), encoding="utf-8")


@pytest.fixture()
def graph_dir(tmp_path, monkeypatch):
    """把 GRAPHS_DIR 指到临时目录，避免污染真实 data/。"""
    d = tmp_path / "graphs"
    d.mkdir(parents=True)
    monkeypatch.setattr(engine, "GRAPHS_DIR", d)
    return d


@pytest.fixture()
def ref_dir(tmp_path):
    d = tmp_path / "ref"
    _write_pack(d)
    return d


def _graph(graph_dir) -> dict:
    g = engine.new_graph("测试图")
    assert (graph_dir / g["id"] / "graph.json").exists()
    return g


class TestBuildReferenceGraph:
    def test_nodes_and_edges(self, ref_dir, graph_dir):
        g = _graph(graph_dir)
        gid = g["id"]
        res = build_reference_graph(ref_dir, gid)

        assert res["ok"] is True
        assert res["graph_id"] == gid
        assert res["nodes_added"] == 5
        assert res["edges_added"] == 4

        # 节点标题标记调用（title 字段 = 人读标签）
        titles = {n["title"] for n in res["nodes"]}
        assert "参考简报" in titles
        assert "剧本·咖啡TVC" in titles   # 标题=剧本·<视频标题>

        # 落盘图再次校验无错，且不破坏原图
        saved = engine.load_graph(gid)
        assert engine.validate_graph(saved) == []

    def test_chain_ports_kinds(self, ref_dir, graph_dir):
        """验证连线端口与数据流方向（text→script→storyboard→…）。"""
        g = _graph(graph_dir)
        res = build_reference_graph(ref_dir, g["id"])
        edges = res["edges"]

        types = {n["id"]: n["type"] for n in res["nodes"]}
        text_id = next(i for i, t in types.items() if t == "text")
        script_id = next(i for i, t in types.items() if t == "script")
        board_id = next(i for i, t in types.items() if t == "storyboard")
        fp_id = next(i for i, t in types.items() if t == "frame_prompts")
        vp_id = next(i for i, t in types.items() if t == "video_prompt")

        by_to = {(e["from"], e["to"]): e for e in edges}
        # text.text → script.brief
        assert (text_id, script_id) in by_to
        assert by_to[(text_id, script_id)]["to_port"] == "brief"
        # script.progress → storyboard
        assert (script_id, board_id) in by_to
        # storyboard → frame_prompts + video_prompt 双分支
        assert by_to[(board_id, fp_id)]["to_port"] == "board"
        assert by_to[(board_id, vp_id)]["to_port"] == "board"
        # 无缺失/多余连线（端口 kind 校验交给 validate_graph）
        assert engine.validate_graph(
            engine.load_graph(g["id"])) == []

    def test_script_and_storyboard_content(self, ref_dir, graph_dir):
        """剧本台词与分镜反推信息落进节点参数（抄剧本/抄分镜）。"""
        g = _graph(graph_dir)
        res = build_reference_graph(ref_dir, g["id"])
        nodes = {n["id"]: n for n in res["nodes"]}
        script = engine.load_graph(g["id"])["nodes"]
        node_of = {n["id"]: n for n in script}

        s_node = next(n for n in script if n["type"] == "script")
        content = s_node["params"]["content"]
        assert "[镜头1 @0.0-4.0] 第一句台词" in content
        assert "[镜头2 @4.0-14.0] 第二句台词在这镜" in content

        b_node = next(n for n in script if n["type"] == "storyboard")
        assert "中景、暖光、暖棕、居中" in b_node["params"]["content"]
        assert "barista pouring coffee" in b_node["params"]["content"]

        fp_node = next(n for n in script if n["type"] == "frame_prompts")
        # frame_prompts 取 script.json lines 里的 image_prompt（非 shots 版）
        assert "[镜头2] cup on counter, sunlight" in fp_node["params"]["first_prompt"]

        vp_node = next(n for n in script if n["type"] == "video_prompt")
        assert "slow push-in" in vp_node["params"]["content"]

    def test_brief_prefill_in_text_node(self, ref_dir, graph_dir):
        g = _graph(graph_dir)
        res = build_reference_graph(ref_dir, g["id"])
        t_node = next(n for n in res["nodes"] if n["type"] == "text")
        assert "style: 参考: 咖啡暖调" in t_node["params"]["text"]

    def test_append_to_existing_graph(self, ref_dir, graph_dir):
        """已有画布留存量不破坏：只追加，不回写原节点。"""
        g = _graph(graph_dir)
        engine.add_node(g, "text", x=10, y=10, params={"text": "已有"})
        res = build_reference_graph(ref_dir, g["id"])
        assert res["nodes_added"] == 5
        saved = engine.load_graph(g["id"])
        text_nodes = [n for n in saved["nodes"] if n["type"] == "text"]
        assert len(text_nodes) == 2   # 原 text + 简报 text
        assert engine.validate_graph(saved) == []

    def test_missing_pack_raises(self, tmp_path, graph_dir):
        g = _graph(graph_dir)
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(ReferenceApplyError):
            build_reference_graph(empty, g["id"])