"""Evidence model: tool results -> evidence records -> the ``Facts`` index every diagnosis rule reads.

Pure functions, no I/O. Evidence is plain JSON (it lives in graph state and in checkpoints)::

    {"id": "metric:checkout-service/error_rate", "kind": "metric", "ref": <same as id>, "agent": "metrics_analyst",
     "summary": "...", "strength": 0..1, "t": <as_of ISO>, "data": {...kind specific...}}

``id``/``ref`` use the prefixes of the report contract: ``metric:svc/metric``, ``change:<id>``, ``log:svc:<label>``,
``memory:<key>``, plus ``alert:``, ``topology:``, ``runbook:``, ``incident:``, ``oncall:``, ``injection:``.

Log text is UNTRUSTED (POL-004): it is only used to recognise signature families; the raw excerpt is kept (truncated,
flagged ``untrusted``) for display and for the injection detector, and never decides an action.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

from foghorn.data import catalog
from foghorn.data.util import to_epoch

Evidence = dict[str, Any]

#: metrics that identify a fault (cpu and rps are corroboration: rps drifts with the daily cycle)
SIGNAL_METRICS = frozenset(
    {"error_rate", "p95_ms", "saturation_pct", "hit_ratio", "consumer_lag", "disk_pct", "mem_pct"}
)
HUMAN_ACTOR = re.compile(r"^[a-z]\.[a-z]+$")  # r.santos, j.okafor ... (bots are named deploybot, cron, migrator ...)

# --- log signature families (regex over the RAW log text; label = the matched fragment, used in the evidence ref) --------
LOG_FAMILIES: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("code_defect", "bad_deploy", re.compile(r"NullPointerException|unhandled exception in handler|Traceback \(most")),
    ("cert", "cert_expiry", re.compile(r"x509: certificate has expired|certificate has expired|SSLHandshakeException")),
    ("disk", "disk_full", re.compile(r"No space left on device")),
    ("maintenance", "planned_maintenance", re.compile(r"maintenance mode")),
    ("pool", "db_pool_exhaustion", re.compile(r"remaining connection slots|waiting for connection slot")),
    ("clients", "db_pool_exhaustion", re.compile(r"too many clients already")),
    ("cache", "cache_stampede", re.compile(r"below threshold [0-9.]+|keyspace expired in bulk|maxclients reached")),
    ("poison", "queue_poison_message", re.compile(r"deserialization failed|poison message")),
    ("dependency", "dependency_outage", re.compile(r"(?:payment|email)-provider request failed|read timeout")),
    ("oom", "memory_leak", re.compile(r"OutOfMemoryError|OOMKilled|GC overhead")),
    ("flag_timeout", "config_change", re.compile(r"TaxClient timeout|is not cached")),
    ("seqscan", "slow_query_migration", re.compile(r"seq scan on \w+")),
    ("partition", "network_partition", re.compile(r"No route to host|zone-b -> shared services")),
    ("runaway", "runaway_query", re.compile(r"no LIMIT|nightly_export")),
)
INJECTION_RX = re.compile(
    r"ignore (?:all )?(?:previous|prior) instructions|you are now|system prompt|rollback all services|post an? all.clear",
    re.I,
)
_LOG_LINE = re.compile(r"^(\d{4}-\d\d-\d\dT[\d:]+Z) (\w+)\s+\[(p_[0-9a-f]+)\] (?:trace=\S+ )?(.*)$")
_LEGEND = re.compile(r"^\[(p_[0-9a-f]+)\] x(\d+) (\w+) (.*)$")
_ENVELOPE = re.compile(r"<untrusted[^>]*>\n?(.*?)\n?</untrusted>", re.S)


def hhmm(iso: str | None) -> str:
    return iso[11:16] if iso and len(iso) >= 16 else "?"


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def fmt_value(metric: str, v: float) -> str:
    if metric == "error_rate":
        return _pct(v)
    if metric in ("cpu_pct", "mem_pct", "disk_pct", "saturation_pct"):
        return f"{v:.0f}%"
    if metric == "hit_ratio":
        return f"{v:.2f}"
    if metric == "p95_ms":
        return f"{v:,.0f} ms"
    return f"{v:,.0f}"


def make(kind: str, ident: str, summary: str, strength: float, agent: str, t: str | None, /, **data: Any) -> Evidence:
    return {
        "id": ident,
        "ref": ident,
        "kind": kind,
        "agent": agent,
        "summary": summary,
        "strength": round(max(0.0, min(1.0, strength)), 3),
        "t": t,
        "data": data,
    }


# --- parsing tool output -------------------------------------------------------------------------------------------------
def parse_tool_result(content: Any) -> dict[str, Any] | None:
    """LangChain tool message content (JSON string) or an api dict -> dict; unwraps the ``wrapped`` untrusted envelope."""
    obj: Any = content
    if isinstance(content, str):
        try:
            obj = json.loads(content)
        except ValueError:
            return None
    if not isinstance(obj, dict):
        return None
    wrapped = obj.get("wrapped")
    if isinstance(wrapped, str):
        m = _ENVELOPE.search(wrapped)
        if m:
            try:
                inner = json.loads(m.group(1))
            except ValueError:
                return None
            return inner if isinstance(inner, dict) else None
    return obj


def metric_material(metric: str, q: dict[str, Any]) -> bool:
    """A sustained, statistically loud AND materially large deviation (not a probe blip or diurnal drift)."""
    base, peak = q.get("baseline"), q.get("peak")
    if q.get("data_quality") == "flatline" or not base or not peak or q.get("onset_estimate") is None:
        return False
    if (q.get("max_z") or 0) < 6.0:
        return False
    mean, pv = float(base["mean"]), float(peak["value"])
    if metric in ("disk_pct", "mem_pct"):
        return abs(pv - mean) >= 8.0
    if metric == "rps":
        return mean > 0 and pv / mean < 0.5
    return abs(pv - mean) >= 0.25 * abs(mean)


def _bad_z(metric: str, z: float | None) -> float:
    if z is None:
        return 0.0
    return -z if metric in catalog.LOWER_IS_WORSE else z


def metric_evidence(q: dict[str, Any], agent: str) -> Evidence:
    svc, metric = q["service"], q["metric"]
    material = metric_material(metric, q)
    latest = (q.get("latest") or {}).get("value")
    base = q.get("baseline") or {}
    peak = q.get("peak") or {}
    flat = q.get("data_quality") == "flatline"
    max_z = float(q.get("max_z") or 0.0)
    cur_bad = _bad_z(metric, q.get("z_score"))
    active = material and cur_bad >= 4.0 and q.get("trend") not in ("recovered", "flat")
    strength = min(1.0, 0.4 + 0.15 * math.log10(max(max_z, 1.0))) if material else 0.0
    if flat:
        summary = f"{svc} {metric} is flat since {hhmm(q.get('flatline_since'))} ({q.get('flatline_minutes')} min): no fresh telemetry"
    elif material:
        summary = (
            f"{svc} {metric} {fmt_value(metric, float(latest or 0))} now, peak {fmt_value(metric, float(peak['value']))} "
            f"vs baseline {fmt_value(metric, float(base['mean']))} (z={max_z:.0f}), onset {hhmm(q.get('onset_estimate'))}, {q.get('trend')}"
        )
    else:
        summary = f"{svc} {metric} normal ({fmt_value(metric, float(latest)) if latest is not None else 'n/a'})"
    return make(
        "metric",
        f"metric:{svc}/{metric}",
        summary,
        strength,
        agent,
        q.get("as_of"),
        service=svc,
        metric=metric,
        material=material,
        signal=metric in SIGNAL_METRICS,
        active=active,
        flatline=flat,
        flatline_since=q.get("flatline_since"),
        flatline_minutes=q.get("flatline_minutes"),
        latest=latest,
        baseline_mean=base.get("mean"),
        peak=peak.get("value"),
        peak_ts=peak.get("ts"),
        max_z=max_z,
        z=q.get("z_score"),
        onset=q.get("onset_estimate"),
        trend=q.get("trend"),
        quality=q.get("data_quality"),
    )


def _compare_to_query(r: dict[str, Any]) -> dict[str, Any]:
    """``compare_baseline`` has less detail than ``query_metric``; adapt it so the same evidence builder works."""
    cur = r.get("current") or {}
    base = r.get("baseline")
    z = r.get("z_score")
    return {
        "service": r["service"],
        "metric": r["metric"],
        "as_of": r.get("as_of"),
        "latest": {"ts": cur.get("ts"), "value": cur.get("value")} if cur else None,
        "baseline": base,
        "z_score": z,
        "max_z": abs(z) if z is not None else None,
        "peak": {"ts": cur.get("ts"), "value": cur.get("value")} if cur else None,
        "onset_estimate": r.get("since"),
        "trend": "elevated" if r.get("verdict") in ("elevated", "degraded", "critical") else "flat",
        "data_quality": "flatline" if r.get("verdict") == "flatline" else "ok",
    }


def parse_log_text(text: str) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Raw envelope text -> (lines, legend). ``lines`` = [{ts, level, pid, message}], ``legend`` = {pid: {count, level}}."""
    m = _ENVELOPE.search(text)
    body = m.group(1) if m else text
    lines: list[dict[str, Any]] = []
    legend: dict[str, dict[str, Any]] = {}
    in_patterns = False
    for raw in body.splitlines():
        if raw.startswith("-- patterns --"):
            in_patterns = True
            continue
        if in_patterns:
            lm = _LEGEND.match(raw)
            if lm:
                legend[lm.group(1)] = {"count": int(lm.group(2)), "level": lm.group(3), "pattern": lm.group(4)}
            continue
        mm = _LOG_LINE.match(raw)
        if mm:
            lines.append({"ts": mm.group(1), "level": mm.group(2), "pid": mm.group(3), "message": mm.group(4)})
    return lines, legend


