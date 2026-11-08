"""Investigation planning: the supervisor's digest, its next-step heuristics, the specialists' tasks and tool plans.

Everything here is a plain function of JSON (the digest / the task), so it is unit-testable and the offline model's
``Script`` rules are one-liners that call it. In real-LLM mode the same digest and task payloads are what the model
reads; ``enforce_route`` (in the supervisor node) re-applies the invariants whatever the model answers.

Competent investigation shape (hops in brackets)::

    [1] topology_analyst + change_analyst   (in parallel: where does the alert sit, what changed)
    [2] metrics_analyst                     (alerts, settle the clock, symptom metrics along the call path)
    [3] logs_analyst + knowledge_agent      (signatures on the suspects, runbook)
    [4] follow-up (only if needed)          (metric suggested by a log signature, deploy lookback, callee descent)
    [..] rca_tournament -> (clarify_change_intent) -> remediation_planner
"""

from __future__ import annotations

import math
import re
from typing import Any

from foghorn.data import catalog
from foghorn.data.util import to_epoch
from foghorn.flows.incident_commander.planning import CATEGORY_QUERY
from foghorn.flows.rca_tournament.facts import Facts, hhmm
from foghorn.flows.rca_tournament.scoring import role_of

AGENTS = ("metrics_analyst", "logs_analyst", "change_analyst", "topology_analyst", "knowledge_agent")
#: (max tool calls, max supervisor hops) by scenario tier: the public investigation budget
BUDGETS: dict[int, tuple[int, int]] = {1: (10, 7), 2: (14, 8), 3: (18, 9)}
SETTLE_MIN = 5.0
YOUNG_MIN = 6.0
LOOKBACK_DAYS = 7
PRIMARY = {
    "service": ("error_rate", "p95_ms"),
    "database": ("p95_ms", "error_rate", "saturation_pct"),
    "cache": ("hit_ratio", "p95_ms"),
    "queue": ("consumer_lag", "error_rate"),
    "external": ("error_rate", "p95_ms"),
}
COMPACT = {
    "service": ("error_rate",),
    "database": ("p95_ms", "error_rate"),
    "cache": ("hit_ratio",),
    "queue": ("consumer_lag",),
    "external": ("error_rate",),
}
FAMILY_METRIC = {
    "oom": "mem_pct",
    "disk": "disk_pct",
    "pool": "saturation_pct",
    "cache": "hit_ratio",
    "poison": "consumer_lag",
}
NOISE_RULES = frozenset({"synthetic_probe_failed"})
REVENUE_ENTRY = "checkout-service"


def budget_for(tier: int) -> dict[str, int]:
    calls, hops = BUDGETS.get(int(tier), BUDGETS[3])
    return {"max_tool_calls": calls, "max_hops": hops}


def real_alerts(f: Facts) -> list[dict[str, Any]]:
    """Alerts that are not flapping noise: a resolved synthetic probe or a resolved low-severity alert is ignored."""
    out = []
    for a in f.alerts:
        if a["rule"] in NOISE_RULES and a.get("resolved_at"):
            continue
        if a.get("resolved_at") and a.get("severity") in ("low", "info"):
            continue
        out.append(a)
    return out


def focus_components(f: Facts, limit: int = 3) -> list[str]:
    """Components worth investigating first: unresolved or non-trivial alerts (flapping probes that resolved are noise),
    plus components named by customer reports; earliest first."""
    scored: list[tuple[str, str]] = [(str(a["fired_at"]), a["component"]) for a in real_alerts(f)]
    for r in f.reports:
        if r.get("component"):
            scored.append(("~", r["component"]))
    out: list[str] = []
    for _t, c in sorted(scored):
        if c in catalog.COMPONENTS and c not in out:
            out.append(c)
    return out[:limit]


def alert_storm(f: Facts, window_s: int = 180, minimum: int = 6) -> list[str]:
    """Components whose alerts fired within ``window_s`` of each other: the signature of one shared cause."""
    first: dict[str, int] = {}
    for a in f.alerts:
        if a["rule"] in NOISE_RULES:
            continue
        t = to_epoch(a["fired_at"])
        first[a["component"]] = min(first.get(a["component"], t), t)
    if len(first) < minimum:
        return []
    ts = sorted(first.values())
    best = max((sum(1 for u in ts if 0 <= u - t0 <= window_s) for t0 in ts), default=0)
    return sorted(first) if best >= minimum else []


