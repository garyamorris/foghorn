"""GET /api/stream over the real ASGI app (httpx's transport buffers whole bodies, so drive the ASGI callable directly)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit


async def sse(
    api,
    url: str,
    *,
    until: Callable[[str], bool],
    headers: dict[str, str] | None = None,
    during: Callable[[], Any] | None = None,
    timeout: float = 3.0,  # noqa: ASYNC109
):
    parts = urlsplit(url)
    hdrs = [(b"host", b"test"), *[(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": parts.path,
        "raw_path": parts.path.encode(),
        "query_string": parts.query.encode(),
        "headers": hdrs,
        "client": ("127.0.0.1", 5),
        "server": ("test", 80),
        "root_path": "",
    }
    gone = asyncio.Event()
    text = ""
    meta: dict[str, Any] = {}

    async def receive():
        await gone.wait()
        return {"type": "http.disconnect"}

    async def send(msg):
        nonlocal text
        if msg["type"] == "http.response.start":
            meta["status"], meta["headers"] = msg["status"], {k.decode(): v.decode() for k, v in msg["headers"]}
        elif msg["type"] == "http.response.body":
            text += msg.get("body", b"").decode()

    task = asyncio.create_task(api.app(scope, receive, send))
    try:
        if during:
            for _ in range(200):
                if api.rt.hub.subscribers:
                    break
                await asyncio.sleep(0.005)
            await during()
        deadline = asyncio.get_running_loop().time() + timeout
        while not until(text) and asyncio.get_running_loop().time() < deadline and not task.done():  # noqa: ASYNC110
            await asyncio.sleep(0.005)
    finally:
        gone.set()
        await asyncio.wait_for(task, 3)
    return meta, parse(text), text


def parse(text: str) -> list[dict[str, Any]]:
    out = []
    for frame in text.split("\n\n"):
        if not frame.strip():
            continue
        if frame.startswith(":"):
            out.append({"comment": frame})
            continue
        rec: dict[str, Any] = {}
        for line in frame.split("\n"):
            k, _, v = line.partition(": ")
            rec[k] = json.loads(v) if k == "data" else v
        out.append(rec)
    return out


async def fire(api, n: int, start: int = 0) -> None:
    for i in range(start, start + n):
        r = await api.event(type="customer.report", severity="medium", subject={"component": f"c{i}"}, payload={"n": i})
        assert r.status_code == 202


async def test_stream_headers_hello_and_live_events(api):
    meta, frames, _ = await sse(
        api, "/api/stream?channels=events", until=lambda t: t.count("foghorn.event") >= 2, during=lambda: fire(api, 2)
    )
    assert (
        meta["status"] == 200
        and meta["headers"]["content-type"].startswith("text/event-stream")
        and meta["headers"]["cache-control"] == "no-cache"
    )
    assert frames[0]["event"] == "foghorn.hello" and frames[0]["data"]["latest_seq"] == 0
    evs = [f for f in frames if f.get("event") == "foghorn.event"]
    assert (
        [f["id"] for f in evs] == ["1", "2"]
        and evs[0]["data"]["type"] == "customer.report"
        and evs[0]["data"]["dispatches"][0]["graph_id"] == "event_router"
    )


async def test_last_event_id_resume_has_no_gaps_and_no_duplicates(api):
    await fire(api, 5)
    _, frames, _ = await sse(
        api,
        "/api/stream?channels=events",
        headers={"Last-Event-ID": "2"},
        until=lambda t: t.count("foghorn.event") >= 6,
        during=lambda: fire(api, 3, start=5),
    )
    ids = [int(f["id"]) for f in frames if f.get("event") == "foghorn.event"]
    assert ids == [3, 4, 5, 6, 7, 8]  # backlog 3..5 from SQLite, then live 6..8, each exactly once
    assert frames[0]["data"]["resumed_from"] == 2
    _, again, _ = await sse(api, "/api/stream?channels=events&since=7", until=lambda t: t.count("foghorn.event") >= 1)
    assert [f["id"] for f in again if f.get("event") == "foghorn.event"] == ["8"]


async def test_stream_filters_and_channels(api):
    await api.event(type="alert.fired", severity="critical", subject={"component": "a"}, payload={"rule": "r"})
    await api.event(type="customer.report", severity="low", subject={"component": "b"}, payload={})
    await api.event(type="alert.fired", severity="high", subject={"component": "c"}, payload={"rule": "r"})
    _, frames, _ = await sse(
        api,
        "/api/stream?channels=events&since=0&types=alert.*&min_severity=high",
        until=lambda t: t.count("foghorn.event") >= 2,
    )
    assert [f["id"] for f in frames if f.get("event") == "foghorn.event"] == ["1", "3"]
    _, frames, _ = await sse(
        api, "/api/stream?channels=events&since=0&component=b", until=lambda t: t.count("foghorn.event") >= 1
    )
    assert [f["id"] for f in frames if f.get("event") == "foghorn.event"] == ["2"]
    r = await api.get("/api/stream?channels=events,bogus")
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_request"
    assert (await api.get("/api/stream?min_severity=huge")).status_code == 400


async def test_queue_clock_and_dispatch_channels(api):
    async def go():
        api.rt.hub.publish("queue", await api.rt.queue_stats())
        api.rt.hub.publish("clock", api.rt.clock_snapshot())
        await fire(api, 1)

    _, frames, _ = await sse(
        api, "/api/stream?channels=queue,clock,dispatches", until=lambda t: "foghorn.dispatch" in t, during=go
    )
    names = [f.get("event") for f in frames]
    assert {"foghorn.hello", "foghorn.queue", "foghorn.clock", "foghorn.dispatch"} <= set(
        names
    ) and "foghorn.event" not in names
    q = next(f for f in frames if f.get("event") == "foghorn.queue")["data"]
    assert q["depth"] == {"P0": 0, "P1": 0, "P2": 0, "P3": 0}
    d = next(f for f in frames if f.get("event") == "foghorn.dispatch")["data"]
    assert d["graph_id"] == "event_router" and d["status"] == "queued" and "plan" not in d
    assert all(
        "id" not in f for f in frames if f.get("event") in {"foghorn.queue", "foghorn.clock", "foghorn.dispatch"}
    )


async def test_heartbeat_comment_and_unsubscribe_on_disconnect(api):
    api.rt.es.sse_heartbeat_s = 0.02
    _, frames, text = await sse(api, "/api/stream?channels=events", until=lambda t: t.count(": heartbeat") >= 2)
    assert text.count(": heartbeat") >= 2
    await asyncio.sleep(0.02)
    assert api.rt.hub.subscribers == 0  # the generator's finally unsubscribed the client


async def test_dispatch_updates_stream_while_a_run_progresses(api):
    async def go():
        await fire(api, 1)
        await api.idle()

    _, frames, _ = await sse(
        api, "/api/stream?channels=dispatches", until=lambda t: '"status":"succeeded"' in t, during=go
    )
    statuses = [f["data"]["status"] for f in frames if f.get("event") == "foghorn.dispatch"]
    assert statuses[0] == "queued" and "running" in statuses and statuses[-1] == "succeeded"
