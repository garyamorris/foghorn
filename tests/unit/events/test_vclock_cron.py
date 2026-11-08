"""Virtual clock, cron expressions and the virtual-time scheduler."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from foghorn.events.config import EventsSettings
from foghorn.events.scheduler import CronError, CronExpr, Scheduler
from foghorn.events.vclock import VirtualClock, parse_iso, to_iso
from tests.unit.events.fakes import BASE, FakeWall


def test_iso_helpers_roundtrip_and_accept_offsets():
    assert to_iso(BASE) == "2026-11-05T10:00:00Z"
    assert to_iso(BASE + timedelta(milliseconds=5), ms=True) == "2026-11-05T10:00:00.005Z"
    assert parse_iso("2026-11-05T10:00:00Z") == BASE
    assert parse_iso("2026-11-05T11:00:00+01:00") == BASE
    assert parse_iso("2026-11-05T10:00:00") == BASE  # naive -> UTC


def test_virtual_clock_speed_pause_and_set():
    wall = FakeWall()
    vc = VirtualClock(wall.now)
    assert vc.now() == BASE
    vc.set(speed=60)
    wall.advance(10)
    assert vc.now() == BASE + timedelta(seconds=600)
    vc.pause()
    wall.advance(100)
    assert vc.now() == BASE + timedelta(seconds=600)  # frozen
    vc.set(virtual_now=BASE, paused=False)
    wall.advance(1)
    assert vc.now() == BASE + timedelta(seconds=60)
    assert vc.real_delay(120) == 2.0
    with pytest.raises(ValueError):
        vc.set(speed=601)
    with pytest.raises(ValueError):
        vc.set(speed=0)


def test_virtual_clock_snapshot_and_restore():
    wall = FakeWall()
    vc = VirtualClock(wall.now)
    vc.set(speed=10, paused=True)
    snap = vc.snapshot(replay={"state": "running"})
    assert snap["mode"] == "replay" and snap["paused"] is True and snap["speed"] == 10
    assert vc.snapshot()["mode"] == "free" and vc.snapshot()["replay"] is None
    other = VirtualClock(wall.now)
    other.restore(vc.state())
    assert other.state() == vc.state()


@pytest.mark.parametrize(
    ("expr", "after", "expected"),
    [
        ("*/15 * * * *", "2026-11-05T10:07:00Z", "2026-11-05T10:15:00Z"),
        ("0 */8 * * *", "2026-11-05T10:00:00Z", "2026-11-05T16:00:00Z"),
        ("0 6,14,22 * * *", "2026-11-05T22:00:00Z", "2026-11-06T06:00:00Z"),
        ("30 9 * * 1", "2026-11-05T10:00:00Z", "2026-11-09T09:30:00Z"),  # next Monday
        ("0 0 1 * *", "2026-11-05T10:00:00Z", "2026-12-01T00:00:00Z"),
        ("0 12 * * 0", "2026-11-05T10:00:00Z", "2026-11-08T12:00:00Z"),  # Sunday = 0
        ("0 12 * * 7", "2026-11-05T10:00:00Z", "2026-11-08T12:00:00Z"),  # ... and 7
        ("5-10/5 1 * * *", "2026-11-05T01:05:00Z", "2026-11-05T01:10:00Z"),
        ("0 0 29 2 *", "2026-11-05T10:00:00Z", "2028-02-29T00:00:00Z"),
    ],
)
def test_cron_next_after(expr, after, expected):
    assert to_iso(CronExpr(expr).next_after(parse_iso(after))) == expected


def test_cron_dom_and_dow_both_restricted_is_or():
    # 13th of the month OR a Friday (standard cron semantics)
    c = CronExpr("0 0 13 * 5")
    assert to_iso(c.next_after(parse_iso("2026-11-05T00:00:00Z"))) == "2026-11-06T00:00:00Z"  # Friday
    assert to_iso(c.next_after(parse_iso("2026-11-12T00:00:00Z"))) == "2026-11-13T00:00:00Z"


@pytest.mark.parametrize(
    "bad", ["* * * *", "61 * * * *", "* 24 * * *", "a b c d e", "*/0 * * * *", "5-1 * * * *", "* * * 13 *"]
)
def test_cron_rejects_bad_expressions(bad):
    with pytest.raises(CronError):
        CronExpr(bad)


async def test_scheduler_fires_each_boundary_once_and_survives_clock_jumps(env):
    rt = env.rt
    rt.vclock.set(virtual_now=BASE, speed=1)
    sch = Scheduler(
        es=EventsSettings(cron_jobs={"shift_handoff": "0 */8 * * *"}),
        bus=rt.bus,
        vclock=rt.vclock,
        sleep=env.wall.sleep,
    )
    await sch.start()
    await sch.stop()
    assert await sch.tick() == 0  # nothing due yet
    rt.vclock.set(virtual_now=BASE + timedelta(hours=7))  # crosses 16:00
    assert await sch.tick() == 1
    assert await sch.tick() == 0  # idempotent: no second tick for the same boundary
    rt.vclock.set(virtual_now=BASE + timedelta(days=3))  # a 600x replay can cross many boundaries at once
    n = await sch.tick()
    assert 1 <= n <= Scheduler.MAX_CATCHUP
    ticks = await rt.db.run(rt.db.list_events, types=["system.cron.tick"], order="asc")
    assert ticks[0]["payload"]["job"] == "shift_handoff" and ticks[0]["source"] == "cron"
    assert len({t["idempotency_key"] for t in ticks}) == len(ticks)
    rt.vclock.set(virtual_now=BASE)  # clock moved backwards: re-anchor instead of replaying the future
    assert await sch.tick() == 0


async def test_cron_tick_is_routed_to_shift_handoff(env):
    rt = env.rt
    rt.vclock.set(virtual_now=datetime(2026, 11, 5, 15, 59, 0, tzinfo=UTC), speed=1)
    sch = Scheduler(es=rt.es, bus=rt.bus, vclock=rt.vclock, sleep=env.wall.sleep)
    await sch.start()
    await sch.stop()
    rt.vclock.set(virtual_now=datetime(2026, 11, 5, 16, 1, 0, tzinfo=UTC))
    assert await sch.tick() == 1
    await env.idle()
    runs = env.client.of("runs.create")
    assert [r["assistant_id"] for r in runs] == ["shift_handoff"]
    assert runs[0]["input"]["shift_end"] == "2026-11-05T16:00:00Z" and runs[0]["input"]["window_hours"] == 8
