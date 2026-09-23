"""TTS narration service using edge-tts with multi-role voice casting.

Reads voice assignments from the cast_roles DB and synthesises per-shot
narration audio.  Same role always maps to the same edge-tts voice, as
required by the platform audio rules (§10.7).
"""
from __future__ import annotations

import asyncio
import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class CastRole:
    role_code: str
    edge_voice: str
    rate: str  # e.g. "-8%", "+0%"


@dataclass
class TtsSegment:
    shot_id: str
    text: str
    role_code: Optional[str] = None
    voice: Optional[str] = None
    rate: str = "0%"
    output_path: str = ""
    duration_sec: float = 0.0
    error: str = ""
    voice_fallback: str = ""  # 轮54:role_code 回落默认音色的原因(空=正常解析)


class TtsService:
    """Synthesises narration from a cast_script via edge-tts."""

    def __init__(self, work_dir: Path, db_path: Path):
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._roles: dict[str, CastRole] = {}
        self._roles_error = ""  # 轮54:角色表加载失败的可诊断原因
        self._load_roles()

    def _load_roles(self) -> None:
        # 轮54(九审 P3-12):db 存在但无 cast_roles 表(半成品库/被外部
        # 改过)旧代码让 sqlite3.OperationalError 穿透 create_tts_service
        # → generate 阶段 500 且报错不可诊断。缺表/坏库按「无角色
        # 配置」降级(build_segment 的默认音色路径照常工作),原因落在
        # _roles_error 供调用方呈现。
        if not self.db_path.exists():
            return
        conn = None
        try:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            for row in conn.execute(
                "SELECT role_code, edge_voice, rate FROM cast_roles"
            ):
                self._roles[row["role_code"]] = CastRole(
                    role_code=row["role_code"],
                    edge_voice=row["edge_voice"],
                    rate=row["rate"] or "0%",
                )
        except sqlite3.Error as e:
            self._roles = {}
            self._roles_error = (
                f"cast_roles 加载失败({type(e).__name__}: {e})"
                "——按无角色配置降级(默认音色)")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass

    def get_role(self, role_code: str) -> Optional[CastRole]:
        return self._roles.get(role_code)

    def build_segment(self, shot_id: str, text: str,
                      role_code: Optional[str] = None,
                      voice: Optional[str] = None,
                      rate: str = "-6%") -> TtsSegment:
        """Build a TtsSegment resolving voice from cast_roles DB.

        轮54(九审 P3-11):指定了 role_code 但库里没有(大小写笔误/空格/
        改名)时旧代码静默回落默认音色——同角色跨镜换人无任何信号,
        与 §10.7「同角色同音色」只差一条日志。回落时在段上记
        voice_fallback,调用方(TTS 报告/节点 meta)可 surfaced。"""
        fallback = ""
        if voice is None and role_code:
            role = self._roles.get(role_code)
            if role:
                voice = role.edge_voice
                rate = role.rate or "-6%"
            else:
                fallback = f"role_code {role_code!r} 未在 cast_roles 配置——回落默认音色"
        seg = TtsSegment(shot_id=shot_id, text=text, voice=voice, rate=rate)
        if fallback:
            seg.voice_fallback = fallback
        return seg

    def list_roles(self) -> list[dict]:
        return [
            {
                "role_code": r.role_code,
                "edge_voice": r.edge_voice,
                "rate": r.rate,
            }
            for r in self._roles.values()
        ]

    async def synthesize_segment(self, segment: TtsSegment) -> TtsSegment:
        """Synthesise one narration segment using edge-tts.

        Voice selection priority: segment.voice > cast role > default female.
        """
        import edge_tts  # lazy import to avoid blocking main thread on init

        voice = segment.voice or "zh-CN-XiaoxiaoNeural"
        rate = segment.rate or "0%"
        out = self.work_dir / f"{segment.shot_id}_{uuid.uuid4().hex[:8]}.mp3"

        communicate = edge_tts.Communicate(segment.text, voice, rate=rate)
        await communicate.save(str(out))

        # probe duration
        dur = self._probe_duration(str(out))
        segment.output_path = str(out)
        segment.duration_sec = dur
        return segment

    def synthesize_segments_sync(self, segments: list[TtsSegment]) -> list[TtsSegment]:
        """Run all segments sequentially (blocking); suitable for sync API calls."""
        async def _run():
            tasks = [self.synthesize_segment(s) for s in segments]
            return await asyncio.gather(*tasks, return_exceptions=True)

        results = asyncio.run(_run())
        out = []
        for seg, res in zip(segments, results):
            if isinstance(res, Exception):
                seg.output_path = ""
                seg.duration_sec = 0.0
                seg.error = str(res)
            else:
                seg = res
            out.append(seg)
        return out

    @staticmethod
    def _probe_duration(path: str) -> float:
        try:
            import subprocess
            r = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries",
                 "format=duration", "-of", "csv=p=0", path],
                capture_output=True, text=True, timeout=10,
            )
            return float(r.stdout.strip()) if r.stdout.strip() else 0.0
        except Exception:
            return 0.0


def create_tts_service(work_dir: Path, db_path: Path) -> TtsService:
    return TtsService(work_dir=work_dir, db_path=db_path)
