"""画布事件总线（SSE）——外部 AI 调用平台函数时，把状态变化实时推给 UI。

用法：
  from shipin_platform.graph import events
  events.publish(gid, {"type": "node", "node_id": "...", "status": "..."})

api 层通过 ``stream(gid)`` 供 EventSource 消费。事件只在进程内存中
保留最近 50 条，重连时先补发历史，避免丢状态。
"""
from __future__ import annotations

import asyncio
import itertools
import time
from collections import defaultdict, deque

_queues: dict[str, set[asyncio.Queue]] = defaultdict(set)
_history: dict[str, deque[dict]] = defaultdict(lambda: deque(maxlen=50))
_seq = itertools.count(1)


def publish(graph_id: str, event: dict) -> None:
    """进程内广播一条事件（线程安全：用 loop.call_soon_threadsafe 兜底）。"""
    ev = {"seq": next(_seq), "ts": time.time(), **event}
    gid = str(graph_id)
    _history[gid].append(ev)
    qs = list(_queues.get(gid, ()))
    if not qs:
        return
    for q in qs:
        try:
            q.put_nowait(ev)
        except asyncio.QueueFull:
            # 消费端落后太多，丢弃（客户端靠历史补发兜底）
            pass


class GraphEventStream:
    """一次 SSE 订阅：先补发历史，再持续收新事件。"""

    def __init__(self, graph_id: str):
        self.gid = str(graph_id)
        self._q: asyncio.Queue = asyncio.Queue(maxsize=256)

    async def __aenter__(self):
        _queues[self.gid].add(self._q)
        return self

    async def __aexit__(self, *exc):
        _queues[self.gid].discard(self._q)

    async def iter_events(self, last_seq: int = 0) -> dict:
        # 补发历史（订阅前已发生、未见过的事件）
        for ev in _history[self.gid]:
            if ev["seq"] > last_seq:
                yield ev
        # 持续新事件；空队列时等 15s 心跳
        while True:
            try:
                ev = await asyncio.wait_for(self._q.get(), timeout=15)
                yield ev
            except asyncio.TimeoutError:
                yield {"type": "heartbeat", "seq": next(_seq)}