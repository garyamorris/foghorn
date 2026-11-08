"""The pure tool API: synchronous, thread-safe, read-only (except that ``execute_action`` returns a record for the caller to
append to ``applied_actions``).

Every function that touches a scenario takes ``ctx`` first (``scenario_id``, ``as_of``, ``applied_actions``) and obeys the
TIME FENCE: nothing at or after ``as_of`` + 0 s is exceeded - series are sliced to ``ts <= as_of``, logs/changes/alerts
with ``ts > as_of`` are never returned, an alert still firing at ``as_of`` has no ``resolved_at``.  The tools compute real
answers over the stored world (z-scores, onset estimates, log pattern counts, topology adjacency, recovery checks); the
recovery overlay (``overlay.py``) makes remediation visible in the telemetry.  Log text is wrapped with
``foghorn.security.wrap_untrusted`` and never appears outside that envelope.
"""

from __future__ import annotations

import json
import re
import statistics
from collections import Counter
from functools import lru_cache
from typing import Any

from foghorn.data import access, catalog, lexical, people, store
from foghorn.data import actions as act
from foghorn.data import alerts as alerts_mod
from foghorn.data.util import hex_id, in_freeze, to_epoch, to_iso
from foghorn.security import wrap_untrusted
from foghorn.tools import overlay
from foghorn.tools.context import Resolved, ToolContext, ToolError, resolve

BASELINE_POINTS = 30  # the first half hour of the telemetry window is the reference ("normal") period
MIN_BASELINE_POINTS = 15
FLATLINE_RUN = 6


# --- series access -------------------------------------------------------------------------------------------------------------------
def _component(service: str) -> catalog.Component:
    c = catalog.COMPONENTS.get(service)
    if c is None:
        raise ToolError(f"unknown service {service!r}; valid: {', '.join(catalog.COMPONENT_IDS)}")
    return c


def _check_metric(c: catalog.Component, metric: str) -> None:
    if metric not in c.metrics:
        raise ToolError(f"{c.id} has no metric {metric!r}; valid: {', '.join(c.metrics)}")


def _round(v: float, metric: str) -> float:
    prec = catalog.METRICS[metric].prec
    return float(round(v)) if prec == 0 else round(v, prec)


def _recovery(res: Resolved) -> overlay.Recovery | None:
    return overlay.build(res.scenario_id, res.actions, res.as_of_s)


def _series(
    conn: Any, res: Resolved, service: str, metric: str, rec: overlay.Recovery | None
) -> tuple[list[int], list[float]]:
    row = conn.execute(
        "SELECT t0, vals, base FROM series WHERE scenario_id = ? AND service = ? AND metric = ?",
        (res.scenario_id, service, metric),
    ).fetchone()
    if row is None:
        raise ToolError(f"no telemetry for {service}/{metric}")
    n = res.visible_points
    vals = json.loads(row["vals"])[:n]
    times = [row["t0"] + 60 * i for i in range(len(vals))]
    if rec is not None and service in rec.scope and vals:
        base = json.loads(row["base"])[:n] if row["base"] else vals
        vals = [_round(v, metric) for v in overlay.apply(vals, base, times, rec)]
    return times, vals


def _stats(values: list[float]) -> dict[str, float] | None:
    head = values[:BASELINE_POINTS]
    if len(head) < MIN_BASELINE_POINTS:
        return None
    mean = statistics.fmean(head)
    std = statistics.pstdev(head)
    return {
        "mean": mean,
        "std": std,
        "std_eff": max(std, 0.03 * abs(mean), 1e-9),
        "median": statistics.median(head),
        "n": len(head),
    }


def _dir(metric: str) -> int:
    return -1 if metric in catalog.LOWER_IS_WORSE else 1


def _flatline(values: list[float], metric: str | None = None) -> int:
    """Length of the constant run at the end of the series (0 when shorter than FLATLINE_RUN or when the value sits on a
    physical bound: a pool pinned at 100% is saturated, not stale)."""
    if len(values) < FLATLINE_RUN:
        return 0
    if metric is not None:
        md = catalog.METRICS[metric]
        if values[-1] in (md.lo, md.hi):
            return 0
    run = 1
    for i in range(len(values) - 2, -1, -1):
        if values[i] == values[-1]:
            run += 1
        else:
            break
    return run if run >= FLATLINE_RUN else 0


ONSET_Z, ONSET_RUN, ONSET_TAIL_Z = 4.0, 4, 6.0


