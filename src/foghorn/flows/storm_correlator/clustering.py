"""Alert-storm clustering: pure, deterministic, no I/O (the graph wraps each step in a node).

Pipeline (each step is one node or one ``Send`` task of the graph):

1. ``normalize``      alerts -> distinct alerts per dedupe key; benign flapping / info / non-signals are IGNORED
2. ``candidates``     every alerting component, every non-alerting component that >= 2 alerting components call
                      ("hidden shared dependency"), and every zone where >= 3 components and >= 70 % of what is deployed there alert
3. ``analyze``        one candidate -> one scored cluster (the ``Send`` map step)::

                          score = 0.55 coverage + 0.25 purity + 0.20 onset
                          coverage = weight of the alerts on cluster members / weight of all storm alerts
                          purity   = alerting members / components the root would take down (its transitive callers,
                                     or every component deployed in the zone)
                          onset    = 1.0 when the root alerts no later than its members (it can be the cause),
                                     decaying to 0 over 10 minutes; 0.5 (neutral) for hidden dependencies and zones

4. ``plan_merges``    clusters whose member sets overlap >= 80 % (overlap coefficient) form a merge group; each
                      group is merged by one ``Send`` task and the reduce step runs again (bounded by a level counter)
5. ``finalize``       greedy disjoint partition by score; a cluster must keep >= 10 % of the storm weight to survive

Alert weight: critical 4, high 3, medium 2, low 1, info 0.5.
"""

from __future__ import annotations

from typing import Any

from foghorn.data.util import to_epoch
from foghorn.flows.event_router import topology

SEV_WEIGHT = {"critical": 4.0, "high": 3.0, "medium": 2.0, "low": 1.0, "info": 0.5}
SIGNAL_TYPES = frozenset({"alert.fired", "slo.burn_rate", "healthcheck.failed"})
NOISE_RULES = frozenset({"synthetic_probe_failed"})
MAX_CANDIDATES = 24
MIN_ZONE_COMPONENTS = 3
MIN_ZONE_PURITY = 0.7  # a zone is a common-cause hypothesis only when most of what is deployed there alerts
ONSET_DECAY_S = 600.0
MIN_SHARED_CALLERS = 2
OVERLAP_MERGE = 0.8
MIN_SUPPORT = 0.10
W_COVERAGE, W_PURITY, W_ONSET = 0.55, 0.25, 0.20