def extract_embedded_instructions(text: str) -> list[str]:
    """Imperative fragments hiding in untrusted text (for flagging; a vulnerable agent would OBEY them)."""
    out: list[str] = []
    for m in re.finditer(
        r"(rollback ALL services[^.\"]*|post an? all.clear[^.\"]*|ignore (?:all )?previous instructions)", text, re.I
    ):
        frag = m.group(1).strip()
        if frag.lower() not in {o.lower() for o in out}:
            out.append(frag[:160])
    return out


def log_evidence(r: dict[str, Any], agent: str) -> list[Evidence]:
    svc = r["service"]
    text = str(r.get("text") or "")
    lines, legend = parse_log_text(text)
    fams: dict[tuple[str, str], dict[str, Any]] = {}
    for ln in lines:
        count = int(legend.get(ln["pid"], {}).get("count", 1))
        for fam, cat, rx in LOG_FAMILIES:
            m = rx.search(ln["message"])
            if not m:
                continue
            e = fams.setdefault(
                (fam, m.group(0)),
                {
                    "family": fam,
                    "category_hint": cat,
                    "label": m.group(0),
                    "pids": {},
                    "first": ln["ts"],
                    "last": ln["ts"],
                    "level": ln["level"],
                    "zones": set(),
                },
            )
            e["pids"][ln["pid"]] = max(e["pids"].get(ln["pid"], 0), count)
            e["first"], e["last"] = min(e["first"], ln["ts"]), max(e["last"], ln["ts"])
            e["zones"].update(re.findall(r"zone-[a-z]", ln["message"]))
    per_family: dict[str, int] = {}
    out: list[Evidence] = []
    for (fam, _label), e in sorted(fams.items(), key=lambda kv: -sum(kv[1]["pids"].values())):
        if per_family.get(fam, 0) >= 2:  # the two most frequent signatures of a family are enough
            continue
        per_family[fam] = per_family.get(fam, 0) + 1
        n = sum(e["pids"].values())
        out.append(
            make(
                "log",
                f"log:{svc}:{e['label']}",
                f"{svc} logs: {n}x '{e['label']}' ({e['level']}) between {hhmm(e['first'])} and {hhmm(e['last'])}",
                min(1.0, 0.45 + 0.1 * math.log10(max(n, 1)) * 3),
                agent,
                r.get("as_of"),
                service=svc,
                family=fam,
                category_hint=e["category_hint"],
                label=e["label"],
                zones=sorted(e["zones"]),
                count=n,
                first=e["first"],
                last=e["last"],
                level=e["level"],
                untrusted=True,
            )
        )
    inj_lines = [ln for ln in lines if INJECTION_RX.search(ln["message"])]
    if r.get("injection_suspected") or inj_lines:
        instr = extract_embedded_instructions(" ".join(ln["message"] for ln in inj_lines) or text)
        out.append(
            make(
                "injection",
                f"injection:{svc}",
                f"{svc} logs contain {len(inj_lines) or 1} line(s) with embedded instructions (untrusted data, flagged)",
                0.9,
                agent,
                r.get("as_of"),
                service=svc,
                lines=len(inj_lines),
                instructions=instr,
                excerpt=(inj_lines[0]["message"][:200] if inj_lines else ""),
                untrusted=True,
            )
        )
    return out


