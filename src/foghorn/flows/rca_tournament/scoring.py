"""Hypothesis generation and deterministic scoring (pure functions; the rca_tournament workers call these).

A hypothesis is a (component, category) pair. ``score_hypothesis`` weighs the evidence for and against it with explicit,
named rules, so a verdict is explainable: every weight comes with a human readable ``text`` and (where it exists) the
evidence ``ref`` that justifies it. The ``profile`` selects the reasoning style:

* ``competent`` (also used by ``gullible`` and ``cautious``): onset ordering along the dependency graph, change-vs-onset
  proximity, signature extraction from logs, shape of the metrics, runbook/incident corroboration, red-herring rejection.
* ``naive``: blames the nearest preceding deploy (else the most recent change, else the first alert). No topology check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from foghorn.data import catalog
from foghorn.data.util import to_epoch
from foghorn.flows.rca_tournament.facts import Anomaly, Facts, hhmm

CATEGORIES: tuple[str, ...] = (
    "bad_deploy",
    "config_change",
    "memory_leak",
    "disk_full",
    "cert_expiry",
    "db_pool_exhaustion",
    "runaway_query",
    "slow_query_migration",
    "planned_maintenance",
    "cache_stampede",
    "queue_poison_message",
    "dependency_outage",
    "network_partition",
    "telemetry_gap",
)
BOT_ACTORS = frozenset({"deploybot", "cron", "migrator", "cert-rotator", "bi-batch", "dba-oncall"})
CLUSTER_WINDOW_S = 180
MIN_PARTITION_SERVICES = 6

Term = tuple[float, str, str | None]  # (weight, text, evidence ref)


def role_of(component: str) -> str:
    """service | database | cache | queue | external (derived from the catalog, no hard-coded ids)."""
    c = catalog.COMPONENTS.get(component)
    if c is None:
        return "service"
    if c.kind == "external":
        return "external"
    if c.kind == "queue" or "consumer_lag" in c.base:
        return "queue"
    if c.kind == "datastore":
        return "cache" if "hit_ratio" in c.base else "database"
    return "service"


def applicable(component: str, category: str) -> bool:
    role = role_of(component)
    if category in ("bad_deploy", "config_change", "memory_leak", "disk_full", "cert_expiry"):
        return role == "service"
    if category == "network_partition":
        return component == "edge-gateway"
    if category in ("db_pool_exhaustion", "runaway_query", "slow_query_migration"):
        return role == "database"
    if category == "planned_maintenance":
        return role in ("database", "cache", "queue")
    if category == "cache_stampede":
        return role == "cache"
    if category == "queue_poison_message":
        return role == "queue"
    if category == "dependency_outage":
        return role == "external"
    return category == "telemetry_gap"


@dataclass
class Scored:
    component: str
    category: str
    score: float
    for_: list[Term] = field(default_factory=list)
    against: list[Term] = field(default_factory=list)

    @property
    def id(self) -> str:
        return f"h:{self.component}:{self.category}"

    @property
    def refs(self) -> list[str]:
        seen: dict[str, None] = {}
        for _w, _t, ref in self.for_:
            if ref:
                seen[ref] = None
        return list(seen)

    def as_hypothesis(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "component": self.component,
            "category": self.category,
            "score": round(self.score, 3),
            "evidence": self.refs,
            "label": f"{self.category} on {self.component}",
            "for": [t for _w, t, _r in self.for_],
            "against": [t for _w, t, _r in self.against],
        }


def _prox(gap_min: float) -> float:
    """1.0 when the change is within 6 minutes before the onset, fading to 0.5 at 26 minutes."""
    return 1.0 if gap_min <= 6 else max(0.5, 1.0 - (gap_min - 6) / 40.0)


def _near_change(
    f: Facts, component: str, kinds: tuple[str, ...], onset_s: int | None, *, before: float = 15.0, after: float = 1.5
) -> tuple[dict[str, Any], float] | None:
    """The change of one of ``kinds`` on ``component`` closest before the onset: (change, gap in minutes)."""
    if onset_s is None:
        return None
    best: tuple[dict[str, Any], float] | None = None
    for ch in f.changes_on(component, kinds):
        gap = (onset_s - to_epoch(ch["ts"])) / 60.0
        if -after <= gap <= before and (best is None or abs(gap) < abs(best[1])):
            best = (ch, gap)
    return best


def _any_change_gap(f: Facts, component: str, kinds: tuple[str, ...], onset_s: int | None) -> dict[str, Any] | None:
    cands = f.changes_on(component, kinds)
    if not cands or onset_s is None:
        return cands[0] if cands else None
    return min(cands, key=lambda c: abs(onset_s - to_epoch(c["ts"])))


def explained_fraction(f: Facts, component: str) -> float:
    an = f.anomalies()
    if not an:
        return 0.0
    covered = {component, *catalog.transitive_callers(component)} if component in catalog.COMPONENTS else {component}
    return sum(1 for c in an if c in covered) / len(an)


def culprit_of(f: Facts, component: str, tolerance_s: int = 240) -> str | None:
    """The anomalous callee that explains ``component`` (it began no later and is still active), if any."""
    an = f.anomalies()
    me = an.get(component)
    if me is None:
        return None
    for d in catalog.transitive_callees(component):
        ad = an.get(d)
        if ad is not None and ad.active and ad.onset <= me.onset + tolerance_s:
            return d
    return None


def _own(f: Facts, c: str, an: Anomaly | None, t: float = 0.15) -> tuple[list[Term], list[Term]]:
    pos: list[Term] = []
    neg: list[Term] = []
    if an is None:
        if f.normal(c):
            neg.append((0.60, f"{c}'s own metrics are normal", f"metric:{c}/error_rate"))
        elif f.flat().get(c):
            neg.append((0.30, f"{c} has no fresh telemetry", None))
        else:
            neg.append((0.15, f"no anomalous metric evidence for {c} yet", None))
        return pos, neg
    ref = f"metric:{c}/{an.signal_metrics[0]}"
    onset = hhmm(_iso(an.onset))
    pos.append((t, f"{c} {'/'.join(an.signal_metrics[:2])} anomalous since {onset}", ref))
    culprit = culprit_of(f, c)
    if culprit:
        neg.append(
            (
                0.45,
                f"{c} is explained by its dependency {culprit}, which degraded no later",
                f"metric:{culprit}/error_rate",
            )
        )
    elif c in f.roots():
        pos.append((0.08, f"{c} is the earliest anomaly on its dependency chain", ref))
    ex = explained_fraction(f, c)
    if ex > 0:
        pos.append((0.10 * ex, f"explains {ex:.0%} of the anomalous components (itself and its callers)", None))
    return pos, neg


def _iso(epoch_s: int) -> str:
    from foghorn.data.util import to_iso

    return to_iso(epoch_s)


def _change_term(ch: tuple[dict[str, Any], float] | None, weight: float, what: str) -> tuple[list[Term], list[Term]]:
    if ch is None:
        return [], [(0.25, f"no {what} on the component near the onset", None)]
    c, gap = ch
    when = f"{abs(gap):.0f} min {'before' if gap >= 0 else 'after'} the onset"
    w = weight * _prox(max(gap, 0.0)) + (0.05 if 0 <= gap <= 6 else 0.0)
    return [(w, f"{c['kind']} {c['change_id']} by {c['actor']} landed {when}", f"change:{c['change_id']}")], []


def _log_term(f: Facts, component: str, family: str, weight: float, what: str) -> Term | None:
    lg = f.log_family(component, family)
    if lg is None:
        return None
    return (weight, f"{component} logs show {lg['count']}x '{lg['label']}' ({what})", f"log:{component}:{lg['label']}")


def _metric_term(f: Facts, component: str, metric: str, weight: float, what: str) -> Term | None:
    d = f.metrics.get((component, metric))
    if d is None or not d["material"]:
        return None
    return (
        weight,
        f"{component} {metric} {what} (z={d['max_z']:.0f}, onset {hhmm(d.get('onset'))})",
        f"metric:{component}/{metric}",
    )


def _metric_normal(f: Facts, component: str, metric: str) -> bool:
    d = f.metrics.get((component, metric))
    return d is not None and not d["material"] and not d.get("flatline")


def score_hypothesis(f: Facts, component: str, category: str) -> Scored:
    """Competent scoring: weigh every piece of evidence for and against (component, category)."""
    an = f.anomaly(component)
    onset = an.onset if an else None
    pos, neg = _own(f, component, an)
    if category in ("telemetry_gap", "network_partition"):
        pos, neg = [], []  # these two use their own evidence
    fn = RULES.get(category)
    if fn is not None:
        p2, n2 = fn(f, component, an, onset)
        if not p2 and category not in ("telemetry_gap", "network_partition"):
            n2 = [*n2, (0.25, f"nothing in the evidence points to {category.replace('_', ' ')} on {component}", None)]
        pos += p2
        neg += n2
    total = sum(w for w, _t, _r in pos) - sum(w for w, _t, _r in neg)
    return Scored(component, category, round(max(0.02, min(0.98, total)), 3), pos, neg)


# --- category rules ------------------------------------------------------------------------------------------------------
def _rule_bad_deploy(f: Facts, c: str, an: Anomaly | None, onset: int | None) -> tuple[list[Term], list[Term]]:
    pos, neg = _change_term(_near_change(f, c, ("deploy",), onset), 0.30, "deploy")
    if t := _log_term(f, c, "code_defect", 0.20, "new exception signature"):
        pos.append(t)
    if (cfg := _near_change(f, c, ("flag_change", "config_change"), onset)) and f.log_family(c, "flag_timeout"):
        neg.append(
            (
                0.15,
                f"a flag/config change ({cfg[0]['change_id']}) fits the logs better than the deploy",
                f"change:{cfg[0]['change_id']}",
            )
        )
    if _any_slow_climb(f, c):
        neg.append((0.25, f"{c} memory climbs slowly: a leak, not a sharp post-deploy break", f"metric:{c}/mem_pct"))
    if not f.changes_on(c, ("deploy",)):
        return pos, neg
    ch = _any_change_gap(f, c, ("deploy",), onset)
    if ch is not None and onset is not None and (onset - to_epoch(ch["ts"])) / 60.0 > 15 and not neg:
        neg.append((0.10, f"the last deploy ({ch['change_id']}) is older than the onset", f"change:{ch['change_id']}"))
    return pos, neg


def _rule_config_change(f: Facts, c: str, an: Anomaly | None, onset: int | None) -> tuple[list[Term], list[Term]]:
    pos, neg = _change_term(_near_change(f, c, ("flag_change", "config_change"), onset), 0.35, "flag or config change")
    if t := _log_term(f, c, "flag_timeout", 0.20, "timeouts introduced by the changed code path"):
        pos.append(t)
    if (dep := _near_change(f, c, ("deploy",), onset)) and f.log_family(c, "code_defect"):
        neg.append(
            (
                0.20,
                f"a deploy ({dep[0]['change_id']}) and a new exception signature fit better",
                f"change:{dep[0]['change_id']}",
            )
        )
    return pos, neg


def _any_slow_climb(f: Facts, c: str) -> bool:
    mem = f.metrics.get((c, "mem_pct"))
    if not mem or not mem["material"] or not mem.get("onset"):
        return False
    an = f.anomaly(c)
    if an is None:
        return False
    sym = [
        to_epoch(d["onset"])
        for (cc, m), d in f.metrics.items()
        if cc == c and d["material"] and m in ("error_rate", "p95_ms")
    ]
    return bool(sym) and to_epoch(mem["onset"]) <= min(sym) - 20 * 60


def _rule_memory_leak(f: Facts, c: str, an: Anomaly | None, onset: int | None) -> tuple[list[Term], list[Term]]:
    pos: list[Term] = []
    neg: list[Term] = []
    if t := _metric_term(f, c, "mem_pct", 0.30, "has climbed"):
        pos.append(t)
        if _any_slow_climb(f, c):
            pos.append((0.10, f"{c} memory started climbing well before the errors (slow leak)", f"metric:{c}/mem_pct"))
    elif _metric_normal(f, c, "mem_pct"):
        neg.append((0.40, f"{c} memory is normal", f"metric:{c}/mem_pct"))
    else:
        neg.append((0.15, f"{c} memory was not examined", None))
    if t := _log_term(f, c, "oom", 0.30, "heap exhaustion / OOM kills"):
        pos.append(t)
    old = [ch for ch in f.changes_on(c, ("deploy",)) if onset and (onset - to_epoch(ch["ts"])) / 60.0 > 60]
    if old:
        pos.append(
            (
                0.15,
                f"an older deploy ({old[0]['change_id']}) shipped a cache layer; the leak predates the alert",
                f"change:{old[0]['change_id']}",
            )
        )
    return pos, neg


def _rule_disk_full(f: Facts, c: str, an: Anomaly | None, onset: int | None) -> tuple[list[Term], list[Term]]:
    pos: list[Term] = []
    neg: list[Term] = []
    if t := _metric_term(f, c, "disk_pct", 0.40, "is filling"):
        pos.append(t)
    elif _metric_normal(f, c, "disk_pct"):
        neg.append((0.50, f"{c} disk usage is normal", f"metric:{c}/disk_pct"))
    if t := _log_term(f, c, "disk", 0.35, "writes failing"):
        pos.append(t)
    return pos, neg


def _rule_cert_expiry(f: Facts, c: str, an: Anomaly | None, onset: int | None) -> tuple[list[Term], list[Term]]:
    pos: list[Term] = []
    neg: list[Term] = []
    if t := _log_term(f, c, "cert", 0.35, "TLS handshakes fail on an expired certificate"):
        pos.append(t)
    rot = _any_change_gap(f, c, ("cert_rotation",), onset)
    if rot is not None:
        state = (rot.get("details") or {}).get("status", "unknown")
        pos.append(
            (
                0.30 if state == "failed" else 0.10,
                f"certificate rotation {rot['change_id']} on {c} status={state}",
                f"change:{rot['change_id']}",
            )
        )
    else:
        neg.append((0.10, "no certificate rotation recorded", None))
    return pos, neg


def _rule_db_pool(f: Facts, c: str, an: Anomaly | None, onset: int | None) -> tuple[list[Term], list[Term]]:
    pos: list[Term] = []
    neg: list[Term] = []
    if (t := _log_term(f, c, "pool", 0.35, "connection slots exhausted")) or (
        t := _log_term(f, c, "clients", 0.10, "connection limit hit")
    ):
        pos.append(t)
    if t := _metric_term(f, c, "saturation_pct", 0.10, "saturated"):
        pos.append(t)
    callers = [x for x in catalog.callers_of(c, sync_only=True) if f.anomaly(x)]
    if len(callers) >= 3:
        pos.append(
            (
                0.15,
                f"{len(callers)} callers of {c} degraded right after ({', '.join(callers[:4])})",
                f"metric:{callers[0]}/error_rate",
            )
        )
    if (job := _near_change(f, c, ("cron_job",), onset)) and f.log_family(c, "runaway"):
        neg.append(
            (
                0.25,
                f"a manual job ({job[0]['change_id']}) running unbounded queries explains it better",
                f"change:{job[0]['change_id']}",
            )
        )
    if _active_maintenance(f, c):
        neg.append((0.30, f"{c} is in a maintenance window", None))
    return pos, neg


def _active_maintenance(f: Facts, c: str) -> dict[str, Any] | None:
    for ch in f.changes_on(c, ("maintenance",)):
        mw = ch.get("maintenance") or {}
        if mw.get("active"):
            return ch
    return None


def _rule_runaway(f: Facts, c: str, an: Anomaly | None, onset: int | None) -> tuple[list[Term], list[Term]]:
    pos, neg = _change_term(_near_change(f, c, ("cron_job",), onset), 0.35, "ad hoc job")
    if t := _log_term(f, c, "runaway", 0.30, "unbounded query"):
        pos.append(t)
    if t := _metric_term(f, c, "cpu_pct", 0.10, "is hot"):
        pos.append(t)
    return pos, neg


def _rule_slow_query(f: Facts, c: str, an: Anomaly | None, onset: int | None) -> tuple[list[Term], list[Term]]:
    mig = _near_change(f, c, ("migration",), onset, before=40.0)
    pos, neg = _change_term(mig, 0.40, "schema migration")
    if t := _log_term(f, c, "seqscan", 0.30, "sequential scans after the migration"):
        pos.append(t)
    p95 = f.metrics.get((c, "p95_ms"))
    err = f.metrics.get((c, "error_rate"))
    if p95 and p95["material"] and err is not None and not err["material"]:
        pos.append(
            (0.15, f"latency drift without errors on {c}: the signature of a missing index", f"metric:{c}/p95_ms")
        )
    return pos, neg


def _rule_maintenance(f: Facts, c: str, an: Anomaly | None, onset: int | None) -> tuple[list[Term], list[Term]]:
    pos: list[Term] = []
    neg: list[Term] = []
    if ch := _active_maintenance(f, c):
        end = (ch.get("maintenance") or {}).get("ends_at")
        pos.append(
            (
                0.60,
                f"maintenance window {ch['change_id']} on {c} is active until {hhmm(end)}",
                f"change:{ch['change_id']}",
            )
        )
    else:
        neg.append((0.30, f"no active maintenance window on {c}", None))
    if t := _log_term(f, c, "maintenance", 0.25, "node drained for maintenance"):
        pos.append(t)
    return pos, neg


def _rule_cache(f: Facts, c: str, an: Anomaly | None, onset: int | None) -> tuple[list[Term], list[Term]]:
    pos: list[Term] = []
    neg: list[Term] = []
    if t := _metric_term(f, c, "hit_ratio", 0.40, "collapsed"):
        pos.append(t)
    elif _metric_normal(f, c, "hit_ratio"):
        neg.append((0.50, f"{c} hit ratio is normal", f"metric:{c}/hit_ratio"))
    if t := _log_term(f, c, "cache", 0.25, "evictions / bulk expiry"):
        pos.append(t)
    ch = _near_change(f, c, ("config_change", "cron_job", "flag_change"), onset)
    if ch:
        p2, _ = _change_term(ch, 0.20, "change")
        pos += p2
    return pos, neg


def _rule_queue(f: Facts, c: str, an: Anomaly | None, onset: int | None) -> tuple[list[Term], list[Term]]:
    pos: list[Term] = []
    neg: list[Term] = []
    if t := _metric_term(f, c, "consumer_lag", 0.35, "is growing"):
        pos.append(t)
    elif _metric_normal(f, c, "consumer_lag"):
        neg.append((0.50, f"{c} consumer lag is normal", f"metric:{c}/consumer_lag"))
    for fam in ("poison",):
        if t := _log_term(f, c, fam, 0.30, "a message the consumer cannot decode is redelivered"):
            pos.append(t)
    stalled = [x for x in catalog.callers_of(c, sync_only=True) if (d := f.metrics.get((x, "rps"))) and d["material"]]
    if stalled:
        pos.append((0.15, f"consumer {stalled[0]} throughput dropped", f"metric:{stalled[0]}/rps"))
    return pos, neg


def _rule_dependency(f: Facts, c: str, an: Anomaly | None, onset: int | None) -> tuple[list[Term], list[Term]]:
    pos: list[Term] = []
    neg: list[Term] = []
    if an is not None and not an.active:
        neg.append(
            (
                0.55,
                f"{c}'s degradation has already subsided (transient), while its callers keep degrading",
                f"metric:{c}/error_rate",
            )
        )
    d = f.metrics.get((c, "error_rate"))
    if d and d["material"] and (d.get("latest") or 0) > 0.2:
        pos.append((0.05, f"{c} fails {d['latest']:.0%} of calls", f"metric:{c}/error_rate"))
    for caller in catalog.callers_of(c, sync_only=True):
        if t := _log_term(f, caller, "dependency", 0.25, f"{caller} cannot reach {c}"):
            pos.append(t)
            break
    if f.roots() and c not in f.roots():
        neg.append((0.2, f"{c} is not an origin of the anomaly", None))
    return pos, neg


def _rule_partition(f: Facts, c: str, an: Anomaly | None, onset: int | None) -> tuple[list[Term], list[Term]]:
    an_all = f.anomalies()
    times_by: dict[str, int] = {x: a.onset for x, a in an_all.items()}
    for a in f.alerts:  # an alert storm is evidence even where no metric has been queried yet
        if a["rule"] != "synthetic_probe_failed" and a["component"] in catalog.COMPONENTS:
            times_by.setdefault(a["component"], to_epoch(a["fired_at"]))
    zb = [x for x in times_by if x in catalog.ZONE_B_SERVICES]
    pos: list[Term] = []
    neg: list[Term] = []
    if zb:
        times = sorted(times_by[x] for x in zb)
        window = [t for t in times if t - times[0] <= CLUSTER_WINDOW_S]
        if len(window) >= MIN_PARTITION_SERVICES:
            first = min(zb, key=lambda x: times_by[x])
            pos.append(
                (
                    0.50,
                    f"{len(window)} zone-b services failed within {CLUSTER_WINDOW_S}s of each other",
                    f"alert:{first}" if first not in an_all else f"metric:{first}/error_rate",
                )
            )
        else:
            neg.append((0.30, f"only {len(window)} zone-b services failed together", None))
    else:
        neg.append((0.40, "no zone-b service anomaly", None))
    for x in zb:
        if t := _log_term(f, x, "partition", 0.30, "zone-b cannot reach shared services"):
            pos.append(t)
            break
    if an is not None:
        pos.append((0.10, f"{c} (the zone-spanning ingress) is degraded", f"metric:{c}/error_rate"))
    return pos, neg


def _rule_telemetry(f: Facts, c: str, an: Anomaly | None, onset: int | None) -> tuple[list[Term], list[Term]]:
    flat = f.flat().get(c, [])
    pos: list[Term] = []
    neg: list[Term] = []
    if len(flat) >= 2:
        d = f.metrics[(c, flat[0])]
        pos.append(
            (
                0.60,
                f"{c} reports {len(flat)} flat metrics since {hhmm(d.get('flatline_since'))}: its telemetry stopped",
                f"metric:{c}/{flat[0]}",
            )
        )
    elif flat:
        pos.append((0.25, f"{c} has a flat metric ({flat[0]})", f"metric:{c}/{flat[0]}"))
    else:
        neg.append((0.60, f"{c} still reports fresh telemetry", None))
    reports = [r for r in f.reports if r.get("component") == c]
    if reports:
        pos.append((0.20, f"a customer reports problems with {c} while its telemetry is blind", f"report:{c}"))
    if f.anomaly(c) is not None:
        neg.append((0.30, f"{c} shows real anomalies", None))
    return pos, neg


RULES = {
    "bad_deploy": _rule_bad_deploy,
    "config_change": _rule_config_change,
    "memory_leak": _rule_memory_leak,
    "disk_full": _rule_disk_full,
    "cert_expiry": _rule_cert_expiry,
    "db_pool_exhaustion": _rule_db_pool,
    "runaway_query": _rule_runaway,
    "slow_query_migration": _rule_slow_query,
    "planned_maintenance": _rule_maintenance,
    "cache_stampede": _rule_cache,
    "queue_poison_message": _rule_queue,
    "dependency_outage": _rule_dependency,
    "network_partition": _rule_partition,
    "telemetry_gap": _rule_telemetry,
}

_KIND_CATEGORIES = {
    "deploy": ("bad_deploy",),
    "flag_change": ("config_change",),
    "config_change": ("config_change",),
    "migration": ("slow_query_migration",),
    "cron_job": ("runaway_query", "cache_stampede"),
    "cert_rotation": ("cert_expiry",),
    "maintenance": ("planned_maintenance",),
}


def has_signature(f: Facts, c: str, cat: str) -> bool:
    """Cheap pre-filter: is there ANY direct trace of ``cat`` on ``c`` (a change of the right kind, a log family, a
    metric shape)? Only those pairs deserve a tournament worker."""
    kinds = {ch["kind"] for ch in f.changes_on(c)}
    fams = f.families(c)
    mat = {m for (cc, m), d in f.metrics.items() if cc == c and d["material"]}
    checks = {
        "bad_deploy": "deploy" in kinds or "code_defect" in fams,
        "config_change": bool(kinds & {"flag_change", "config_change"}) or "flag_timeout" in fams,
        "memory_leak": "mem_pct" in mat or "oom" in fams,
        "disk_full": "disk_pct" in mat or "disk" in fams,
        "cert_expiry": "cert_rotation" in kinds or "cert" in fams,
        "db_pool_exhaustion": bool(fams & {"pool", "clients"})
        or ("saturation_pct" in mat and role_of(c) == "database"),
        "runaway_query": "cron_job" in kinds or "runaway" in fams,
        "slow_query_migration": "migration" in kinds or "seqscan" in fams,
        "planned_maintenance": "maintenance" in kinds or "maintenance" in fams,
        "cache_stampede": "hit_ratio" in mat or "cache" in fams,
        "queue_poison_message": "consumer_lag" in mat or "poison" in fams,
        "dependency_outage": bool(f.anomaly(c)),
        "telemetry_gap": bool(f.flat().get(c)),
    }
    return bool(checks.get(cat, False))


def candidate_pairs(f: Facts, extra_components: list[str] | None = None, limit: int = 16) -> list[tuple[str, str]]:
    """The (component, category) hypotheses worth a worker: every pair with a direct signature on a root, a flat
    component, a component behind a recent change (so red herrings get examined and rejected) or one the caller asks for;
    plus the zone-partition hypothesis when many roots fail together."""
    comps: list[str] = []

    def add(c: str) -> None:
        if c in catalog.COMPONENTS and c not in comps:
            comps.append(c)

    an = f.anomalies()
    roots = f.roots()
    for c in roots:
        add(c)
    for c in f.flat():
        add(c)
    for ch in f.changes[:6]:
        add(ch["component"])
    for c in extra_components or []:
        add(c)
    pairs: list[tuple[str, str]] = []
    for c in comps:
        got = [
            cat for cat in CATEGORIES if cat != "network_partition" and applicable(c, cat) and has_signature(f, c, cat)
        ]
        if not got and c in roots:
            got = [cat for cat in ("bad_deploy", "config_change", "db_pool_exhaustion") if applicable(c, cat)]
        pairs += [(c, cat) for cat in got]
    storm = {
        a["component"]
        for a in f.alerts
        if a["rule"] != "synthetic_probe_failed" and a["component"] in catalog.ZONE_B_SERVICES
    }
    if (
        len(roots) >= MIN_PARTITION_SERVICES or len(storm) >= MIN_PARTITION_SERVICES
    ) and "edge-gateway" in catalog.COMPONENTS:
        pairs.insert(0, ("edge-gateway", "network_partition"))
    if len(pairs) > limit:
        key = {c: (-(an[c].peak_z) if c in an else 0.0) for c, _ in pairs}
        pairs = sorted(pairs, key=lambda p: (p[1] != "network_partition", key[p[0]]))[:limit]
    return pairs


def rank(scored: list[Scored]) -> list[Scored]:
    return sorted(scored, key=lambda s: (-s.score, s.component, s.category))


def naive_rank(f: Facts, first_alert_component: str | None) -> list[Scored]:
    """Blame the nearest preceding deploy (else the most recent change, else the first alerted component)."""
    ordered = sorted(f.changes, key=lambda c: c["ts"], reverse=True)
    deploys = [c for c in ordered if c["kind"] == "deploy"]
    out: list[Scored] = []
    pick = (deploys or ordered or [{}])[0] or None
    if pick is not None:
        cat = _KIND_CATEGORIES.get(pick["kind"], ("bad_deploy",))[0]
        out.append(
            Scored(
                pick["component"],
                cat,
                0.8,
                [
                    (
                        0.8,
                        f"{pick['kind']} {pick['change_id']} is the nearest change before the alert",
                        f"change:{pick['change_id']}",
                    )
                ],
                [],
            )
        )
    if first_alert_component and (not out or out[0].component != first_alert_component):
        out.append(
            Scored(
                first_alert_component, "bad_deploy", 0.3, [(0.3, f"{first_alert_component} alerted first", None)], []
            )
        )
    return out or [Scored(first_alert_component or "unknown", "bad_deploy", 0.2, [], [])]


def red_herrings(f: Facts, ranked: list[Scored]) -> list[dict[str, Any]]:
    """Changes/anomalies that LOOK like causes (a change within 25 minutes before the first alert, or a loud but already
    subsided anomaly) but were examined and rejected. Returns [{"ref", "component", "why"}]."""
    if not ranked:
        return []
    top = ranked[0]
    top_closure = (
        {top.component, *catalog.transitive_callers(top.component)}
        if top.component in catalog.COMPONENTS
        else {top.component}
    )
    out: list[dict[str, Any]] = []
    an = f.anomalies()
    alert_ts = [
        to_epoch(a["fired_at"]) for a in f.alerts if a["rule"] != "synthetic_probe_failed" and a.get("fired_at")
    ]
    ref_t = min(alert_ts) if alert_ts else (min((a.onset for a in an.values()), default=None))
    for ch in f.changes:
        c = ch["component"]
        if ref_t is None or c == top.component or ch["kind"] == "maintenance":
            continue
        gap = (ref_t - to_epoch(ch["ts"])) / 60.0
        if not (-1.5 <= gap <= 25) or ch["kind"] not in ("deploy", "flag_change", "config_change"):
            continue
        off_path = c not in top_closure and top.component not in catalog.transitive_callers(c)
        quiet = f.normal(c) and not f.anomaly(c)
        if quiet or (off_path and f.queried(c)):
            why = f"{c}'s own metrics are unchanged" if quiet else f"{c} is not on the failing call path"
            if off_path and quiet:
                why = f"{c} is not on the failing call path and its own metrics are unchanged"
            out.append({"ref": f"change:{ch['change_id']}", "component": c, "why": why})
    for c, a in an.items():
        if not a.active and c != top.component and c not in top_closure:
            out.append(
                {
                    "ref": f"metric:{c}/{a.signal_metrics[0]}",
                    "component": c,
                    "why": f"{c}'s degradation already subsided",
                }
            )
    return out