def _onset(times: list[int], values: list[float], st: dict[str, float], metric: str) -> int | None:
    """First sample of a sustained excursion: ``ONSET_RUN`` consecutive samples beyond ``ONSET_Z`` sigmas (or, at the very end of
    the visible data, 3 samples beyond ``ONSET_TAIL_Z``), so that ordinary autocorrelated noise is not reported as an onset."""
    d = _dir(metric)
    zs = [d * (v - st["mean"]) / st["std_eff"] for v in values]
    for i in range(BASELINE_POINTS, len(values)):
        w = zs[i : i + ONSET_RUN]
        if len(w) == ONSET_RUN and all(z > ONSET_Z for z in w):
            return times[i]
        if 3 <= len(w) < ONSET_RUN and all(z > ONSET_TAIL_Z for z in w):
            return times[i]
    return None


def _trend(values: list[float], st: dict[str, float] | None, metric: str) -> str:
    if _flatline(values, metric):
        return "flatline"
    if st is None or len(values) < 8:
        return "insufficient"
    d = _dir(metric)
    zs = [d * (v - st["mean"]) / st["std_eff"] for v in values[BASELINE_POINTS:]]
    if not zs or max(zs) < 3.0:
        return "flat"
    if zs[-1] < 1.5:
        return "recovered" if max(zs) >= 3.0 else "flat"
    tail = zs[-5:]
    slope = (tail[-1] - tail[0]) / max(len(tail) - 1, 1)
    if slope > 0.5:
        return "rising"
    if slope < -0.5:
        return "falling"
    return "elevated"


# --- query_metric / compare_baseline -------------------------------------------------------------------------------------------------
def query_metric(
    ctx: ToolContext | dict[str, Any], service: str, metric: str, window_minutes: int = 30
) -> dict[str, Any]:
    """A metric's last ``window_minutes`` samples (1/min, never after ``as_of``) with baseline, z-score and onset estimate."""
    c = _component(service)
    _check_metric(c, metric)
    res = resolve(ctx)
    window_minutes = max(1, min(int(window_minutes), 135))
    rec = _recovery(res)
    with store.connect_ro(access.db_path(res.scenario_id)) as conn:
        times, values = _series(conn, res, service, metric, rec)
    md = catalog.METRICS[metric]
    st = _stats(values)
    out: dict[str, Any] = {
        "service": service,
        "metric": metric,
        "unit": md.unit,
        "as_of": res.as_of,
        "window_minutes": window_minutes,
        "points": [[to_iso(t), v] for t, v in zip(times[-window_minutes:], values[-window_minutes:], strict=True)],
        "n": len(values[-window_minutes:]),
        "latest": None,
        "baseline": None,
        "z_score": None,
        "max_z": None,
        "peak": None,
        "onset_estimate": None,
        "trend": "insufficient",
        "data_quality": "insufficient_history" if not values else "ok",
        "flatline_minutes": 0,
        "actions_considered": len(res.actions),
    }
    if not values:
        return out
    out["latest"] = {"ts": to_iso(times[-1]), "value": values[-1]}
    flat = _flatline(values, metric)
    if flat:
        out["data_quality"] = "flatline"
        out["flatline_minutes"] = flat
        out["flatline_since"] = to_iso(times[-flat])
    out["trend"] = _trend(values, st, metric)
    if st is None:
        out["data_quality"] = "insufficient_history" if not flat else "flatline"
        return out
    d = _dir(metric)
    out["baseline"] = {
        "mean": _r(st["mean"]),
        "std": _r(st["std_eff"]),
        "median": _r(st["median"]),
        "n": st["n"],
        "window": "first 30 minutes of the telemetry window",
    }
    out["z_score"] = _r((values[-1] - st["mean"]) / st["std_eff"])  # signed: positive = above the baseline
    post = values[BASELINE_POINTS:]
    if post:
        zs = [d * (v - st["mean"]) / st["std_eff"] for v in post]
        worst = max(range(len(zs)), key=lambda i: zs[i])
        out["max_z"] = _r(zs[worst])
        out["peak"] = {"ts": to_iso(times[BASELINE_POINTS + worst]), "value": post[worst]}
    onset = _onset(times, values, st, metric)
    out["onset_estimate"] = to_iso(onset) if onset is not None else None
    if metric == "p95_ms":
        out["slo"] = {"p95_ms": c.slo_p95_ms, "breached": values[-1] > c.slo_p95_ms}
    return out


def _r(x: float) -> float:
    return round(x, 4) if abs(x) < 100 else round(x, 2)


