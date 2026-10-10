"""Run windows (§9): parsing, open/closed, and when the state next changes."""

from datetime import datetime, timedelta, timezone

import pytest

from pace import windows as w

TZ = timezone(timedelta(hours=2))


def at(hh, mm=0, day=10):
    return datetime(2026, 10, day, hh, mm, 30, tzinfo=TZ)


def test_parse():
    assert w.parse("22:00-08:00") == (1320, 480)
    assert w.parse(" 9:30 - 17:00 ") == (570, 1020)
    assert w.parse("20:00-24:00") == (1200, 0)
    for bad in ("25:00-01:00", "22:00", "08:00-08:00", "00:00-24:00", "10:60-11:00",
                "10:00-24:30", "", "ten-eleven"):
        with pytest.raises(ValueError):
            w.parse(bad)


def test_daytime_window():
    ws = ["09:00-17:00"]
    assert not w.is_open(ws, at(8, 59))
    assert w.is_open(ws, at(9, 0))  # start inclusive
    assert w.is_open(ws, at(16, 59))
    assert not w.is_open(ws, at(17, 0))  # end exclusive


def test_window_spanning_midnight():
    ws = ["22:00-08:00"]
    assert w.is_open(ws, at(23))
    assert w.is_open(ws, at(0))
    assert w.is_open(ws, at(7, 59))
    assert not w.is_open(ws, at(8))
    assert not w.is_open(ws, at(12))
    assert not w.is_open(ws, at(21, 59))
    assert w.is_open(["20:00-24:00"], at(23, 59)) and not w.is_open(["20:00-24:00"], at(0))


def test_empty_is_always_and_several_combine():
    assert w.is_open([], at(12))
    assert w.status([], at(12)) == w.WindowStatus(always=True, open=True)
    ws = ["01:00-02:00", "13:00-14:00"]
    assert w.is_open(ws, at(1, 30)) and w.is_open(ws, at(13, 30))
    assert not w.is_open(ws, at(12))


def test_status_next_open_and_closes_at():
    ws = ["22:00-08:00"]
    s = w.status(ws, at(12))
    assert not s.open and not s.always and s.next_open == at(22).replace(second=0)
    s = w.status(ws, at(23))
    assert s.open and s.closes_at == at(8, day=11).replace(second=0)
    s = w.status(["01:00-02:00"], at(3))  # next opening is tomorrow
    assert s.next_open == at(1, day=11).replace(second=0)
    # adjacent windows close at the end of the last one
    s = w.status(["22:00-02:00", "02:00-08:00"], at(23))
    assert s.closes_at == at(8, day=11).replace(second=0)
    # windows covering the whole day never close
    assert w.status(["00:00-12:00", "12:00-24:00"], at(5)) == w.WindowStatus(False, True)
