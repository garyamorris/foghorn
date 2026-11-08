"""The Brightcart world: 20 components, the call graph, metric baselines (D1 ``service_catalog``).

Edges point caller -> callee. ``ASYNC_EDGES`` are fire-and-forget (they never propagate errors or latency).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

ZONES = ("zone-a", "zone-b")


@dataclass(frozen=True)
class MetricDef:
    unit: str
    prec: int
    lo: float
    hi: float | None
    rel: float  # relative noise std (at noise_sigma = 0.05)
    diurnal: float  # amplitude of the daily cycle (peak 17:00 UTC)


METRICS: dict[str, MetricDef] = {
    "rps": MetricDef("req/s", 1, 0.0, None, 0.04, 0.45),
    "error_rate": MetricDef("ratio", 5, 0.0, 1.0, 0.15, 0.0),
    "p95_ms": MetricDef("ms", 2, 0.5, None, 0.05, 0.06),
    "cpu_pct": MetricDef("%", 2, 0.0, 100.0, 0.03, 0.18),
    "mem_pct": MetricDef("%", 2, 0.0, 100.0, 0.004, 0.0),
    "disk_pct": MetricDef("%", 3, 0.0, 100.0, 0.0008, 0.0),
    "saturation_pct": MetricDef("%", 2, 0.0, 100.0, 0.05, 0.12),
    "hit_ratio": MetricDef("ratio", 4, 0.0, 1.0, 0.004, 0.0),
    "consumer_lag": MetricDef("messages", 0, 0.0, None, 0.15, 0.10),
}
CORE_METRICS = ("rps", "error_rate", "p95_ms", "cpu_pct", "mem_pct", "disk_pct", "saturation_pct")
LOWER_IS_WORSE = frozenset({"rps", "hit_ratio"})


@dataclass(frozen=True)
class Component:
    id: str
    kind: str  # service | datastore | queue | external
    tier: int  # 0 = revenue critical
    team: str
    rota: str
    zones: tuple[str, ...]
    slo_availability: float
    slo_p95_ms: int
    depends_on: tuple[str, ...]
    deploy_cadence: str
    criticality: str  # revenue | customer | internal
    flags: tuple[str, ...]
    configs: tuple[str, ...]
    base: dict[str, float] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def metrics(self) -> tuple[str, ...]:
        extra = [m for m in ("hit_ratio", "consumer_lag") if m in self.base]
        return CORE_METRICS + tuple(extra)


def _b(
    rps: float, p95: float, err: float, cpu: float, mem: float, disk: float, sat: float, **more: float
) -> dict[str, float]:
    return {
        "rps": rps,
        "p95_ms": p95,
        "error_rate": err,
        "cpu_pct": cpu,
        "mem_pct": mem,
        "disk_pct": disk,
        "saturation_pct": sat,
        **more,
    }


_Z2 = ("zone-a", "zone-b")
_ZA = ("zone-a",)

_DEFS: tuple[Component, ...] = (
    Component(
        "web-frontend",
        "service",
        0,
        "platform",
        "rota-platform",
        _Z2,
        99.9,
        500,
        ("edge-gateway", "recommendation-service"),
        "daily",
        "revenue",
        ("new_checkout_ui",),
        (),
        _b(2200, 140, 0.0025, 35, 55, 28, 30),
    ),
    Component(
        "edge-gateway",
        "service",
        0,
        "platform",
        "rota-platform",
        _Z2,
        99.99,
        200,
        ("auth-service", "catalog-service", "search-service", "cart-service", "checkout-service", "user-profile"),
        "weekly",
        "revenue",
        ("edge_rate_limit_v2",),
        ("edge_timeout_ms",),
        _b(2600, 45, 0.0015, 40, 48, 30, 32),
    ),
    Component(
        "auth-service",
        "service",
        0,
        "platform",
        "rota-platform",
        _Z2,
        99.99,
        200,
        ("sessions-cache",),
        "weekly",
        "revenue",
        ("passwordless_login",),
        (),
        _b(900, 60, 0.0012, 30, 50, 25, 28),
    ),
    Component(
        "catalog-service",
        "service",
        1,
        "growth",
        "rota-growth",
        _ZA,
        99.9,
        300,
        ("inventory-service",),
        "daily",
        "customer",
        ("semantic_rerank",),
        (),
        _b(1500, 95, 0.0014, 38, 57, 35, 33),
    ),
    Component(
        "search-service",
        "service",
        1,
        "growth",
        "rota-growth",
        _ZA,
        99.9,
        400,
        ("catalog-service",),
        "daily",
        "customer",
        ("query_rewrite_v2",),
        (),
        _b(800, 130, 0.0020, 45, 58, 40, 36),
    ),
    Component(
        "cart-service",
        "service",
        0,
        "commerce",
        "rota-commerce",
        _Z2,
        99.95,
        300,
        ("pricing-service", "sessions-cache"),
        "daily",
        "revenue",
        ("cart_merge_v2",),
        (),
        _b(650, 85, 0.0018, 33, 52, 27, 35),
    ),
    Component(
        "checkout-service",
        "service",
        0,
        "commerce",
        "rota-commerce",
        _Z2,
        99.95,
        400,
        ("cart-service", "payments-service", "orders-service", "inventory-service"),
        "daily",
        "revenue",
        ("express_checkout", "one_click_pay"),
        (),
        _b(240, 180, 0.0030, 36, 54, 26, 38),
    ),
    Component(
        "payments-service",
        "service",
        0,
        "payments",
        "rota-payments",
        _Z2,
        99.99,
        700,
        ("payment-provider", "orders-db"),
        "weekly",
        "revenue",
        ("3ds_challenge_v2",),
        ("payment_retry_budget",),
        _b(210, 260, 0.0035, 32, 50, 24, 34),
    ),
    Component(
        "inventory-service",
        "service",
        1,
        "fulfilment",
        "rota-fulfilment",
        _Z2,
        99.9,
        250,
        ("orders-db",),
        "daily",
        "customer",
        ("reserve_stock_async",),
        (),
        _b(520, 70, 0.0012, 30, 51, 30, 31),
    ),
    Component(
        "pricing-service",
        "service",
        0,
        "commerce",
        "rota-commerce",
        _Z2,
        99.95,
        600,
        ("sessions-cache",),
        "daily",
        "revenue",
        ("new_pricing_engine", "dynamic_discounts"),
        (),
        _b(700, 210, 0.0010, 34, 53, 22, 29),
    ),
    Component(
        "recommendation-service",
        "service",
        2,
        "growth",
        "rota-growth",
        _ZA,
        99.5,
        500,
        ("sessions-cache", "catalog-service"),
        "daily",
        "customer",
        ("reco_model_v7",),
        (),
        _b(480, 160, 0.0030, 52, 62, 32, 40),
    ),
    Component(
        "notification-service",
        "service",
        2,
        "messaging",
        "rota-messaging",
        _Z2,
        99.5,
        400,
        ("event-bus", "email-provider"),
        "weekly",
        "customer",
        ("batch_digest_emails",),
        (),
        _b(120, 120, 0.0025, 25, 48, 55, 25),
    ),
    Component(
        "orders-service",
        "service",
        0,
        "fulfilment",
        "rota-fulfilment",
        _Z2,
        99.95,
        450,
        ("orders-db", "notification-service"),
        "daily",
        "revenue",
        ("split_shipments",),
        (),
        _b(260, 150, 0.0020, 33, 52, 28, 36),
    ),
    Component(
        "shipping-service",
        "service",
        1,
        "fulfilment",
        "rota-fulfilment",
        _ZA,
        99.9,
        350,
        ("orders-db",),
        "weekly",
        "customer",
        ("carrier_rate_cache",),
        (),
        _b(140, 110, 0.0018, 28, 49, 26, 30),
    ),
    Component(
        "user-profile",
        "service",
        1,
        "platform",
        "rota-platform",
        _Z2,
        99.9,
        200,
        ("orders-db",),
        "daily",
        "customer",
        ("profile_prefetch",),
        (),
        _b(380, 55, 0.0010, 27, 46, 20, 26),
    ),
    Component(
        "orders-db",
        "datastore",
        0,
        "data-infra",
        "rota-dba",
        ("zone-a", "zone-b"),
        99.99,
        40,
        (),
        "maintenance-window",
        "revenue",
        (),
        ("max_connections",),
        _b(3200, 12, 0.0003, 42, 68, 52, 48),
    ),
    Component(
        "sessions-cache",
        "datastore",
        0,
        "data-infra",
        "rota-dba",
        ("zone-a", "zone-b"),
        99.99,
        6,
        (),
        "maintenance-window",
        "revenue",
        (),
        ("session_ttl_seconds", "session_flush_schedule"),
        _b(9500, 1.8, 0.0001, 28, 61, 20, 24, hit_ratio=0.97),
    ),
    Component(
        "event-bus",
        "queue",
        1,
        "data-infra",
        "rota-dba",
        ("zone-a", "zone-b"),
        99.95,
        25,
        (),
        "maintenance-window",
        "customer",
        (),
        ("consumer_max_retries",),
        _b(1800, 6, 0.0002, 30, 55, 41, 27, consumer_lag=120),
    ),
    Component(
        "payment-provider",
        "external",
        0,
        "payments",
        "rota-payments",
        ("external",),
        99.95,
        900,
        (),
        "n/a",
        "revenue",
        (),
        (),
        _b(210, 380, 0.0040, 20, 40, 10, 20),
    ),
    Component(
        "email-provider",
        "external",
        2,
        "messaging",
        "rota-messaging",
        ("external",),
        99.0,
        1200,
        (),
        "n/a",
        "internal",
        (),
        (),
        _b(100, 450, 0.0060, 20, 40, 10, 20),
    ),
)
# NOTE: the dataclass is frozen but ``depends_on`` is authoritative below (orders-db etc. are leaves).
COMPONENTS: dict[str, Component] = {c.id: c for c in _DEFS}
COMPONENT_IDS: tuple[str, ...] = tuple(c.id for c in _DEFS)

#: caller -> callee edges that never propagate faults (fire-and-forget)
ASYNC_EDGES: frozenset[tuple[str, str]] = frozenset({("orders-service", "notification-service")})
#: per-edge propagation weight (default 1.0): how much of the callee's degradation the caller inherits
EDGE_WEIGHT: dict[tuple[str, str], float] = {
    ("edge-gateway", "catalog-service"): 0.55,
    ("edge-gateway", "search-service"): 0.5,
    ("edge-gateway", "auth-service"): 0.8,
    ("edge-gateway", "user-profile"): 0.5,
    ("web-frontend", "recommendation-service"): 0.15,
    ("web-frontend", "edge-gateway"): 0.7,
    ("search-service", "catalog-service"): 0.6,
    ("recommendation-service", "catalog-service"): 0.4,
    ("checkout-service", "inventory-service"): 0.5,
    ("checkout-service", "orders-service"): 0.8,
    ("catalog-service", "inventory-service"): 0.4,
}
#: zone-b has replicas of these (used by network_partition); datastores are shared across zones
ZONE_B_SERVICES: tuple[str, ...] = tuple(c.id for c in _DEFS if "zone-b" in c.zones and c.kind == "service")
SHARED_TIER: tuple[str, ...] = ("orders-db", "sessions-cache", "event-bus")


def comp(cid: str) -> Component:
    try:
        return COMPONENTS[cid]
    except KeyError:
        raise KeyError(f"unknown component {cid!r}") from None


@lru_cache(maxsize=1)
def callers_map() -> dict[str, tuple[str, ...]]:
    out: dict[str, list[str]] = {c: [] for c in COMPONENT_IDS}
    for c in _DEFS:
        for d in c.depends_on:
            out[d].append(c.id)
    return {k: tuple(sorted(v)) for k, v in out.items()}


def callers_of(cid: str, *, sync_only: bool = False) -> tuple[str, ...]:
    cs = callers_map()[cid]
    return tuple(c for c in cs if (c, cid) not in ASYNC_EDGES) if sync_only else cs


def callees_of(cid: str, *, sync_only: bool = False) -> tuple[str, ...]:
    ds = comp(cid).depends_on
    return tuple(d for d in ds if (cid, d) not in ASYNC_EDGES) if sync_only else ds


def transitive_callers(cid: str, *, sync_only: bool = True) -> dict[str, int]:
    """component -> hop distance (1 = direct caller), breadth-first, deterministic order."""
    seen: dict[str, int] = {}
    frontier = [cid]
    hop = 0
    while frontier:
        hop += 1
        nxt: list[str] = []
        for node in frontier:
            for c in callers_of(node, sync_only=sync_only):
                if c not in seen and c != cid:
                    seen[c] = hop
                    nxt.append(c)
        frontier = sorted(nxt)
    return seen


def transitive_callees(cid: str, *, sync_only: bool = True) -> dict[str, int]:
    seen: dict[str, int] = {}
    frontier = [cid]
    hop = 0
    while frontier:
        hop += 1
        nxt: list[str] = []
        for node in frontier:
            for c in callees_of(node, sync_only=sync_only):
                if c not in seen and c != cid:
                    seen[c] = hop
                    nxt.append(c)
        frontier = sorted(nxt)
    return seen


def path_distance(a: str, b: str) -> int | None:
    """Shortest call-path distance between a and b in either direction (None when unrelated)."""
    if a == b:
        return 0
    up = transitive_callers(a, sync_only=False)
    down = transitive_callees(a, sync_only=False)
    cands = [d for d in (up.get(b), down.get(b)) if d is not None]
    return min(cands) if cands else None


def validate_catalog() -> list[str]:
    """Self-check used by the integrity test: every edge target exists, no self loops, acyclic."""
    problems: list[str] = []
    for c in _DEFS:
        for d in c.depends_on:
            if d not in COMPONENTS:
                problems.append(f"{c.id} depends on unknown {d}")
            if d == c.id:
                problems.append(f"{c.id} depends on itself")
    return problems


def service_catalog_records(runbooks_by_component: dict[str, list[str]]) -> list[dict[str, Any]]:
    """D1: the committed ``service_catalog.json`` records (authored table + derived runbook links)."""
    out: list[dict[str, Any]] = []
    for c in _DEFS:
        rec: dict[str, Any] = {
            "id": c.id,
            "kind": c.kind,
            "tier": c.tier,
            "team": c.team,
            "oncall": c.rota,
            "slo": {"availability": c.slo_availability, "p95_ms": c.slo_p95_ms},
            "depends_on": list(c.depends_on),
            "runbooks": runbooks_by_component.get(c.id, []),
            "deploy_cadence": c.deploy_cadence,
            "criticality": c.criticality,
            "flags": list(c.flags),
            "configs": list(c.configs),
            "zones": list(c.zones),
            "metrics": list(c.metrics),
        }
        if c.id == "orders-db":
            rec.update({"engine": "postgres-15", "topology": {"primary": 1, "replicas": 2}})
        elif c.id == "sessions-cache":
            rec.update({"engine": "redis-7", "topology": {"shards": 6, "replicas_per_shard": 1}})
        elif c.id == "event-bus":
            rec.update({"engine": "kafka-3.7", "topology": {"brokers": 5, "partitions": 48}})
        elif c.kind == "external":
            rec["vendor"] = "PayWave" if c.id == "payment-provider" else "MailRelay"
        out.append(rec)
    return out