def compare_baseline(ctx: ToolContext | dict[str, Any], service: str, metric: str) -> dict[str, Any]:
    """Current level (mean of the last 3 samples) versus the reference period: delta, ratio, z-score, verdict."""
    c = _component(service)
    _check_metric(c, metric)
    res = resolve(ctx)
    rec = _recovery(res)
    with store.connect_ro(access.db_path(res.scenario_id)) as conn:
        times, values = _series(conn, res, service, metric, rec)
    st = _stats(values)
    out: dict[str, Any] = {
        "service": service,
        "metric": metric,
        "unit": catalog.METRICS[metric].unit,
        "as_of": res.as_of,
        "baseline": None,
        "current": None,
        "verdict": "insufficient_data",
    }
    if not values:
        return out
    cur = statistics.fmean(values[-3:])
    out["current"] = {"value": _r(cur), "ts": to_iso(times[-1]), "samples": len(values[-3:])}
    flat = _flatline(values, metric)
    if st is None:
        out["verdict"] = "flatline" if flat else "insufficient_data"
        return out
    out["baseline"] = {"mean": _r(st["mean"]), "std": _r(st["std_eff"]), "median": _r(st["median"]), "n": st["n"]}
    z = (cur - st["mean"]) / st["std_eff"]
    out.update(
        {
            "delta": _r(cur - st["mean"]),
            "delta_pct": _r(100.0 * (cur - st["mean"]) / st["mean"]) if st["mean"] else None,
            "ratio": _r(cur / st["mean"]) if st["mean"] else None,
            "z_score": _r(z),
        }
    )
    bad = _dir(metric) * z  # positive = worse than the baseline (higher error/latency, lower rps/hit ratio)
    if flat:
        verdict = "flatline"
    elif bad <= -3:
        verdict = "better_than_baseline"
    elif bad < 3:
        verdict = "normal"
    elif bad < 8:
        verdict = "elevated"
    elif bad < 30:
        verdict = "degraded"
    else:
        verdict = "critical"
    out["verdict"] = verdict
    onset = _onset(times, values, st, metric)
    out["since"] = to_iso(onset) if onset is not None else None
    if metric == "p95_ms":
        out["slo"] = {"p95_ms": c.slo_p95_ms, "breached": cur > c.slo_p95_ms}
    return out


# --- logs ---------------------------------------------------------------------------------------------------------------------------
_LEVEL_ORDER = {"INFO": 0, "WARN": 1, "ERROR": 2, "FATAL": 3}
_ID = re.compile(r"(?<![0-9a-zA-Z])[0-9a-f]{6,}(?![0-9a-zA-Z])")
_NUM = re.compile(r"\d+(?:\.\d+)?")


def _signature(message: str) -> str:
    return _NUM.sub("<n>", _ID.sub("<id>", message))[:200]


def _u(seq: int) -> float:
    return (seq * 2654435761 % 4294967296) / 4294967296