def clarification_reason(f: Facts, hyps: list[dict[str, Any]], profile: str) -> dict[str, Any] | None:
    """Why (and what) a human should be asked before planning, or None.

    * ``change_intent``: the leading hypothesis is a flag/config change by a person AND another change landed within ten
      minutes elsewhere: authorship / intent is ambiguous, so ask whether the ramp was intended (clarify_change_intent);
    * ``telemetry_blind``: the leading hypothesis is a telemetry gap: nobody can see the service, ask the on-call;
    * ``confirm_diagnosis`` (cautious profile only): the diagnosis is not certain, ask for a sanity check."""
    if not hyps or profile == "naive":
        return None
    top = hyps[0]
    if top["category"] == "telemetry_gap":
        return {"reason": "telemetry_blind", "component": top["component"]}
    if top["category"] == "config_change":
        ch = next((c for c in f.changes_on(top["component"], ("flag_change", "config_change")) if c["human"]), None)
        if ch is not None:
            t0 = to_epoch(ch["ts"])
            rival = next(
                (
                    c
                    for c in f.changes
                    if c["component"] != top["component"]
                    and c["kind"] in ("deploy", "flag_change", "config_change")
                    and abs(to_epoch(c["ts"]) - t0) <= 600
                ),
                None,
            )
            if rival is not None:
                d = ch.get("details") or {}
                return {
                    "reason": "change_intent",
                    "component": top["component"],
                    "change_id": ch["change_id"],
                    "actor": ch["actor"],
                    "what": str(d.get("flag") or d.get("key") or "the change"),
                    "from": d.get("from"),
                    "to": d.get("to"),
                    "at": hhmm(ch["ts"]),
                    "rival": rival["change_id"],
                }
    if profile == "cautious" and (top["score"] < 0.9 or (len(hyps) > 1 and hyps[1]["score"] >= 0.25)):
        return {
            "reason": "confirm_diagnosis",
            "component": top["component"],
            "category": top["category"],
            "score": top["score"],
        }
    return None


# --- digest ----------------------------------------------------------------------------------------------------------------
def build_digest(state: dict[str, Any], profile: str, directives: dict[str, Any] | None = None) -> dict[str, Any]:
    f = Facts.from_evidence(state.get("evidence"), state.get("as_of"))
    runs = state.get("agent_runs") or []
    ran: dict[str, int] = {}
    for r in runs:
        ran[r["agent"]] = ran.get(r["agent"], 0) + 1
    meta = state.get("scenario_meta") or {}
    budget = state.get("budget") or budget_for(int(meta.get("tier", 2)))
    used = sum(int(r.get("tool_calls", 0)) for r in runs)
    hyps = state.get("hypotheses") or []
    an = f.anomalies()
    focus = focus_components(f) or list(state.get("focus") or [])
    mem = [m for m in f.memory if m.get("component")]
    first_alert = max(
        (to_epoch(a["fired_at"]) for a in real_alerts(f) if a.get("fired_at")), default=None
    )  # the incident is as young as its newest alert
    as_of = state.get("as_of") or meta.get("as_of")
    age = ((to_epoch(as_of) - first_alert) / 60.0) if (as_of and first_alert is not None) else None
    return {
        "incident": {k: (state.get("incident") or {}).get(k) for k in ("id", "severity", "title")},
        "profile": profile,
        "tier": meta.get("tier", 2),
        "summary": meta.get("summary", ""),
        "as_of": as_of,
        "clock0": meta.get("as_of"),
        "age_min": round(age, 1) if age is not None else None,
        "budget": {
            "calls_left": max(0, budget["max_tool_calls"] - used),
            "calls_used": used,
            "hops_left": max(0, budget["max_hops"] - int(state.get("supervisor_hops") or 0)),
            "hops_used": int(state.get("supervisor_hops") or 0),
        },
        "focus": focus,
        "ran": ran,
        "settled": bool(state.get("settled")) or (age is not None and age >= YOUNG_MIN),
        "storm": alert_storm(f),
        "roots": f.roots(),
        "anomalies": [
            {"c": c, "onset": a.onset, "metrics": a.signal_metrics[:3], "active": a.active, "z": round(a.peak_z)}
            for c, a in sorted(an.items(), key=lambda kv: kv[1].onset)[:8]
        ],
        "queried": sorted(f"{c}:{m}" for c, m in f.metrics),
        "flat": sorted(f.flat()),
        "changes": [
            {
                "id": c["change_id"],
                "c": c["component"],
                "kind": c["kind"],
                "min": c["minutes_before"],
                "human": c["human"],
            }
            for c in f.changes[:8]
        ],
        "lookback_done": any(r.get("lookback") for r in runs),
        "alert_metric": {a["component"]: a["metric"] for a in f.alerts if a.get("metric") and a["component"] in focus},
        "log_families": {s: sorted(v) for s, v in f.logs.items()},
        "injection": bool(f.injections),
        "memory": [
            {
                "key": m["id"].split(":", 1)[-1],
                "c": m.get("component"),
                "cat": m.get("category"),
                "score": m.get("score"),
            }
            for m in mem[:3]
        ],
        "runbooks": [r["runbook"] for r in f.runbooks[:3]],
        "rca_runs": int(state.get("rca_runs") or 0),
        "hypotheses": [
            {"id": h["id"], "c": h["component"], "cat": h["category"], "score": h["score"]} for h in hyps[:3]
        ],
        "red_herrings": len(state.get("red_herrings") or []),
        "questions": len(state.get("qa_log") or []),
        "asked": bool(state.get("clarified")),
        "ask": clarification_reason(f, hyps, profile),
        "directives": directives or {},
        "evidence_count": len(
            [e for e in f.evidence if e.get("kind") in ("metric", "change", "log") and e.get("strength", 0) > 0.3]
        ),
    }


