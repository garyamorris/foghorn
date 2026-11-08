"""D4 ``past_incidents.jsonl`` (120 records) and the D11 seed memories (prefs + lessons).

Past incidents are sampled from the fault catalogue with seeded variation and are consistent with the runbooks
(D2): every resolution action id resolves. INC-0061 and INC-0087 are hand-authored anchors that scenarios S10 and S16
must recall.
"""

from __future__ import annotations

import itertools
from typing import Any

from foghorn.data import catalog
from foghorn.data.util import rng

N_INCIDENTS = 120
_SERVICES = [c.id for c in catalog.COMPONENTS.values() if c.kind == "service"]

# category -> (components, titles, symptoms, resolution, runbook, lessons, severities, mttr range, fingerprint hint)
_CAT: dict[str, dict[str, Any]] = {
    "bad_deploy": {
        "a": (6, 38),
        "comps": _SERVICES,
        "rb": "RB-001",
        "sev": ["SEV2", "SEV3", "SEV3"],
        "mttr": (12, 45),
        "hint": "error_rate>5x after deploy",
        "titles": [
            "{c}: 5xx spike after release {v}",
            "Elevated errors on {c} following deploy {v}",
            "{c} deploy {v} broke a handler",
        ],
        "sym": "error_rate {b}% -> {a}% within {m} min of deploy {v}; new exception signature in {c} logs; callers degrade",
        "res": ["rollback_deploy:{c}"],
        "lessons": [
            "Check the deploy time against the first error_rate increase before looking anywhere else",
            "Roll back only the component that changed; rolling back neighbours changes nothing",
            "Add a canary step for this service so a bad release stops at 5 percent",
        ],
    },
    "cert_expiry": {
        "a": (20, 95),
        "comps": ["payments-service", "edge-gateway", "notification-service", "web-frontend"],
        "rb": "RB-005",
        "sev": ["SEV2", "SEV3"],
        "mttr": (15, 40),
        "hint": "tls handshake failures at round time",
        "titles": [
            "TLS handshake failures on {c}",
            "Expired certificate breaks {c}",
            "{c} cannot negotiate TLS after midnight",
        ],
        "sym": "TLS handshake failures from {a}% of requests starting exactly at the certificate expiry; x509 certificate has expired in {c} logs; no deploy in the previous 24h",
        "res": ["renew_cert:{c}"],
        "lessons": [
            "Alert on certificate expiry 14 days ahead; the rotation job failed silently",
            "A step change exactly at a round time with no deploy is a certificate or a schedule, not a bug",
        ],
    },
    "disk_full": {
        "a": (94, 100),
        "comps": ["notification-service", "search-service", "catalog-service", "shipping-service", "event-bus"],
        "rb": "RB-006",
        "sev": ["SEV3", "SEV4"],
        "mttr": (8, 30),
        "hint": "disk_pct>95",
        "titles": ["Disk full on {c}", "{c} stops writing: no space left on device", "Log volume fills the {c} disk"],
        "sym": "disk_pct climbed to {a}% over an hour; no space left on device errors; log rotation had failed on {c}",
        "res": ["rotate_logs:{c}"],
        "lessons": [
            "Rotate logs first: it is low risk and needs no approval",
            "Alert on disk growth rate, not only on the absolute level",
        ],
    },
    "planned_maintenance": {
        "a": (0, 0),
        "comps": ["orders-db", "sessions-cache", "event-bus"],
        "rb": "RB-018",
        "sev": ["SEV4"],
        "mttr": (5, 15),
        "hint": "alerts inside maintenance window",
        "titles": ["Maintenance alerts on {c} opened as an incident", "False incident during {c} maintenance window"],
        "sym": "p95_ms and saturation_pct alerts on {c} during the scheduled maintenance window; no customer impact",
        "res": ["suppress_alert:{c}"],
        "lessons": [
            "Check the change feed for a maintenance window before opening an incident",
            "Alerts outside the window are real: verify the window covers the alert time",
        ],
    },
    "db_pool_exhaustion": {
        "a": (91, 99),
        "comps": ["orders-db", "orders-db", "inventory-service"],
        "rb": "RB-007",
        "sev": ["SEV2", "SEV2", "SEV3"],
        "mttr": (18, 55),
        "hint": "saturation_pct>90 multiple callers",
        "titles": [
            "{c} connection pool exhausted",
            "Connection pool saturation on {c} degrades four services",
            "Callers time out waiting for {c} connections",
        ],
        "sym": "saturation_pct {a}% on {c}; HikariPool connection timeout in payments, orders and inventory; an unrelated deploy {m} minutes earlier was not the cause",
        "res": ["kill_queries:{c}"],
        "lessons": [
            "Several callers degrading in the same minute points at the shared database, not at any single deploy",
            "Never restart the database primary for pool saturation: kill the long queries",
        ],
    },
    "cache_stampede": {
        "a": (88, 97),
        "comps": ["sessions-cache"],
        "rb": "RB-010",
        "sev": ["SEV2", "SEV3"],
        "mttr": (20, 50),
        "hint": "cache_stampede|hit_ratio<0.5",
        "titles": [
            "Cache stampede on {c} after TTL change",
            "{c} hit ratio collapse after configuration change",
            "Session cache flush causes latency",
        ],
        "sym": "hit_ratio fell 0.97->0.31; auth p95 x4; orders-db saturation {a}% as misses fall through to the database",
        "res": ["revert_config:session_ttl_seconds", "warm_cache:sessions-cache"],
        "lessons": [
            "Database load is the symptom; look at the cache hit ratio first",
            "Warm the cache as part of the revert: reverting alone leaves it cold",
        ],
    },
    "queue_poison_message": {
        "a": (3000, 15000),
        "comps": ["event-bus"],
        "rb": "RB-012",
        "sev": ["SEV3"],
        "mttr": (15, 45),
        "hint": "consumer_lag ramp, single offset retried",
        "titles": [
            "Poison message blocks consumer on {c}",
            "Notification consumer stuck on one offset",
            "Consumer lag climbs on {c}",
        ],
        "sym": "consumer_lag rose from 120 to {a}; notification-service cpu_pct high with near zero throughput; same offset retried; email provider latency was a coincidence",
        "res": ["quarantine_message:event-bus"],
        "lessons": [
            "A stuck consumer shows high cpu with no throughput; look for the repeated offset",
            "Pausing the consumer does not drain the lag; quarantine the message",
        ],
    },
    "dependency_outage": {
        "a": (35, 95),
        "comps": ["payment-provider", "email-provider"],
        "rb": "RB-013",
        "sev": ["SEV2", "SEV3"],
        "mttr": (25, 90),
        "hint": "external error_rate high no change",
        "titles": ["{c} outage", "Third-party {c} degraded", "{c} timeouts affect customers"],
        "sym": "{c} error_rate {a}% and timeouts; our own components healthy; no deploy or config change",
        "res": ["enable_fallback_provider:payments-service", "post_status_update"],
        "lessons": [
            "With an external outage there is nothing to roll back: fail over and communicate",
            "Check the vendor status page before touching our own services",
        ],
    },
    "memory_leak": {
        "a": (91, 98),
        "comps": ["search-service", "recommendation-service", "catalog-service", "cart-service"],
        "rb": "RB-011",
        "sev": ["SEV3", "SEV3", "SEV2"],
        "mttr": (30, 120),
        "hint": "mem_pct slow climb sawtooth",
        "titles": ["Memory leak in {c}", "{c} OOM restarts", "Slow memory growth on {c} since an earlier release"],
        "sym": "mem_pct climbed to {a}% with sawtooth pattern; p95_ms degraded from garbage collection; the leaking release was {m} days old",
        "res": ["restart_service:{c}", "open_ticket"],
        "lessons": [
            "Widen the change lookback to days when memory climbs slowly",
            "A restart is only a stopgap; open a ticket for the leaking release",
        ],
    },
    "config_change": {
        "a": (380, 1900),
        "comps": [
            "pricing-service",
            "catalog-service",
            "cart-service",
            "search-service",
            "checkout-service",
            "recommendation-service",
        ],
        "rb": "RB-002",
        "sev": ["SEV2", "SEV3"],
        "mttr": (14, 40),
        "hint": "config_change|p95_ms>5x",
        "titles": ["Flag ramp degrades {c}", "Config change slows {c}", "{c} errors after feature flag rollout"],
        "sym": "p95 {b}ms -> {a}ms after a feature flag ramp on {c}; error_rate {m}%",
        "res": ["revert_flag:{f}"],
        "lessons": [
            "Revert first when a ramp exceeds 25 percent; ask the flag owner afterwards",
            "Compare the change time with the onset; the change closest to the onset on the failing path wins",
        ],
    },
    "slow_query_migration": {
        "a": (41, 95),
        "comps": ["orders-db"],
        "rb": "RB-009",
        "sev": ["SEV3", "SEV2"],
        "mttr": (35, 120),
        "hint": "p95_ms creep after migration",
        "titles": [
            "Latency creep on {c} after schema migration",
            "{c} slow queries after migration",
            "Missing index after migration slows {c}",
        ],
        "sym": "p95_ms on {c} drifted from 12ms to {a}ms over an hour after a migration; no errors; callers slow down three hops away",
        "res": ["add_index:orders-db"],
        "lessons": [
            "Always build indexes with CREATE INDEX CONCURRENTLY",
            "Drift without errors after a migration is a missing index until proven otherwise",
        ],
    },
    "network_partition": {
        "a": (34, 72),
        "comps": ["edge-gateway"],
        "rb": "RB-014",
        "sev": ["SEV1", "SEV2"],
        "mttr": (20, 70),
        "hint": "alert storm one zone",
        "titles": [
            "Zone-b network partition causes alert storm",
            "Zone network partition: dozens of alerts",
            "Alert storm from partitioned zone",
        ],
        "sym": "{a} alerts across 11 components in 3 minutes; all in zone-b; no deploys; shared services unreachable from the zone",
        "res": ["drain_zone:zone-b"],
        "lessons": [
            "One storm is one incident: cluster by zone and minute first",
            "Drain the zone; do not restart or roll back individual services",
        ],
    },
    "telemetry_gap": {
        "a": (12, 55),
        "comps": ["payments-service", "orders-service", "shipping-service"],
        "rb": "RB-015",
        "sev": ["SEV3"],
        "mttr": (30, 90),
        "hint": "metrics flatline customer reports",
        "titles": [
            "Telemetry gap on {c}",
            "{c} metrics flat while customers report errors",
            "Exporter outage hides {c}",
        ],
        "sym": "{c} metrics exactly flat for {a} minutes; support tickets mention failures; no alerts",
        "res": ["escalate_human:{c}"],
        "lessons": [
            "Exactly constant metrics are a collection fault: escalate, do not guess",
            "Never act on a component you cannot observe",
        ],
    },
    "runaway_query": {
        "a": (95, 100),
        "comps": ["orders-db"],
        "rb": "RB-008",
        "sev": ["SEV2", "SEV3"],
        "mttr": (10, 35),
        "hint": "cpu_pct 100 runaway query",
        "titles": [
            "Runaway query saturates {c}",
            "{c} cpu at 100 percent from an analytics export",
            "Ad hoc report overloads {c}",
        ],
        "sym": "cpu_pct 100 on {c}; one session running {m} minutes; callers time out; database restart was proposed and rejected",
        "res": ["kill_queries:{c}"],
        "lessons": [
            "Kill the query before considering any restart",
            "A restart of the primary drops every connection and is almost never the answer",
        ],
    },
}
_CAT_ORDER = sorted(_CAT)
_WEIGHTS = {
    "bad_deploy": 12,
    "config_change": 12,
    "db_pool_exhaustion": 9,
    "cache_stampede": 8,
    "memory_leak": 8,
    "disk_full": 7,
    "cert_expiry": 6,
    "dependency_outage": 7,
    "queue_poison_message": 6,
    "slow_query_migration": 6,
    "runaway_query": 6,
    "network_partition": 4,
    "telemetry_gap": 5,
    "planned_maintenance": 5,
}