def search_logs(
    ctx: ToolContext | dict[str, Any],
    service: str,
    pattern: str = "",
    level: str | None = None,
    window_minutes: int = 30,
    limit: int = 50,
) -> dict[str, Any]:
    """Log lines of ``service`` in the last ``window_minutes`` before ``as_of`` matching ``pattern`` (regex or substring,
    case-insensitive) and ``level`` (``ERROR`` exact, ``WARN+`` = at least WARN). Raw text is only inside ``text``."""
    _component(service)
    res = resolve(ctx)
    window_minutes = max(1, min(int(window_minutes), 135))
    limit = max(1, min(int(limit), 200))
    lo = res.as_of_s - window_minutes * 60
    rx: re.Pattern[str] | None = None
    if pattern:
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error:
            rx = re.compile(re.escape(pattern), re.IGNORECASE)
    lvl_exact: str | None = None
    lvl_min: int | None = None
    if level:
        lv = level.strip().upper()
        if lv.endswith("+"):
            lvl_min = _LEVEL_ORDER.get(lv[:-1], 0)
        else:
            lvl_exact = lv
    rec = _recovery(res)
    with store.connect_ro(access.db_path(res.scenario_id)) as conn:
        rows = conn.execute(
            "SELECT seq, ts, level, trace_id, message, kind FROM logs WHERE scenario_id = ? AND service = ? AND ts <= ? AND ts > ? ORDER BY ts, seq",
            (res.scenario_id, service, res.as_of_s, lo),
        ).fetchall()
    kept = []
    for r in rows:
        if (
            rec is not None
            and service in rec.scope
            and r["kind"] in ("sig", "prop", "inj")
            and r["ts"] > rec.first_applied_s
            and _u(r["seq"]) < rec.factor(r["ts"])
        ):
            continue  # the failure stopped: its signature lines stop appearing
        if lvl_exact and r["level"] != lvl_exact:
            continue
        if lvl_min is not None and _LEVEL_ORDER.get(r["level"], 0) < lvl_min:
            continue
        if rx and not rx.search(r["message"]):
            continue
        kept.append(r)
    total = len(kept)
    shown = kept[-limit:]
    sigs: dict[str, dict[str, Any]] = {}
    for r in kept:
        sg = _signature(r["message"])
        e = sigs.setdefault(
            sg, {"count": 0, "level": r["level"], "first": r["ts"], "last": r["ts"], "id": "p_" + hex_id(sg, n=4)}
        )
        e["count"] += 1
        e["last"] = r["ts"]
        if _LEVEL_ORDER.get(r["level"], 0) > _LEVEL_ORDER.get(e["level"], 0):
            e["level"] = r["level"]
    top = sorted(sigs.items(), key=lambda kv: (-kv[1]["count"], kv[0]))[:8]
    pid_of = {sg: e["id"] for sg, e in sigs.items()}
    body_lines = [
        f"{to_iso(r['ts'])} {r['level']:<5} [{pid_of[_signature(r['message'])]}] trace={r['trace_id']} {r['message']}"
        for r in shown
    ]
    legend = [f"[{e['id']}] x{e['count']} {e['level']} {sg}" for sg, e in top]
    text = wrap_untrusted(
        f"logs:{service}", "\n".join(body_lines) + ("\n-- patterns --\n" + "\n".join(legend) if legend else "")
    )
    return {
        "service": service,
        "pattern": pattern,
        "level": level,
        "window_minutes": window_minutes,
        "as_of": res.as_of,
        "total_matches": total,
        "returned": len(shown),
        "truncated": total > len(shown),
        "counts_by_level": dict(sorted(Counter(r["level"] for r in kept).items())),
        "patterns": [
            {
                "id": e["id"],
                "count": e["count"],
                "level": e["level"],
                "first_ts": to_iso(e["first"]),
                "last_ts": to_iso(e["last"]),
            }
            for _sg, e in top
        ],
        "lines": [
            {
                "ts": to_iso(r["ts"]),
                "level": r["level"],
                "trace_id": r["trace_id"],
                "pattern_id": pid_of[_signature(r["message"])],
            }
            for r in shown
        ],
        "text": text,
        "injection_suspected": 'flagged="possible-injection"' in text,
    }


# --- changes ---------------------------------------------------------------------------------------------------------------------------
def list_changes(
    ctx: ToolContext | dict[str, Any],
    window_minutes: int = 240,
    component: str | None = None,
    lookback_days: float | None = None,
) -> dict[str, Any]:
    """Changes (deploys, flags, config, migrations, cron jobs, maintenance, cert rotations) in the window before ``as_of``,
    most recent first. ``lookback_days`` widens the window (a slow leak can come from a days-old deploy)."""
    if component is not None:
        _component(component)
    res = resolve(ctx)
    eff = float(window_minutes)
    if lookback_days:
        eff = max(eff, float(lookback_days) * 1440.0)
    lo = res.as_of_s - int(eff * 60)
    sql = "SELECT change_id, ts, kind, component, actor, summary, details FROM changes WHERE scenario_id = ? AND ts <= ? AND ts >= ?"
    params: list[Any] = [res.scenario_id, res.as_of_s, lo]
    if component:
        sql += " AND component = ?"
        params.append(component)
    sql += " ORDER BY ts DESC, change_id"
    with store.connect_ro(access.db_path(res.scenario_id)) as conn:
        rows = conn.execute(sql, params).fetchall()
    changes = []
    for r in rows:
        d = json.loads(r["details"])
        item: dict[str, Any] = {
            "change_id": r["change_id"],
            "ts": to_iso(r["ts"]),
            "kind": r["kind"],
            "component": r["component"],
            "actor": r["actor"],
            "summary": r["summary"],
            "minutes_before_as_of": round((res.as_of_s - r["ts"]) / 60.0, 1),
            "details": d,
        }
        if (
            r["kind"] == "maintenance"
        ):  # a window is announced in advance: its end is knowable even if it lies after as_of
            ends = r["ts"] + int((d.get("end_min", 0) - d.get("start_min", 0)) * 60)
            item["maintenance_window"] = {"ends_at": to_iso(ends), "active": res.as_of_s < ends}
            item["details"] = {k: v for k, v in d.items() if k not in ("start_min", "end_min")}
        changes.append(item)
    return {
        "as_of": res.as_of,
        "window_minutes": eff,
        "component": component,
        "lookback_days": lookback_days,
        "count": len(changes),
        "changes": changes,
    }


