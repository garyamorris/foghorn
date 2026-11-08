"""ToolContext: the three graph-state keys every tool reads (``scenario_id``, ``as_of``, ``applied_actions``).

The keys have the SAME names in graph state, so a node can pass ``state`` straight through as the context and the
LangChain wrappers read them from ``ToolRuntime.state``.  ``as_of`` is virtual time: no tool ever returns data after it.
``applied_actions`` is the append-only list of ``execute_action`` records; the recovery overlay is a pure function of it,
so forks and replays of a thread see a consistent world.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, NotRequired

from typing_extensions import TypedDict

from foghorn.data import access
from foghorn.data.util import to_epoch, to_iso


class ToolContext(TypedDict):
    scenario_id: str
    as_of: NotRequired[str | None]  # ISO virtual time; None => the scenario's default "now"
    applied_actions: NotRequired[list[dict[str, Any]]]


class ToolError(ValueError):
    """Bad arguments to a tool (unknown service/metric/action...). The LangChain wrappers turn it into an error payload."""


def add_actions(left: list[dict[str, Any]] | None, right: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Idempotent reducer for ``applied_actions``: appends records whose ``id`` is new (replays cannot double-apply)."""
    out = list(left or [])
    seen = {r.get("id") for r in out}
    for r in right or []:
        if r.get("id") not in seen:
            out.append(r)
            seen.add(r.get("id"))
    return out


@dataclass(frozen=True)
class Resolved:
    scenario_id: str
    as_of: str
    as_of_s: int
    actions: tuple[dict[str, Any], ...]
    t0: int
    n_points: int

    @property
    def visible_points(self) -> int:
        """How many 1-minute samples are at or before ``as_of`` (the time fence)."""
        if self.as_of_s < self.t0:
            return 0
        return min(self.n_points, (self.as_of_s - self.t0) // 60 + 1)

    @property
    def window_end_s(self) -> int:
        return self.t0 + (self.n_points - 1) * 60


def resolve(ctx: ToolContext | dict[str, Any]) -> Resolved:
    sid = ctx.get("scenario_id")
    if not sid:
        raise ToolError("no scenario selected (ctx['scenario_id'] is empty)")
    rec = access.scenario_record(str(sid))  # raises ScenarioNotFound
    as_of = ctx.get("as_of") or rec["trigger"]["as_of"]
    try:
        as_of_s = to_epoch(str(as_of))
    except ValueError as e:
        raise ToolError(f"as_of {as_of!r} is not an ISO-8601 timestamp") from e
    window = rec["world"]["window"]
    return Resolved(
        str(sid),
        to_iso(as_of_s),
        as_of_s,
        tuple(ctx.get("applied_actions") or ()),
        to_epoch(window[0]),
        int(rec["world"]["n_points"]),
    )


def advance_as_of(ctx: ToolContext | dict[str, Any], minutes: float) -> str:
    """Virtual time ``minutes`` after the context's ``as_of``, clamped to the end of the scenario's telemetry window."""
    r = resolve(ctx)
    return to_iso(min(r.as_of_s + round(minutes * 60), r.window_end_s))
