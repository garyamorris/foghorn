"""D7 replayable event streams (+ label sidecars): ``stream_quiet_hour``, ``stream_workday``, ``stream_storm``.

A stream interleaves the events of curated scenarios (at their real virtual times, so a replayed alert can be
investigated with the scenario's telemetry) with seeded ambient noise, duplicates, re-deliveries and poison events.
The label sidecar holds one record per event: expected route / triage decision / priority / duplicate-of / poison.
"""

from __future__ import annotations

from typing import Any

from foghorn.data import catalog
from foghorn.data import events as ev
from foghorn.data.triage_rules import expected_route, reference_triage
from foghorn.data.util import rng, to_epoch, to_iso

STREAMS: dict[str, dict[str, Any]] = {
    "stream_quiet_hour": {
        "short": "qh",
        "start": "2026-11-02T02:30:00Z",
        "end": "2026-11-02T03:30:00Z",
        "n": 240,
        "scenarios": ["S04"],
        "storm": False,
        "description": "One quiet night hour with a maintenance window on orders-db: mostly ticks, probes and ledger noise.",
    },
    "stream_workday": {
        "short": "wd",
        "start": "2026-11-05T08:00:00Z",
        "end": "2026-11-05T18:00:00Z",
        "n": 1800,
        "scenarios": ["S10", "S01"],
        "storm": False,
        "description": "A Thursday 08:00-18:00 UTC with two real incidents (S10 at 10:45, S01 at 14:03) hidden in ambient traffic.",
    },
    "stream_storm": {
        "short": "st",
        "start": "2026-11-06T11:30:00Z",
        "end": "2026-11-06T11:55:00Z",
        "n": 650,
        "scenarios": ["S12"],
        "storm": True,
        "description": "A zone partition: 58 distinct alerts re-notified every ~45s, plus duplicates, re-deliveries and 3 poison events.",
    },
}
_SERVICES = [c.id for c in catalog.COMPONENTS.values() if c.kind == "service"]
_TIER2 = ["recommendation-service", "notification-service", "email-provider"]


