"""Recovery overlay: remediation as a deterministic function of ``applied_actions`` over the stored (un-remediated) world.

For every applied action that is ACCEPTABLE for the scenario (``truth.remediation.acceptable``) and aimed at the right
target, the affected components' series move from the faulty values toward the fault-free baseline stored next to them::

    value'(t) = value(t) + (baseline(t) - value(t)) * r(t)
    r(t)      = 1 - prod_i (1 - fraction_i * (1 - exp(-(x_i / tau_i) ** 1.5)))        x_i = max(0, t - applied_i - delay_i)

so right-action-on-right-component recovers, a stopgap recovers partially or relapses, and a wrong or misdirected action
leaves the world degraded (the verify step then fails and the commander replans).  Pure function of
(scenario truth, applied_actions): forks and replays are consistent.  Hidden truth is only used inside this module.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from foghorn.data import access
from foghorn.data import actions as act
from foghorn.data.util import to_epoch

_DEFAULT = {"fraction": 1.0, "delay_min": 2.0, "tau_min": 2.5}
_RELAPSE_TAU_S = 360.0


@dataclass(frozen=True)
class Entry:
    action_id: str
    applied_s: int
    fraction: float
    delay_s: float
    tau_s: float
    relapse_s: float | None

    def progress(self, t: float) -> float:
        x = t - self.applied_s - self.delay_s
        if x <= 0:
            return 0.0
        p = self.fraction * (1.0 - math.exp(-((x / self.tau_s) ** 1.5)))
        if self.relapse_s is not None:
            y = (t - self.applied_s - self.relapse_s) / _RELAPSE_TAU_S
            if y > 0:
                p *= 0.0 if y >= 1.0 else 1.0 - y * y * (3.0 - 2.0 * y)
        return p


@dataclass(frozen=True)
class Recovery:
    entries: tuple[Entry, ...]
    scope: frozenset[str]

    def factor(self, t: float) -> float:
        left = 1.0
        for e in self.entries:
            left *= 1.0 - e.progress(t)
        return 1.0 - left

    @property
    def first_applied_s(self) -> int:
        return min(e.applied_s for e in self.entries)


def build(scenario_id: str, actions: tuple[dict[str, Any], ...], as_of_s: int) -> Recovery | None:
    if not actions:
        return None
    t = access.truth(scenario_id)
    rem = t.get("remediation") or {}
    acceptable: list[str] = rem.get("acceptable", [])
    profiles: dict[str, dict[str, Any]] = rem.get("recovery", {})
    entries: list[Entry] = []
    for rec in actions:
        if rec.get("outcome", "executed") != "executed":
            continue
        action, target = str(rec.get("action", "")), str(rec.get("target", "*"))
        applied = rec.get("applied_at")
        if not applied:
            continue
        applied_s = to_epoch(str(applied))
        if applied_s > as_of_s:
            continue  # applied "in the future" of this view (a fork that rewound as_of): not visible yet
        for entry in acceptable:
            if act.action_matches(entry, action, target):
                p = {**_DEFAULT, **profiles.get(entry, {})}
                relapse = p.get("relapse_after_min")
                entries.append(
                    Entry(
                        entry,
                        applied_s,
                        float(p["fraction"]),
                        float(p["delay_min"]) * 60.0,
                        float(p["tau_min"]) * 60.0,
                        float(relapse) * 60.0 if relapse is not None else None,
                    )
                )
                break
    if not entries:
        return None
    return Recovery(tuple(entries), frozenset(t.get("affected", [])))


def apply(values: list[float], base: list[float], times: list[int], rec: Recovery) -> list[float]:
    """Blend ``values`` toward ``base`` by the recovery factor at each time (lengths must match)."""
    out: list[float] = []
    first = rec.first_applied_s
    for v, b, ts in zip(values, base, times, strict=True):
        if ts <= first:
            out.append(v)
            continue
        r = rec.factor(ts)
        out.append(v if r <= 0.0 else v + (b - v) * r)
    return out
