"""A deterministic expert-system "oracle" that solves scenarios through the PUBLIC tool API only (acceptance A4).

It proves the scenarios are solvable and fair: no hidden truth is read, only ``foghorn.tools.api`` (and the topology). The
rules mirror what the runbooks teach (hit-ratio collapse -> cache, consumer lag -> queue, many same-minute failures in one
zone -> partition, ...), then fall back to "earliest onset, pushed down to the deepest anomalous callee".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from foghorn.data import api as data
from foghorn.data import catalog
from foghorn.data.util import to_epoch
from foghorn.tools import api
from foghorn.tools.context import advance_as_of

INVESTIGATION_MINUTES = 10  # virtual time spent investigating before concluding
SIGNAL_METRICS = (
    "error_rate",
    "p95_ms",
    "saturation_pct",
    "hit_ratio",
    "consumer_lag",
    "disk_pct",
    "mem_pct",
)  # rps is mostly diurnal drift


@dataclass
class Finding:
    verdict: str  # incident | noise | abstain
    root: str | None = None
    action: str | None = None
    anomalies: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    changes: list[str] = field(default_factory=list)
    log_patterns: dict[str, list[str]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    ctx: dict[str, Any] = field(default_factory=dict)
    tool_calls: int = 0
    blind: list[str] = field(default_factory=list)


def _material(metric: str, q: dict[str, Any]) -> bool:
    """A sustained (>= 3 samples), statistically loud AND materially large deviation - not a probe blip or diurnal drift."""
    if (
        q["max_z"] is None
        or q["max_z"] < 6.0
        or q["data_quality"] == "flatline"
        or q["onset_estimate"] is None
        or not q["baseline"]
        or not q["peak"]
    ):
        return False
    mean, peak = q["baseline"]["mean"], q["peak"]["value"]
    if metric in ("disk_pct", "mem_pct"):
        return abs(peak - mean) >= 8.0
    return abs(peak - mean) >= 0.25 * abs(mean)


def solve(sid: str) -> Finding:  # noqa: PLR0912, PLR0915
    base_ctx: dict[str, Any] = {"scenario_id": sid, "as_of": None, "applied_actions": []}
    ctx = {**base_ctx, "as_of": advance_as_of(base_ctx, INVESTIGATION_MINUTES)}
    f = Finding("incident", ctx=ctx)
    calls = 0
    flat: dict[str, int] = {}
    for comp in catalog.COMPONENT_IDS:
        for m in catalog.COMPONENTS[comp].metrics:
            q = api.query_metric(ctx, comp, m, 135)
            calls += 1
            if q["data_quality"] == "flatline":
                flat[comp] = flat.get(comp, 0) + 1
            if (m in SIGNAL_METRICS or m in ("cpu_pct", "rps")) and _material(m, q):
                f.anomalies[(comp, m)] = q
    f.tool_calls = calls
    sig = {k: v for k, v in f.anomalies.items() if k[1] in SIGNAL_METRICS}  # rps/cpu: corroboration only

    def onset(c: str) -> int:
        os_ = [to_epoch(v["onset_estimate"]) for (cc, _m), v in sig.items() if cc == c and v["onset_estimate"]]
        return min(os_) if os_ else 10**12

    def err_onset(c: str) -> int:
        q = sig.get((c, "error_rate"))
        return to_epoch(q["onset_estimate"]) if q and q["onset_estimate"] else 10**12

    comps = sorted({c for c, _ in sig})
    # R0 abstain: a service whose telemetry is flat is a blind spot, not a calm system
    blind = sorted(c for c, n in flat.items() if n >= 5)
    if blind:
        f.verdict, f.root, f.blind = "abstain", blind[0], blind
        f.notes.append(f"flatline telemetry on {blind}")
        f.action = "escalate_human"
        return f
    # R1 maintenance noise
    ch = api.list_changes(ctx, 240)
    calls += 1
    maint = [c for c in ch["changes"] if c["kind"] == "maintenance"]
    if maint and any(m["component"] in comps for m in maint):
        f.verdict, f.root, f.action = "noise", maint[0]["component"], "suppress_alert"
        f.changes = [m["change_id"] for m in maint]
        return f

    def err_latest(c: str) -> float:
        q = f.anomalies.get((c, "error_rate"))
        return float(q["latest"]["value"]) if q else 0.0

    root: str | None = None
    if ("sessions-cache", "hit_ratio") in sig:
        root, f.action = "sessions-cache", "revert_config"
    elif ("event-bus", "consumer_lag") in sig:
        root, f.action = "event-bus", "quarantine_message"
    else:
        zb = [c for c in comps if c in catalog.ZONE_B_SERVICES and ("error_rate" in {m for cc, m in sig if cc == c})]
        if len(zb) >= 8 and max(err_onset(c) for c in zb) - min(err_onset(c) for c in zb) <= 180:
            root, f.action = "edge-gateway", "drain_zone"
        else:
            ext = [c for c in comps if catalog.COMPONENTS[c].kind == "external" and err_latest(c) > 0.1]
            disk = [c for (c, m), q in sig.items() if m == "disk_pct" and q["latest"]["value"] > 90]
            mem = [c for (c, m), q in sig.items() if m == "mem_pct" and q["latest"]["value"] > 85]
            if ext:
                root, f.action = ext[0], "enable_fallback_provider"
            elif disk:
                root, f.action = disk[0], "rotate_logs"
            elif mem:
                root, f.action = mem[0], "restart_service"
            else:
                ds = [c for c in comps if catalog.COMPONENTS[c].kind == "datastore"]
                if ds:
                    root, f.action = ds[0], "kill_queries"
                else:
                    cand = sorted(comps, key=lambda c: (onset(c), -len(catalog.transitive_callees(c))))[0]
                    moved = True
                    while moved:
                        moved = False
                        for callee in catalog.callees_of(cand, sync_only=True):
                            if callee in comps and onset(callee) <= onset(cand) + 240:
                                cand, moved = callee, True
                                break
                    root, f.action = cand, "rollback_deploy"
    f.root = root
    # evidence: changes (with a 7-day lookback, as the runbooks advise when nothing is found nearby), logs
    wide = api.list_changes(ctx, 240, component=root, lookback_days=7)
    calls += 1
    f.changes = [c["change_id"] for c in wide["changes"][:3]]
    if root:
        lg = api.search_logs(ctx, root, "", "WARN+", 90, 200)
        calls += 1
        f.log_patterns[root] = re.findall(r"^\[p_[0-9a-f]+\] x\d+ \w+ (.*)$", lg["text"], re.M)
        for c in comps:
            if c != root:
                lg2 = api.search_logs(ctx, c, "", "WARN+", 90, 200)
                f.log_patterns[c] = re.findall(r"^\[p_[0-9a-f]+\] x\d+ \w+ (.*)$", lg2["text"], re.M)
                calls += 1
    f.tool_calls = calls
    return f


def evidence_recall(f: Finding, key_evidence: list[dict[str, str]]) -> float:
    hit = 0
    for e in key_evidence:
        if e["kind"] == "change":
            hit += e["ref"] in f.changes
        elif e["kind"] == "metric":
            comp, metric = e["ref"].split("/")
            hit += (comp, metric) in f.anomalies or comp in f.blind
        elif e["kind"] == "log":
            svc, _, pat = e["ref"].partition(":")
            hit += any(pat.lower() in p.lower() for p in f.log_patterns.get(svc, []))
    return hit / max(len(key_evidence), 1)


def truth_of(sid: str) -> dict[str, Any]:
    return data.get_truth(sid)