# fixed anchors: (index -> opened_at)
_DATE_POINTS = [
    (1, "2025-03-03T08:15:00Z"),
    (61, "2025-12-17T15:22:00Z"),
    (87, "2026-01-26T09:04:00Z"),
    (120, "2026-10-22T13:40:00Z"),
]


def _opened_at(idx: int, r: Any) -> str:
    (i0, d0), (i1, d1) = next(((a, b) for a, b in itertools.pairwise(_DATE_POINTS) if a[0] <= idx <= b[0]))
    from foghorn.data.util import to_epoch, to_iso

    e0, e1 = to_epoch(d0), to_epoch(d1)
    base = e0 + (e1 - e0) * (idx - i0) // max(i1 - i0, 1)
    jitter = r.randint(-9 * 3600, 9 * 3600)
    day_start = (base + jitter) // 86400 * 86400
    return to_iso(day_start + r.randint(7 * 3600, 21 * 3600) // 60 * 60)


def _fmt(t: str, comp: str, r: Any, flag: str, a_range: tuple[int, int]) -> str:
    lo, hi = a_range
    a = r.randint(lo, hi)
    return (
        t.replace("{c}", comp)
        .replace("{v}", f"2026.{r.randint(1, 10):02d}.{r.randint(1, 28):02d}.{r.randint(1, 5)}")
        .replace("{f}", flag)
        .replace("{m}", str(r.randint(3, 14)))
        .replace("{a}", str(a))
        .replace("{b}", str(r.randint(1, 9) if a < 100 else r.randint(120, 260)))
    )


def past_incidents(seed: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cats = [c for c in _CAT_ORDER for _ in range(_WEIGHTS[c])]
    for idx in range(1, N_INCIDENTS + 1):
        iid = f"INC-{idx:04d}"
        if iid == "INC-0087":
            out.append(_INC_0087)
            continue
        if iid == "INC-0061":
            out.append(_INC_0061)
            continue
        r = rng(seed, "incident", idx)
        cat = cats[r.randrange(len(cats))]
        spec = _CAT[cat]
        comp = spec["comps"][r.randrange(len(spec["comps"]))]
        c = catalog.COMPONENTS[comp]
        flag = (c.flags or c.configs or ("config_flag",))[0]
        a_range = spec["a"]
        title = _fmt(spec["titles"][r.randrange(len(spec["titles"]))], comp, r, flag, a_range)
        symptoms = _fmt(spec["sym"], comp, r, flag, a_range)
        affected = [
            comp,
            *sorted(
                catalog.transitive_callers(comp, sync_only=True), key=lambda x: (catalog.transitive_callers(comp)[x], x)
            )[:3],
        ]
        if cat in ("planned_maintenance", "telemetry_gap", "disk_full"):
            affected = [comp]
        lessons = r.sample(spec["lessons"], k=min(len(spec["lessons"]), r.choice([1, 2])))
        res = [_fmt(x, comp, r, flag, a_range) for x in spec["res"]]
        lo, hi = spec["mttr"]
        out.append(
            {
                "id": iid,
                "opened_at": _opened_at(idx, r),
                "severity": spec["sev"][r.randrange(len(spec["sev"]))],
                "title": title,
                "components": affected,
                "root_cause_category": cat,
                "root_cause_component": comp,
                "symptoms": symptoms,
                "resolution": res,
                "mttr_min": r.randint(lo, hi),
                "lessons": lessons,
                "fingerprint": f"{cat}|{comp}|{spec['hint']}",
                "runbook": spec["rb"],
                "status": "closed",
            }
        )
    return out


_INC_0087 = {
    "id": "INC-0087",
    "opened_at": "2026-01-26T09:04:00Z",
    "severity": "SEV2",
    "title": "Weekly cache flush causes checkout latency",
    "components": ["sessions-cache", "auth-service", "checkout-service"],
    "root_cause_category": "cache_stampede",
    "root_cause_component": "sessions-cache",
    "symptoms": "hit_ratio fell 0.97->0.31 at 09:00 when the weekly session flush job ran; auth p95 x4; orders-db saturation 94% as cache misses fell through; weekly flush stampede",
    "resolution": ["revert_config:session_flush_schedule", "warm_cache:sessions-cache"],
    "mttr_min": 38,
    "lessons": [
        "Weekly flush job must stagger by shard",
        "Warm the cache before or while reverting the flush schedule: reverting alone leaves it cold for about 6 minutes",
    ],
    "fingerprint": "cache_stampede|sessions-cache|hit_ratio<0.5",
    "runbook": "RB-010",
    "status": "closed",
}
_INC_0061 = {
    "id": "INC-0061",
    "opened_at": "2025-12-17T15:22:00Z",
    "severity": "SEV2",
    "title": "Search relevance flag ramp doubles catalog latency",
    "components": ["catalog-service", "search-service", "edge-gateway"],
    "root_cause_category": "config_change",
    "root_cause_component": "catalog-service",
    "symptoms": "p95 catalog 180ms->1.4s within 3 min of a feature flag ramp; error_rate 5%; an unrelated web-frontend deploy 6 min earlier was a red herring",
    "resolution": ["revert_flag:semantic_rerank"],
    "mttr_min": 21,
    "lessons": [
        "Revert first when ramp > 25%; ask the flag owner afterwards",
        "An unrelated deploy minutes before the onset is not evidence: check the call path and the deployed component's own metrics",
    ],
    "fingerprint": "config_change|catalog-service|p95_ms>5x",
    "runbook": "RB-002",
    "status": "closed",
}


# --- seed memories (D11) ------------------------------------------------------------------------------------------------
def _pref(n: int, team: str, text: str, rule: dict[str, Any], conf: float) -> dict[str, Any]:
    return {
        "namespace": ["prefs", team],
        "key": f"pref-{n:03d}",
        "value": {"text": text, "rule": rule, "source": "seed", "confidence": conf},
    }


def seed_prefs() -> list[dict[str, Any]]:
    return [
        _pref(
            1,
            "team-commerce",
            "Commerce on-call wants a status update drafted for every SEV2 checkout incident, published only after the commander approves.",
            {"prefer": "post_status_update_draft", "scope": "checkout-service"},
            0.9,
        ),
        _pref(
            2,
            "team-commerce",
            "For pricing problems prefer reverting the feature flag over scaling out; scaling hides the slow call.",
            {"prefer": "revert_flag", "over": "scale_out", "scope": "pricing-service"},
            0.8,
        ),
        _pref(
            3,
            "team-data-infra",
            "Never run kill_queries on orders-db without listing the longest sessions and their owners first.",
            {"require": "list_sessions_first", "scope": "orders-db"},
            0.9,
        ),
        _pref(
            4,
            "team-data-infra",
            "DBA on-call must approve every index build; the SQL must use CREATE INDEX CONCURRENTLY.",
            {"require": "dba_approval", "scope": "add_index"},
            1.0,
        ),
        _pref(
            5,
            "team-platform",
            "During the weekday 09:00-18:00 UTC freeze, platform prefers rollback over roll-forward for any bad deploy.",
            {"prefer": "rollback_deploy", "over": "roll_forward"},
            0.85,
        ),
        _pref(
            6,
            "team-messaging",
            "Notification incidents are SEV3 unless the order confirmation email is affected; do not page the commander.",
            {"severity_cap": "SEV3", "scope": "notification-service"},
            0.8,
        ),
        _pref(
            7,
            "team-growth",
            "Search and catalog flag ramps must be reverted first and discussed afterwards when the ramp is above 25 percent.",
            {"prefer": "revert_flag", "scope": "catalog-service"},
            0.8,
        ),
        _pref(
            8,
            "user-oncall",
            "Keep incident narration terse: findings with numbers, no filler.",
            {"style": "terse"},
            0.7,
        ),
        _pref(
            9,
            "user-oncall",
            "Always include the rollback command in the proposal summary.",
            {"include": "rollback_command"},
            0.6,
        ),
        _pref(
            10,
            "team-sre",
            "Escalate to a human immediately when telemetry is flat or missing; do not act on guesses.",
            {"prefer": "escalate_human", "when": "telemetry_gap"},
            0.95,
        ),
        _pref(
            11,
            "team-sre",
            "A single alert storm is one incident; open exactly one and attach the rest.",
            {"policy": "one_incident_per_storm"},
            0.9,
        ),
        _pref(
            12,
            "team-fulfilment",
            "Shipping and inventory incidents never justify restarting the orders database.",
            {"forbid": "restart_db_primary", "scope": "orders-db"},
            1.0,
        ),
        _pref(
            13,
            "team-messaging",
            "Email provider latency alone is not an incident; check consumer lag before paging.",
            {"check": "consumer_lag", "scope": "email-provider"},
            0.75,
        ),
        _pref(
            14,
            "team-payments",
            "Payments on-call prefers disabling the new-checkout flag over rolling back checkout-service; rollbacks need a 15 minute drain.",
            {"prefer": "revert_flag", "over": "rollback_deploy", "scope": "checkout-service"},
            0.8,
        ),
    ]


def seed_lessons(incidents: list[dict[str, Any]], seed: int) -> list[dict[str, Any]]:
    """30 lessons distilled from past incidents (2 per category + the two anchors). ``les-007`` is INC-0087's."""
    base: list[dict[str, Any]] = []
    for cat in _CAT_ORDER:
        members = [i for i in incidents if i["root_cause_category"] == cat and i["id"] not in ("INC-0087", "INC-0061")]
        for text in _CAT[cat]["lessons"][:2]:
            src = next((i for i in members if text in i["lessons"]), members[0] if members else incidents[0])
            base.append({"component": src["root_cause_component"], "text": text, "inc": src["id"], "cat": cat})
    inc87 = {
        "component": "sessions-cache",
        "inc": "INC-0087",
        "cat": "cache_stampede",
        "text": "After TTL or flush-schedule config changes, warm the cache before reverting; reverting alone leaves a cold cache for about 6 minutes.",
    }
    inc61 = {
        "component": "catalog-service",
        "inc": "INC-0061",
        "cat": "config_change",
        "text": "Revert first when a flag ramp is above 25 percent; ask the flag owner afterwards, and do not blame an unrelated deploy minutes earlier.",
    }
    base.insert(2, inc61)
    base.insert(6, inc87)
    return [
        {
            "namespace": ["lessons", x["component"]],
            "key": f"les-{n:03d}",
            "value": {"text": x["text"], "from_incident": x["inc"], "category": x["cat"], "source": "seed"},
        }
        for n, x in enumerate(base, 1)
    ]
