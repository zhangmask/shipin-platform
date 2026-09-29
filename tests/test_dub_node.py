"""轮73:配音(dub)节点与配音模式测试。

覆盖三处新增:
- node_types:REGISTRY["dub"] 定义 + definitions_json 可见(画布能选)
- engine:EXECUTORS["dub"] 映射 + _exec_dub 的参数校验与本地走线
- pipeline_runner:_dub_mode 开关语义 / _pad_audio_to 音频 pad
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from shipin_platform.graph import engine
from shipin_platform.graph.node_types import definitions_json
from shipin_platform.orchestration import pipeline_runner as pr


class TestDubNodeType:
    def test_dub_registered_and_visible_in_definitions(self):
        defs = definitions_json()
        assert "dub" in defs
        d = defs["dub"]
        assert [i["name"] for i in d["inputs"]] == ["first_frame", "audio"]
        assert [o["name"] for o in d["outputs"]] == ["video"]
        keys = [p["key"] for p in d["params"]]
        assert keys == ["engine", "duration", "aspect"]

    def test_dub_executor_mapped(self):
        assert "dub" in engine.EXECUTORS
        assert engine.EXECUTORS["dub"] is engine._exec_dub

    def test_dub_is_spend_node(self):
        # 配音要花钱(一次视频生成算力),必须进预算熔断名单
        assert "dub" in engine._SPEND_NODE_TYPES


class TestExecDubValidation:
    def test_requires_first_frame_and_audio(self):
        g = engine.new_graph("t")
        g["nodes"].append({"id": "n1", "type": "dub", "x": 0, "y": 0,
                           "params": {}})
        engine.save_graph(g)
        with pytest.raises(ValueError, match="首帧"):
            engine._exec_dub(g, g["nodes"][0], {"n1": g["nodes"][0]})

    def test_unknown_engine_rejected(self, monkeypatch, tmp_path):
        g = engine.new_graph("t")
        # 首帧/音频经 params 直给(resolved_input 的 param 回落路径),
        # 只为走到引擎校验那一步
        g["nodes"].append({"id": "n1", "type": "dub", "x": 0, "y": 0,
                           "params": {"engine": "nope",
                                      "first_frame": str(tmp_path / "f.png"),
                                      "audio": str(tmp_path / "a.wav")}})
        engine.save_graph(g)
        monkeypatch.setattr(engine, "_artifact_path",
                            lambda gid, nid, ext: str(tmp_path / f"{nid}.{ext}"))
        with pytest.raises(ValueError, match="配音引擎"):
            engine._exec_dub(g, g["nodes"][0], {"n1": g["nodes"][0]})


class TestDubMode:
    def test_default_off(self, monkeypatch):
        monkeypatch.delenv("SHIPIN_DUB_MODE", raising=False)
        pr._DUB_STATE.update({"checked": False, "on": False})
        assert pr._dub_mode("p") is False

    def test_on_forces(self, monkeypatch):
        monkeypatch.setenv("SHIPIN_DUB_MODE", "on")
        assert pr._dub_mode("p") is True

    def test_auto_on_low_vram(self, monkeypatch):
        monkeypatch.setenv("SHIPIN_DUB_MODE", "auto")
        pr._DUB_STATE.update({"checked": False, "on": False})

        class _LM:
            @staticmethod
            def _get(path):
                return {"comfy_stats": {"devices": [
                    {"name": "cuda:0 RTX 4090", "vram_total": 25.8e9}]}}
        monkeypatch.setattr(
            "shipin_platform.generation.local_media._get", _LM._get,
            raising=False)
        import shipin_platform.generation.local_media as lm
        monkeypatch.setattr(lm, "_get", _LM._get)
        assert pr._dub_mode("p") is True
        assert pr._DUB_STATE["vram_gb"] == 25.8

    def test_auto_off_big_vram(self, monkeypatch):
        monkeypatch.setenv("SHIPIN_DUB_MODE", "auto")
        pr._DUB_STATE.update({"checked": False, "on": False})
        import shipin_platform.generation.local_media as lm

        def _big(path):
            return {"comfy_stats": {"devices": [
                {"name": "cuda:0 GB10", "vram_total": 130e9}]}}
        monkeypatch.setattr(lm, "_get", _big)
        assert pr._dub_mode("p") is False


class TestPadAudio:
    def test_pad_shortens_and_pads(self, tmp_path):
        import subprocess
        # 造 1s 音频,pad 到 3s:产物时长应≈3s 且可 ffprobe
        src = tmp_path / "a.wav"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                        "-i", "sine=frequency=440:duration=1",
                        "-ac", "1", "-ar", "16000", str(src)], check=True)
        dst = tmp_path / "b.wav"
        assert pr._pad_audio_to(str(src), str(dst), 3.0)
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                              "format=duration", "-of",
                              "default=noprint_wrappers=1:nokey=1", str(dst)],
                             capture_output=True, text=True).stdout.strip()
        assert abs(float(out) - 3.0) < 0.15


class TestDubPassPipeline:
    """_dub_pass 本体(管线级):只对有台词的人物镜配音,产物替换 clip/
    master 并重算哈希;无台词镜/产品镜保持原样;失败 fail-closed。"""

    def _mk_shot(self, sid, subject, with_dlg, work):
        rec = {"first_frame": str(work / f"{sid}.jpg")}
        if with_dlg:
            rec["dlg"] = str(work / f"{sid}_dlg.mp3")
        return {"shot_id": sid, "subject": subject,
                "duration_sec": 5, "_rec": rec}

    def test_person_dialogue_shot_gets_dubbed(self, tmp_path, monkeypatch):
        import shipin_platform.orchestration.pipeline_runner as pr
        from shipin_platform.generation import local_media
        work = tmp_path
        (work / "S01.jpg").write_bytes(b"img")
        (work / "S01_dlg.mp3").write_bytes(b"aud")
        shots = [self._mk_shot("S01", "一位女性咖啡师", True, work),
                 self._mk_shot("S02", "无人物:咖啡机产品特写", True, work)]
        manifest = {"shots": {s["shot_id"]: s["_rec"] for s in shots}}

        called = {}

        def _fake_dub(frame, audio, out, **kw):
            called["args"] = (frame, audio, out, kw)
            Path(out).write_bytes(b"dubbedvideo")
            return {"ok": True, "path": out}
        monkeypatch.setattr(local_media, "local_dub", _fake_dub)
        # 归一化/记账/音频 pad 都不真跑(pad 的 ffmpeg 对假音频必然失败)
        def _fake_norm(src, dst, w, h):
            shutil.copyfile(src, dst)  # 归一化的产物=配音产物本身
            return True
        monkeypatch.setattr(pr, "_normalize_canvas", _fake_norm)
        monkeypatch.setattr(pr, "_pad_audio_to", lambda src, dst, sec: True)
        monkeypatch.setattr(pr, "record_cost", lambda *a, **k: None)
        r = pr._dub_pass(shots, manifest, work, "p", 720, 1280)
        assert r["error"] is None, r
        assert len(r["dubbed"]) == 1 and r["dubbed"][0]["shot_id"] == "S01"
        # S02(无人物镜)未被配音;S01 的 clip/master 被换成配音产物
        assert "clip" not in manifest["shots"]["S02"]
        rec = manifest["shots"]["S01"]
        assert rec["clip"] == rec["master"]
        assert Path(rec["clip"]).read_bytes() == b"dubbedvideo"
        assert rec["clip_sha256"] and rec["dub"] and rec["dub_from_frame"]
        # 画布尺寸透传、音频时长按镜头秒数
        assert called["args"][3]["duration"] == 5.0
        assert called["args"][3]["width"] == 720

    def test_dub_failure_is_fail_closed(self, tmp_path, monkeypatch):
        import shipin_platform.orchestration.pipeline_runner as pr
        from shipin_platform.generation import local_media
        work = tmp_path
        (work / "S01.jpg").write_bytes(b"img")
        (work / "S01_dlg.mp3").write_bytes(b"aud")
        shots = [self._mk_shot("S01", "一位男性顾客", True, work)]
        manifest = {"shots": {s["shot_id"]: s["_rec"] for s in shots}}
        monkeypatch.setattr(local_media, "local_dub",
                            lambda *a, **k: {"ok": False, "error": "boom"})
        monkeypatch.setattr(pr, "_pad_audio_to", lambda src, dst, sec: True)
        r = pr._dub_pass(shots, manifest, work, "p", 720, 1280)
        # 失败必须带 error(fail-closed),不能静默留原片
        assert r["error"] and "S01" in r["error"]
        assert "clip" not in manifest["shots"]["S01"]
