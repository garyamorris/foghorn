"""Postmortem playbook per root-cause category: the wording a competent SRE would reach for.

This is deterministic DOMAIN KNOWLEDGE (category -> cause phrase, contributing factors, standard follow-ups, the lesson
to remember). It is handed to the drafting model inside the facts bundle, so a real model can use or ignore it and the
offline model composes from it. It is NOT ground truth: the category itself always comes from the commander's report.
"""

from __future__ import annotations

from typing import Literal, TypedDict

OwnerRole = Literal["primary", "secondary", "incident_commander", "dba"]


class FollowUp(TypedDict):
    title: str  # "{component}" is substituted
    kind: Literal["prevent", "detect", "mitigate", "process"]
    owner_role: OwnerRole
    priority: Literal["p0", "p1", "p2"]


class Playbook(TypedDict):
    label: str  # the cause in one phrase
    factors: list[str]
    follow_ups: list[FollowUp]
    lessons: list[str]


def _fu(title: str, kind: str, role: OwnerRole, prio: str = "p1") -> FollowUp:
    return {"title": title, "kind": kind, "owner_role": role, "priority": prio}  # type: ignore[typeddict-item]


PLAYBOOK: dict[str, Playbook] = {
    "bad_deploy": {
        "label": "a faulty release shipped to {component}",
        "factors": [
            "The release reached all traffic without a canary stage",
            "No automatic rollback on error-rate regression",
        ],
        "follow_ups": [
            _fu(
                "Add a canary stage and automatic rollback on error-rate regression for {component}",
                "prevent",
                "primary",
                "p0",
            ),
            _fu("Alert on the first error-rate increase after every {component} deploy", "detect", "secondary"),
            _fu("Document the rollback decision in the {component} runbook", "process", "incident_commander", "p2"),
        ],
        "lessons": [
            "Check the deploy time against the first error rate increase, then roll back only the component that changed"
        ],
    },
    "config_change": {
        "label": "a configuration or feature-flag change on {component}",
        "factors": [
            "The change ramped without a guardrail on blast radius",
            "The flag owner was not paged when the ramp crossed 25 percent",
        ],
        "follow_ups": [
            _fu(
                "Gate flag ramps above 25 percent behind an automated health check on {component}",
                "prevent",
                "primary",
                "p0",
            ),
            _fu(
                "Page the flag owner automatically when a ramp coincides with an alert on {component}",
                "detect",
                "secondary",
            ),
        ],
        "lessons": ["Revert first when a flag ramp is above 25 percent and ask the flag owner afterwards"],
    },
    "cache_stampede": {
        "label": "a cache stampede on {component}",
        "factors": [
            "Cache entries expired together after a TTL or flush-schedule change",
            "The database had no protection against a cold cache",
        ],
        "follow_ups": [
            _fu(
                "Add jitter to {component} TTLs and a warm-up step to the revert procedure", "prevent", "primary", "p0"
            ),
            _fu("Alert on the {component} hit ratio, not only on database load", "detect", "secondary"),
        ],
        "lessons": [
            "Database load is the symptom: look at the cache hit ratio first, and warm the cache as part of the revert"
        ],
    },
    "cert_expiry": {
        "label": "an expired certificate on {component}",
        "factors": ["The rotation job failed silently", "Certificate expiry was not monitored ahead of time"],
        "follow_ups": [
            _fu("Alert on certificate expiry 14 days ahead for {component}", "detect", "primary", "p0"),
            _fu("Make the rotation job fail loudly and page the owner", "prevent", "secondary"),
        ],
        "lessons": [
            "Alert on certificate expiry 14 days ahead; a step change at a round time with no deploy is a certificate"
        ],
    },
    "disk_full": {
        "label": "a full disk on {component}",
        "factors": ["Log growth was not rate-limited", "Alerting only watched the absolute disk level"],
        "follow_ups": [
            _fu("Alert on disk growth rate for {component}, not only on the level", "detect", "primary", "p0"),
            _fu("Automate log rotation on {component}", "prevent", "secondary"),
        ],
        "lessons": ["Rotate logs first: it is low risk and needs no approval; alert on disk growth rate"],
    },
    "planned_maintenance": {
        "label": "alerts raised during a planned maintenance window on {component}",
        "factors": ["The maintenance window was not correlated with the alert stream"],
        "follow_ups": [
            _fu(
                "Suppress alerts automatically for the announced maintenance window on {component}",
                "prevent",
                "primary",
            ),
            _fu(
                "Check the change feed for a maintenance window before opening an incident",
                "process",
                "incident_commander",
                "p2",
            ),
        ],
        "lessons": ["Check the change feed for a maintenance window before opening an incident"],
    },
    "db_pool_exhaustion": {
        "label": "connection pool exhaustion on {component}",
        "factors": [
            "Long-running queries held connections",
            "Several callers degraded in the same minute with no shared-dependency view",
        ],
        "follow_ups": [
            _fu("Cap query runtime and size the {component} connection pool for peak load", "prevent", "dba", "p0"),
            _fu("Alert on pool saturation for {component}", "detect", "primary"),
        ],
        "lessons": ["Never restart the database primary for pool saturation: kill the long queries"],
    },
    "queue_poison_message": {
        "label": "a poison message blocking a consumer of {component}",
        "factors": ["The consumer retried the same offset forever", "No dead-letter path for unprocessable messages"],
        "follow_ups": [
            _fu("Add a dead-letter queue and a retry cap to the {component} consumer", "prevent", "primary", "p0"),
            _fu("Alert on a stuck offset with high CPU and no throughput", "detect", "secondary"),
        ],
        "lessons": [
            "A stuck consumer shows high cpu with no throughput: quarantine the repeated message instead of pausing"
        ],
    },
    "dependency_outage": {
        "label": "an outage of an external dependency of {component}",
        "factors": [
            "No automatic failover to the secondary provider",
            "The vendor status page was not wired into alerting",
        ],
        "follow_ups": [
            _fu("Automate failover to the secondary provider for {component}", "prevent", "primary", "p0"),
            _fu("Subscribe alerting to the vendor status feed", "detect", "secondary", "p2"),
        ],
        "lessons": [
            "With an external outage there is nothing to roll back: check the vendor status page, fail over and communicate"
        ],
    },
    "memory_leak": {
        "label": "a memory leak in {component}",
        "factors": [
            "The leaking release was days old, outside the default change lookback",
            "A restart masked the leak",
        ],
        "follow_ups": [
            _fu("Add a memory-growth alert with a multi-day lookback for {component}", "detect", "primary", "p0"),
            _fu("Open a ticket against the leaking release and add a soak test", "prevent", "secondary"),
        ],
        "lessons": ["Widen the change lookback to days when memory climbs slowly; a restart is only a stopgap"],
    },
    "slow_query_migration": {
        "label": "a slow query after a schema migration on {component}",
        "factors": ["The migration dropped or skipped an index", "Latency drift without errors went unnoticed"],
        "follow_ups": [
            _fu(
                "Require CREATE INDEX CONCURRENTLY and a query-plan check in the migration checklist for {component}",
                "prevent",
                "dba",
                "p0",
            ),
            _fu("Alert on p95 latency drift after migrations", "detect", "secondary"),
        ],
        "lessons": [
            "Drift without errors after a migration is a missing index until proven otherwise; always build indexes CONCURRENTLY"
        ],
    },
    "network_partition": {
        "label": "a network partition affecting {component}",
        "factors": [
            "Alerts arrived as a storm rather than one incident",
            "Individual services were restarted instead of draining the zone",
        ],
        "follow_ups": [
            _fu("Cluster alerts by zone and minute before paging for {component}", "detect", "primary"),
            _fu("Automate zone drain as the first response to a partition", "mitigate", "incident_commander", "p0"),
        ],
        "lessons": ["One storm is one incident: cluster by zone and minute first, then drain the zone"],
    },
    "telemetry_gap": {
        "label": "a telemetry gap on {component}",
        "factors": ["Metrics went constant, which looks healthy", "No staleness detection on the collection pipeline"],
        "follow_ups": [
            _fu("Alert on constant or missing metrics for {component}", "detect", "primary", "p0"),
            _fu("Escalate collection faults to the observability on-call", "process", "incident_commander", "p2"),
        ],
        "lessons": [
            "Exactly constant metrics are a collection fault: escalate, and never act on a component you cannot observe"
        ],
    },
    "runaway_query": {
        "label": "a runaway query on {component}",
        "factors": ["No statement timeout protected the primary", "The query came from an unbounded report job"],
        "follow_ups": [
            _fu("Set a statement timeout and a per-role resource limit on {component}", "prevent", "dba", "p0"),
            _fu("Alert on queries running longer than the SLO budget", "detect", "secondary"),
        ],
        "lessons": [
            "Kill the query before considering any restart; a restart of the primary is almost never the answer"
        ],
    },
}

GENERIC: Playbook = {
    "label": "an unclassified fault on {component}",
    "factors": ["The cause was not confirmed by the response team"],
    "follow_ups": [
        _fu(
            "Run a focused review to classify the cause of the {component} incident",
            "process",
            "incident_commander",
            "p1",
        ),
        _fu("Add detection that would have confirmed the cause on {component}", "detect", "primary"),
    ],
    "lessons": [
        "When the cause cannot be confirmed, record what was ruled out so the next responder starts further along"
    ],
}


def playbook_for(category: str | None) -> Playbook:
    return PLAYBOOK.get(category or "", GENERIC)
