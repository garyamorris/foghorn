"""World generation: ``FaultSpec -> World`` (series, changes, logs, alerts, reports, events, truth).

Pipeline (docs/design/03 section 2.3): baseline (diurnal x AR(1) noise) -> fault effects -> propagation up the call
graph -> changes (root + red herrings + ambient) -> logs -> alerts (rules evaluated over the series) -> customer reports
-> events -> truth.  Everything is a pure function of the spec (and its seed).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from foghorn.data import alerts as alerts_mod
from foghorn.data import catalog
from foghorn.data import events as events_mod
from foghorn.data import logs as logs_mod
from foghorn.data import reports as reports_mod
from foghorn.data import truth as truth_mod
from foghorn.data.faults import (
    N_POINTS,
    PRE_MINUTES,
    ChangeSpec,
    Effect,
    FaultSpec,
    Plan,
    apply_effects,
    make_plan,
    propagate,
    validate_spec,
)
from foghorn.data.util import hex_id, rng, stable_int, to_epoch, to_iso

ASSUMED_DETECTION_LAG_S = 30


@dataclass
class World:
    spec: FaultSpec
    t0: int
    times: list[int]
    series: dict[tuple[str, str], list[float]]
    base: dict[tuple[str, str], list[float]]
    logs: list[logs_mod.LogLine]
    changes: list[dict[str, Any]]
    alerts: list[dict[str, Any]]
    reports: list[dict[str, Any]]
    events: list[dict[str, Any]]
    truth: dict[str, Any]
    meta: dict[str, Any]
    effects: list[Effect] = field(default_factory=list)


# --- baseline series ---------------------------------------------------------------------------------------------------
def make_base(seed: int, sid: str, cid: str, metric: str, t0: int, scale: float) -> list[float]:
    c = catalog.COMPONENTS[cid]
    md = catalog.METRICS[metric]
    b = c.base[metric]
    r = rng(seed, sid, "series", cid, metric)
    sd = b * md.rel * scale
    phi = 0.6
    innov = sd * math.sqrt(1 - phi * phi)
    ar = r.gauss(0.0, sd)
    out: list[float] = []
    minute0 = t0 // 60
    amp = md.diurnal
    trend = 0.004 if metric == "disk_pct" else 0.0
    for i in range(N_POINTS):
        ar = phi * ar + r.gauss(0.0, innov)
        d = 1.0 + amp * math.cos(2 * math.pi * (((minute0 + i) % 1440) - 1020) / 1440) if amp else 1.0
        out.append(b * d + ar + trend * i)
    return out


def finalize(values: list[float], metric: str) -> list[float]:
    md = catalog.METRICS[metric]
    lo, hi, prec = md.lo, md.hi, md.prec
    out: list[float] = []
    for v in values:
        if v < lo:
            v = lo
        elif hi is not None and v > hi:
            v = hi
        out.append(float(round(v)) if prec == 0 else round(v, prec))
    return out


# --- herrings, ambient noise ---------------------------------------------------------------------------------------------------
def _change_id(spec: FaultSpec, kind: str, key: str, used: set[str], preset: str | None) -> str:
    if preset:
        used.add(preset)
        return preset
    n = stable_int(spec.seed, spec.scenario_id, kind, key, mod=10000)
    while True:
        short = key.split("-")[0][:5]
        cid = {
            "deploy": f"chg-{short}-{n:04d}",
            "flag_change": f"cfg-{n:04x}",
            "config_change": f"cfg-{n:04x}",
            "migration": f"mig-{n:04d}",
            "cron_job": f"job-{n:04d}",
            "maintenance": f"mnt-{n:04d}",
            "cert_rotation": f"crt-{n:04x}",
        }[kind]
        if cid not in used:
            used.add(cid)
            return cid
        n = (n + 1) % 10000


def _version(r: Any) -> str:
    return f"2026.{r.randint(10, 11)}.{r.randint(1, 8):02d}.{r.randint(1, 5)}"


def make_herrings(
    spec: FaultSpec, plan: Plan, affected_hint: set[str]
) -> tuple[list[ChangeSpec], list[Effect], list[dict[str, Any]]]:
    """Innocent changes near the onset. Returns (changes, effects, info for truth)."""
    changes: list[ChangeSpec] = []
    effects: list[Effect] = []
    info: list[dict[str, Any]] = []
    explicit = spec.params.get("herrings")
    r = rng(spec.seed, spec.scenario_id, "herring")
    specs: list[dict[str, Any]] = []
    if explicit is not None:
        specs = list(explicit)
    elif spec.red_herrings:
        used = set(affected_hint) | {spec.component}
        cands = sorted(
            c.id
            for c in catalog.COMPONENTS.values()
            if c.kind == "service"
            and c.id not in used
            and (catalog.path_distance(spec.component, c.id) in (None, 3, 4, 5))
        )
        r.shuffle(cands)
        for i in range(min(spec.red_herrings, len(cands))):
            specs.append({"component": cands[i], "minutes": -round(r.uniform(2.0, 14.0), 1)})
    for i, h in enumerate(specs):
        comp = h["component"]
        ver = h.get("version") or _version(r)
        mins = float(h.get("minutes", -5.0))
        if h.get("kind") == "metric":  # a suspicious-looking metric excursion with no change behind it
            for ed in h["effects"]:
                effects.append(
                    Effect(
                        comp,
                        ed["metric"],
                        ed.get("mode", "mul"),
                        float(ed["amount"]),
                        float(ed.get("start", mins)),
                        float(ed.get("rise", 2.0)),
                        ed.get("shape", "trap"),
                        float(ed.get("width", 8.0)),
                        "herring",
                        False,
                    )
                )
            info.append(
                {
                    "kind": "metric",
                    "component": comp,
                    "minutes": mins,
                    "ref": f"{comp}/{h['effects'][0]['metric']}",
                    "why": h["why"],
                }
            )
            continue
        cs = ChangeSpec(
            f"herring{i}",
            mins,
            h.get("kind", "deploy"),
            comp,
            h.get("actor", "deploybot"),
            h.get("summary", f"deploy {ver} ({r.randint(3, 11)} commits)"),
            {"version": ver, "previous": h.get("previous", "previous"), **h.get("details", {})},
            "herring",
            h.get("change_id"),
        )
        changes.append(cs)
        for ed in h.get(
            "effects",
            [
                {
                    "metric": "cpu_pct",
                    "mode": "add",
                    "amount": round(r.uniform(6, 10), 1),
                    "start": mins + 1,
                    "rise": 1.5,
                    "shape": "trap",
                    "width": 5.0,
                }
            ],
        ):
            effects.append(
                Effect(
                    comp,
                    ed["metric"],
                    ed.get("mode", "add"),
                    float(ed["amount"]),
                    float(ed.get("start", mins + 1)),
                    float(ed.get("rise", 1.5)),
                    ed.get("shape", "trap"),
                    float(ed.get("width", 5.0)),
                    "herring",
                    False,
                )
            )
        info.append(
            {
                "kind": "change",
                "component": comp,
                "minutes": mins,
                "why": h.get(
                    "why",
                    f"{comp} {h.get('kind', 'deploy')} {abs(mins):g} min before onset; not on the call path of {spec.component}; its own error and latency metrics are unchanged",
                ),
            }
        )
    return changes, effects, info


def make_ambient(spec: FaultSpec, avoid: set[str]) -> list[ChangeSpec]:
    r = rng(spec.seed, spec.scenario_id, "ambient")
    pool = sorted(c.id for c in catalog.COMPONENTS.values() if c.kind == "service" and c.id not in avoid)
    n = 4 + r.randint(0, 3)
    out: list[ChangeSpec] = []
    for i in range(n):
        comp = pool[r.randrange(len(pool))]
        cm = catalog.COMPONENTS[comp]
        roll = r.random()
        mins = -round(r.uniform(36.0, 88.0), 1) if i < n - 1 else round(r.uniform(12.0, 30.0), 1)
        if roll < 0.6 or not (cm.flags or cm.configs):
            ver = _version(r)
            out.append(
                ChangeSpec(
                    f"amb{i}",
                    mins,
                    "deploy",
                    comp,
                    "deploybot",
                    f"deploy {ver} ({r.randint(2, 12)} commits)",
                    {"version": ver, "previous": "previous"},
                    "ambient",
                )
            )
        elif roll < 0.85 and cm.flags:
            f = cm.flags[r.randrange(len(cm.flags))]
            to = r.choice([5, 10, 25, 100, 0])
            out.append(
                ChangeSpec(
                    f"amb{i}",
                    mins,
                    "flag_change",
                    comp,
                    r.choice(["m.ortiz", "k.tanaka", "l.haddad"]),
                    f"{f} rollout -> {to}%",
                    {"flag": f, "from": 0, "to": to},
                    "ambient",
                )
            )
        else:
            k = (cm.configs or cm.flags)[0]
            out.append(
                ChangeSpec(
                    f"amb{i}",
                    mins,
                    "config_change",
                    comp,
                    r.choice(["r.santos", "k.tanaka"]),
                    f"{k} adjusted",
                    {"key": k, "from": 1, "to": 2},
                    "ambient",
                )
            )
    return out


def make_blips(spec: FaultSpec) -> list[Effect]:
    out: list[Effect] = []
    for cid in ("email-provider", "payment-provider"):
        r = rng(spec.seed, spec.scenario_id, "blip", cid)
        k = r.choice([0, 1, 2, 3]) if cid == "email-provider" else r.choice([0, 0, 1])
        for _ in range(k):
            minute = r.randint(4, PRE_MINUTES + 40)
            out.append(
                Effect(cid, "error_rate", "add", 0.22, float(minute - PRE_MINUTES), 1.0, "pulse", 0.0, "ambient", False)
            )
    return out


# --- builder ----------------------------------------------------------------------------------------------------------------------
def build_world(spec: FaultSpec) -> World:
    validate_spec(spec.category, spec.component)
    seed, sid = spec.seed, spec.scenario_id
    onset = to_epoch(spec.onset)
    t0 = onset - PRE_MINUTES * 60
    t_end = t0 + N_POINTS * 60
    times = [t0 + 60 * i for i in range(N_POINTS)]
    rel = [float(i - PRE_MINUTES) for i in range(N_POINTS)]

    plan = make_plan(spec)
    extra = [
        Effect(
            **{
                **{
                    "mode": "add",
                    "start": 0.0,
                    "rise": 1.0,
                    "shape": "ramp",
                    "width": 0.0,
                    "role": "fault",
                    "propagate": False,
                },
                **e,
            }
        )
        for e in spec.params.get("extra_effects", [])
    ]
    plan.effects += extra
    declared = set(spec.affected) if spec.affected is not None else None
    fault_roots = [e for e in plan.effects if e.role == "fault"]
    prop = propagate(seed, sid, fault_roots, declared)
    fault_effects = fault_roots + prop
    fault_comps = {e.component for e in fault_effects}
    if declared is not None and declared != fault_comps:
        raise ValueError(
            f"{sid}: declared affected {sorted(declared)} != generated {sorted(fault_comps)} "
            f"(missing {sorted(declared - fault_comps)}, extra {sorted(fault_comps - declared)})"
        )
    dist = catalog.transitive_callers(spec.component, sync_only=False)
    affected = sorted(fault_comps, key=lambda x: (x != spec.component, dist.get(x, 99), x))

    h_changes, h_effects, h_info = make_herrings(spec, plan, fault_comps)
    herring_comps = {h["component"] for h in h_info}
    amb_changes = make_ambient(spec, fault_comps | herring_comps)
    blips = make_blips(spec)

    # --- changes ---------------------------------------------------------------------------------------------------
    used: set[str] = set()
    changes: list[dict[str, Any]] = []
    for cs in [*plan.changes, *h_changes, *amb_changes]:
        cid = _change_id(spec, cs.kind, f"{cs.component}-{cs.key}", used, cs.change_id)
        changes.append(
            {
                "change_id": cid,
                "ts": onset + round(cs.minutes * 60),
                "kind": cs.kind,
                "component": cs.component,
                "actor": cs.actor,
                "summary": cs.summary,
                "details": cs.details,
                "role": cs.role,
            }
        )
    changes.sort(key=lambda c: (c["ts"], c["change_id"]))
    root_change = next((c for c in changes if c["role"] == "root"), None)
    for hi in h_info:
        if hi["kind"] == "metric":
            hi["id"] = hi["ref"]
        else:
            hi["id"] = next(
                c["change_id"] for c in changes if c["role"] == "herring" and c["component"] == hi["component"]
            )

    # --- series ----------------------------------------------------------------------------------------------------
    scale = spec.noise_sigma / 0.05
    all_effects = fault_effects + h_effects + blips + [e for e in plan.effects if e.role != "fault"]
    by_series: dict[tuple[str, str], list[Effect]] = {}
    for e in all_effects:
        by_series.setdefault((e.component, e.metric), []).append(e)
    series: dict[tuple[str, str], list[float]] = {}
    base_series: dict[tuple[str, str], list[float]] = {}
    for cid in catalog.COMPONENT_IDS:
        for metric in catalog.COMPONENTS[cid].metrics:
            raw = make_base(seed, sid, cid, metric, t0, scale)
            effs = by_series.get((cid, metric), [])
            if not effs:
                v = finalize(raw, metric)
                series[(cid, metric)], base_series[(cid, metric)] = v, v
                continue
            jit = None
            if any(e.mode == "add" for e in effs):
                jr = rng(seed, sid, "jit", cid, metric)
                jit = [jr.gauss(0.0, 1.0) for _ in range(N_POINTS)]
            nonfault = [e for e in effs if e.role != "fault"]
            base_series[(cid, metric)] = finalize(apply_effects(raw, nonfault, rel, jit), metric)
            series[(cid, metric)] = finalize(apply_effects(raw, effs, rel, jit), metric)

    # --- alerts ------------------------------------------------------------------------------------------------------
    raw_alerts = alerts_mod.evaluate_world(series, times)
    maint = plan.maintenance
    alerts: list[dict[str, Any]] = []
    for n, a in enumerate(raw_alerts, 1):
        comp, ts = str(a["component"]), int(a["fired_at_s"] or 0)
        if (
            plan.is_noise
            and maint
            and comp in fault_comps
            and onset + int(maint[0] * 60) <= ts <= onset + int(maint[1] * 60)
        ):
            label = "noise_maintenance"
        elif comp in fault_comps:
            label = "root" if comp == spec.component else "symptom"
        elif a["rule"] == "synthetic_probe_failed":
            label = "noise_flapping"
        elif comp in herring_comps:
            label = "red_herring"
        else:
            label = "unrelated"
        alerts.append({**a, "alert_id": f"al_{sid}_{n:04d}", "label": label, "fired_at_s": ts})

    # --- logs ---------------------------------------------------------------------------------------------------------
    gap = (
        tuple(dict.fromkeys((spec.component, *spec.telemetry_gap_components)))
        if spec.category == "telemetry_gap"
        else ()
    )
    herring_logs_changes = list(changes)
    logs = logs_mod.generate_logs(spec, t0, fault_effects, affected, herring_logs_changes, root_change, h_info, gap)

    # --- reports ---------------------------------------------------------------------------------------------------------
    reports = reports_mod.generate_reports(seed, sid, spec.category, spec.component, onset, t_end, spec.params)
    for rp in reports:
        rp["component"] = spec.component

    # --- events + trigger ---------------------------------------------------------------------------------------------------
    events = events_mod.build_events(sid, t0, t_end, changes, alerts, reports)
    label_by_alert = {a["alert_id"]: a["label"] for a in alerts}
    trig = next(
        (
            e
            for e in events
            if e["type"] in ("alert.fired", "healthcheck.failed", "slo.burn_rate")
            and label_by_alert.get(e["payload"].get("alert_id")) in ("root", "symptom", "noise_maintenance")
        ),
        None,
    )
    if trig is None:
        trig = next((e for e in events if e["type"] == "customer.report"), None)
    if trig is not None:
        trigger_ts = to_epoch(trig["occurred_at"])
        trigger_id = trig["id"]
    else:
        trigger_ts, trigger_id = onset + 300, None
    as_of = to_iso(max(trigger_ts + ASSUMED_DETECTION_LAG_S, onset + 120))  # the incident starts after the onset sample

    # --- truth ------------------------------------------------------------------------------------------------------------------
    imp = next(
        (x for x in ("checkout-service", "edge-gateway") if x in fault_comps),
        affected[1] if len(affected) > 1 else spec.component,
    )
    short = imp.replace("-service", "").replace("-", "_")
    impact = {
        f"{short}_error_rate_peak": round(max(series[(imp, "error_rate")][PRE_MINUTES:]), 3),
        f"{short}_p95_ms_peak": round(max(series[(imp, "p95_ms")][PRE_MINUTES:]), 1),
    }
    facts = {
        "affected": affected,
        "herrings": h_info,
        "root_change_id": root_change["change_id"] if root_change else None,
        "impact": impact,
        "impact_component": imp,
        "recall": None,
    }
    truth = truth_mod.default_truth(spec, facts)

    meta = {
        "onset": spec.onset,
        "window": [to_iso(t0), to_iso(t_end)],
        "as_of": as_of,
        "trigger_event": trigger_id,
        "n_points": N_POINTS,
        "step_s": 60,
    }
    return World(
        spec, t0, times, series, base_series, logs, changes, alerts, reports, events, truth, meta, fault_effects
    )


# --- summaries used by build/CLI/tests --------------------------------------------------------------------------------------------
def world_digest_payload(w: World) -> dict[str, Any]:
    """Canonical content of a world (hashed into MANIFEST)."""
    return {
        "id": w.spec.scenario_id,
        "series": {
            f"{c}/{m}": [v, w.base[(c, m)] if w.base[(c, m)] is not v else None]
            for (c, m), v in sorted(w.series.items())
        },
        "logs": [[ln.ts, ln.service, ln.level, ln.trace_id, ln.message, ln.kind] for ln in w.logs],
        "changes": w.changes,
        "alerts": [dict(a.items()) for a in w.alerts],
        "reports": w.reports,
        "events": w.events,
        "meta": w.meta,
    }


def seed_key_hex(*keys: object) -> str:
    return hex_id(*keys)
