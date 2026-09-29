"""轮73b:ASR 门禁双读(偏置+无偏置)。

真实事故(2026-09-29 咖啡 E2E):VoxCPM 坏 take 无偏置 ASR 读出「音箱断播」,
initial_prompt 偏置后读出期望词、相似度虚高过门,坏音频带到成片,终审
NARRATION_MISMATCH 才抓到。修法:偏置读过关后再用无偏置复核一次。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from shipin_platform.orchestration import pipeline_runner as pr


def _mk(tmp_path, sid, narration, fname="t.mp3"):
    a = tmp_path / fname
    a.write_bytes(b"x")
    return ([{"shot_id": sid, "narration": narration,
              "duration_sec": 3}],
            {"shots": {sid: {"tts": str(a)}}})


def test_bad_take_caught_by_unbiased_reread(tmp_path, monkeypatch):
    """偏置读过关但无偏置读到完全无关内容 → 坏 take,必须 fail-closed。"""
    shots, manifest = _mk(tmp_path, "S03", "推门，迎向暖光", "bad.mp3")

    def fake(video, initial_prompt=""):
        if initial_prompt:
            return [{"text": "推门迎向暖光"}]     # 偏置:虚高过关
        return [{"text": "音箱断播"}]              # 无偏置:坏 take 真相
    monkeypatch.setattr(
        "shipin_platform.review.hard_gates._asr_segments_default", fake)
    bad = pr._tts_asr_check(shots, manifest)
    assert len(bad) == 1 and "坏 take" in bad[0] and "无偏置" in bad[0]


def test_silent_take_caught_by_unbiased_reread(tmp_path, monkeypatch):
    """无偏置读不到任何内容(静音/纯噪声)→ 坏 take。"""
    shots, manifest = _mk(tmp_path, "S01", "加班六点，揉揉眼", "silent.mp3")

    def fake(video, initial_prompt=""):
        if initial_prompt:
            return [{"text": "加班六点揉揉眼"}]
        return [{"text": ""}]
    monkeypatch.setattr(
        "shipin_platform.review.hard_gates._asr_segments_default", fake)
    bad = pr._tts_asr_check(shots, manifest)
    assert len(bad) == 1 and "读不到任何内容" in bad[0]


def test_good_take_passes_both_reads(tmp_path, monkeypatch):
    """好 take:偏置与无偏置都读到期望内容 → 放行(不能误杀)。"""
    shots, manifest = _mk(tmp_path, "S02", "来个您慢用", "good.mp3")

    def fake(video, initial_prompt=""):
        if initial_prompt:
            return [{"text": "来个您慢用"}]
        return [{"text": "来嘞您慢用"}]   # ASR 常见近音差异
    monkeypatch.setattr(
        "shipin_platform.review.hard_gates._asr_segments_default", fake)
    assert pr._tts_asr_check(shots, manifest) == []


def test_unbiased_failure_does_not_block(tmp_path, monkeypatch):
    """无偏置调用本身失败:只以偏置结果为准(不回拦)。"""
    shots, manifest = _mk(tmp_path, "S04", "还是老规矩", "u.mp3")
    calls = {"n": 0}

    def fake(video, initial_prompt=""):
        if initial_prompt:
            return [{"text": "还是老规矩"}]
        raise RuntimeError("sidecar down")
    monkeypatch.setattr(
        "shipin_platform.review.hard_gates._asr_segments_default", fake)
    assert pr._tts_asr_check(shots, manifest) == []
