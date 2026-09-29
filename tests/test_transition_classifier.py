"""轮64:逐边界转场选型分类器测试(_classify_boundaries/_same_scene)。

用户实测反馈「片段之间切换生硬」——旧行为所有边界一律 dissolve,本地
无 master 时冻结入镜首帧借位,每切一刀约 10 帧静止顿挫。分类器按场景
同一性选型:同场景软切/换场景叠化/进落版卡叠化,链式(cut)保留。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shipin_platform.orchestration.pipeline_runner import (  # noqa: E402
    _classify_boundaries, _same_scene)


def _shot(sid, scene, beat=""):
    return {"shot_id": sid, "scene": scene, "beat": beat}


class TestSameScene:
    def test_shared_location_tokens(self):
        # 共享「吧台」→ 同场景换景别
        assert _same_scene("吧台后，店主笑着抬手示意", "吧台前特写，递出杯子")

    def test_single_char_overlap_is_not_same(self):
        # 「吧台后」vs「台灯下」只有单字「台」相通 → 换场景
        assert not _same_scene("吧台后", "台灯下，笔尖划过试卷")

    def test_subject_word_in_tail_is_not_location(self):
        # 两句都以「女孩」做主语但地点不同(出租屋 vs 走廊)——
        # 地点声明在句头,尾部主语词不参与判定
        assert not _same_scene("冷白出租屋，女孩揉眉叹气",
                                "走廊灯下，女孩夹书快走")

    def test_same_building_head_shared(self):
        assert _same_scene("写字楼大堂等电梯", "写字楼电梯间内部")

    def test_empty_scene_not_same(self):
        assert not _same_scene("", "走廊")


class TestClassifyBoundaries:
    def test_same_scene_softcut_scene_change_dissolve(self):
        shots = [
            _shot("S01", "吧台后，店主抬手"),
            _shot("S02", "吧台前，递出杯子"),
            _shot("S03", "街道夜景，女孩走来"),
            _shot("S04", "门店门头", beat="落"),
        ]
        bts = _classify_boundaries(shots, {"shots": {}})
        assert bts == ["softcut", "dissolve", "dissolve"]

    def test_manifest_cut_wins(self):
        shots = [_shot("S01", "吧台后"), _shot("S02", "吧台前")]
        bts = _classify_boundaries(
            shots, {"shots": {"S02": {"boundary": "cut"}}})
        assert bts == ["cut"]

    def test_last_beat_card_dissolve(self):
        shots = [_shot("S01", "街道"), _shot("S02", "门店门头画面", beat="落")]
        bts = _classify_boundaries(shots, {"shots": {}})
        assert bts == ["dissolve"]
