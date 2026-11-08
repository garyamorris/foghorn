"""Fault model: effect profiles, propagation along the call graph, and the 14 category templates.

A scenario is a ``FaultSpec`` (category + root component + difficulty knobs + hand-tuned ``params``).  A template turns it
into a ``Plan``: metric ``Effect``s on the root, changes, log signatures and the customer-report voice.  Effects travel
UP the call graph (to callers) with per-edge lag and attenuation; they are constrained to ``spec.affected`` when that is
declared (curated scenarios), so the declared truth and the generated telemetry cannot disagree.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from foghorn.data import catalog
from foghorn.data.util import hex_id, rng, stable_int

CATEGORIES: tuple[str, ...] = (
    "bad_deploy",
    "cert_expiry",
    "disk_full",
    "planned_maintenance",
    "db_pool_exhaustion",
    "cache_stampede",
    "queue_poison_message",
    "dependency_outage",
    "memory_leak",
    "config_change",
    "slow_query_migration",
    "network_partition",
    "telemetry_gap",
    "runaway_query",
)
PRE_MINUTES = 90  # points before the onset
POST_MINUTES = 45  # points after the onset (exclusive of the onset sample index 90 .. 134)
N_POINTS = PRE_MINUTES + POST_MINUTES  # 135
BASELINE_POINTS = 60  # first hour of the window is the "normal" reference


@dataclass(frozen=True)
class Effect:
    component: str
    metric: str
    mode: str  # mul | add | hold
    amount: float
    start: float  # minutes relative to the onset sample (negative = before)
    rise: float = 1.0
    shape: str = "ramp"  # ramp | trap | saw | decay | pulse | hold
    width: float = 0.0  # trap: plateau minutes; saw: period minutes
    role: str = "fault"  # fault | herring | ambient
    propagate: bool = False


@dataclass(frozen=True)
class ChangeSpec:
    key: str  # stable local key
    minutes: float  # relative to onset (days for old ones are expressed in minutes too)
    kind: str  # deploy | flag_change | config_change | migration | cron_job | maintenance | cert_rotation
    component: str
    actor: str
    summary: str
    details: dict[str, Any] = field(default_factory=dict)
    role: str = "ambient"  # root | herring | ambient
    change_id: str | None = None


@dataclass(frozen=True)
class FaultSpec:
    scenario_id: str
    seed: int
    category: str
    component: str
    onset: str
    tier: int
    title: str
    summary: str
    magnitude: float = 0.7
    red_herrings: int = 0
    noise_sigma: float = 0.05
    evidence_lag_min: int = 2
    signal_strength: float = 0.8
    lookback_days_needed: int = 0
    telemetry_gap_components: tuple[str, ...] = ()
    affected: tuple[str, ...] | None = None
    params: dict[str, Any] = field(default_factory=dict)
    truth_overrides: dict[str, Any] = field(default_factory=dict)

    @property
    def seed_key(self) -> str:
        return self.scenario_id


@dataclass
class Plan:
    effects: list[Effect] = field(default_factory=list)
    changes: list[ChangeSpec] = field(default_factory=list)
    root_change_key: str | None = None
    is_noise: bool = False
    maintenance: tuple[float, float] | None = None  # (start_min, end_min) relative to onset


# --- profiles -----------------------------------------------------------------------------------------------------------
def profile(e: Effect, t: float) -> float:
    """Effect intensity in [0, 1] at ``t`` minutes after the onset sample."""
    x = t - e.start
    if e.shape == "pulse":
        return 1.0 if abs(t - e.start) < 0.5 else 0.0
    if x <= 0:
        return 0.0
    rise = max(e.rise, 0.25)
    if e.shape == "ramp":
        return min(1.0, x / rise)
    if e.shape == "trap":
        up = min(1.0, x / rise)
        if x <= rise + e.width:
            return up
        return max(0.0, 1.0 - (x - rise - e.width) / rise)
    if e.shape == "saw":
        base = min(1.0, x / rise)
        period = max(e.width, 1.0)
        return base * (1.0 - 0.05 * ((x % period) / period))
    if e.shape == "decay":
        return math.exp(-x / rise)
    return 0.0


def apply_effects(
    base: list[float],
    effects: list[Effect],
    rel: list[float],
    jitter: list[float] | None,
) -> list[float]:
    """Apply effects to a base series. ``mul`` first, then ``add``, then ``hold`` (flatline)."""
    if not effects:
        return list(base)
    v = list(base)
    n = len(v)
    for e in (x for x in effects if x.mode == "mul"):
        amt = e.amount
        for i in range(n):
            f = profile(e, rel[i])
            if f:
                v[i] *= 1.0 + amt * f
    for e in (x for x in effects if x.mode == "add"):
        amt = e.amount
        for i in range(n):
            f = profile(e, rel[i])
            if f:
                j = 1.0 + 0.03 * jitter[i] if jitter else 1.0
                v[i] += amt * f * j
    for e in (x for x in effects if x.mode == "hold"):
        k = next((i for i in range(n) if rel[i] > e.start), n)
        if 0 < k < n:
            held = v[k - 1]
            for i in range(k, n):
                v[i] = held
    return v


# --- propagation --------------------------------------------------------------------------------------------------------
_PROPAGATING = ("error_rate", "p95_ms", "saturation_pct")
_ERR_ATT, _LAT_ATT, _SAT_ATT = 0.8, 0.88, 0.5
_MIN_ERR, _MIN_LAT_MS, _MIN_SAT = 0.0015, 8.0, 3.0


def propagate(seed: int, sid: str, roots: list[Effect], affected: set[str] | None) -> list[Effect]:
    """Derive caller-side effects from ``propagate=True`` root effects (level-by-level up the sync call graph).

    A caller reachable through several callees at the same hop inherits the strongest of them.
    """
    out: list[Effect] = []
    for e in roots:
        if not e.propagate or e.metric not in _PROPAGATING:
            continue
        if e.metric == "p95_ms":
            amount0 = catalog.comp(e.component).base["p95_ms"] * e.amount if e.mode == "mul" else e.amount
        else:
            amount0 = e.amount
        frontier: list[tuple[str, float, float]] = [(e.component, amount0, e.start)]
        seen = {e.component}
        while frontier:
            cands: dict[str, tuple[float, float]] = {}
            for node, amt, start in sorted(frontier):
                for caller in catalog.callers_of(node, sync_only=True):
                    if caller in seen or (affected is not None and caller not in affected):
                        continue
                    w = catalog.EDGE_WEIGHT.get((caller, node), 1.0)
                    if e.metric == "error_rate":
                        a = min(0.9, amt * _ERR_ATT * w)
                        floor = _MIN_ERR
                    elif e.metric == "p95_ms":
                        a = min(3500.0, amt * _LAT_ATT * w)
                        floor = _MIN_LAT_MS
                    else:
                        a = amt * _SAT_ATT * w
                        floor = _MIN_SAT
                    if a < floor:
                        continue
                    lag = rng(seed, sid, "lag", caller, node).choice((1.0, 1.0, 1.5, 2.0))
                    if caller not in cands or a > cands[caller][0]:
                        cands[caller] = (a, start + lag)
            frontier = []
            for caller in sorted(cands):
                a, st = cands[caller]
                seen.add(caller)
                frontier.append((caller, a, st))
                out.append(Effect(caller, e.metric, "add", round(a, 5), st, e.rise, e.shape, e.width, e.role, False))
    return out


# --- category templates ---------------------------------------------------------------------------------------------------
def _m(spec: FaultSpec) -> float:
    return spec.magnitude * (0.55 + 0.45 * spec.signal_strength)


def _err(spec: FaultSpec) -> float:
    return float(spec.params.get("err", round(0.03 + 0.15 * _m(spec), 4)))


def _lat(spec: FaultSpec) -> float:
    return float(spec.params.get("lat", round(1 + 7 * _m(spec), 2)))


def _cid(spec: FaultSpec, kind: str, key: str) -> str:
    n = stable_int(spec.seed, spec.scenario_id, kind, key, mod=10000)
    return {
        "deploy": f"chg-{key.split('-')[0][:5]}-{n:04d}",
        "flag_change": f"cfg-{hex_id(spec.seed, spec.scenario_id, key, n=4)}",
        "config_change": f"cfg-{hex_id(spec.seed, spec.scenario_id, key, n=4)}",
        "migration": f"mig-{n:04d}",
        "cron_job": f"job-{n:04d}",
        "maintenance": f"mnt-{n:04d}",
        "cert_rotation": f"crt-{hex_id(spec.seed, spec.scenario_id, key, n=4)}",
    }[kind]


def _version(spec: FaultSpec, key: str) -> str:
    r = rng(spec.seed, spec.scenario_id, "version", key)
    return f"2026.{r.randint(10, 11)}.{r.randint(1, 8):02d}.{r.randint(1, 5)}"


def make_plan(spec: FaultSpec) -> Plan:
    c, cat, p = spec.component, spec.category, spec.params
    base = catalog.comp(c).base
    plan = Plan()
    eff = plan.effects
    err, lat = _err(spec), _lat(spec)
    m = _m(spec)

    if cat == "bad_deploy":
        ver = p.get("version") or _version(spec, c)
        plan.changes.append(
            ChangeSpec(
                "root",
                p.get("change_minutes", -2.0),
                "deploy",
                c,
                p.get("actor", "deploybot"),
                p.get("change_summary", f"deploy {ver} ({stable_int(spec.seed, c, 'commits', mod=9) + 3} commits)"),
                {"version": ver, "previous": p.get("previous", "previous"), "commits": 7},
                "root",
                p.get("change_id"),
            )
        )
        plan.root_change_key = "root"
        eff += [
            Effect(c, "error_rate", "add", err, 0.0, 1.5, propagate=True),
            Effect(c, "p95_ms", "mul", round(lat * 0.45, 3), 0.0, 2.0, propagate=True),
            Effect(c, "cpu_pct", "add", round(8 + 10 * m, 2), 0.0, 3.0),
        ]
    elif cat == "cert_expiry":
        plan.changes.append(
            ChangeSpec(
                "root",
                p.get("change_minutes", -75.0),
                "cert_rotation",
                c,
                "cert-rotator",
                p.get(
                    "change_summary",
                    f"rotation job for the {c} client certificate FAILED (acme challenge timeout); certificate still valid until onset",
                ),
                {"status": "failed", "expires_at": "at-onset"},
                "root",
                p.get("change_id"),
            )
        )
        plan.root_change_key = "root"
        eff += [
            Effect(c, "error_rate", "add", float(p.get("err", round(0.25 + 0.45 * m, 4))), 0.0, 1.0, propagate=True),
            Effect(c, "p95_ms", "mul", 0.3, 0.0, 1.0),
            Effect(c, "rps", "mul", -0.12, 0.0, 2.0),
        ]
    elif cat == "disk_full":
        lead = float(p.get("lead", 55.0))
        e0 = float(p.get("err_start", -2.0))
        eff += [
            Effect(c, "disk_pct", "add", round(99.6 - base["disk_pct"], 3), -lead, float(p.get("rise", lead))),
            Effect(c, "error_rate", "add", float(p.get("err", round(0.02 + 0.2 * m, 4))), e0, 2.0, propagate=True),
            Effect(c, "p95_ms", "mul", 0.8, e0, 3.0),
        ]
    elif cat == "planned_maintenance":
        start, end = p.get("window", (-5.0, 25.0))
        plan.is_noise = True
        plan.maintenance = (float(start), float(end))
        plan.changes.append(
            ChangeSpec(
                "root",
                float(start),
                "maintenance",
                c,
                p.get("actor", "dba-oncall"),
                p.get("change_summary", f"scheduled maintenance on {c}: minor version upgrade and failover test"),
                {
                    "start_min": float(start),
                    "end_min": float(end),
                    "reason": "minor version upgrade",
                    "suppress_alerts": True,
                },
                "root",
                p.get("change_id"),
            )
        )
        plan.root_change_key = "root"
        width = float(end) - 2.0
        eff += [
            Effect(c, "p95_ms", "mul", float(p.get("lat", 2.5 + 3 * m)), 0.0, 2.0, "trap", width),
            Effect(c, "saturation_pct", "add", 22.0, 0.0, 3.0, "trap", width - 1),
            Effect(c, "error_rate", "add", 0.004, 0.0, 2.0, "trap", width),
            Effect(c, "cpu_pct", "add", 15.0, 0.0, 3.0, "trap", width - 1),
        ]
        for caller in catalog.callers_of(c, sync_only=True)[:1]:
            eff.append(Effect(caller, "p95_ms", "mul", 0.25, 1.0, 2.0, "trap", width - 2))
    elif cat == "db_pool_exhaustion":
        eff += [
            Effect(c, "saturation_pct", "add", round(92.0 - base["saturation_pct"], 2), 0.0, 4.0, propagate=True),
            Effect(c, "p95_ms", "mul", float(p.get("lat", round(1 + 9 * m, 2))), 0.0, 4.0, propagate=True),
            Effect(c, "error_rate", "add", float(p.get("err", round(0.02 + 0.07 * m, 4))), 0.0, 4.0, propagate=True),
            Effect(c, "cpu_pct", "add", 15.0, 0.0, 4.0),
        ]
    elif cat == "cache_stampede":
        key = p.get("config_key", "session_ttl_seconds")
        if "cron" in p:
            plan.changes.append(
                ChangeSpec(
                    "cron",
                    p.get("cron_minutes", -1.0),
                    "cron_job",
                    c,
                    "cron",
                    p["cron"],
                    {"job": "weekly_session_flush", "schedule": "Mon 09:00 UTC"},
                    "root",
                    p.get("change_id"),
                )
            )
            plan.root_change_key = "cron"
        else:
            plan.changes.append(
                ChangeSpec(
                    "root",
                    p.get("change_minutes", -2.0),
                    "config_change",
                    c,
                    p.get("actor", "r.santos"),
                    p.get("change_summary", f"{key} 3600 -> 60"),
                    {"key": key, "from": 3600, "to": 60},
                    "root",
                    p.get("change_id"),
                )
            )
            plan.root_change_key = "root"
        drop = float(p.get("hit_drop", -0.68))
        hit_rise = float(p.get("hit_rise", 1.5))
        eff += [
            Effect(c, "hit_ratio", "mul", drop, 0.0, hit_rise),
            Effect(c, "cpu_pct", "add", round(12 + 20 * m, 2), 0.0, 3.0),
            Effect(c, "p95_ms", "mul", round(1 + 2.2 * m, 2), 0.0, 2.0),
            Effect(c, "saturation_pct", "add", round(15 + 30 * m, 2), 0.0, 3.0),
        ]
        # callers fall back to the database on a miss: their latency multiplies (auth x4 in INC-0087)
        scale = min(1.3, m / 0.55)
        mult = {"auth-service": 3.0, "cart-service": 1.6, "pricing-service": 2.0}
        errs = {"auth-service": 0.03, "cart-service": 0.02, "pricing-service": 0.02}
        for caller in catalog.callers_of(c, sync_only=True):
            if spec.affected is not None and caller not in spec.affected:
                continue
            eff.append(
                Effect(caller, "p95_ms", "mul", round(mult.get(caller, 1.2) * scale, 2), 1.0, 3.0, propagate=True)
            )
            eff.append(Effect(caller, "error_rate", "add", errs.get(caller, 0.012), 1.0, 3.0, propagate=True))
        fb = p.get("fallback_to", "orders-db")
        if fb:
            eff += [
                Effect(fb, "saturation_pct", "add", float(p.get("fb_sat", round(20 + 40 * m, 2))), 0.0, 2.0),
                Effect(fb, "cpu_pct", "add", round(10 + 22 * m, 2), 0.0, 2.0),
                Effect(fb, "p95_ms", "mul", round(0.8 + 1.4 * m, 2), 0.0, 3.0),
            ]
    elif cat == "queue_poison_message":
        consumer = p.get("consumer", "notification-service")
        eff += [
            Effect(c, "consumer_lag", "add", float(p.get("lag", 6000 + 8000 * m)), 0.0, 35.0),
            Effect(consumer, "rps", "mul", -0.85, 0.0, 2.0),
            Effect(consumer, "cpu_pct", "add", 40.0, 0.0, 3.0),
            Effect(consumer, "error_rate", "add", 0.012, 0.0, 3.0),
        ]
    elif cat == "dependency_outage":
        eff += [
            Effect(c, "error_rate", "add", float(p.get("err", round(0.3 + 0.6 * m, 4))), 0.0, 1.5, propagate=True),
            Effect(c, "p95_ms", "mul", float(p.get("lat", round(1 + 5 * m, 2))), 0.0, 1.5, propagate=True),
        ]
    elif cat == "memory_leak":
        days = int(spec.lookback_days_needed or p.get("deploy_days_ago", 3))
        ver = p.get("version") or _version(spec, c)
        plan.changes.append(
            ChangeSpec(
                "root",
                -days * 1440.0 + p.get("change_offset_min", 35.0),
                "deploy",
                c,
                "deploybot",
                p.get("change_summary", f"deploy {ver} (14 commits, includes new result-cache layer)"),
                {"version": ver, "previous": "previous"},
                "root",
                p.get("change_id"),
            )
        )
        plan.root_change_key = "root"
        lead = float(p.get("lead", 75.0))
        eff += [
            Effect(
                c,
                "mem_pct",
                "add",
                round(97.0 - base["mem_pct"], 2),
                -lead,
                float(p.get("leak_minutes", 105.0)),
                "saw",
                12.0,
            ),
            Effect(c, "cpu_pct", "add", 10.0, -10.0, 20.0),
            Effect(c, "p95_ms", "mul", float(p.get("lat", round(1 + 1.6 * m, 2))), -2.0, 14.0, propagate=True),
            Effect(c, "error_rate", "add", float(p.get("err", round(0.02 + 0.04 * m, 4))), 4.0, 12.0, propagate=True),
        ]
    elif cat == "config_change":
        flag = p.get("flag") or (catalog.comp(c).flags or catalog.comp(c).configs or ("config_flag",))[0]
        plan.changes.append(
            ChangeSpec(
                "root",
                p.get("change_minutes", -3.0),
                "flag_change" if p.get("is_flag", True) else "config_change",
                c,
                p.get("actor", "j.okafor"),
                p.get("change_summary", f"{flag} rollout 0% -> 50%"),
                {"flag": flag, "from": 0, "to": 50},
                "root",
                p.get("change_id"),
            )
        )
        plan.root_change_key = "root"
        eff += [
            Effect(c, "p95_ms", "mul", lat, 0.0, 2.0, propagate=True),
            Effect(c, "error_rate", "add", round(err * 0.8, 5), 0.0, 2.0, propagate=True),
            Effect(c, "cpu_pct", "add", 6.0, 0.0, 3.0),
        ]
    elif cat == "slow_query_migration":
        plan.changes.append(
            ChangeSpec(
                "root",
                p.get("change_minutes", -5.0),
                "migration",
                c,
                p.get("actor", "migrator"),
                p.get(
                    "change_summary", "migration 0147_add_fulfilment_notes: new FK orders.fulfilment_id without index"
                ),
                {"migration": "0147_add_fulfilment_notes", "tables": ["orders", "order_lines"]},
                "root",
                p.get("change_id"),
            )
        )
        plan.root_change_key = "root"
        creep = float(p.get("creep_minutes", 22.0))
        eff += [
            Effect(c, "p95_ms", "mul", float(p.get("lat", round(1 + 4.6 * m, 2))), -2.0, creep, propagate=True),
            Effect(c, "saturation_pct", "add", 12.0, -2.0, creep),
            Effect(c, "cpu_pct", "add", 14.0, -2.0, creep),
        ]
    elif cat == "network_partition":
        zone = p.get("zone", "zone-b")
        members = list(catalog.ZONE_B_SERVICES) if zone == "zone-b" else []
        for idx, k in enumerate(members):
            jit = rng(spec.seed, spec.scenario_id, "np", k).uniform(0.0, 1.0)
            ek = float(p.get("err", round(0.16 + 0.22 * m, 4)))
            eff += [
                Effect(
                    k,
                    "error_rate",
                    "add",
                    round(ek * (0.8 + 0.4 * rng(spec.seed, spec.scenario_id, "npe", k).random()), 4),
                    jit,
                    1.0,
                ),
                Effect(k, "rps", "mul", -0.62 if idx < 6 else -0.42, jit, 1.0),
                Effect(k, "saturation_pct", "add", 24.0, jit, 1.5),
                Effect(k, "p95_ms", "mul", round(3.0 + 3.5 * m, 2), jit, 1.0),
            ]
        for shared in catalog.SHARED_TIER:
            eff.append(Effect(shared, "rps", "mul", -0.3, 0.5, 1.0))
    elif cat == "telemetry_gap":
        comps = tuple(dict.fromkeys((c, *spec.telemetry_gap_components)))
        for k in comps:
            for mt in catalog.comp(k).metrics:
                eff.append(Effect(k, mt, "hold", 0.0, 0.0, 1.0, "hold"))
    elif cat == "runaway_query":
        plan.changes.append(
            ChangeSpec(
                "root",
                p.get("change_minutes", -3.0),
                "cron_job",
                c,
                p.get("actor", "bi-batch"),
                p.get(
                    "change_summary",
                    "ad hoc revenue report export started manually (analytics.nightly_export, no LIMIT)",
                ),
                {"job": "analytics.nightly_export", "started_by": "m.ortiz"},
                "root",
                p.get("change_id"),
            )
        )
        plan.root_change_key = "root"
        eff += [
            Effect(c, "cpu_pct", "add", round(93.0 - base["cpu_pct"], 2), 0.0, 2.0),
            Effect(c, "p95_ms", "mul", float(p.get("lat", round(1 + 13 * m, 2))), 0.0, 3.0, propagate=True),
            Effect(c, "saturation_pct", "add", round(92.0 - base["saturation_pct"], 2), 0.0, 3.0, propagate=True),
            Effect(c, "error_rate", "add", float(p.get("err", round(0.02 + 0.05 * m, 4))), 0.0, 3.0, propagate=True),
            Effect(c, "rps", "mul", -0.2, 0.0, 3.0),
        ]
    else:  # pragma: no cover - guarded by validate_spec
        raise ValueError(f"unknown category {cat!r}")
    return plan


ALLOWED_KINDS: dict[str, tuple[str, ...]] = {
    "bad_deploy": ("service",),
    "cert_expiry": ("service",),
    "disk_full": ("service", "datastore", "queue"),
    "planned_maintenance": ("service", "datastore", "queue"),
    "db_pool_exhaustion": ("datastore", "service"),
    "cache_stampede": ("datastore",),
    "queue_poison_message": ("queue",),
    "dependency_outage": ("external",),
    "memory_leak": ("service",),
    "config_change": ("service",),
    "slow_query_migration": ("datastore",),
    "network_partition": ("service",),
    "telemetry_gap": ("service",),
    "runaway_query": ("datastore",),
}


def validate_spec(category: str, component: str) -> None:
    if category not in CATEGORIES:
        raise ValueError(f"unknown fault category {category!r}; choose one of {', '.join(CATEGORIES)}")
    if component not in catalog.COMPONENTS:
        raise ValueError(f"unknown component {component!r}")
    kinds = ALLOWED_KINDS[category]
    if catalog.COMPONENTS[component].kind not in kinds:
        ok = [c.id for c in catalog.COMPONENTS.values() if c.kind in kinds]
        raise ValueError(
            f"category {category!r} cannot target {component!r} ({catalog.COMPONENTS[component].kind}); valid: {', '.join(ok)}"
        )
    if category == "cache_stampede" and component != "sessions-cache":
        raise ValueError("cache_stampede targets sessions-cache")
    if category == "queue_poison_message" and component != "event-bus":
        raise ValueError("queue_poison_message targets event-bus")
