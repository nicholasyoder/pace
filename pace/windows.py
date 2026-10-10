"""Run windows (§9): when the scheduler may start runs.

A window is `"HH:MM-HH:MM"` in the server's local time; the start is inclusive,
the end exclusive, and an end earlier than the start spans midnight. Several
windows combine (any open one is enough); an empty list means always open.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


@dataclass(frozen=True)
class WindowStatus:
    always: bool
    open: bool
    next_open: datetime | None = None
    closes_at: datetime | None = None


def local_now() -> datetime:
    return datetime.now().astimezone()


def parse(spec: str) -> tuple[int, int]:
    """`"22:00-08:00"` → (start, end) in minutes after midnight."""
    m = _RE.match(spec) if isinstance(spec, str) else None
    if not m:
        raise ValueError(f"run window {spec!r} is not HH:MM-HH:MM")
    sh, sm, eh, em = map(int, m.groups())
    if sh > 23 or sm > 59 or em > 59 or eh > 24 or (eh == 24 and em):
        raise ValueError(f"run window {spec!r} has an invalid time")
    start, end = sh * 60 + sm, eh * 60 + em
    if end == 1440:
        end = 0
    if start == end:
        raise ValueError(f"run window {spec!r} starts and ends at the same time")
    return start, end


def _inside(m: int, start: int, end: int) -> bool:
    return start <= m < end if start < end else (m >= start or m < end)


def _open_at(spans: list[tuple[int, int]], t: datetime) -> bool:
    m = t.hour * 60 + t.minute
    return any(_inside(m, s, e) for s, e in spans)


def is_open(windows: list[str], now: datetime | None = None) -> bool:
    if not windows:
        return True
    return _open_at([parse(w) for w in windows], now or local_now())


def status(windows: list[str], now: datetime | None = None) -> WindowStatus:
    if not windows:
        return WindowStatus(always=True, open=True)
    now = now or local_now()
    spans = [parse(w) for w in windows]
    is_open_now = _open_at(spans, now)
    t = now.replace(second=0, microsecond=0)
    for _ in range(1440):
        t += timedelta(minutes=1)
        if _open_at(spans, t) != is_open_now:
            break
    else:  # windows cover the whole day
        return WindowStatus(always=False, open=True)
    if is_open_now:
        return WindowStatus(always=False, open=True, closes_at=t)
    return WindowStatus(always=False, open=False, next_open=t)