def _ambient(r: Any, kind: str, ts: int, n: int, short: str, avoid: set[str]) -> list[tuple[int, dict[str, Any]]]:
    """One noise unit -> [(ts, partial envelope kwargs)]."""
    pool = [c for c in _SERVICES if c not in avoid]
    if kind == "deploy":
        comp = pool[r.randrange(len(pool))]
        ver = f"2026.11.{r.randint(1, 8):02d}.{r.randint(1, 5)}"
        pl = {
            "change_id": f"chg-{comp.split('-')[0][:5]}-{n:05d}",
            "version": ver,
            "previous": "previous",
            "actor": "deploybot",
            "summary": f"deploy {ver} ({r.randint(2, 12)} commits)",
        }
        return [
            (
                ts - 200,
                {
                    "type": "deploy.started",
                    "source": "deploybot",
                    "severity": "info",
                    "component": comp,
                    "payload": pl,
                    "dedupe_key": f"deploy:{comp}:{ver}:s",
                },
            ),
            (
                ts,
                {
                    "type": "deploy.finished",
                    "source": "deploybot",
                    "severity": "info",
                    "component": comp,
                    "payload": {**pl, "status": "succeeded"},
                    "dedupe_key": f"deploy:{comp}:{ver}",
                },
            ),
        ]
    if kind in ("flag", "config"):
        comp = pool[r.randrange(len(pool))]
        c = catalog.COMPONENTS[comp]
        key = (c.flags or c.configs or ("config_flag",))[r.randrange(len(c.flags or c.configs or ("config_flag",)))]
        to = r.choice([5, 10, 25, 50, 100, 0])
        t = "flag.changed" if kind == "flag" else "config.changed"
        return [
            (
                ts,
                {
                    "type": t,
                    "source": "feature-flags" if kind == "flag" else "config-service",
                    "severity": "info",
                    "component": comp,
                    "payload": {
                        "change_id": f"cfg-{n:05x}",
                        "flag" if kind == "flag" else "key": key,
                        "from": 0,
                        "to": to,
                        "actor": r.choice(["m.ortiz", "k.tanaka", "l.haddad"]),
                        "summary": f"{key} -> {to}",
                    },
                    "dedupe_key": f"{kind}:{comp}:{key}:{to}",
                },
            )
        ]
    if kind == "probe":
        comp = r.choice(["email-provider", "email-provider", "payment-provider"])
        al = {
            "alert_id": f"al_{short}_{n:05d}",
            "rule": "synthetic_probe_failed",
            "metric": "error_rate",
            "value": round(r.uniform(0.16, 0.4), 3),
            "threshold": 0.15,
            "for_s": 0,
            "labels": {
                "team": catalog.COMPONENTS[comp].team,
                "tier": str(catalog.COMPONENTS[comp].tier),
                "kind": "external",
            },
        }
        return [
            (
                ts,
                {
                    "type": "alert.fired",
                    "source": "alertmanager",
                    "severity": "low",
                    "component": comp,
                    "payload": al,
                    "dedupe_key": f"alert:{comp}:synthetic_probe_failed",
                },
            ),
            (
                ts + 60,
                {
                    "type": "alert.resolved",
                    "source": "alertmanager",
                    "severity": "info",
                    "component": comp,
                    "payload": {**al, "resolved_at": to_iso(ts + 60)},
                    "dedupe_key": f"alert:{comp}:synthetic_probe_failed",
                },
            ),
        ]
    if kind == "low_alert":
        comp = _TIER2[r.randrange(len(_TIER2))]
        slo = catalog.COMPONENTS[comp].slo_p95_ms
        al = {
            "alert_id": f"al_{short}_{n:05d}",
            "rule": "p95_latency_degraded",
            "metric": "p95_ms",
            "value": round(0.75 * slo * r.uniform(1.02, 1.25), 1),
            "threshold": round(0.75 * slo, 1),
            "for_s": 300,
            "labels": {
                "team": catalog.COMPONENTS[comp].team,
                "tier": str(catalog.COMPONENTS[comp].tier),
                "kind": catalog.COMPONENTS[comp].kind,
            },
        }
        return [
            (
                ts,
                {
                    "type": "alert.fired",
                    "source": "alertmanager",
                    "severity": "low",
                    "component": comp,
                    "payload": al,
                    "dedupe_key": f"alert:{comp}:p95_latency_degraded",
                },
            ),
            (
                ts + r.randint(180, 600),
                {
                    "type": "alert.resolved",
                    "source": "alertmanager",
                    "severity": "info",
                    "component": comp,
                    "payload": al,
                    "dedupe_key": f"alert:{comp}:p95_latency_degraded",
                },
            ),
        ]
    if kind == "health":
        comp = _TIER2[r.randrange(2)]
        return [
            (
                ts,
                {
                    "type": "healthcheck.failed",
                    "source": "probe",
                    "severity": "low",
                    "component": comp,
                    "payload": {
                        "alert_id": f"al_{short}_{n:05d}",
                        "rule": "healthcheck_failed",
                        "check": "readiness",
                        "consecutive_failures": 1,
                    },
                    "dedupe_key": f"health:{comp}",
                },
            )
        ]
    if kind == "slo":
        comp = pool[r.randrange(len(pool))]
        return [
            (
                ts,
                {
                    "type": "slo.burn_rate",
                    "source": "alertmanager",
                    "severity": "info",
                    "component": comp,
                    "payload": {
                        "alert_id": f"al_{short}_{n:05d}",
                        "rule": "slo_burn_rate_fast",
                        "burn_rate": round(r.uniform(0.4, 1.6), 2),
                        "window": "1h",
                    },
                    "dedupe_key": f"slo:{comp}:{n}",
                },
            )
        ]
    if kind == "report":
        comp = pool[r.randrange(len(pool))]
        text = r.choice(
            [
                "Coupon code SAVE10 not accepted on my order.",
                "Address autocomplete suggests the wrong street.",
                "App shows an old price for oat milk.",
                "Can't change my delivery slot, button is greyed out.",
                "Order history missing last week's order.",
            ]
        )
        return [
            (
                ts,
                {
                    "type": "customer.report",
                    "source": "support-desk",
                    "severity": "low",
                    "component": comp,
                    "payload": {
                        "ticket_id": f"CS-{short}-{n:05d}",
                        "channel": r.choice(["chat", "email", "phone"]),
                        "text": text,
                        "customer_tier": "standard",
                        "order_ref": None,
                    },
                    "dedupe_key": f"report:{short}:{n}",
                },
            )
        ]
    raise ValueError(kind)


