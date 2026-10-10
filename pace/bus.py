"""In-process pub/sub from worker threads to SSE subscribers.

Topics: `item:<ID>` (stream lines and state changes for one item) and
`global` (anything that changes the inbox or run list). Publishing is
thread-safe; delivery happens on the event loop. Slow subscribers drop
messages rather than block a run.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any


class Bus:
    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self._subs: dict[str, set[asyncio.Queue]] = {}
        self._lock = threading.Lock()

    def attach(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    def publish(self, topic: str, msg: dict[str, Any]) -> None:
        loop = self.loop
        if loop is None or loop.is_closed():
            return
        with self._lock:
            queues = list(self._subs.get(topic, ()))
        for q in queues:
            loop.call_soon_threadsafe(self._put, q, msg)

    @staticmethod
    def _put(q: asyncio.Queue, msg: dict[str, Any]) -> None:
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            pass

    def open(self, *topics: str) -> asyncio.Queue:
        """Subscribe; read with `await q.get()` (safe to cancel), then `close(q)`."""
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        with self._lock:
            for t in topics:
                self._subs.setdefault(t, set()).add(q)
        return q

    def close(self, q: asyncio.Queue) -> None:
        with self._lock:
            for subs in self._subs.values():
                subs.discard(q)
