"""Server-sent events hub: bounded per-subscriber buffers, filters, ``Last-Event-ID`` resume, heartbeat, gap markers.

Hand-rolled on ``StreamingResponse`` (no extra dependency, and the frame format is under our control). Everything here
runs on the event loop; publishers on other threads must hop with ``loop.call_soon_threadsafe`` (the lifecycle sink
does) - the hub itself is deliberately not thread-safe.

Frame:  ``id: <seq>\\nevent: foghorn.event\\ndata: {json}\\n\\n``  (only ``foghorn.event`` carries an ``id``).
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from foghorn.contracts.events import SEVERITY_ORDER

CHANNELS = ("events", "queue", "clock", "dispatches")
CHANNEL_EVENT_NAMES = {
    "events": "foghorn.event",
    "queue": "foghorn.queue",
    "clock": "foghorn.clock",
    "dispatches": "foghorn.dispatch",
}


def format_frame(name: str, data: Any, *, event_id: int | str | None = None) -> str:
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {name}")
    lines.append("data: " + json.dumps(data, separators=(",", ":"), default=str))
    return "\n".join(lines) + "\n\n"


HEARTBEAT = ": heartbeat\n\n"


@dataclass
class EventFilter:
    types: tuple[str, ...] = ()
    min_severity: str | None = None
    component: str | None = None
    correlation_id: str | None = None
    scenario_id: str | None = None

    def matches(self, ev: dict[str, Any]) -> bool:
        if self.types and not any(fnmatch.fnmatchcase(str(ev.get("type")), p) for p in self.types):
            return False
        if self.min_severity and SEVERITY_ORDER.get(str(ev.get("severity")), 0) < SEVERITY_ORDER.get(
            self.min_severity, 0
        ):
            return False
        if self.component and ev.get("component") != self.component:
            return False
        if self.correlation_id and ev.get("correlation_id") != self.correlation_id:
            return False
        return not (self.scenario_id and ev.get("scenario_id") != self.scenario_id)


@dataclass
class Message:
    channel: str
    data: Any
    seq: int | None = None  # set for events-channel messages


@dataclass(eq=False)
class Subscriber:
    channels: frozenset[str]
    filt: EventFilter
    maxsize: int
    buf: deque[Message] = field(default_factory=deque)
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    missed_from: int | None = None
    missed_to: int | None = None
    dropped: int = 0
    closed: bool = False

    def push(self, msg: Message) -> None:
        if len(self.buf) >= self.maxsize:
            old = self.buf.popleft()
            self.dropped += 1
            if old.seq is not None:
                self.missed_from = old.seq if self.missed_from is None else min(self.missed_from, old.seq)
                self.missed_to = old.seq if self.missed_to is None else max(self.missed_to, old.seq)
        self.buf.append(msg)
        self.wake.set()


class SSEHub:
    def __init__(self, buffer: int = 1000) -> None:
        self.buffer = buffer
        self._subs: set[Subscriber] = set()
        self.published = 0

    @property
    def subscribers(self) -> int:
        return len(self._subs)

    def subscribe(self, channels: frozenset[str], filt: EventFilter | None = None) -> Subscriber:
        sub = Subscriber(channels=channels, filt=filt or EventFilter(), maxsize=self.buffer)
        self._subs.add(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        sub.closed = True
        self._subs.discard(sub)
        sub.wake.set()

    def publish(self, channel: str, data: Any, *, seq: int | None = None) -> None:
        self.published += 1
        for sub in list(self._subs):
            if channel not in sub.channels:
                continue
            if channel == "events" and isinstance(data, dict) and not sub.filt.matches(data):
                continue
            sub.push(Message(channel, data, seq))

    def close_all(self) -> None:
        for sub in list(self._subs):
            self.unsubscribe(sub)


EventPager = Callable[[int, int], "asyncio.Future[list[dict[str, Any]]] | Any"]


async def stream_frames(
    hub: SSEHub,
    sub: Subscriber,
    *,
    hello: dict[str, Any],
    backlog: Callable[[int, int], Any] | None = None,
    since: int | None = None,
    heartbeat_s: float = 15.0,
    page: int = 500,
) -> AsyncIterator[str]:
    """Yield SSE frames for one subscriber.

    ``backlog(after_seq, limit)`` is an awaitable returning events (already filtered, ascending) with ``seq > after_seq``.
    The subscriber was registered BEFORE this runs, so frames published during the backlog replay are buffered; they are
    de-duplicated by ``seq`` afterwards -> no gaps, no duplicates across a reconnect.
    """
    try:
        yield format_frame("foghorn.hello", hello)
        last_sent = since if since is not None else -1
        if since is not None and backlog is not None and "events" in sub.channels:
            cursor = since
            while True:
                rows = await backlog(cursor, page)
                if not rows:
                    break
                for row in rows:
                    cursor = max(cursor, int(row["seq"]))
                    yield format_frame("foghorn.event", row, event_id=row["seq"])
                last_sent = max(last_sent, cursor)
                if len(rows) < page:
                    break
        while not sub.closed:
            if sub.missed_from is not None:
                yield format_frame("foghorn.gap", {"missed_from": sub.missed_from, "missed_to": sub.missed_to})
                sub.missed_from = sub.missed_to = None
            if not sub.buf:
                sub.wake.clear()
                try:
                    await asyncio.wait_for(sub.wake.wait(), timeout=heartbeat_s)
                except TimeoutError:
                    yield HEARTBEAT
                continue
            msg = sub.buf.popleft()
            if msg.channel == "events":
                if msg.seq is not None:
                    if msg.seq <= last_sent:
                        continue
                    last_sent = msg.seq
                yield format_frame(CHANNEL_EVENT_NAMES["events"], msg.data, event_id=msg.seq)
            else:
                yield format_frame(CHANNEL_EVENT_NAMES.get(msg.channel, f"foghorn.{msg.channel}"), msg.data)
    finally:
        hub.unsubscribe(sub)
