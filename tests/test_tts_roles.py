"""TTS 角色配置韧性测试（轮54/九审 P3-11/P3-12）。

覆盖三类降级路径的可诊断性：
- cast_roles 表缺失/库损坏：不得 500（OperationalError 穿透旧代码），
  按无角色配置降级并记录 _roles_error；
- role_code 笔误（大小写/空格/改名）：不得静默换默认音色，
  段上记 voice_fallback（§10.7 同角色同音色的可观测性）；
- 正常解析：无 fallback 标记。
不触 edge-tts 网络（只测 build_segment/角色加载）。
"""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from shipin_platform.services.tts_service import (  # noqa: E402
    create_tts_service, TtsService)


def _cast_db(path: Path, roles=(("hero_male", "zh-CN-YunxiNeural", "-8%"),)):
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE cast_roles(role_code TEXT, edge_voice TEXT,"
                 " rate TEXT)")
    conn.executemany("INSERT INTO cast_roles VALUES (?,?,?)", list(roles))
    conn.commit()
    conn.close()
    return path


def test_missing_cast_roles_table_degrades_not_raise(tmp_path):
    """db 存在但无 cast_roles 表:旧代码 OperationalError 穿透 →
    generate 500 不可诊断。现在按无角色配置降级 + 原因可读。"""
    db = tmp_path / "broken.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE other(x)")
    conn.commit()
    conn.close()
    svc = create_tts_service(tmp_path / "w", db)
    assert svc.list_roles() == []
    assert "cast_roles 加载失败" in svc._roles_error
    # 段仍可构建(默认音色路径)
    seg = svc.build_segment("S01", "你好", role_code="hero_male")
    assert seg.shot_id == "S01"
    assert seg.voice_fallback  # 回落有记录,不静默


def test_missing_db_file_is_clean(tmp_path):
    """db 文件不存在:空角色、无错误标记(正常部署形态)。"""
    svc = create_tts_service(tmp_path / "w", tmp_path / "nope.db")
    assert svc.list_roles() == []
    assert svc._roles_error == ""


def test_known_role_resolves_without_fallback(tmp_path):
    db = _cast_db(tmp_path / "cast.db")
    svc = create_tts_service(tmp_path / "w", db)
    seg = svc.build_segment("S01", "你好", role_code="hero_male")
    assert seg.voice == "zh-CN-YunxiNeural"
    assert seg.rate == "-8%"
    assert seg.voice_fallback == ""


@pytest.mark.parametrize("bad", ["Hero_Male", "hero_male ", " hero_male",
                                 "hero-male"])
def test_role_code_typo_records_fallback(tmp_path, bad):
    """九审 P3-11:拼写笔误旧代码静默回落默认音色——同角色跨镜
    换人无信号。现在段上记 voice_fallback(含具体 role_code)。"""
    db = _cast_db(tmp_path / "cast.db")
    svc = create_tts_service(tmp_path / "w", db)
    seg = svc.build_segment("S01", "你好", role_code=bad)
    assert seg.voice is None  # 未解析到音色
    assert bad in seg.voice_fallback
    assert "回落默认音色" in seg.voice_fallback


def test_explicit_voice_bypasses_role_lookup(tmp_path):
    """显式传 voice 时不查角色表、不记 fallback(覆盖通道照旧)。"""
    db = _cast_db(tmp_path / "cast.db")
    svc = create_tts_service(tmp_path / "w", db)
    seg = svc.build_segment("S01", "你好", voice="zh-CN-XiaoyiNeural")
    assert seg.voice == "zh-CN-XiaoyiNeural"
    assert seg.voice_fallback == ""