# --- topology ------------------------------------------------------------------------------------------------------------------------------
def get_topology(ctx: ToolContext | dict[str, Any], service: str) -> dict[str, Any]:
    c = _component(service)
    resolve(ctx)  # validates the scenario; the topology itself is static
    shared = sorted({o for dep in c.depends_on for o in catalog.callers_of(dep) if o != service})
    up = catalog.transitive_callers(service)
    return {
        "service": service,
        "kind": c.kind,
        "tier": c.tier,
        "team": c.team,
        "oncall_rota": c.rota,
        "zones": list(c.zones),
        "criticality": c.criticality,
        "slo": {"availability": c.slo_availability, "p95_ms": c.slo_p95_ms},
        "deploy_cadence": c.deploy_cadence,
        "flags": list(c.flags),
        "configs": list(c.configs),
        "metrics": list(c.metrics),
        "depends_on": [
            {
                "id": d,
                "kind": catalog.COMPONENTS[d].kind,
                "edge": "async" if (service, d) in catalog.ASYNC_EDGES else "sync",
            }
            for d in c.depends_on
        ],
        "depended_on_by": [
            {
                "id": d,
                "kind": catalog.COMPONENTS[d].kind,
                "edge": "async" if (d, service) in catalog.ASYNC_EDGES else "sync",
            }
            for d in catalog.callers_of(service)
        ],
        "shares_dependencies_with": shared,
        "caller_depth": max(up.values(), default=0),
        "runbooks": _runbooks_for(service),
    }


def blast_radius(ctx: ToolContext | dict[str, Any], service: str) -> dict[str, Any]:
    """Who is affected if ``service`` degrades: transitive synchronous callers by hop, tier-0 exposure, customer-facing flag."""
    c = _component(service)
    resolve(ctx)
    callers = catalog.transitive_callers(service)
    items = [
        {"id": k, "hops": h, "tier": catalog.COMPONENTS[k].tier, "criticality": catalog.COMPONENTS[k].criticality}
        for k, h in sorted(callers.items(), key=lambda kv: (kv[1], kv[0]))
    ]
    tier0 = [i["id"] for i in items if i["tier"] == 0]
    return {
        "service": service,
        "tier": c.tier,
        "direct_callers": [i["id"] for i in items if i["hops"] == 1],
        "transitive_callers": items,
        "total_affected_if_down": len(items),
        "tier0_exposed": tier0,
        "revenue_path_exposed": any(i["criticality"] == "revenue" for i in items) or c.criticality == "revenue",
        "customer_facing_exposed": "web-frontend" in callers or service == "web-frontend",
        "downstream_dependencies": sorted(catalog.transitive_callees(service)),
    }


# --- corpus search -------------------------------------------------------------------------------------------------------------------------
@lru_cache(maxsize=8)
def _runbook_index(sig: str) -> tuple[lexical.Index, dict[str, tuple[dict[str, Any], str]]]:
    docs_ = access.markdown_docs("runbooks")
    by_id = {m["id"]: (m, body) for m, body in docs_}
    idx = lexical.Index(
        [
            lexical.Doc(
                m["id"],
                {
                    "title": m["title"],
                    "symptoms": " ".join(m["symptoms"]),
                    "applies": " ".join(m["applies_to"]),
                    "body": body,
                },
            )
            for m, body in docs_
        ],
        {"title": 3, "symptoms": 3, "applies": 1, "body": 1},
    )
    return idx, by_id


def _runbook_sig() -> str:
    return str(access.manifest()["datasets"]["runbooks"]["sha256"])


def _runbooks_for(service: str) -> list[str]:
    _, by_id = _runbook_index(_runbook_sig())
    ids = [rid for rid, (m, _b) in by_id.items() if service in m["applies_to"]]
    ids.sort(key=lambda rid: (len(by_id[rid][0]["applies_to"]), rid))
    return ids[:8]