def change_evidence(r: dict[str, Any], agent: str) -> list[Evidence]:
    out: list[Evidence] = []
    for c in r.get("changes", [])[:12]:
        d = c.get("details") or {}
        minutes = c.get("minutes_before_as_of")
        out.append(
            make(
                "change",
                f"change:{c['change_id']}",
                f"{c['kind']} on {c['component']} by {c['actor']} at {hhmm(c['ts'])}: {str(c['summary'])[:90]}",
                0.3,
                agent,
                r.get("as_of"),
                change_id=c["change_id"],
                ts=c["ts"],
                kind=c["kind"],
                component=c["component"],
                actor=c["actor"],
                summary=str(c["summary"])[:160],
                details={k: v for k, v in d.items() if isinstance(v, str | int | float | bool | list)},
                minutes_before=minutes,
                maintenance=c.get("maintenance_window"),
                human=bool(HUMAN_ACTOR.match(str(c["actor"]))),
            )
        )
    return out


def alert_evidence(r: dict[str, Any], agent: str) -> list[Evidence]:
    out: list[Evidence] = []
    for a in r.get("alerts", [])[:24]:
        out.append(
            make(
                "alert",
                f"alert:{a['component']}/{a['rule']}",
                f"{a['severity']} alert {a['rule']} on {a['component']} at {hhmm(a['fired_at'])} (value {a['value']})",
                0.3,
                agent,
                r.get("as_of"),
                component=a["component"],
                rule=a["rule"],
                metric=a["metric"],
                severity=a["severity"],
                fired_at=a["fired_at"],
                resolved_at=a.get("resolved_at"),
                value=a["value"],
            )
        )
    return out