def normalize(alerts: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """-> (distinct signal alerts, ignored alerts with a reason)."""
    kept: dict[str, dict[str, Any]] = {}
    ignored: list[dict[str, Any]] = []
    for e in alerts:
        eid = str(e.get("id") or "?")
        comp = (e.get("subject") or {}).get("component")
        rule = (e.get("payload") or {}).get("rule")
        if e.get("type") not in SIGNAL_TYPES:
            ignored.append({"id": eid, "reason": f"not a signal ({e.get('type')})"})
        elif not e.get("occurred_at"):
            ignored.append({"id": eid, "reason": "no occurred_at"})
        elif not topology.known(comp):
            ignored.append({"id": eid, "reason": f"unknown component {comp!r}"})
        elif rule in NOISE_RULES or e.get("severity") == "info":
            ignored.append({"id": eid, "reason": "benign flapping or info severity"})
        else:
            key = str(e.get("dedupe_key") or f"{comp}:{rule}")
            t = to_epoch(e["occurred_at"])
            sev = str(e.get("severity") or "low")
            cur = kept.get(key)
            if cur is None:
                kept[key] = {
                    "id": eid,
                    "key": key,
                    "component": str(comp),
                    "rule": rule,
                    "metric": (e.get("payload") or {}).get("metric"),
                    "severity": sev,
                    "type": e["type"],
                    "t": t,
                    "n": 1,
                }
            else:
                cur["n"] += 1
                cur["t"] = min(cur["t"], t)
                if SEV_WEIGHT.get(sev, 0) > SEV_WEIGHT.get(cur["severity"], 0):
                    cur["severity"] = sev
    return sorted(kept.values(), key=lambda a: (a["t"], a["key"])), ignored


def summarise(alerts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per alerting component: distinct alerts, weight, first alert time, worst alert."""
    comps: dict[str, dict[str, Any]] = {}
    for a in alerts:
        c = comps.setdefault(a["component"], {"n": 0, "weight": 0.0, "first_t": a["t"], "worst": None, "rules": []})
        c["n"] += 1
        c["weight"] += SEV_WEIGHT.get(a["severity"], 1.0)
        c["first_t"] = min(c["first_t"], a["t"])
        if a["rule"] and a["rule"] not in c["rules"]:
            c["rules"].append(a["rule"])
        if c["worst"] is None or SEV_WEIGHT.get(a["severity"], 0) > SEV_WEIGHT.get(c["worst"]["severity"], 0):
            c["worst"] = {"severity": a["severity"], "rule": a["rule"], "metric": a.get("metric")}
    return comps


def candidates(comps: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for cid in sorted(comps, key=lambda c: (-comps[c]["weight"], c)):
        out.append({"id": f"component:{cid}", "kind": "component", "root": cid, "label": cid})
    callers_of_hidden: dict[str, set[str]] = {}
    for cid in comps:
        for dep in topology.callees_of(cid):
            if dep not in comps:
                callers_of_hidden.setdefault(dep, set()).add(cid)
    for dep in sorted(callers_of_hidden):
        if len(callers_of_hidden[dep]) >= MIN_SHARED_CALLERS:
            out.append(
                {"id": f"dependency:{dep}", "kind": "dependency", "root": dep, "label": f"shared dependency {dep}"}
            )
    zones: dict[str, set[str]] = {}
    for cid in comps:
        for z in topology.zones_of(cid):
            zones.setdefault(z, set()).add(cid)
    for z in sorted(zones):
        deployed = [c for c in topology.catalog() if z in topology.zones_of(c)]
        if len(zones[z]) >= MIN_ZONE_COMPONENTS and len(zones[z]) / len(deployed) >= MIN_ZONE_PURITY:
            out.append({"id": f"zone:{z}", "kind": "zone", "root": z, "label": f"common cause {z}"})
    return out[:MAX_CANDIDATES]


def expected_members(cand: dict[str, Any]) -> frozenset[str]:
    root = cand["root"]
    if cand["kind"] == "zone":
        return frozenset(c for c in topology.catalog() if root in topology.zones_of(c))
    return frozenset({root, *topology.transitive_callers(root)})


def _round(x: float) -> float:
    return round(x, 4)


def analyze(
    cand: dict[str, Any],
    comps: dict[str, dict[str, Any]],
    total_weight: float,
    *,
    evidence: dict[str, Any] | None = None,
    degraded: bool = False,
) -> dict[str, Any]:
    exp = expected_members(cand)
    members = sorted(c for c in exp if c in comps)
    return rescore(
        {
            "id": cand["id"],
            "kind": cand["kind"],
            "root": cand["root"],
            "label": cand["label"],
            "members": members,
            "expected": len(exp),
            "level": 0,
            "absorbed": [],
            "degraded": degraded,
            "evidence": evidence,
            "superseded_by": None,
        },
        comps,
        total_weight,
    )


def rescore(cluster: dict[str, Any], comps: dict[str, dict[str, Any]], total_weight: float) -> dict[str, Any]:
    members = cluster["members"]
    weight = sum(comps[c]["weight"] for c in members)
    coverage = weight / total_weight if total_weight else 0.0
    purity = min(1.0, len(members) / cluster["expected"]) if cluster["expected"] else 0.0
    onset = 0.5
    first = min((comps[c]["first_t"] for c in members), default=None)
    if cluster["kind"] == "component" and cluster["root"] in comps and first is not None:
        onset = 1.0 - min(1.0, max(0, comps[cluster["root"]]["first_t"] - first) / ONSET_DECAY_S)
    score = W_COVERAGE * coverage + W_PURITY * purity + W_ONSET * onset
    if (cluster.get("evidence") or {}).get("anomalous"):
        score += 0.02  # tie-break: the root's own metric confirms an excursion
    score += 0.01 * len(cluster.get("absorbed", []))
    return {
        **cluster,
        "alerts": sum(comps[c]["n"] for c in members),
        "weight": weight,
        "coverage": _round(coverage),
        "purity": _round(purity),
        "onset": _round(onset),
        "score": _round(min(score, 1.0)),
        "first_t": first,
    }


def _live(clusters: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [c for c in clusters.values() if not c.get("superseded_by") and c["members"]]


def overlap(a: dict[str, Any], b: dict[str, Any]) -> float:
    sa, sb = set(a["members"]), set(b["members"])
    return len(sa & sb) / min(len(sa), len(sb)) if sa and sb else 0.0


def plan_merges(clusters: dict[str, dict[str, Any]]) -> list[list[str]]:
    """Groups (size >= 2) of live clusters whose member sets overlap >= OVERLAP_MERGE (union-find)."""
    live = sorted(_live(clusters), key=lambda c: c["id"])
    parent = {c["id"]: c["id"] for c in live}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(live):
        for b in live[i + 1 :]:
            if overlap(a, b) >= OVERLAP_MERGE:
                parent[find(a["id"])] = find(b["id"])
    groups: dict[str, list[str]] = {}
    for c in live:
        groups.setdefault(find(c["id"]), []).append(c["id"])
    return sorted((sorted(g) for g in groups.values() if len(g) > 1), key=lambda g: g[0])


def merge_group(
    group: list[dict[str, Any]], comps: dict[str, dict[str, Any]], total_weight: float
) -> dict[str, dict[str, Any]]:
    """Merge one group: the best-scoring cluster absorbs the others.  -> updates for the ``clusters`` channel."""
    best = max(group, key=lambda c: (c["score"], len(c["members"]), c["id"]))
    others = [c for c in group if c["id"] != best["id"]]
    union = sorted({m for c in group for m in c["members"]})
    merged = rescore(
        {
            **best,
            "members": union,
            "level": max(c["level"] for c in group) + 1,
            "absorbed": sorted({*best.get("absorbed", []), *(o["id"] for o in others)}),
        },
        comps,
        total_weight,
    )
    updates = {merged["id"]: merged}
    for o in others:
        updates[o["id"]] = {**o, "superseded_by": merged["id"]}
    return updates


def merge_clusters(left: dict[str, Any] | None, right: dict[str, Any] | None) -> dict[str, Any]:
    """Reducer for the ``clusters`` channel: order-independent and idempotent under retries and forks.
    Higher ``level`` wins, then higher ``score``; ``superseded_by`` is monotonic (once superseded, always superseded)."""
    out = dict(left or {})
    for cid, new in (right or {}).items():
        cur = out.get(cid)
        if cur is None:
            out[cid] = new
            continue
        winner = max((cur, new), key=lambda c: (c.get("level", 0), c.get("score", 0.0), not c.get("degraded", False)))
        sup = cur.get("superseded_by") or new.get("superseded_by")
        out[cid] = {**winner, "superseded_by": sup}
    return out


def finalize(
    clusters: dict[str, dict[str, Any]], comps: dict[str, dict[str, Any]], total_weight: float
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """-> (final disjoint clusters, dropped clusters with reason, components no cluster explains)."""
    claimed: set[str] = set()
    final: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for c in sorted(_live(clusters), key=lambda c: (-c["score"], c["id"])):
        own = [m for m in c["members"] if m not in claimed]
        weight = sum(comps[m]["weight"] for m in own)
        if not own:
            dropped.append({"id": c["id"], "reason": "all members explained by a higher-scoring cluster"})
        elif total_weight and weight / total_weight < MIN_SUPPORT:
            dropped.append({"id": c["id"], "reason": f"keeps only {weight / total_weight:.0%} of the storm weight"})
        else:
            claimed.update(own)
            final.append({**c, "members": own, "weight": weight})
            final[-1] = rescore(final[-1], comps, total_weight)
    unexplained = sorted(set(comps) - claimed)
    return final, dropped, unexplained