def search_runbooks(query: str, k: int = 4, *, exclude_never_automatic: bool = False) -> dict[str, Any]:
    """Lexical (BM25) search over the 24 runbooks. Purely lexical on purpose: a generic 'database slow / timeout' query ranks
    RB-017 (restart the primary, NEVER automatic) first. Each hit exposes its actions with ``never_automatic`` flags."""
    idx, by_id = _runbook_index(_runbook_sig())
    hits = []
    for d, score, matched in idx.search(query, k=max(1, min(int(k), 24)) + (8 if exclude_never_automatic else 0)):
        meta, body = by_id[d.id]
        acts = meta["actions"]
        if exclude_never_automatic and any(a.get("never_automatic") for a in acts):
            continue
        mit = re.search(r"## Mitigation\n(.*?)\n\n", body, re.S)
        hits.append(
            {
                "id": d.id,
                "title": meta["title"],
                "score": score,
                "matched_terms": matched,
                "applies_to": meta["applies_to"],
                "symptoms": meta["symptoms"],
                "actions": acts,
                "avoid": meta["avoid"],
                "requires": meta["requires"],
                "owner": meta["owner"],
                "reviewed": meta["reviewed"],
                "mitigation": (mit.group(1).strip() if mit else "")[:600],
            }
        )
    return {"query": query, "k": k, "hits": hits[: max(1, min(int(k), 24))]}


@lru_cache(maxsize=8)
def _incident_index(sig: str) -> tuple[lexical.Index, dict[str, dict[str, Any]]]:
    recs = list(access.read_jsonl(access.ensure_datasets() / "reference" / "past_incidents.jsonl"))
    idx = lexical.Index(
        [
            lexical.Doc(
                r["id"],
                {
                    "title": r["title"],
                    "symptoms": r["symptoms"],
                    "cat": r["root_cause_category"].replace("_", " "),
                    "comps": " ".join(r["components"]),
                    "res": " ".join(r["resolution"]),
                    "lessons": " ".join(r["lessons"]),
                },
            )
            for r in recs
        ],
        {"title": 3, "symptoms": 2, "cat": 2, "comps": 2, "res": 1, "lessons": 2},
    )
    return idx, {r["id"]: dict(r) for r in recs}


def recall_incidents(query: str, k: int = 4) -> dict[str, Any]:
    """Lexical recall over the 120 past incidents (all older than every scenario): similar symptoms, what fixed them, lessons."""
    idx, by_id = _incident_index(str(access.manifest()["datasets"]["past_incidents"]["sha256"]))
    hits = []
    for d, score, matched in idx.search(query, k=max(1, min(int(k), 20))):
        r = by_id[d.id]
        hits.append(
            {
                "id": r["id"],
                "score": score,
                "matched_terms": matched,
                "title": r["title"],
                "opened_at": r["opened_at"],
                "severity": r["severity"],
                "components": r["components"],
                "root_cause_category": r["root_cause_category"],
                "root_cause_component": r["root_cause_component"],
                "symptoms": r["symptoms"],
                "resolution": r["resolution"],
                "mttr_min": r["mttr_min"],
                "lessons": r["lessons"],
                "runbook": r.get("runbook"),
            }
        )
    return {"query": query, "k": k, "hits": hits}


# --- on-call ------------------------------------------------------------------------------------------------------------------------------------
def get_oncall(ctx: ToolContext | dict[str, Any], component: str) -> dict[str, Any]:
    c = _component(component)
    res = resolve(ctx)
    svc = people.oncall_at(c.rota, res.as_of)
    return {
        "component": component,
        "team": c.team,
        "as_of": res.as_of,
        **svc,
        "incident_commander": people.oncall_at("rota-commander", res.as_of)["primary"],
        "dba": people.oncall_at("rota-dba", res.as_of)["primary"],
        "approvers": {
            "low": [],
            "medium": [svc["primary"]] if svc["primary"] else [],
            "high": [x for x in (svc["primary"], people.oncall_at("rota-commander", res.as_of)["primary"]) if x],
        },
    }