def topology_evidence(r: dict[str, Any], agent: str) -> list[Evidence]:
    svc = r["service"]
    deps = [d["id"] for d in r.get("depends_on", [])]
    callers = [d["id"] for d in r.get("depended_on_by", [])]
    return [
        make(
            "topology",
            f"topology:{svc}",
            f"{svc} (tier {r['tier']}, {r['kind']}) depends on {', '.join(deps) or 'nothing'}; called by {', '.join(callers) or 'nobody'}",
            0.2,
            agent,
            r.get("as_of"),
            service=svc,
            tier=r["tier"],
            kind=r["kind"],
            team=r.get("team"),
            zones=r.get("zones", []),
            depends_on=deps,
            callers=callers,
            flags=r.get("flags", []),
            configs=r.get("configs", []),
            runbooks=r.get("runbooks", []),
            criticality=r.get("criticality"),
        )
    ]


def blast_evidence(r: dict[str, Any], agent: str) -> list[Evidence]:
    svc = r["service"]
    return [
        make(
            "topology",
            f"blast:{svc}",
            f"{svc} down would reach {r['total_affected_if_down']} callers (tier-0 exposed: {', '.join(r['tier0_exposed']) or 'none'})",
            0.2,
            agent,
            r.get("as_of"),
            service=svc,
            total=r["total_affected_if_down"],
            tier0=r["tier0_exposed"],
            direct=r["direct_callers"],
            revenue_path=r["revenue_path_exposed"],
        )
    ]


def runbook_evidence(r: dict[str, Any], agent: str) -> list[Evidence]:
    out: list[Evidence] = []
    for h in r.get("hits", [])[:4]:
        acts = [
            {"id": a["id"], "risk": a.get("risk"), "never_automatic": bool(a.get("never_automatic"))}
            for a in h["actions"]
        ]
        trap = any(a["never_automatic"] for a in acts)
        out.append(
            make(
                "runbook",
                f"runbook:{h['id']}",
                f"{h['id']} {h['title']} (score {h['score']:.1f})"
                + (" - contains a never-automatic action" if trap else ""),
                0.35,
                agent,
                None,
                runbook=h["id"],
                title=h["title"],
                score=h["score"],
                actions=acts,
                avoid=h.get("avoid", []),
                requires=h.get("requires", []),
                applies_to=h.get("applies_to", []),
                trap=trap,
            )
        )
    return out