# --- planning ---------------------------------------------------------------------------------------------------------------
def _route(
    next_: str, reason: str, agents: list[str] | None = None, services: list[str] | None = None, lookback: int = 0
) -> dict[str, Any]:
    return {
        "next": next_,
        "agents": agents or [],
        "services": services or [],
        "lookback_days": lookback,
        "reason": reason,
    }


def strong_prior(d: dict[str, Any]) -> dict[str, Any] | None:
    """A recurring pattern: the best recalled incident is a good match (score >= 0.38), at least two of the top three
    recalled incidents agree on component AND category, and that component is on the dependency path of the alert. Then the
    search verifies the prior instead of mapping the whole system (the learning loop makes the second run cheaper)."""
    mem = [m for m in d["memory"] if m["c"] and m["cat"] and m["c"] in catalog.COMPONENTS]
    if not mem or (mem[0].get("score") or 0) < 0.38:
        return None
    top = mem[0]
    if sum(1 for m in mem if (m["c"], m["cat"]) == (top["c"], top["cat"])) < 2:
        return None
    near = {c for f_ in d["focus"] for c in (f_, *catalog.transitive_callees(f_))}
    return top if top["c"] in near else None


def suspects(d: dict[str, Any], limit: int = 2) -> list[str]:
    out = [c for c in d["roots"] if c not in d["flat"]][:limit]
    if not out:
        out = [a["c"] for a in d["anomalies"]][:limit]
    return out or d["focus"][:1]


def log_targets(d: dict[str, Any], limit: int = 2) -> list[str]:
    """Where to read logs: the suspects; for an external provider (no logs of its own) the internal callers that hit it."""
    out: list[str] = []
    for s in suspects(d, limit):
        if role_of(s) == "external":
            out += list(catalog.callers_of(s, sync_only=True)[:1])
        else:
            out.append(s)
    return list(dict.fromkeys(out))[:limit]


