"""The virtual clock service: simulated "now" that runs at a configurable speed, can be paused and set.

Everything the simulated world timestamps (``occurred_at``, ``as_of``, cron ticks, dedupe windows) is virtual time.
Real time is only used for ``received_at`` and for pacing. The wall clock is taken from :mod:`foghorn.clock` at call
time so tests can freeze it with ``clock.install(now_fn=...)`` or pass their own ``wall`` function.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from foghorn import clock

MAX_SPEED = 600.0


def wall_now() -> datetime:
    return clock.now()


def to_iso(dt: datetime, *, ms: bool = False) -> str:
    """UTC ISO-8601 with a ``Z`` suffix (seconds, or milliseconds for real-time stamps)."""
    dt = dt.astimezone(UTC)
    return dt.isoformat(timespec="milliseconds" if ms else "seconds").replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 string (``Z`` or offset, with or without fractions) to an aware UTC datetime."""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class VirtualClock:
    """``virtual_now = anchor_virtual + (wall - anchor_wall) * speed`` while running; frozen while paused."""

    def __init__(self, wall: Callable[[], datetime] | None = None, *, start: datetime | None = None) -> None:
        self._wall = wall or wall_now
        self._lock = threading.Lock()
        w = self._wall()
        self._anchor_virtual = (start or w).astimezone(UTC)
        self._anchor_wall = w
        self._speed = 1.0
        self._paused = False

    # --- reads -------------------------------------------------------------------------------------------------
    def now(self) -> datetime:
        with self._lock:
            return self._now_locked()

    def _now_locked(self) -> datetime:
        if self._paused:
            return self._anchor_virtual
        elapsed = (self._wall() - self._anchor_wall).total_seconds()
        return self._anchor_virtual + timedelta(seconds=elapsed * self._speed)

    def now_iso(self) -> str:
        return to_iso(self.now())

    @property
    def speed(self) -> float:
        return self._speed

    @property
    def paused(self) -> bool:
        return self._paused

    # --- writes ------------------------------------------------------------------------------------------------
    def set(
        self, *, virtual_now: datetime | None = None, speed: float | None = None, paused: bool | None = None
    ) -> None:
        if speed is not None and not (0 < speed <= MAX_SPEED):
            raise ValueError(f"speed must be in (0, {MAX_SPEED:g}]")
        with self._lock:
            current = self._now_locked()  # re-anchor so a speed/pause change never jumps the clock
            self._anchor_virtual = (virtual_now or current).astimezone(UTC)
            self._anchor_wall = self._wall()
            if speed is not None:
                self._speed = float(speed)
            if paused is not None:
                self._paused = paused

    def pause(self) -> None:
        self.set(paused=True)

    def resume(self) -> None:
        self.set(paused=False)

    def real_delay(self, virtual_seconds: float) -> float:
        """Real seconds needed for ``virtual_seconds`` to elapse at the current speed."""
        return max(0.0, virtual_seconds) / self._speed

    # --- persistence / views ---------------------------------------------------------------------------------------
    def state(self) -> dict[str, Any]:
        with self._lock:
            return {"virtual_now": to_iso(self._now_locked()), "speed": self._speed, "paused": self._paused}

    def restore(self, state: dict[str, Any]) -> None:
        """Restore a persisted state (virtual time resumes from where it was saved)."""
        vn = state.get("virtual_now")
        self.set(
            virtual_now=parse_iso(vn) if isinstance(vn, str) else None,
            speed=float(state["speed"]) if state.get("speed") else None,
            paused=bool(state.get("paused", False)),
        )

    def snapshot(self, *, replay: dict[str, Any] | None = None) -> dict[str, Any]:
        s = self.state()
        s["wall_now"] = to_iso(self._wall(), ms=True)
        s["mode"] = "replay" if replay and replay.get("state") in {"running", "paused"} else "free"
        s["replay"] = replay if replay and replay.get("state") != "idle" else None
        return s