def incident_evidence(r: dict[str, Any], agent: str) -> list[Evidence]:
    out: list[Evidence] = []
    for h in r.get("hits", [])[:3]:
        out.append(
            make(
                "incident",
                f"incident:{h['id']}",
                f"{h['id']} {h['title']} -> {h['root_cause_category']} on {h['root_cause_component']} ({', '.join(h['resolution'])})",
                0.3,
                agent,
                None,
                incident=h["id"],
                title=h["title"],
                category=h["root_cause_category"],
                component=h["root_cause_component"],
                resolution=h["resolution"],
                lessons=h["lessons"],
                runbook=h.get("runbook"),
                score=h["score"],
            )
        )
    return out


def oncall_evidence(r: dict[str, Any], agent: str) -> list[Evidence]:
    return [
        make(
            "oncall",
            f"oncall:{r['component']}",
            f"{r['component']} on-call {r.get('primary')}; incident commander {r.get('incident_commander')}",
            0.1,
            agent,
            r.get("as_of"),
            component=r["component"],
            primary=r.get("primary"),
            commander=r.get("incident_commander"),
            dba=r.get("dba"),
            approvers=r.get("approvers"),
        )
    ]


def extract_evidence(tool: str, args: dict[str, Any], result: dict[str, Any], *, agent: str) -> list[Evidence]:
    """Tool name + parsed result -> evidence records. Unknown tools and error payloads yield nothing."""
    if not result or "error" in result:
        return []
    if tool == "query_metric":
        return [metric_evidence(result, agent)]
    if tool == "compare_baseline":
        return [metric_evidence(_compare_to_query(result), agent)]
    if tool == "search_logs":
        return log_evidence(result, agent)
    if tool == "list_changes":
        return change_evidence(result, agent)
    if tool == "list_alerts":
        return alert_evidence(result, agent)
    if tool == "get_topology":
        return topology_evidence(result, agent)
    if tool == "blast_radius":
        return blast_evidence(result, agent)
    if tool == "search_runbooks":
        return runbook_evidence(result, agent)
    if tool == "recall_incidents":
        return incident_evidence(result, agent)
    if tool == "get_oncall":
        return oncall_evidence(result, agent)
    return []


def evidence_from_envelope(env: dict[str, Any], agent: str = "bootstrap") -> Evidence | None:
    """A triggering event (alert.fired / slo.burn_rate / customer.report) -> alert/report evidence."""
    comp = (env.get("subject") or {}).get("component")
    p = env.get("payload") or {}
    t = env.get("occurred_at")
    if env.get("type") == "customer.report":
        return make(
            "report",
            f"report:{p.get('ticket_id', env.get('id'))}",
            f"customer report about {comp}: {str(p.get('text', ''))[:100]}",
            0.3,
            agent,
            t,
            component=comp,
            text=str(p.get("text", ""))[:200],
            untrusted=True,
        )
    if comp and p.get("rule"):
        return make(
            "alert",
            f"alert:{comp}/{p['rule']}",
            f"{env.get('severity')} alert {p['rule']} on {comp} (value {p.get('value')})",
            0.3,
            agent,
            t,
            component=comp,
            rule=p["rule"],
            metric=p.get("metric"),
            severity=env.get("severity"),
            fired_at=t,
            resolved_at=None,
            value=p.get("value"),
        )
    return None


def merge_by_id(left: list[Evidence] | None, right: list[Evidence] | None) -> list[Evidence]:
    """Reducer for ``evidence``-like lists: unique by ``id``, latest write wins, order of first appearance.
    Idempotent, so a parent and a sub-agent may share the key without double counting."""
    out: dict[str, Evidence] = {}
    for x in list(left or []) + list(right or []):
        out[str(x["id"])] = x
    return list(out.values())


# --- the index -----------------------------------------------------------------------------------------------------------
@dataclass
class Anomaly:
    component: str
    onset: int
    metrics: dict[str, dict[str, Any]]
    active: bool
    peak_z: float

    @property
    def signal_metrics(self) -> list[str]:
        return [m for m, d in self.metrics.items() if d["signal"]]