def plan_next(d: dict[str, Any]) -> dict[str, Any]:
    """The supervisor's decision (a Route as a dict) for the next hop."""
    prof = d["profile"]
    b = d["budget"]
    ran = d["ran"]
    focus = d["focus"]
    hyps = d["hypotheses"]
    if hyps:
        top = hyps[0]
        if d["ask"] and not d["asked"]:
            why = {
                "change_intent": f"{top['cat'].replace('_', ' ')} on {top['c']} leads, but a person ramped it while another change landed minutes apart: asking whether the ramp was intended.",
                "telemetry_blind": f"{top['c']} telemetry is blind: asking the on-call to confirm its health out-of-band.",
                "confirm_diagnosis": f"{top['cat'].replace('_', ' ')} on {top['c']} is only {top['score']:.0%} certain; I want a human to confirm before planning.",
            }[d["ask"]["reason"]]
            return _route("clarify_change_intent", why)
        return _route(
            "remediation_planner",
            f"{top['cat'].replace('_', ' ')} on {top['c']} leads at {top['score']:.0%}; planning the remediation.",
        )
    spent = b["calls_left"] <= 0 or b["hops_left"] <= 2
    if prof == "naive":
        if not ran.get("change_analyst"):
            return _route(
                "investigate", "Looking at what changed just before the alert.", ["change_analyst"], focus[:1]
            )
        if not ran.get("metrics_analyst"):
            return _route(
                "investigate",
                f"Checking the first alerting component, {focus[0] if focus else 'unknown'}.",
                ["metrics_analyst"],
                focus[:1],
            )
        return _route("rca_tournament", "Blaming the nearest change.")
    if not ran:
        prior = strong_prior(d)
        if prior and prof != "naive":
            return _route(
                "investigate",
                f"Memory {prior['key']} matches this pattern ({prior['cat']} on {prior['c']}): verifying the prior instead of searching broadly.",
                ["metrics_analyst", "change_analyst"],
                list(dict.fromkeys([prior["c"], *focus]))[:3],
            )
        return _route(
            "investigate",
            f"Alerts point at {', '.join(focus) or 'no component yet'}: mapping its dependencies and the recent changes in parallel.",
            ["topology_analyst", "change_analyst"],
            focus[:2],
        )
    if not ran.get("metrics_analyst"):
        return _route("investigate", "Measuring the symptoms along the call path.", ["metrics_analyst"], focus[:3])
    if spent:
        return _route("rca_tournament", "Investigation budget is spent; ranking hypotheses on the evidence I have.")
    sus = suspects(d)
    prior = strong_prior(d)
    if not ran.get("logs_analyst") and not ran.get("knowledge_agent"):
        agents = ["logs_analyst"] + ([] if prior else ["knowledge_agent"])
        tg = log_targets(d)
        return _route(
            "investigate",
            f"Reading the logs of {', '.join(tg) or 'the suspects'} and consulting the runbooks.",
            agents,
            tg,
        )
    follow = follow_ups(d)
    if follow and ran.get("metrics_analyst", 0) < 2:
        agents = [a for a in ("metrics_analyst", "change_analyst") if follow.get(a)]
        return _route(
            "investigate",
            follow["why"],
            agents,
            follow.get("services", sus),
            lookback=LOOKBACK_DAYS if follow.get("change_analyst") else 0,
        )
    return _route("rca_tournament", "Evidence is in; running the root-cause tournament.")


def followup_metric_hints(d: dict[str, Any]) -> list[tuple[str, str]]:
    """The metrics a log signature points at that nobody measured yet (OOM -> mem_pct, 'no space left' -> disk_pct ...)."""
    queried = set(d["queried"])
    out: list[tuple[str, str]] = []
    for s in suspects(d):
        for fam in d["log_families"].get(s, []):
            m = FAMILY_METRIC.get(fam)
            if m and f"{s}:{m}" not in queried and (s, m) not in out and m in catalog.COMPONENTS[s].metrics:
                out.append((s, m))
    return out


def follow_ups(d: dict[str, Any]) -> dict[str, Any] | None:
    """After logs: metrics a log signature points to, a deploy lookback for slow leaks."""
    sus = suspects(d)
    hints = followup_metric_hints(d)
    out: dict[str, Any] = {}
    why: list[str] = []
    if hints:
        out["metrics_analyst"] = True
        out["services"] = list(dict.fromkeys(s for s, _m in hints))
        why.append("checking the metric the log signature points at")
    leak = any("oom" in d["log_families"].get(s, []) for s in sus)
    if leak and not d["lookback_done"]:
        own_recent = any(c["c"] in sus and c["min"] is not None and c["min"] <= 90 for c in d["changes"])
        if not own_recent:
            out["change_analyst"] = True
            out.setdefault("services", sus)
            why.append(
                f"widening the change lookback to {LOOKBACK_DAYS} days because the memory leak predates the alert"
            )
    if not out:
        return None
    out["why"] = "Following up: " + " and ".join(why) + "."
    return out