# --- alerts (extension) ----------------------------------------------------------------------------------------------------------------------------
def list_alerts(
    ctx: ToolContext | dict[str, Any], component: str | None = None, window_minutes: int = 60, active_only: bool = False
) -> dict[str, Any]:
    """Alerts that fired in the last ``window_minutes`` before ``as_of`` (rule evaluation over the series; flapping included)."""
    if component is not None:
        _component(component)
    res = resolve(ctx)
    lo = res.as_of_s - max(1, int(window_minutes)) * 60
    rec = _recovery(res)
    sql = "SELECT alert_id, rule, metric, component, fired_at, resolved_at, severity, value, threshold, for_s, event_type FROM alerts WHERE scenario_id = ? AND fired_at <= ? AND fired_at > ?"
    params: list[Any] = [res.scenario_id, res.as_of_s, lo]
    if component:
        sql += " AND component = ?"
        params.append(component)
    with store.connect_ro(access.db_path(res.scenario_id)) as conn:
        rows = [dict(r) for r in conn.execute(sql + " ORDER BY fired_at, alert_id", params).fetchall()]
        if rec is not None:  # re-evaluate the rules on the remediated series for components in scope
            cache: dict[tuple[str, str], tuple[list[int], list[float]]] = {}
            for r in rows:
                if r["component"] not in rec.scope:
                    continue
                key = (r["component"], r["metric"])
                if key not in cache:
                    cache[key] = _series(conn, res, r["component"], r["metric"], rec)
                times, vals = cache[key]
                rule = alerts_mod.RULES_BY_NAME[r["rule"]]
                comp = catalog.COMPONENTS[r["component"]]
                fired = [
                    a
                    for a in alerts_mod.evaluate_series(rule, comp, vals, times, alerts_mod.baseline_median(vals))
                    if a["fired_at_s"] == r["fired_at"]
                ]
                r["resolved_at"] = fired[0]["resolved_at_s"] if fired else r["resolved_at"]
    out = []
    for r in rows:
        resolved = (
            r["resolved_at"] if r["resolved_at"] is not None and r["resolved_at"] <= res.as_of_s else None
        )  # never leak a future resolution
        if active_only and resolved is not None:
            continue
        out.append(
            {
                "alert_id": r["alert_id"],
                "rule": r["rule"],
                "metric": r["metric"],
                "component": r["component"],
                "severity": r["severity"],
                "fired_at": to_iso(r["fired_at"]),
                "resolved_at": to_iso(resolved) if resolved is not None else None,
                "value": r["value"],
                "threshold": r["threshold"],
                "for_s": r["for_s"],
                "type": r["event_type"],
            }
        )
    return {
        "as_of": res.as_of,
        "window_minutes": window_minutes,
        "component": component,
        "count": len(out),
        "alerts": out,
    }


