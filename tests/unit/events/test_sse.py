"""SSE hub: filters, bounded buffers + gap markers, Last-Event-ID resume without gaps or duplicates, heartbeat."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from foghorn.events.sse import HEARTBEAT, EventFilter, SSEHub, format_frame, stream_frames


def parse(frames: list[str]) -> list[dict[str, Any]]:
    out = []
    for f in frames:
        if f.startswith(":"):
            out.append({"comment": f.strip()})
            continue
        rec: dict[str, Any] = {}
        for line in f.strip("\n").split("\n"):
            k, _, v = line.partition(": ")
            rec[k] = json.loads(v) if k == "data" else v
        out.append(rec)
    return out


def row(
    seq: int, type_: str = "alert.fired", severity: str = "high", component: str = "c1", **kw: Any
) -> dict[str, Any]:
    return {
        "seq": seq,
        "id": f"evt_{seq}",
        "type": type_,
        "severity": severity,
        "component": component,
        "correlation_id": kw.get("corr"),
        "scenario_id": kw.get("scenario"),
    }


async def collect(gen, n: int, timeout: float = 2.0) -> list[str]:  # noqa: ASYNC109
    out: list[str] = []

    async def run() -> None:
        async for f in gen:
            out.append(f)
            if len(out) >= n:
                return

    await asyncio.wait_for(run(), timeout)
    return out


def test_frame_format():
    assert format_frame("foghorn.event", {"a": 1}, event_id=7) == 'id: 7\nevent: foghorn.event\ndata: {"a":1}\n\n'
    assert format_frame("foghorn.clock", {"x": 2}) == 'event: foghorn.clock\ndata: {"x":2}\n\n'
    assert HEARTBEAT.startswith(":") and HEARTBEAT.endswith("\n\n")


def test_filters_apply_to_events_only_and_channels_are_respected():
    hub = SSEHub(10)
    alerts = hub.subscribe(frozenset({"events"}), EventFilter(types=("alert.*",), min_severity="high", component="c1"))
    clocky = hub.subscribe(frozenset({"clock"}))
    hub.publish("events", row(1), seq=1)
    hub.publish("events", row(2, "deploy.finished"), seq=2)
    hub.publish("events", row(3, severity="low"), seq=3)
    hub.publish("events", row(4, component="c2"), seq=4)
    hub.publish("events", row(5, severity="critical"), seq=5)
    hub.publish("clock", {"speed": 1})
    assert [m.seq for m in alerts.buf] == [1, 5] and [m.channel for m in clocky.buf] == ["clock"]
    by_corr = hub.subscribe(frozenset({"events"}), EventFilter(correlation_id="corr_1", scenario_id="S1"))
    hub.publish("events", row(6, corr="corr_1", scenario="S1"), seq=6)
    hub.publish("events", row(7, corr="corr_2", scenario="S1"), seq=7)
    assert [m.seq for m in by_corr.buf] == [6]


def test_bounded_buffer_drops_oldest_and_remembers_the_missed_range():
    hub = SSEHub(3)
    sub = hub.subscribe(frozenset({"events"}))
    for i in range(1, 8):
        hub.publish("events", row(i), seq=i)
    assert [m.seq for m in sub.buf] == [5, 6, 7] and (sub.missed_from, sub.missed_to) == (1, 4) and sub.dropped == 4


async def test_slow_client_gets_a_gap_marker_before_the_surviving_events():
    hub = SSEHub(3)
    sub = hub.subscribe(frozenset({"events"}))
    for i in range(1, 8):
        hub.publish("events", row(i), seq=i)
    frames = parse(await collect(stream_frames(hub, sub, hello={"latest_seq": 7}), 5))
    assert frames[0]["event"] == "foghorn.hello"
    assert frames[1]["event"] == "foghorn.gap" and frames[1]["data"] == {"missed_from": 1, "missed_to": 4}
    assert [f["id"] for f in frames[2:]] == ["5", "6", "7"]


async def test_reconnect_with_last_event_id_replays_from_the_store_with_no_gaps_and_no_duplicates():
    hub = SSEHub(100)
    stored = [row(i) for i in range(1, 11)]
    calls = []

    async def backlog(after: int, limit: int):
        calls.append((after, limit))
        rows = [r for r in stored if r["seq"] > after][:limit]
        if (
            after == 4
        ):  # while the backlog is being read, live events 9..12 are published (9 and 10 are ALSO in the store)
            for i in (9, 10, 11, 12):
                hub.publish("events", row(i), seq=i)
        return rows

    sub = hub.subscribe(frozenset({"events"}))
    gen = stream_frames(hub, sub, hello={}, backlog=backlog, since=4, page=3)
    frames = parse(await collect(gen, 1 + 8))  # hello + seq 5..12
    ids = [int(f["id"]) for f in frames if "id" in f]
    assert ids == [5, 6, 7, 8, 9, 10, 11, 12]  # contiguous, each exactly once
    assert [c[0] for c in calls][:3] == [4, 7, 10]  # paged by 3
    assert hub.subscribers == 1
    await gen.aclose()
    assert hub.subscribers == 0  # unsubscribed on disconnect


async def test_live_only_connection_has_no_backlog_and_skips_nothing():
    hub = SSEHub(10)
    sub = hub.subscribe(frozenset({"events", "queue"}))
    gen = stream_frames(hub, sub, hello={"latest_seq": 0})
    task = asyncio.create_task(collect(gen, 4))
    await asyncio.sleep(0)
    hub.publish("events", row(1), seq=1)
    hub.publish("queue", {"depth": {"P0": 0}})
    hub.publish("events", row(2), seq=2)
    frames = parse(await task)
    assert [f.get("event") for f in frames] == ["foghorn.hello", "foghorn.event", "foghorn.queue", "foghorn.event"]
    assert "id" not in frames[2]  # only events carry ids


async def test_heartbeat_comment_when_idle():
    hub = SSEHub(10)
    sub = hub.subscribe(frozenset({"events"}))
    frames = await collect(stream_frames(hub, sub, hello={}, heartbeat_s=0.02), 3)
    assert frames[0].startswith("event: foghorn.hello") and frames[1] == HEARTBEAT and frames[2] == HEARTBEAT


async def test_close_all_ends_streams():
    hub = SSEHub(10)
    sub = hub.subscribe(frozenset({"events"}))
    gen = stream_frames(hub, sub, hello={})
    await collect(gen, 1)
    hub.close_all()
    rest = [f async for f in gen]
    assert rest == [] and hub.subscribers == 0