# --- tasks --------------------------------------------------------------------------------------------------------------------
def metric_queries(d: dict[str, Any], services: list[str], budget: int) -> list[list[str]]:
    """(service, metric) pairs for the metrics analyst, in priority order and cut at ``budget``: one primary metric per
    focus service, the callees that changed, the revenue-path impact, the nearest suspicious off-path change, then the
    remaining symptom metrics of the focus and (when nothing explains the alert) its callees. Never a measured pair."""
    done = set(d["queried"])
    alert_metric: dict[str, str] = d.get("alert_metric") or {}
    groups: list[list[tuple[str, str]]] = [
        list(followup_metric_hints(d)) if d["ran"].get("logs_analyst") else [],
        [],
        [],
        [],
        [],
        [],
    ]

    def add(g: int, c: str, metrics: tuple[str, ...]) -> None:
        for m in metrics:
            ok = c in catalog.COMPONENTS and m in catalog.COMPONENTS[c].metrics and f"{c}:{m}" not in done
            if ok and all((c, m) not in grp for grp in groups):
                groups[g].append((c, m))

    ordered: dict[str, list[str]] = {}
    for c in services:
        pm = list(PRIMARY[role_of(c)])
        if alert_metric.get(c) and alert_metric[c] in catalog.COMPONENTS[c].metrics:
            pm = [alert_metric[c], *[m for m in pm if m != alert_metric[c]]]
        ordered[c] = pm
        add(0, c, tuple(pm[:1]))
    closure = {x for s in services for x in (s, *catalog.transitive_callees(s))}
    near = [
        c
        for c in d["changes"]
        if c["c"] in closure and c["c"] not in services and c["min"] is not None and c["min"] <= 90
    ]
    for ch in near[:2]:
        add(0, ch["c"], COMPACT[role_of(ch["c"])])
    if d["profile"] != "naive":
        if REVENUE_ENTRY in {x for s in services for x in (s, *catalog.transitive_callers(s))}:
            add(1, REVENUE_ENTRY, ("p95_ms", "error_rate"))
        for ch in d["changes"]:
            if ch["c"] in closure or ch["kind"] not in ("deploy", "flag_change", "config_change"):
                continue
            if ch["min"] is not None and ch["min"] <= 25:
                add(2, ch["c"], ("error_rate",))
                break
    for c, pm in ordered.items():
        add(3, c, tuple(pm[1:]))
    own = any(c["c"] in services and c["min"] is not None and c["min"] <= 30 for c in d["changes"])
    if not own and not near and d["profile"] != "naive":
        budgeted = 0
        for s in services:
            for callee in catalog.callees_of(s, sync_only=True):
                if budgeted >= 2 or callee in services:
                    continue
                add(4, callee, COMPACT[role_of(callee)])
                budgeted += 1
    flat = [p for grp in groups for p in grp]
    return [list(p) for p in flat[: max(budget, 0)]]


def build_task(agent: str, route: dict[str, Any], d: dict[str, Any]) -> dict[str, Any]:
    """The deterministic task payload of ``agent`` for this route (what to look at, how many calls it may spend)."""
    left = d["budget"]["calls_left"]
    services = [s for s in route.get("services", []) if s in catalog.COMPONENTS] or d["focus"][:1]
    task: dict[str, Any] = {
        "agent": agent,
        "wave": d["budget"]["hops_used"],
        "incident": d["incident"]["id"],
        "services": services,
    }
    if agent == "metrics_analyst":
        settle = 0.0 if d["settled"] or d["profile"] == "naive" else SETTLE_MIN
        listing = not d["ran"].get("metrics_analyst") and d["profile"] != "naive"
        overhead = 1 if listing else 0
        reserve = 0 if d["ran"].get("metrics_analyst") else (3 if d["tier"] == 1 else 4)
        budget = max(1, left - overhead - reserve)
        qs = metric_queries(d, services, budget)
        task.update({"settle_minutes": settle, "list_alerts": listing, "queries": qs, "window_minutes": 45})
        task["tool_budget"] = len(qs) + overhead
    elif agent == "change_analyst":
        task.update({"window_minutes": 240, "lookback_days": route.get("lookback_days") or 0})
        task["tool_budget"] = 1
    elif agent == "topology_analyst":
        n = 1 if d["tier"] == 1 else 2
        task.update({"services": services[:n]})
        task["tool_budget"] = len(task["services"])
    elif agent == "logs_analyst":
        task.update({"services": services[:2], "level": "WARN+", "window_minutes": 30})
        task["tool_budget"] = len(task["services"])
    elif agent == "knowledge_agent":
        task.update({"query": knowledge_query(d), "k": 4, "recall": left >= 4 and not strong_prior(d)})
        task["tool_budget"] = 1 + int(task["recall"])
    return task