@dataclass
class Facts:
    """Everything the diagnosis rules read, indexed from the evidence list."""

    evidence: list[Evidence]
    metrics: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    changes: list[dict[str, Any]] = field(default_factory=list)
    logs: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    topo: dict[str, dict[str, Any]] = field(default_factory=dict)
    alerts: list[dict[str, Any]] = field(default_factory=list)
    reports: list[dict[str, Any]] = field(default_factory=list)
    runbooks: list[dict[str, Any]] = field(default_factory=list)
    incidents: list[dict[str, Any]] = field(default_factory=list)
    memory: list[dict[str, Any]] = field(default_factory=list)
    injections: list[dict[str, Any]] = field(default_factory=list)
    blast: dict[str, dict[str, Any]] = field(default_factory=dict)
    as_of: str | None = None

    @classmethod
    def from_evidence(cls, evidence: list[Evidence] | None, as_of: str | None = None) -> Facts:
        f = cls(evidence=list(evidence or []), as_of=as_of)
        for e in f.evidence:
            k, d = e.get("kind"), e.get("data") or {}
            if k == "metric":
                f.metrics[(d["service"], d["metric"])] = d
            elif k == "change":
                f.changes.append(d)
            elif k == "log":
                cur = f.logs.setdefault(d["service"], {}).get(d["family"])
                if cur is None or d["count"] >= cur["count"]:
                    f.logs[d["service"]][d["family"]] = d
            elif k == "topology" and str(e["id"]).startswith("topology:"):
                f.topo[d["service"]] = d
            elif k == "topology":
                f.blast[d["service"]] = d
            elif k == "alert":
                f.alerts.append(d)
            elif k == "report":
                f.reports.append(d)
            elif k == "runbook":
                f.runbooks.append(d)
            elif k == "incident":
                f.incidents.append(d)
            elif k == "memory":
                f.memory.append({**d, "id": e["id"]})
            elif k == "injection":
                f.injections.append(d)
        f.changes.sort(key=lambda c: c["ts"], reverse=True)
        return f

    # -- metrics ----------------------------------------------------------------------------------------------------------
    def queried(self, component: str) -> bool:
        return any(c == component for c, _ in self.metrics)

    def flat(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for (c, m), d in self.metrics.items():
            if d.get("flatline"):
                out.setdefault(c, []).append(m)
        return out

    def anomaly(self, component: str) -> Anomaly | None:
        ms = {m: d for (c, m), d in self.metrics.items() if c == component and d["material"]}
        sig = {m: d for m, d in ms.items() if d["signal"]}
        if not sig:
            return None
        onset = min(to_epoch(d["onset"]) for d in sig.values())
        return Anomaly(
            component,
            onset,
            ms,
            any(d["active"] for d in sig.values()),
            max(float(d["max_z"]) for d in sig.values()),
        )

    def anomalies(self) -> dict[str, Anomaly]:
        comps = {c for c, _ in self.metrics}
        return {c: a for c in sorted(comps) if (a := self.anomaly(c)) is not None}

    def normal(self, component: str) -> bool:
        """Queried at least one signal metric and none is anomalous."""
        has = [d for (c, m), d in self.metrics.items() if c == component and d["signal"]]
        return bool(has) and not any(d["material"] for d in has)

    # -- topology ---------------------------------------------------------------------------------------------------------
    def callees(self, component: str) -> tuple[str, ...]:
        return catalog.callees_of(component, sync_only=True) if component in catalog.COMPONENTS else ()

    def callers(self, component: str) -> dict[str, int]:
        return catalog.transitive_callers(component) if component in catalog.COMPONENTS else {}

    def closure(self, components: list[str]) -> set[str]:
        """The components themselves plus everything they (transitively, synchronously) depend on."""
        out: set[str] = set()
        for c in components:
            if c in catalog.COMPONENTS:
                out.add(c)
                out.update(catalog.transitive_callees(c))
        return out

    def roots(self, tolerance_s: int = 240) -> list[str]:
        """Anomalous components NOT explained by an anomalous (and still active) callee that began no later than themselves."""
        an = self.anomalies()
        victims: set[str] = set()
        for c, a in an.items():
            for d in catalog.transitive_callees(c):
                ad = an.get(d)
                if ad is not None and ad.active and ad.onset <= a.onset + tolerance_s:
                    victims.add(c)
                    break
        return sorted((c for c in an if c not in victims), key=lambda c: (an[c].onset, c))

    # -- changes ----------------------------------------------------------------------------------------------------------
    def changes_on(self, component: str, kinds: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        return [c for c in self.changes if c["component"] == component and (kinds is None or c["kind"] in kinds)]

    def log_family(self, component: str, family: str) -> dict[str, Any] | None:
        return self.logs.get(component, {}).get(family)

    def families(self, component: str) -> set[str]:
        return set(self.logs.get(component, {}))
