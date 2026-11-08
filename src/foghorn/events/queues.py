"""Four bounded in-memory priority queues (P0-P3) with weighted-fair draining, aging, backpressure and shedding.

* The SQLite ``dispatches`` table is the durable record; these queues only order work. On start the dispatcher
  re-enqueues every ``queued`` dispatch, so a crash loses nothing.
* Draining is weighted round-robin 8:4:2:1 (configurable) so P3 is never starved by P0 and P0 still dominates.
  An item that has waited longer than ``aging_s`` is promoted one class at a time.
* Backpressure has hysteresis: active at ``utilization >= high_water``, cleared at ``<= low_water``. While active the
  pipeline answers 429 for work that would be P2/P3 and replay/ambient sources pause themselves.
* Shedding is the last resort (hard cap): the OLDEST item of the LOWEST-priority non-empty class at or below the
  incoming item's class goes first, and P0 is never shed while any lower class still holds work. Every shed item is
  returned to the caller so it can be recorded (``event.shed``) - never silent.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from foghorn.events.config import PRIORITIES


@dataclass
class QItem:
    dispatch_id: str
    event_id: str
    priority: str
    graph_id: str
    thread_id: str | None
    enqueued: float = field(default_factory=time.monotonic)
    attempt_mode: str | None = None  # "resume" / "start" override used by retries

    @property
    def cls(self) -> int:
        return PRIORITIES.index(self.priority)


class PriorityQueues:
    def __init__(
        self,
        caps: dict[str, int],
        *,
        weights: tuple[int, int, int, int] = (8, 4, 2, 1),
        aging_s: float = 30.0,
        high_water: float = 0.8,
        low_water: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.caps = [int(caps[p]) for p in PRIORITIES]
        self.weights = list(weights)
        self.aging_s = aging_s
        self.high = high_water
        self.low = low_water
        self._clock = clock
        self._q: list[deque[QItem]] = [deque() for _ in PRIORITIES]
        self._credits = list(self.weights)
        self._active = False
        self.clear_event = asyncio.Event()
        self.clear_event.set()
        self.shed_total = 0
        self.promoted_total = 0
        self.enqueued_total = 0

    # --- introspection -----------------------------------------------------------------------------------------
    @property
    def total_cap(self) -> int:
        return sum(self.caps)

    @property
    def total_depth(self) -> int:
        return sum(len(q) for q in self._q)

    @property
    def utilization(self) -> float:
        return self.total_depth / self.total_cap

    @property
    def bp_active(self) -> bool:
        return self._active

    def depth(self) -> dict[str, int]:
        return {p: len(self._q[i]) for i, p in enumerate(PRIORITIES)}

    def oldest_wait_s(self) -> float:
        waits = [self._clock() - q[0].enqueued for q in self._q if q]
        return max(waits) if waits else 0.0

    def items(self) -> list[QItem]:
        return [i for q in self._q for i in q]

    def would_exceed(self, priority: str) -> bool:
        """True when a new item of this class should be refused at ingress (429): above high-water and P2/P3."""
        return self._active and PRIORITIES.index(priority) >= 2

    # --- mutation ------------------------------------------------------------------------------------------------
    def _update_bp(self) -> None:
        u = self.utilization
        if not self._active and u >= self.high:
            self._active = True
            self.clear_event.clear()
        elif self._active and u <= self.low:
            self._active = False
            self.clear_event.set()

    def put(self, item: QItem) -> list[QItem]:
        """Enqueue; returns the items that had to be shed to make room (possibly ``item`` itself)."""
        victims: list[QItem] = []
        cls = item.cls
        if cls >= 2 and len(self._q[cls]) >= self.caps[cls]:  # bounded low classes shed their own oldest
            victims.append(self._q[cls].popleft())
        while self.total_depth >= self.total_cap:
            v = next((c for c in range(3, cls - 1, -1) if self._q[c]), None)
            if v is None:
                victims.append(item)
                self.shed_total += len(victims)
                self._update_bp()
                return victims
            victims.append(self._q[v].popleft())
        self._q[cls].append(item)
        self.enqueued_total += 1
        self.shed_total += len(victims)
        self._update_bp()
        return victims

    def remove(self, dispatch_id: str) -> bool:
        for q in self._q:
            for it in q:
                if it.dispatch_id == dispatch_id:
                    q.remove(it)
                    self._update_bp()
                    return True
        return False

    def _promote(self) -> None:
        now = self._clock()
        for c in (1, 2, 3):
            while self._q[c] and now - self._q[c][0].enqueued >= self.aging_s:
                self._q[c - 1].append(self._q[c].popleft())
                self.promoted_total += 1

    def pop(self, eligible: Callable[[QItem], bool] = lambda _i: True) -> QItem | None:
        """Next eligible item by weighted-fair order, or ``None`` if nothing eligible is waiting."""
        self._promote()
        for attempt in range(2):
            for c in range(4):
                if self._credits[c] <= 0:
                    continue
                for it in self._q[c]:
                    if eligible(it):
                        self._q[c].remove(it)
                        self._credits[c] -= 1
                        self._update_bp()
                        return it
            if attempt == 0:
                self._credits = list(self.weights)  # a full round without a taker: new round of credits
        return None

    def reset(self) -> None:
        for q in self._q:
            q.clear()
        self._credits = list(self.weights)
        self._active = False
        self.clear_event.set()
        self.shed_total = self.promoted_total = self.enqueued_total = 0

    def snapshot(self) -> dict[str, Any]:
        return {
            "depth": self.depth(),
            "caps": dict(zip(PRIORITIES, self.caps, strict=True)),
            "total_depth": self.total_depth,
            "total_cap": self.total_cap,
            "utilization": round(self.utilization, 4),
            "high_water": self.high,
            "low_water": self.low,
            "backpressure": self._active,
            "oldest_wait_s": round(self.oldest_wait_s(), 3),
        }