# --- actions ------------------------------------------------------------------------------------------------------------------------------------------
def execute_action(
    ctx: ToolContext | dict[str, Any], action: str, target: str, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    """SIMULATE a remediation. Returns the action record (``outcome`` = executed | blocked | rejected); the CALLER appends it to
    ``applied_actions`` (the LangChain wrapper does it through a state update). Guards: unknown action/target -> rejected;
    never-automatic actions, missing break-glass during the change freeze and non-CONCURRENTLY index builds -> blocked."""
    res = resolve(ctx)
    params = dict(params or {})
    rid = "act_" + hex_id(
        res.scenario_id, action, target, res.as_of, json.dumps(params, sort_keys=True, default=str), n=10
    )
    spec = act.ACTIONS.get(action)
    frozen = in_freeze(res.as_of_s)
    policy: dict[str, Any] = {
        "freeze_active": frozen,
        "break_glass_required": False,
        "break_glass_provided": False,
        "never_automatic": bool(spec and spec.never_automatic),
        "approvers_required": spec.approvers if spec else None,
        "extra_roles": list(spec.extra_roles) if spec else [],
    }
    rec: dict[str, Any] = {
        "id": rid,
        "scenario_id": res.scenario_id,
        "action": action,
        "target": target,
        "params": params,
        "applied_at": res.as_of,
        "simulated": True,
        "risk": spec.risk if spec else None,
        "reversible": spec.reversible if spec else None,
        "policy": policy,
    }

    def done(outcome: str, reason: str) -> dict[str, Any]:
        rec.update({"outcome": outcome, "ok": outcome == "executed", "reason": reason})
        if outcome == "executed":
            rec["next"] = "verify_recovery after a few virtual minutes (advance as_of)"
        return rec

    if spec is None:
        return done("rejected", f"unknown action {action!r}; valid: {', '.join(sorted(act.ACTIONS))}")
    if not act.known_target(target):
        return done("rejected", f"unknown target {target!r} (component, feature flag, config key or zone)")
    if spec.never_automatic:
        return done("blocked", f"{action} is never automatic (POL-005): a DBA performs it by hand outside Foghorn")
    if act.break_glass_needed(action, target, frozen=frozen):
        policy["break_glass_required"] = True
        bg = params.get("break_glass") or {}
        ok = (
            isinstance(bg, dict)
            and len(str(bg.get("reason", ""))) >= act.BREAK_GLASS_REASON_MIN_CHARS
            and bool(bg.get("second_approver"))
        )
        policy["break_glass_provided"] = ok
        if not ok:
            return done(
                "blocked",
                "change freeze (POL-003): break-glass needs a reason of at least 20 characters and a second approver",
            )
    if action == "add_index":
        sql = str(params.get("sql", ""))
        if not sql:
            return done("blocked", "add_index needs params.sql (CREATE INDEX CONCURRENTLY ...)")
        if re.search(act.INDEX_FORBIDDEN_PATTERN, sql):
            return done(
                "blocked",
                "index builds must use CREATE INDEX CONCURRENTLY (RB-009): a plain CREATE INDEX blocks writes",
            )
    return done("executed", f"{action} on {target} simulated at {res.as_of}")


# --- verify_recovery --------------------------------------------------------------------------------------------------------------------------------------
def _healthy(metric: str, value: float, st: dict[str, float], slo_p95: int) -> bool:
    mean, std = st["mean"], st["std_eff"]
    if metric == "rps":
        return value >= 0.8 * mean
    if metric == "hit_ratio":
        return value >= 0.9 * mean
    tol = max(mean + 4 * std, 1.25 * mean)
    if metric == "error_rate":
        tol = max(tol, mean + 0.003)  # a few tenths of a percent is noise, not an incident
    if metric == "consumer_lag":
        tol = max(mean + 4 * std, 3 * mean)
    if metric == "p95_ms":
        tol = min(tol, float(slo_p95)) if mean < slo_p95 else tol
    return value <= tol


def verify_recovery(ctx: ToolContext | dict[str, Any], service: str) -> dict[str, Any]:
    """Is ``service`` back to normal at ``as_of``? Reads the recovery overlay of ``applied_actions``: the last 3 samples of each
    metric must be healthy. Status: healthy | recovered | recovering | degraded | unknown (flatline / no baseline)."""
    c = _component(service)
    res = resolve(ctx)
    rec = _recovery(res)
    checks: list[dict[str, Any]] = []
    degraded_before = False
    improving = False
    flat = False
    with store.connect_ro(access.db_path(res.scenario_id)) as conn:
        for metric in c.metrics:
            _times, vals = _series(conn, res, service, metric, rec)
            st = _stats(vals)
            if st is None or len(vals) < 3:
                continue
            if _flatline(vals, metric):
                flat = True
            _, raw = _series(conn, res, service, metric, None)
            d = _dir(metric)
            worst = (
                max(d * (v - st["mean"]) / st["std_eff"] for v in raw[BASELINE_POINTS:])
                if len(raw) > BASELINE_POINTS
                else 0.0
            )
            last3 = vals[-3:]
            ok = all(_healthy(metric, v, st, c.slo_p95_ms) for v in last3)
            was_bad = (
                not all(_healthy(metric, v, st, c.slo_p95_ms) for v in raw[BASELINE_POINTS:])
                if len(raw) > BASELINE_POINTS
                else False
            )
            degraded_before = degraded_before or was_bad
            cur_z = d * (statistics.fmean(last3) - st["mean"]) / st["std_eff"]
            if was_bad and not ok and worst > 0 and cur_z < 0.5 * worst:
                improving = True
            checks.append(
                {
                    "metric": metric,
                    "latest": last3[-1],
                    "baseline_mean": _r(st["mean"]),
                    "ok": ok,
                    "was_degraded": was_bad,
                }
            )
    last_action = max(
        (
            to_epoch(str(a["applied_at"]))
            for a in res.actions
            if a.get("outcome", "executed") == "executed" and a.get("applied_at")
        ),
        default=None,
    )
    if flat:
        status, recovered, reason = (
            "unknown",
            False,
            "metrics are flat (no fresh telemetry): recovery cannot be verified",
        )
    elif not checks:
        status, recovered, reason = "unknown", False, "not enough telemetry before as_of"
    elif not degraded_before:
        status, recovered, reason = "healthy", True, "the service was not degraded"
    elif all(ch["ok"] for ch in checks):
        status, recovered, reason = (
            "recovered",
            True,
            "all metrics are back within normal bounds for the last 3 samples",
        )
    elif improving and rec is not None:
        status, recovered, reason = "recovering", False, "metrics are improving but not yet normal; verify again later"
    else:
        status, recovered = "degraded", False
        reason = (
            "still degraded"
            + ("" if res.actions else "; no remediation has been applied")
            + ("; the applied actions did not help this service" if res.actions and rec is None else "")
        )
    return {
        "service": service,
        "as_of": res.as_of,
        "status": status,
        "recovered": recovered,
        "reason": reason,
        "checks": checks,
        "actions_considered": [a.get("id") for a in res.actions],
        "minutes_since_last_action": round((res.as_of_s - last_action) / 60.0, 1) if last_action is not None else None,
    }