def assemble(name: str, seed: int, worlds: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cfg = STREAMS[name]
    short = cfg["short"]
    r = rng(seed, "stream", name)
    t_start, t_end = to_epoch(cfg["start"]), to_epoch(cfg["end"])
    items: list[tuple[int, int, dict[str, Any], dict[str, Any]]] = []  # (ts, tiebreak, event kwargs, extra label info)
    tie = 0

    def add(ts: int, kw: dict[str, Any], **extra: Any) -> None:
        nonlocal tie
        tie += 1
        items.append((ts, tie, kw, extra))

    avoid: set[str] = set()
    scenario_alerts: list[tuple[int, dict[str, Any]]] = []
    for sid in cfg["scenarios"]:
        w = worlds[sid]
        avoid |= set(w.truth["affected"])
        for e in w.events:
            ts = to_epoch(e["occurred_at"])
            if not (t_start <= ts < t_end):
                continue
            kw = {
                "type": e["type"],
                "source": e["source"],
                "severity": e["severity"],
                "component": e["subject"]["component"],
                "payload": e["payload"],
                "dedupe_key": e["dedupe_key"],
                "scenario_id": sid,
            }
            add(ts, kw)
            if e["type"] == "alert.fired":
                scenario_alerts.append((ts, kw))

    dup_count = 0
    if cfg["storm"]:  # re-notifications every ~45 s while the alert is firing (alertmanager repeat_interval)
        for ts, kw in sorted(scenario_alerts, key=lambda x: (x[0], x[1]["dedupe_key"])):
            for k in range(1, r.randint(8, 12)):
                t2 = ts + k * 45 + r.randint(-6, 6)
                if t2 < t_end:
                    add(t2, dict(kw), dup_of_key=kw["dedupe_key"])
                    dup_count += 1
    poison = 3 if cfg["storm"] else 0
    # periodic cron ticks
    tick_every = 600
    for i, ts in enumerate(range(t_start, t_end, tick_every)):
        job = "shift_handoff" if (ts % 28800 == 0) else ("retention_sweep" if i % 3 == 0 else "heartbeat")
        add(
            ts,
            {
                "type": "system.cron.tick",
                "source": "cron",
                "severity": "info",
                "component": None,
                "payload": {"job": job, "tick": i},
                "dedupe_key": f"cron:{job}:{i}",
            },
        )
    # maintenance windows in the long stream
    if name == "stream_workday":
        s0 = to_epoch("2026-11-05T12:00:00Z")
        for comp, off in (("sessions-cache", 0),):
            body = {
                "change_id": f"mnt-wd-{off:03d}",
                "actor": "dba-oncall",
                "summary": f"scheduled maintenance on {comp}: key rebalancing",
                "starts_at": to_iso(s0),
                "ends_at": to_iso(s0 + 1200),
                "reason": "key rebalancing",
            }
            add(
                s0,
                {
                    "type": "maintenance.window.started",
                    "source": "calendar",
                    "severity": "info",
                    "component": comp,
                    "payload": body,
                    "dedupe_key": f"maint:{comp}:{s0}",
                },
            )
            add(
                s0 + 1200,
                {
                    "type": "maintenance.window.ended",
                    "source": "calendar",
                    "severity": "info",
                    "component": comp,
                    "payload": body,
                    "dedupe_key": f"maint:{comp}:{s0}:end",
                },
            )
            slo = catalog.COMPONENTS[comp].slo_p95_ms
            for k in range(3):
                t2 = s0 + 240 + k * 200
                al = {
                    "alert_id": f"al_{short}_m{k}",
                    "rule": "p95_latency_high",
                    "metric": "p95_ms",
                    "value": round(slo * 1.5, 1),
                    "threshold": float(slo),
                    "for_s": 180,
                    "labels": {"team": "data-infra", "tier": "0", "kind": "datastore"},
                }
                add(
                    t2,
                    {
                        "type": "alert.fired",
                        "source": "alertmanager",
                        "severity": "high",
                        "component": comp,
                        "payload": al,
                        "dedupe_key": f"alert:{comp}:p95_latency_high:m{k}",
                    },
                )

    fixed = len(items) + poison
    target_noise = max(cfg["n"] - fixed, 0)
    kinds = ["deploy", "flag", "config", "probe", "low_alert", "health", "slo", "report"]
    weights = [6, 5, 3, 9, 8, 6, 12, 3] if not cfg["storm"] else [2, 2, 2, 3, 0, 0, 0, 0]
    produced = 0
    n_unit = 0
    pad_kinds = ["slo", "report", "health"]
    while produced < target_noise:
        n_unit += 1
        remaining = target_noise - produced
        kind = r.choices(kinds, weights)[0]
        ts = r.randrange(t_start + 30, t_end - 30)
        unit = _ambient(r, kind, ts, n_unit, short, avoid)
        if len(unit) > remaining:
            kind = pad_kinds[n_unit % len(pad_kinds)]
            unit = _ambient(r, kind, ts, n_unit, short, avoid)
        for uts, kw in unit:
            if uts < t_start or uts >= t_end:
                uts = min(max(uts, t_start), t_end - 1)
            add(uts, kw)
        produced += len(unit)

    # poison events (storm only)
    poison_specs: list[tuple[int, dict[str, Any], str]] = []
    if poison:
        base = t_start + 600
        poison_specs = [
            (
                base + 77,
                {
                    "raw": {
                        "schema_version": "foghorn.event/1",
                        "type": None,
                        "source": "alertmanager",
                        "severity": "high",
                        "payload": {"note": "malformed: missing type"},
                    }
                },
                "malformed",
            ),
            (
                base + 301,
                {
                    "raw": {
                        "schema_version": "foghorn.event/1",
                        "type": "alert.fired",
                        "source": "alertmanager",
                        "severity": "catastrophic",
                        "payload": {"note": "malformed: unknown severity"},
                    }
                },
                "malformed",
            ),
            (
                base + 523,
                {
                    "type": "alert.fired",
                    "source": "alertmanager",
                    "severity": "medium",
                    "component": "user-profile",
                    "payload": {
                        "alert_id": f"al_{short}_poison",
                        "rule": "error_rate_high",
                        "value": 0.03,
                        "threshold": 0.02,
                        "for_s": 180,
                    },
                    "dedupe_key": "alert:user-profile:error_rate_high:poison",
                    "attributes": {"chaos": "poison"},
                },
                "handler",
            ),
        ]
        for ts, kw, kind in poison_specs:
            add(ts, kw, poison=kind)

    items.sort(key=lambda x: (x[0], x[1]))
    out_events: list[dict[str, Any]] = []
    labels: list[dict[str, Any]] = []
    recent: dict[str, tuple[int, str, str | None]] = {}
    windows: list[dict[str, str]] = []
    incidents: list[dict[str, Any]] = []
    inc_n = 0
    alert_times: list[tuple[int, str]] = []
    for seq, (ts, _tie, kw, extra) in enumerate(items, 1):
        eid = f"evt_{short}_{seq:04d}"
        if "raw" in kw:
            e = {
                **kw["raw"],
                "id": eid,
                "occurred_at": to_iso(ts),
                "subject": {"component": None, "environment": "prod"},
                "trace": {"stream": name, "seq": seq, "scenario_id": None},
            }
            out_events.append(e)
            labels.append(
                {
                    "id": eid,
                    "expected_route": "none",
                    "expected_decision": None,
                    "expected_priority": "P3",
                    "dup_of": None,
                    "poison": True,
                    "poison_kind": extra["poison"],
                    "storm": False,
                }
            )
            continue
        e = ev.envelope(
            id=eid,
            type=kw["type"],
            source=kw["source"],
            severity=kw["severity"],
            occurred_at=to_iso(ts),
            component=kw["component"],
            payload=kw["payload"],
            dedupe_key=kw["dedupe_key"],
            scenario_id=kw.get("scenario_id"),
            stream=name,
            seq=seq,
            attributes=kw.get("attributes"),
        )
        out_events.append(e)
        route = expected_route(e)
        lab: dict[str, Any] = {
            "id": eid,
            "expected_route": route,
            "expected_decision": None,
            "expected_priority": None,
            "dup_of": None,
            "poison": bool(extra.get("poison")),
            "storm": False,
        }
        if extra.get("poison"):
            lab["poison_kind"] = extra["poison"]
        if e["type"] == "maintenance.window.started":
            windows.append(
                {
                    "component": e["subject"]["component"],
                    "start": e["payload"]["starts_at"],
                    "end": e["payload"]["ends_at"],
                }
            )
        if route == "triage":
            incidents = [i for i in incidents if i["until"] > ts]
            ctx = {
                "open_incidents": [
                    {"id": i["id"], "components": i["components"], "severity": i["severity"]} for i in incidents
                ],
                "maintenance_windows": windows,
                "recent_alert_keys": [
                    {"key": k, "age_s": ts - t, "event_id": i, "component": c}
                    for k, (t, i, c) in recent.items()
                    if ts - t < 900
                ],
            }
            exp = reference_triage(e, ctx)
            lab["expected_decision"] = exp["decision"]
            lab["expected_priority"] = exp["priority"]
            lab["dup_of"] = exp["dedupe_of"]
            if exp["decision"] == "open_incident":
                inc_n += 1
                incidents.append(
                    {
                        "id": f"INC-{short.upper()}{inc_n:03d}",
                        "components": [e["subject"]["component"]],
                        "severity": exp["severity"],
                        "until": ts + 3600,
                    }
                )
            if exp["attach_to"]:
                lab["attach_to"] = exp["attach_to"]
            if e["type"] == "alert.fired":
                alert_times.append((ts, e["dedupe_key"] or ""))
                recent_window = {k for t, k in alert_times if ts - t <= 60}
                if len(recent_window) >= 8:
                    lab["storm"] = True
                    lab["expected_route"] = "storm"
            if e["dedupe_key"]:  # sliding window: every occurrence extends last_seen, dup_of points at the first one
                first = (
                    recent[e["dedupe_key"]][1] if (e["dedupe_key"] in recent and exp["decision"] == "dedupe") else eid
                )
                recent[e["dedupe_key"]] = (ts, first, e["subject"]["component"])
        else:
            lab["expected_priority"] = {"critical": "P0", "high": "P1", "medium": "P2"}.get(e["severity"], "P3")
        labels.append(lab)
    return out_events, labels


def to_jsonl(rows: list[dict[str, Any]]) -> str:
    import json

    return "".join(json.dumps(r, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n" for r in rows)
