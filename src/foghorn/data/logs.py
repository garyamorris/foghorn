"""Log generation: per-component baseline templates, fault signatures (root + callers), red herrings, injection.

Every line carries a hidden ``kind`` label (``base | sig | prop | herring | inj | amb``) used by the recovery overlay
(signature lines stop after a successful remediation) and by tests; the tools never return it.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Any

from foghorn.data import catalog
from foghorn.data.faults import N_POINTS, PRE_MINUTES, Effect, FaultSpec, profile
from foghorn.data.util import hex_id, rng


@dataclass(frozen=True)
class LogLine:
    ts: int
    service: str
    level: str
    trace_id: str
    message: str
    kind: str


_METHODS = ("GET", "GET", "GET", "POST", "PUT")
_PATHS = {
    "web-frontend": ["/", "/search", "/p/{n}", "/cart", "/checkout"],
    "edge-gateway": ["/api/v2/catalog", "/api/v2/cart", "/api/v2/checkout", "/api/v2/me", "/api/v2/search"],
    "auth-service": ["/v1/token", "/v1/session", "/v1/introspect"],
    "catalog-service": ["/v1/products/{n}", "/v1/categories", "/v1/products"],
    "search-service": ["/v1/query", "/v1/suggest"],
    "cart-service": ["/v1/carts/{n}", "/v1/carts/{n}/lines"],
    "checkout-service": ["/v1/checkout", "/v1/checkout/{n}/confirm"],
    "payments-service": ["/v1/authorize", "/v1/capture", "/v1/refunds"],
    "inventory-service": ["/v1/stock/{n}", "/v1/reserve"],
    "pricing-service": ["/v1/price", "/v1/quote", "/v1/discounts"],
    "recommendation-service": ["/v1/recs", "/v1/similar/{n}"],
    "notification-service": ["/v1/send", "/v1/digest"],
    "orders-service": ["/v1/orders", "/v1/orders/{n}"],
    "shipping-service": ["/v1/rates", "/v1/labels"],
    "user-profile": ["/v1/profile", "/v1/addresses"],
}
_GENERIC: dict[str, list[tuple[str, str]]] = {
    "service": [
        ("INFO", "{method} {path} 200 {ms}ms trace_id={tid}"),
        ("INFO", "{method} {path} 200 {ms}ms trace_id={tid}"),
        ("INFO", "health check ok uptime_s={n}"),
        ("INFO", "config reload complete etag={id}"),
        ("INFO", "connection pool stats active={a} idle={b} waiting=0"),
        ("WARN", "slow request {method} {path} latency_ms={ms3} threshold_ms=500"),
        ("WARN", "retrying upstream call attempt=1/3 reason=timeout"),
        ("WARN", "thread pool utilisation {pct}% (alert at 90%)"),
        ("ERROR", "request failed status=500 path={path} request_id={id} (retried, succeeded)"),
        ("ERROR", "client disconnected before response was written path={path}"),
    ],
    "datastore": [
        ("INFO", "checkpoint complete: wrote {n} buffers ({pct}%); write={ms}ms"),
        ("INFO", "connection received: host=10.{a}.{b}.{n} port=5432"),
        ("INFO", "automatic vacuum of table orders.order_lines: index scans: 1"),
        ("WARN", "slow query {ms3}ms: SELECT id FROM orders WHERE customer_id = $1 ORDER BY created_at DESC LIMIT 20"),
        ("WARN", "replication lag {ms}ms on replica-{a}"),
        ("ERROR", "canceling statement due to user request (retried)"),
    ],
    "queue": [
        ("INFO", "partition rebalance complete group=notif-sender generation={n}"),
        ("INFO", "log segment rolled topic=orders.events offset={off}"),
        ("INFO", "consumer group notif-sender committed offset={off}"),
        ("WARN", "request handler purgatory size {n}"),
        ("ERROR", "failed to send heartbeat to coordinator (retried)"),
    ],
    "external": [
        ("INFO", "synthetic probe ok latency_ms={ms}"),
        ("INFO", "synthetic probe ok latency_ms={ms}"),
        ("WARN", "synthetic probe slow latency_ms={ms3}"),
    ],
}
_SPECIFIC: dict[str, list[tuple[str, str]]] = {
    "checkout-service": [
        ("INFO", "order placed order_id=ord_{id} total={amt} items={a} trace_id={tid}"),
        ("INFO", "payment authorised for order ord_{id} in {ms}ms"),
    ],
    "payments-service": [
        ("INFO", "authorised payment pay_{id} amount={amt} provider=PayWave in {ms}ms"),
        ("INFO", "capture settled pay_{id}"),
    ],
    "cart-service": [("INFO", "cart c_{id} updated lines={a}"), ("INFO", "cart merge complete anon->user cart=c_{id}")],
    "pricing-service": [
        ("INFO", "quote computed sku=SKU-{n} price={amt} engine=legacy in {ms}ms"),
        ("INFO", "discount rules cache refreshed rules={n}"),
    ],
    "auth-service": [("INFO", "token issued sub=u_{id} ttl=900s"), ("INFO", "session cache hit sid=s_{id}")],
    "catalog-service": [("INFO", "product SKU-{n} served from cache"), ("INFO", "category tree refreshed nodes={n}")],
    "search-service": [("INFO", 'query q="oat milk" hits={n} in {ms}ms'), ("INFO", "index segment merged docs={n}")],
    "inventory-service": [
        ("INFO", "stock reserved sku=SKU-{n} qty={a}"),
        ("INFO", "stock sync batch applied rows={n}"),
    ],
    "recommendation-service": [
        ("INFO", "model warm-up complete version=2026.11.0{a} in {ms}s"),
        ("INFO", "recs served user=u_{id} items=12"),
    ],
    "notification-service": [
        ("INFO", "email queued template=order_confirmation to=<redacted> id={id}"),
        ("INFO", "digest batch sent count={n}"),
    ],
    "orders-service": [
        ("INFO", "order ord_{id} persisted state=PAID"),
        ("INFO", "fulfilment event published order=ord_{id}"),
    ],
    "shipping-service": [
        ("INFO", "label created carrier=Parcelio order=ord_{id}"),
        ("INFO", "rate card refreshed carriers=4"),
    ],
    "user-profile": [("INFO", "profile u_{id} loaded in {ms}ms"), ("INFO", "address validated u_{id}")],
    "web-frontend": [("INFO", "SSR render / in {ms}ms cache=HIT"), ("INFO", "asset manifest loaded build={id}")],
    "edge-gateway": [
        ("INFO", "route /api/v2/checkout -> checkout-service 200 {ms}ms"),
        ("INFO", "rate limiter bucket refill ok tenant=web"),
    ],
}

_RAND_GEN = {
    "id": lambda r: f"{r.randrange(1 << 24):06x}",
    "tid": lambda r: f"tr_{r.randrange(1 << 24):06x}",
    "n": lambda r: str(r.randint(1, 9999)),
    "ms": lambda r: str(r.randint(8, 240)),
    "ms3": lambda r: str(r.randint(520, 1800)),
    "a": lambda r: str(r.randint(1, 9)),
    "b": lambda r: str(r.randint(1, 99)),
    "pct": lambda r: str(r.randint(20, 79)),
    "amt": lambda r: f"{r.uniform(8, 240):.2f}",
    "off": lambda r: str(r.randint(1_000_000, 9_999_999)),
    "ip": lambda r: f"10.{r.randint(0, 9)}.{r.randint(0, 255)}.{r.randint(2, 250)}",
    "pid": lambda r: str(r.randint(1000, 99999)),
    "hr": lambda r: f"{r.uniform(0.28, 0.45):.2f}",
    "method": lambda r: _METHODS[r.randrange(len(_METHODS))],
    "lag": lambda r: str(r.randint(2000, 14000)),
}
_PH = re.compile(r"\{(\w+)\}")


def fill(tpl: str, r: random.Random, ctx: dict[str, Any] | None = None) -> str:
    ctx = ctx or {}

    def sub(m: re.Match[str]) -> str:
        k = m.group(1)
        if k in ctx:
            return str(ctx[k])
        gen = _RAND_GEN.get(k)
        return gen(r) if gen else m.group(0)

    return _PH.sub(sub, tpl)


# --- fault signatures -------------------------------------------------------------------------------------------------------
# (level, template, weight)
_SIG: dict[str, dict[str, list[tuple[str, str, int]]]] = {
    "bad_deploy": {
        "root": [
            (
                "ERROR",
                'java.lang.NullPointerException: Cannot invoke "LineItem.getSku()" because "item" is null at com.brightcart.{svc_path}.mapper.OrderMapper.map(OrderMapper.java:142) request_id={id}',
                5,
            ),
            ("ERROR", "unhandled exception in handler POST /v2/{svc_path} status=500 release={ver} request_id={id}", 3),
            ("WARN", "circuit breaker half-open for {svc} after {n} consecutive failures", 1),
        ],
        "caller": [("ERROR", "upstream {callee} returned 500 after {ms}ms (attempt {a}/2) request_id={id}", 4)],
    },
    "cert_expiry": {
        "root": [
            (
                "ERROR",
                "tls: handshake failure with {peer}: x509: certificate has expired or is not yet valid: current time is after {expires} (client cert CN={cn})",
                6,
            ),
            (
                "ERROR",
                "{peer} authorize failed: javax.net.ssl.SSLHandshakeException: PKIX path validation failed request_id={id}",
                3,
            ),
        ],
        "caller": [("ERROR", "upstream {callee} failed: TLS handshake error request_id={id}", 4)],
        "pre": [("WARN", "client certificate CN={cn} expires in {mins}m; rotation job last status=failed", 1)],
    },
    "disk_full": {
        "root": [
            ("ERROR", "write failed: No space left on device (path=/var/log/{svc}/app.log)", 5),
            ("ERROR", "failed to persist batch {id}: IOError [Errno 28] No space left on device", 3),
            ("WARN", "log rotation failed: logrotate exit status 1 (disk {dpct}% used)", 1),
        ],
        "caller": [],
        "pre": [("WARN", "disk usage {dpct}% on /var/log (threshold 85%)", 1)],
    },
    "planned_maintenance": {
        "root": [
            ("WARN", "maintenance mode: connections drained from replica {a}", 2),
            ("INFO", "pg_upgrade step {a}/9 complete", 2),
            ("WARN", "replication lag {ms}ms during failover test", 1),
        ],
        "caller": [("WARN", "{callee} connection reset (maintenance), reconnecting attempt {a}", 2)],
    },
    "db_pool_exhaustion": {
        "root": [
            (
                "ERROR",
                "FATAL: remaining connection slots are reserved for non-replication superuser connections (active={n})",
                3,
            ),
            ("WARN", "too many clients already: {n} sessions waiting", 3),
            ("WARN", "slow query {ms3}ms: SELECT ... FROM orders WHERE status = $1 (waiting for connection slot)", 2),
        ],
        "caller": [
            (
                "ERROR",
                "HikariPool-1 - Connection is not available, request timed out after 30000ms (active=40, idle=0, waiting={n})",
                5,
            )
        ],
    },
    "cache_stampede": {
        "root": [
            ("WARN", "hit_ratio {hr} below threshold 0.60; evictions={n}/s", 3),
            ("ERROR", "maxclients reached, rejecting connection ({n} clients)", 1),
            ("WARN", "keyspace expired in bulk: {n} keys dropped in 1s", 2),
        ],
        "caller": [
            ("WARN", "session lookup miss for sid=s_{id}; falling back to database ({ms3}ms)", 5),
            ("ERROR", "SessionStore timeout after {ms3}ms (database fallback saturated)", 2),
        ],
    },
    "queue_poison_message": {
        "root": [
            (
                "ERROR",
                "consumer group notif-sender: offset {off} redelivered (attempt {n}) deserialization failed: unexpected token at position 214",
                5,
            )
        ],
        "consumer": [
            (
                "ERROR",
                "failed to deserialize message key={id} partition=3 offset={off}: InvalidProtocolBufferException; retrying",
                5,
            ),
            ("WARN", "consumer lag {lag} messages (group notif-sender) not decreasing", 1),
        ],
        "caller": [],
    },
    "dependency_outage": {
        "root": [],
        "caller": [
            ("ERROR", "{callee} request failed: read timeout after 5000ms (attempt {a}/3) request_id={id}", 5),
            (
                "ERROR",
                '{callee} responded 503 Service Unavailable body="upstream connect error or disconnect/reset before headers"',
                3,
            ),
            ("WARN", "circuit breaker open for {callee}", 1),
        ],
    },
    "memory_leak": {
        "root": [
            ("WARN", "GC overhead: {gc}% of time in GC over the last minute (heap {hp}%)", 3),
            (
                "ERROR",
                "java.lang.OutOfMemoryError: Java heap space at com.brightcart.{svc_path}.cache.ResultCache.put request_id={id}",
                2,
            ),
            ("WARN", "container {pid} OOMKilled; restarting (restarts={a})", 1),
        ],
        "caller": [("ERROR", "upstream {callee} timed out after {ms3}ms request_id={id}", 3)],
        "pre": [("INFO", "result cache size {entries} entries (heap {hp}%)", 1)],
    },
    "config_change": {
        "root": [
            ("ERROR", "{flag}: dependency call timeout after {ms3}ms (attempt 2/2) request_id={id}", 4),
            ("WARN", "slow path enabled by flag {flag}: p95 {ms3}ms", 2),
        ],
        "caller": [("ERROR", "upstream {callee} timed out after {ms3}ms request_id={id}", 3)],
    },
    "slow_query_migration": {
        "root": [
            (
                "WARN",
                "slow query {ms3}ms: SELECT o.* FROM orders o JOIN order_lines l ON l.fulfilment_id = o.fulfilment_id (seq scan on order_lines)",
                4,
            ),
            ("INFO", "autovacuum: table order_lines {n} dead tuples", 1),
        ],
        "caller": [("WARN", "{callee} query took {ms3}ms (threshold 200ms)", 2)],
    },
    "network_partition": {
        "root": [
            ("ERROR", "connect to {shared}: No route to host (zone-b)", 4),
            ("ERROR", "dial tcp {ip}:6379: i/o timeout (zone-b -> shared services)", 3),
            ("WARN", "readiness probe failed: dependency check timeout (zone=zone-b)", 2),
        ],
        "caller": [],
    },
    "telemetry_gap": {"root": [], "caller": []},
    "runaway_query": {
        "root": [
            (
                "WARN",
                "slow query {ms3}000ms: SELECT ... FROM orders o JOIN customers c ... (analytics.nightly_export, no LIMIT) pid={pid}",
                5,
            ),
            ("WARN", "autovacuum worker cancelled: conflicting lock", 1),
            ("ERROR", "FATAL: sorry, too many clients already", 1),
        ],
        "caller": [
            (
                "ERROR",
                "HikariPool-1 - Connection is not available, request timed out after 30000ms (active=40, idle=0, waiting={n})",
                5,
            )
        ],
    },
}


def _pick(r: random.Random, items: list[tuple[str, str, int]]) -> tuple[str, str]:
    total = sum(w for _, _, w in items)
    x = r.uniform(0, total)
    for lvl, tpl, w in items:
        x -= w
        if x <= 0:
            return lvl, tpl
    return items[-1][0], items[-1][1]


def _main_effect(effects: list[Effect], comp: str) -> Effect | None:
    best = None
    for e in effects:
        if (
            e.component == comp
            and e.role == "fault"
            and e.mode in ("add", "mul")
            and e.metric
            in (
                "error_rate",
                "p95_ms",
                "saturation_pct",
                "consumer_lag",
                "hit_ratio",
                "mem_pct",
                "disk_pct",
                "cpu_pct",
                "rps",
            )
        ) and (best is None or _prio(e.metric) < _prio(best.metric)):
            best = e
    return best


def _prio(m: str) -> int:
    order = (
        "error_rate",
        "consumer_lag",
        "hit_ratio",
        "saturation_pct",
        "p95_ms",
        "mem_pct",
        "disk_pct",
        "cpu_pct",
        "rps",
    )
    return order.index(m)


def generate_logs(
    spec: FaultSpec,
    t0: int,
    effects: list[Effect],
    affected: list[str],
    changes: list[dict[str, Any]],
    root_change: dict[str, Any] | None,
    herrings: list[dict[str, Any]],
    gap_components: tuple[str, ...],
) -> list[LogLine]:
    seed, sid = spec.seed, spec.scenario_id
    onset = t0 + PRE_MINUTES * 60
    end = t0 + N_POINTS * 60
    out: list[LogLine] = []
    cat = spec.category
    sig = _SIG[cat]
    p = spec.params

    # 1. baseline chatter -------------------------------------------------------------------------------------------------
    for cid in catalog.COMPONENT_IDS:
        r = rng(seed, sid, "logs-base", cid)
        c = catalog.COMPONENTS[cid]
        pool = list(_GENERIC[c.kind]) + list(_SPECIFIC.get(cid, []))
        paths = _PATHS.get(cid, ["/"])
        n = r.randint(30, 42) if c.kind != "external" else r.randint(8, 12)
        times = sorted(r.randrange(t0, end) for _ in range(n))
        for ts in times:
            lvl, tpl = pool[r.randrange(len(pool))]
            if lvl == "ERROR" and r.random() < 0.5:
                lvl, tpl = pool[0]
            ctx = {"path": paths[r.randrange(len(paths))].replace("{n}", str(r.randint(1, 9999)))}
            out.append(LogLine(ts, cid, lvl, "tr_" + hex_id(sid, cid, ts, n=6), fill(tpl, r, ctx), "base"))

    # 2. fault signatures ---------------------------------------------------------------------------------------------------
    lag_s = spec.evidence_lag_min * 60
    root = spec.component
    ver = (root_change or {}).get("details", {}).get("version", "n/a")
    base_ctx: dict[str, Any] = {
        "svc": root,
        "svc_path": root.replace("-service", ""),
        "ver": ver,
        "flag": p.get("flag", (catalog.comp(root).flags or ("flag",))[0]),
        "cn": f"{root}.brightcart.internal",
        "expires": p.get("cert_expiry_at", "the expiry instant"),
        "dpct": 99,
        "gc": 34,
        "hp": 96,
        "entries": 88000,
        "mins": 0,
    }
    sig_root = p.get("root_logs")
    root_items = [(a, b, int(w)) for a, b, w in sig_root] if sig_root else sig["root"]
    root_rate = float(p.get("root_log_rate", 3.0))
    caller_rate = float(p.get("caller_log_rate", 1.2))

    def emit_series(
        cid: str,
        items: list[tuple[str, str, int]],
        rate: float,
        ef: Effect | None,
        kind: str,
        extra_ctx: dict[str, Any],
        key: str,
        delay_s: int,
    ) -> None:
        if not items or ef is None:
            return
        r = rng(seed, sid, "logs-sig", key, cid)
        for i in range(N_POINTS):
            f = profile(ef, i - PRE_MINUTES)
            if f <= 0.12:
                continue
            k = rate * f
            cnt = int(k) + (1 if r.random() < k - int(k) else 0)
            for _ in range(cnt):
                ts = t0 + i * 60 + r.randrange(0, 60) + delay_s
                if ts >= end:
                    continue
                lvl, tpl = _pick(r, items)
                ctx = {**base_ctx, **extra_ctx}
                out.append(LogLine(ts, cid, lvl, "tr_" + hex_id(sid, cid, ts, kind, n=6), fill(tpl, r, ctx), kind))

    main = _main_effect(effects, root)
    if cat == "network_partition":
        for k in catalog.ZONE_B_SERVICES:
            ef = _main_effect(effects, k)
            shared = ("orders-db:5432", "sessions-cache:6379", "event-bus:9092")[stable_pick(sid, k, 3)]
            emit_series(k, root_items, root_rate, ef, "sig", {"shared": shared}, "np", lag_s)
    elif cat == "queue_poison_message":
        emit_series(root, sig["root"], root_rate, main, "sig", {}, "bus", lag_s)
        consumer = p.get("consumer", "notification-service")
        emit_series(consumer, sig["consumer"], root_rate, _main_effect(effects, consumer), "sig", {}, "cons", lag_s)
    elif cat == "telemetry_gap":
        pass
    else:
        emit_series(
            root,
            root_items,
            root_rate,
            main,
            "sig",
            {"callee": (first_callee := (catalog.callees_of(root) or ("dependency",))[0]), "peer": first_callee},
            "root",
            lag_s,
        )
        # callers of the root: log the downstream failure, aligned with their own propagated effect
        for cid in affected:
            if cid == root:
                continue
            ef = _main_effect(effects, cid)
            callee = next((d for d in catalog.callees_of(cid) if d == root or d in affected), root)
            peer = (catalog.callees_of(root) or (root,))[0]
            emit_series(
                cid, sig.get("caller", []), caller_rate, ef, "prop", {"callee": callee, "peer": peer}, "caller", lag_s
            )

    # externals produce no logs of their own for dependency_outage: callers log the failure (above).
    # pre-onset warning breadcrumbs (cert expiry countdown, disk growth, cache growth)
    pre = sig.get("pre", [])
    if pre and cat in ("cert_expiry", "disk_full", "memory_leak"):
        r = rng(seed, sid, "logs-pre", root)
        step = 5 if cat != "disk_full" else 6
        for mins_before in range(60, 0, -step):
            ts = onset - mins_before * 60 + r.randrange(0, 30)
            lvl, tpl = _pick(r, pre)
            pre_ctx: dict[str, Any] = dict(base_ctx)
            pre_ctx.update(
                {
                    "mins": mins_before,
                    "dpct": min(99, 62 + (60 - mins_before) * 0.6),
                    "entries": 41000 + (60 - mins_before) * 900,
                    "hp": 55 + int((60 - mins_before) * 0.7),
                }
            )
            pre_ctx["dpct"] = int(pre_ctx["dpct"])
            out.append(LogLine(ts, root, lvl, "tr_" + hex_id(sid, "pre", ts, n=6), fill(tpl, r, pre_ctx), "sig"))

    # 3. custom evidence lines from hand-tuned scenarios ------------------------------------------------------------------------
    for item in p.get("extra_logs", []):
        r = rng(seed, sid, "logs-extra", item["service"], item["template"][:20])
        count = int(item.get("count", 6))
        t_start = onset + int(item.get("start_min", 1) * 60)
        t_span = max(int(item.get("span_min", 20) * 60), 60)
        for _ in range(count):
            ts = t_start + r.randrange(0, t_span)
            if ts >= end:
                continue
            out.append(
                LogLine(
                    ts,
                    item["service"],
                    item.get("level", "ERROR"),
                    "tr_" + hex_id(sid, item["service"], ts, "x", n=6),
                    fill(item["template"], r, {**base_ctx, **item.get("ctx", {})}),
                    item.get("kind", "sig"),
                )
            )

    # 4. red-herring and ambient change breadcrumbs -------------------------------------------------------------------------------
    for ch in changes:
        r = rng(seed, sid, "logs-change", ch["change_id"])
        ts0 = ch["ts"]
        comp = ch["component"]
        kind = "herring" if ch["role"] == "herring" else "amb"
        if ch["kind"] == "deploy":
            out.append(
                LogLine(
                    ts0,
                    comp,
                    "INFO",
                    "tr_" + hex_id(sid, ch["change_id"], n=6),
                    f"deploy {ch['details'].get('version', '')} complete; rolling restart finished in {r.randint(40, 180)}s",
                    kind,
                )
            )
            if kind == "herring":
                for j in range(r.randint(4, 8)):
                    lvl = "WARN" if j % 2 == 0 else "INFO"
                    if lvl == "WARN":
                        msg = f"feature store cache miss ratio 0.{r.randint(30, 60)} during warm-up (expected after release)"
                    else:
                        msg = f"model warm-up step {j + 1}/8 complete in {r.randint(200, 900)}ms"
                    out.append(
                        LogLine(
                            ts0 + 20 + j * 45 + r.randrange(0, 20),
                            comp,
                            lvl,
                            "tr_" + hex_id(sid, ch["change_id"], j, n=6),
                            msg,
                            "herring",
                        )
                    )
        elif ch["kind"] in ("flag_change", "config_change"):
            out.append(
                LogLine(
                    ts0,
                    comp,
                    "INFO",
                    "tr_" + hex_id(sid, ch["change_id"], n=6),
                    f"config change applied: {ch['summary']}",
                    kind,
                )
            )
        elif ch["kind"] == "migration":
            out.append(
                LogLine(
                    ts0,
                    comp,
                    "INFO",
                    "tr_" + hex_id(sid, ch["change_id"], n=6),
                    f"migration applied: {ch['summary']}",
                    kind,
                )
            )
        elif ch["kind"] in ("cron_job", "cert_rotation", "maintenance"):
            out.append(
                LogLine(
                    ts0,
                    comp,
                    "INFO" if ch["kind"] != "cert_rotation" else "ERROR",
                    "tr_" + hex_id(sid, ch["change_id"], n=6),
                    f"{ch['kind']}: {ch['summary']}",
                    kind,
                )
            )

    # 5. telemetry gap: the gap components stop logging at the onset --------------------------------------------------------------
    if gap_components:
        out = [ln for ln in out if not (ln.service in gap_components and ln.ts >= onset)]

    out.sort(key=lambda ln: (ln.ts, ln.service, ln.level, ln.message))
    return out


def stable_pick(sid: str, key: str, n: int) -> int:
    return int(hex_id(sid, key, n=4), 16) % n
