"""The 14 alert rules and their evaluation over metric series.

Alerts are NOT authored: they are produced by running these rules over the generated series (Prometheus semantics:
the rule fires once its condition has held for ``for_s`` seconds; ``for_s = 0`` fires on the first true sample), so
alerts are consistent with the metrics by construction, including benign flapping on the external providers.

The very same ``evaluate_series`` runs in the generator, in the tools' recovery overlay, and in the consistency test.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable
from dataclasses import dataclass

from foghorn.data import catalog
from foghorn.data.faults import BASELINE_POINTS


@dataclass(frozen=True)
class Rule:
    name: str
    metric: str
    op: str  # ">" | "<"
    for_s: int
    event_type: str
    severity: Callable[[catalog.Component], str]
    threshold: Callable[[catalog.Component, float], float]  # (component, baseline median) -> threshold
    applies: Callable[[catalog.Component], bool]
    description: str


def _sev(tier_map: dict[int, str]) -> Callable[[catalog.Component], str]:
    return lambda c: tier_map.get(c.tier, tier_map[max(tier_map)])


def _always(_: catalog.Component) -> bool:
    return True


RULES: tuple[Rule, ...] = (
    Rule(
        "error_rate_high",
        "error_rate",
        ">",
        180,
        "alert.fired",
        _sev({0: "high", 1: "high", 2: "medium"}),
        lambda c, b: 0.02,
        _always,
        "error_rate above 2% for 3 minutes",
    ),
    Rule(
        "error_rate_critical",
        "error_rate",
        ">",
        120,
        "alert.fired",
        _sev({0: "critical"}),
        lambda c, b: 0.10,
        _always,
        "error_rate above 10% for 2 minutes",
    ),
    Rule(
        "p95_latency_high",
        "p95_ms",
        ">",
        180,
        "alert.fired",
        _sev({0: "high", 1: "medium", 2: "medium"}),
        lambda c, b: float(c.slo_p95_ms),
        _always,
        "p95 latency above the SLO for 3 minutes",
    ),
    Rule(
        "p95_latency_degraded",
        "p95_ms",
        ">",
        300,
        "alert.fired",
        _sev({0: "medium", 1: "medium", 2: "low"}),
        lambda c, b: 0.75 * c.slo_p95_ms,
        _always,
        "p95 latency above 75% of the SLO for 5 minutes",
    ),
    Rule(
        "cpu_saturated",
        "cpu_pct",
        ">",
        300,
        "alert.fired",
        _sev({0: "medium"}),
        lambda c, b: 90.0,
        _always,
        "cpu above 90% for 5 minutes",
    ),
    Rule(
        "memory_high",
        "mem_pct",
        ">",
        300,
        "alert.fired",
        _sev({0: "medium"}),
        lambda c, b: 90.0,
        _always,
        "memory above 90% for 5 minutes",
    ),
    Rule(
        "disk_usage_high",
        "disk_pct",
        ">",
        120,
        "alert.fired",
        _sev({0: "high"}),
        lambda c, b: 90.0,
        _always,
        "disk above 90% for 2 minutes",
    ),
    Rule(
        "pool_saturation_high",
        "saturation_pct",
        ">",
        180,
        "alert.fired",
        _sev({0: "high"}),
        lambda c, b: 90.0,
        _always,
        "pool/queue saturation above 90% for 3 minutes",
    ),
    Rule(
        "traffic_drop",
        "rps",
        "<",
        180,
        "alert.fired",
        _sev({0: "high", 1: "medium", 2: "medium"}),
        lambda c, b: 0.5 * b,
        lambda c: c.kind != "external",
        "request rate below 50% of the baseline for 3 minutes",
    ),
    Rule(
        "cache_hit_ratio_low",
        "hit_ratio",
        "<",
        180,
        "alert.fired",
        _sev({0: "high"}),
        lambda c, b: 0.6,
        lambda c: "hit_ratio" in c.base,
        "cache hit ratio below 0.6 for 3 minutes",
    ),
    Rule(
        "consumer_lag_high",
        "consumer_lag",
        ">",
        180,
        "alert.fired",
        _sev({1: "high"}),
        lambda c, b: 3000.0,
        lambda c: "consumer_lag" in c.base,
        "consumer lag above 3000 messages for 3 minutes",
    ),
    Rule(
        "synthetic_probe_failed",
        "error_rate",
        ">",
        0,
        "alert.fired",
        lambda c: "low",
        lambda c, b: 0.15,
        lambda c: c.kind == "external",
        "synthetic probe of an external provider failed",
    ),
    Rule(
        "healthcheck_failed",
        "error_rate",
        ">",
        60,
        "healthcheck.failed",
        lambda c: "critical",
        lambda c, b: 0.5,
        _always,
        "health check failing (error_rate above 50%) for 1 minute",
    ),
    Rule(
        "slo_burn_rate_fast",
        "error_rate",
        ">",
        120,
        "slo.burn_rate",
        lambda c: "medium",
        lambda c, b: max(5 * b, 0.008),
        lambda c: c.tier == 0 and c.kind == "service",
        "error budget burning at 5x baseline for 2 minutes",
    ),
)
RULES_BY_NAME: dict[str, Rule] = {r.name: r for r in RULES}


def baseline_median(values: list[float]) -> float:
    head = values[:BASELINE_POINTS]
    return statistics.median(head) if head else 0.0


def evaluate_series(
    rule: Rule, comp: catalog.Component, values: list[float], times: list[int], base_median: float
) -> list[dict[str, float | int | str | None]]:
    """Run one rule over one series. Returns fired alerts as dicts (value/threshold at the firing sample)."""
    thr = rule.threshold(comp, base_median)
    gt = rule.op == ">"
    n = len(values)
    need = rule.for_s // 60  # additional samples beyond the first that must also be true
    out: list[dict[str, float | int | str | None]] = []
    i = 0
    while i < n:
        cond = values[i] > thr if gt else values[i] < thr
        if not cond:
            i += 1
            continue
        j = i
        while j + 1 < n and (values[j + 1] > thr if gt else values[j + 1] < thr):
            j += 1
        # the condition holds on samples i..j
        if j - i >= need:
            fire_idx = i + need
            resolved = times[j + 1] if j + 1 < n else None
            out.append(
                {
                    "rule": rule.name,
                    "metric": rule.metric,
                    "fired_at_s": times[fire_idx],
                    "resolved_at_s": resolved,
                    "value": values[fire_idx],
                    "threshold": round(thr, 5),
                    "for_s": rule.for_s,
                }
            )
        i = j + 1
    return out


def evaluate_world(
    series: dict[tuple[str, str], list[float]], times: list[int]
) -> list[dict[str, float | int | str | None]]:
    """All rules over all series, sorted by (fired_at, component, rule)."""
    alerts: list[dict[str, float | int | str | None]] = []
    for cid in catalog.COMPONENT_IDS:
        comp = catalog.COMPONENTS[cid]
        for rule in RULES:
            if not rule.applies(comp) or rule.metric not in comp.metrics:
                continue
            vals = series[(cid, rule.metric)]
            for a in evaluate_series(rule, comp, vals, times, baseline_median(vals)):
                a["component"] = cid
                a["severity"] = rule.severity(comp)
                a["event_type"] = rule.event_type
                alerts.append(a)
    alerts.sort(key=lambda a: (a["fired_at_s"], a["component"], a["rule"]))
    return alerts


def rules_public() -> list[dict[str, object]]:
    return [
        {
            "name": r.name,
            "metric": r.metric,
            "op": r.op,
            "for_s": r.for_s,
            "event_type": r.event_type,
            "description": r.description,
        }
        for r in RULES
    ]