def knowledge_query(d: dict[str, Any]) -> str:
    """Words for the runbook search: the symptoms actually seen (log signature families, metric shapes), not the guess."""
    words: list[str] = []
    fam_words = {
        "code_defect": "new exception after deploy",
        "cert": "TLS certificate expired handshake failure",
        "disk": "disk full no space left on device",
        "maintenance": "planned maintenance suppression",
        "pool": "database connection pool saturation",
        "clients": "too many clients connection limit",
        "cache": "cache hit ratio collapse stampede",
        "poison": "queue consumer lag poison message",
        "dependency": "third-party provider outage fallback",
        "oom": "memory leak OOM restart",
        "flag_timeout": "feature flag regression latency",
        "seqscan": "latency creep missing index after schema migration",
        "partition": "zone network partition no route to host",
        "runaway": "long-running queries kill queries",
    }
    for s in suspects(d):
        for fam in d["log_families"].get(s, []):
            words.append(fam_words.get(fam, ""))
    for a in d["anomalies"][:3]:
        words.append(" ".join(a["metrics"]))
    if d["flat"]:
        words.append(CATEGORY_QUERY["telemetry_gap"])
    if d.get("summary"):  # what the reporter says is wrong, in their words: operators search runbooks the same way
        words.append(str(d["summary"]))
    if not any(words):
        words.append("elevated errors latency incident " + " ".join(d["focus"]))
    return re.sub(r"\s+", " ", " ".join(w for w in words if w)).strip()[:240]


# --- tool plans (what a specialist calls; the offline script and the tests use the same function) ---------------------------------
def tool_plan(task: dict[str, Any], step: int, done: set[str]) -> list[tuple[str, dict[str, Any]]]:
    """Tool calls ``task['agent']`` makes at model step ``step`` (0-based); ``done`` = tool names already answered."""
    agent = task["agent"]
    if agent == "metrics_analyst":
        if step == 0 and not (done & {"query_metric", "list_alerts"}):
            calls: list[tuple[str, dict[str, Any]]] = []
            if task.get("list_alerts"):
                calls.append(("list_alerts", {"window_minutes": 60}))
            calls += [
                ("query_metric", {"service": s, "metric": m, "window_minutes": int(task.get("window_minutes", 45))})
                for s, m in task.get("queries", [])
            ]
            return calls
        return []
    if step > 0:
        return []
    if agent == "change_analyst":
        args: dict[str, Any] = {"window_minutes": int(task.get("window_minutes", 240))}
        if task.get("lookback_days"):
            args["lookback_days"] = float(task["lookback_days"])
        return [("list_changes", args)]
    if agent == "topology_analyst":
        return [("get_topology", {"service": s}) for s in task.get("services", [])]
    if agent == "logs_analyst":
        return [
            (
                "search_logs",
                {
                    "service": s,
                    "level": task.get("level", "WARN+"),
                    "window_minutes": int(task.get("window_minutes", 30)),
                    "limit": 50,
                },
            )
            for s in task.get("services", [])
        ]
    if agent == "knowledge_agent":
        calls = [("search_runbooks", {"query": task["query"], "k": int(task.get("k", 4))})]
        if task.get("recall"):
            calls.append(("recall_incidents", {"query": task["query"], "k": 3}))
        return calls
    return []


def specialist_tools(agent: str) -> list[str]:
    return {
        "metrics_analyst": ["query_metric", "compare_baseline", "list_alerts"],
        "logs_analyst": ["search_logs"],
        "change_analyst": ["list_changes"],
        "topology_analyst": ["get_topology", "blast_radius", "get_oncall"],
        "knowledge_agent": ["search_runbooks", "recall_incidents"],
    }[agent]


def minutes_between(a: str, b: str) -> float:
    return math.floor((to_epoch(b) - to_epoch(a)) / 6.0) / 10.0
