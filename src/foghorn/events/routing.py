"""Declarative routing: ``routing.yaml`` -> an ordered rule table -> a plan of steps for one event.

The plan is pure data (no I/O besides the narrow :class:`RouteLookup` protocol), so it is trivially testable and the
dispatcher never re-evaluates rules: everything it needs to create a run is stored in the dispatch's ``plan``.
"""

from __future__ import annotations

import fnmatch
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml

from foghorn.contracts import inputs as ci  # the shared TypedDict vocabulary
from foghorn.contracts.meta import (
    CTX_APPROVAL_POLICY,
    CTX_AS_OF,
    CTX_BREAKPOINTS,
    CTX_CHAOS,
    CTX_MEMORY_ENABLED,
    CTX_MODEL_PROFILE,
    CTX_SCENARIO_ID,
    CTX_TOKEN_DELAY_MS,
    GRAPH_IDS,
    M_CORRELATION_ID,
    M_EVAL_RUN_ID,
    M_EVENT_ID,
    M_INCIDENT_ID,
    M_SCENARIO_ID,
    M_TRIGGER,
    det_thread_id,
    thread_metadata,
)
from foghorn.events.config import PRIORITIES, EventsSettings
from foghorn.events.vclock import parse_iso

DEFAULT_ROUTING_FILE = Path(__file__).with_name("routing.yaml")
ACTIONS = {"record", "ledger", "update_suppression_table", "resume_thread", "dead_letter", "suppress"}
THREAD_MODES = {"by_event", "by_correlation", "by_incident", "by_eval_run", "by_dedupe_key"}
#: value of the ``chaos`` run-context key while the runtime "chaos mode" switch is on
CHAOS_MODE_VALUE = "tools"
SIGNAL_DEDUPE_TYPES = {"alert.fired", "slo.burn_rate", "healthcheck.failed", "customer.report"}


class RoutingError(ValueError):
    pass


@dataclass(frozen=True)
class Rule:
    id: str
    when: dict[str, Any]
    action: str | None = None
    route: dict[str, Any] | None = None
    priority: str | None = None
    priority_from: str = "severity"
    enabled: bool = True
    cont: bool = False
    reason: str | None = None
    fallback: bool = False  # only evaluated when no earlier rule produced a step


@dataclass
class PlanStep:
    rule_id: str
    action: str  # "dispatch" or one of ACTIONS
    priority: str
    reason: str | None = None
    plan: dict[str, Any] | None = None  # dispatch plan (graph, thread, input, context, metadata ...)


class RouteLookup(Protocol):
    """The only side-channel rule evaluation needs; implemented over the DB + storm tracker by the pipeline."""

    def open_incident_on(self, component: str | None) -> dict[str, Any] | None: ...
    def in_storm(self, event: dict[str, Any]) -> bool: ...
    def storm_window_hit(self, event: dict[str, Any], count_ge: int, within_s: float) -> bool: ...
    def storm_alerts(self) -> list[dict[str, Any]]: ...
    def launch_context(self, event: dict[str, Any]) -> dict[str, Any]: ...


class StormTracker:
    """Sliding window of alerts over VIRTUAL time; declares a storm once and then folds members until the cooldown ends."""

    def __init__(self, cooldown_s: float, suppress_members: bool) -> None:
        self.cooldown_s = cooldown_s
        self.suppress_members = suppress_members
        self._win: deque[tuple[float, dict[str, Any]]] = deque()
        self._until: float = float("-inf")

    @staticmethod
    def _t(event: dict[str, Any]) -> float:
        return parse_iso(event["occurred_at"]).timestamp()

    def in_storm(self, event: dict[str, Any]) -> bool:
        """True while a declared storm is still going. Every folded member extends the storm (it ends after
        ``cooldown_s`` virtual seconds of quiet), so one burst episode yields exactly one correlator run."""
        t = self._t(event)
        if self.suppress_members and t < self._until:
            self._until = max(self._until, t + self.cooldown_s)
            return True
        return False

    def hit(self, event: dict[str, Any], count_ge: int, within_s: float) -> bool:
        t = self._t(event)
        self._win.append((t, event))
        newest = max(x[0] for x in self._win)
        while self._win and self._win[0][0] < newest - within_s:
            self._win.popleft()
        if len(self._win) >= count_ge and t >= self._until:
            self._until = t + self.cooldown_s
            return True
        return False

    def alerts(self) -> list[dict[str, Any]]:
        return [e for _, e in self._win][-200:]

    def reset(self) -> None:
        self._win.clear()
        self._until = float("-inf")


# ------------------------------------------------------------------------------------------------ matching helpers
def _as_list(v: Any) -> list[Any]:
    return list(v) if isinstance(v, (list, tuple, set)) else [v]


def _glob_any(value: str | None, patterns: Any) -> bool:
    return value is not None and any(fnmatch.fnmatchcase(value, str(p)) for p in _as_list(patterns))


def _dig(obj: Any, path: str) -> Any:
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _value_match(actual: Any, expected: Any) -> bool:
    return actual is not None and any(actual == e or str(actual) == str(e) for e in _as_list(expected))


def rule_matches(rule: Rule, event: dict[str, Any], lookup: RouteLookup) -> bool:
    for key, want in rule.when.items():
        if key == "type":
            ok = _glob_any(event.get("type"), want)
        elif key == "source":
            ok = _glob_any(event.get("source"), want)
        elif key == "severity":
            ok = event.get("severity") in _as_list(want)
        elif key == "component":
            ok = _glob_any((event.get("subject") or {}).get("component"), want)
        elif key.startswith("payload."):
            ok = _value_match(_dig(event.get("payload") or {}, key[len("payload.") :]), want)
        elif key.startswith("attributes."):
            ok = _value_match(_dig(event.get("attributes") or {}, key[len("attributes.") :]), want)
        elif key == "open_incident_on_component":
            has = lookup.open_incident_on((event.get("subject") or {}).get("component")) is not None
            ok = has == bool(want)
        elif key == "in_storm":
            ok = lookup.in_storm(event) == bool(want)
        elif key == "window":
            ok = lookup.storm_window_hit(event, int(want["count_ge"]), float(want["within_s"]))
        else:
            raise RoutingError(f"rule {rule.id}: unknown `when` key {key!r}")
        if not ok:
            return False
    return True


# ------------------------------------------------------------------------------------------------- input builders
@dataclass
class BuildCtx:
    event: dict[str, Any]
    storm_alerts: list[dict[str, Any]] = field(default_factory=list)


def _router_input(b: BuildCtx) -> ci.RouterInput:
    return ci.RouterInput(event=b.event)


def _storm_input(b: BuildCtx) -> ci.StormInput:
    alerts = b.storm_alerts or [b.event]
    scenario = b.event.get("trace", {}).get("scenario_id") or b.event.get("trace", {}).get("stream") or "unknown"
    return ci.StormInput(scenario_id=str(scenario), alerts=alerts, as_of=b.event.get("occurred_at"))


def _postmortem_input(b: BuildCtx) -> ci.PostmortemInput:
    p = b.event.get("payload") or {}
    out = ci.PostmortemInput(
        incident_id=str(p.get("incident_id") or b.event.get("correlation_id") or b.event["id"]),
        scenario_id=str(p.get("scenario_id") or (b.event.get("trace") or {}).get("scenario_id") or ""),
    )
    if p.get("thread_id"):
        out["source_thread_id"] = str(p["thread_id"])
    return out


def _memory_input(b: BuildCtx) -> ci.MemoryKeeperInput:
    p = b.event.get("payload") or {}
    out = ci.MemoryKeeperInput(mode="learn")
    if p.get("incident_id"):
        out["incident_id"] = str(p["incident_id"])
    scenario = p.get("scenario_id") or (b.event.get("trace") or {}).get("scenario_id")
    if scenario:
        out["scenario_id"] = str(scenario)
    if isinstance(p.get("postmortem"), dict):
        out["postmortem"] = p["postmortem"]
    return out


def _handoff_input(b: BuildCtx) -> ci.ShiftHandoffInput:
    p = b.event.get("payload") or {}
    out = ci.ShiftHandoffInput(
        shift_end=str(p.get("shift_end") or b.event["occurred_at"]), window_hours=int(p.get("window_hours") or 8)
    )
    if p.get("crash_after"):
        out["crash_after"] = str(p["crash_after"])
    return out


def _eval_input(b: BuildCtx) -> ci.EvalRunnerInput:
    p = b.event.get("payload") or {}
    out = ci.EvalRunnerInput()
    for key in (
        "suite",
        "scenario_ids",
        "profile",
        "concurrency",
        "baseline_id",
        "max_scenarios",
        "eval_run_id",
        "label",
    ):
        if p.get(key) not in (None, [], ""):
            out[key] = p[key]
    return out


def _change_gate_input(b: BuildCtx) -> ci.ChangeGateInput:
    p = b.event.get("payload") or {}
    change = p.get("change")
    if not isinstance(change, dict):
        change = {
            "id": str(p.get("id") or p.get("deploy_id") or b.event["id"]),
            "kind": p.get("kind") or ("deploy" if str(b.event["type"]).startswith("deploy") else "config"),
            "component": (b.event.get("subject") or {}).get("component") or "unknown",
            "description": str(p.get("description") or p.get("summary") or b.event["type"]),
            "risk": p.get("risk") or "medium",
            "requested_by": str(p.get("requested_by") or p.get("actor") or "unknown"),
        }
    out = ci.ChangeGateInput(change=change)  # type: ignore[typeddict-item]
    if p.get("mode") in {"standard", "gauntlet"}:
        out["mode"] = p["mode"]
    # optional context the gate uses for freeze windows and incident linkage (additive fields of its own Input)
    extras: dict[str, Any] = {
        "requested_at": b.event.get("occurred_at"),
        "scenario_id": (b.event.get("trace") or {}).get("scenario_id"),
        "incident_id": p.get("incident_id"),
    }
    out.update({k: v for k, v in extras.items() if v})  # type: ignore[typeddict-item]
    return out


INPUT_BUILDERS: dict[str, Any] = {
    "router": _router_input,
    "storm": _storm_input,
    "postmortem": _postmortem_input,
    "memory_learn": _memory_input,
    "shift_handoff": _handoff_input,
    "eval": _eval_input,
    "change_gate": _change_gate_input,
}


# ----------------------------------------------------------------------------------------------------- the table
def _thread_key(mode: str, event: dict[str, Any]) -> str:
    p = event.get("payload") or {}
    if mode == "by_correlation":
        return str(event.get("correlation_id") or event["id"])
    if mode == "by_incident":
        return str(p.get("incident_id") or event.get("correlation_id") or event["id"])
    if mode == "by_eval_run":
        return str(p.get("eval_run_id") or event["id"])
    if mode == "by_dedupe_key":
        return str(event.get("dedupe_key") or event["id"])
    return str(event["id"])


def trigger_for(event: dict[str, Any]) -> str:
    src = str(event.get("source") or "")
    if (event.get("attributes") or {}).get("ambient") or src.startswith("replay:"):
        return "replay"
    if src in {"cron", "scheduler"} or event.get("type") == "system.cron.tick":
        return "cron"
    if src.startswith("ui") or src == "api":
        return "ui"
    if event.get("type") == "eval.requested":
        return "eval"
    return "event"


def run_context(event: dict[str, Any], launch_ctx: dict[str, Any], es: EventsSettings) -> dict[str, Any]:
    """The per-run settings carried by the launch (or defaults); keys are the ``CTX_*`` names of ``contracts.meta``."""
    ctx: dict[str, Any] = {
        CTX_MODEL_PROFILE: launch_ctx.get(CTX_MODEL_PROFILE) or es.default_model_profile,
        CTX_APPROVAL_POLICY: launch_ctx.get(CTX_APPROVAL_POLICY) or es.default_approval_policy,
        CTX_TOKEN_DELAY_MS: launch_ctx.get(CTX_TOKEN_DELAY_MS, es.default_token_delay_ms),
    }
    for key in (CTX_BREAKPOINTS, CTX_MEMORY_ENABLED, CTX_CHAOS, CTX_AS_OF):
        if launch_ctx.get(key) not in (None, [], ""):
            ctx[key] = launch_ctx[key]
    if es.chaos_mode and CTX_CHAOS not in ctx:
        ctx[CTX_CHAOS] = CHAOS_MODE_VALUE
    scenario = launch_ctx.get(CTX_SCENARIO_ID) or (event.get("trace") or {}).get("scenario_id")
    if scenario:
        ctx[CTX_SCENARIO_ID] = scenario
    return ctx


class RoutingTable:
    def __init__(
        self, rules: list[Rule], defaults: dict[str, Any], prio_sev: dict[str, str], prio_inc: dict[str, str]
    ) -> None:
        self.rules = rules
        self.defaults = defaults
        self.priority_from_severity = prio_sev
        self.priority_from_incident_severity = prio_inc

    # --- loading -----------------------------------------------------------------------------------------------
    @classmethod
    def load(cls, path: Path | None = None) -> RoutingTable:
        text = (path or DEFAULT_ROUTING_FILE).read_text(encoding="utf-8")
        return cls.from_dict(yaml.safe_load(text) or {})

    @classmethod
    def from_dict(cls, doc: dict[str, Any]) -> RoutingTable:
        if int(doc.get("version", 1)) != 1:
            raise RoutingError("unsupported routing version")
        defaults = {"priority": "P2", "max_attempts": 3, "backoff_s": [1, 4, 16], **(doc.get("defaults") or {})}
        rules: list[Rule] = []
        seen: set[str] = set()
        for raw in doc.get("rules") or []:
            rid = str(raw.get("id") or "")
            if not rid or rid in seen:
                raise RoutingError(f"rule id missing or duplicated: {rid!r}")
            seen.add(rid)
            action, route = raw.get("action"), raw.get("route")
            if bool(action) == bool(route):
                raise RoutingError(f"rule {rid}: exactly one of `action` / `route` is required")
            if action and action not in ACTIONS:
                raise RoutingError(f"rule {rid}: unknown action {action!r}")
            if route:
                if route.get("graph") not in GRAPH_IDS:
                    raise RoutingError(f"rule {rid}: unknown graph {route.get('graph')!r}")
                if route.get("input") not in INPUT_BUILDERS:
                    raise RoutingError(f"rule {rid}: unknown input builder {route.get('input')!r}")
                if route.get("thread", "by_event") not in THREAD_MODES:
                    raise RoutingError(f"rule {rid}: unknown thread mode {route.get('thread')!r}")
            prio = raw.get("priority")
            if prio is not None and prio not in PRIORITIES:
                raise RoutingError(f"rule {rid}: bad priority {prio!r}")
            rules.append(
                Rule(
                    id=rid,
                    when=dict(raw.get("when") or {}),
                    action=action,
                    route=dict(route) if route else None,
                    priority=prio,
                    priority_from=str(raw.get("priority_from", "severity")),
                    enabled=bool(raw.get("enabled", True)),
                    cont=bool(raw.get("continue", False)),
                    fallback=bool(raw.get("fallback", False)),
                    reason=raw.get("reason"),
                )
            )
        if not rules:
            raise RoutingError("routing table has no rules")
        return cls(
            rules,
            defaults,
            dict(doc.get("priority_from_severity") or {}),
            dict(doc.get("priority_from_incident_severity") or {}),
        )

    def rule(self, rule_id: str) -> Rule | None:
        return next((r for r in self.rules if r.id == rule_id), None)

    # --- evaluation --------------------------------------------------------------------------------------------
    def priority_for(self, rule: Rule, event: dict[str, Any]) -> str:
        if rule.priority:
            return rule.priority
        if rule.priority_from == "incident_severity":
            sev = (event.get("payload") or {}).get("severity")
            return self.priority_from_incident_severity.get(str(sev), str(self.defaults["priority"]))
        return self.priority_from_severity.get(str(event.get("severity")), str(self.defaults["priority"]))

    def plan(self, event: dict[str, Any], lookup: RouteLookup, es: EventsSettings) -> list[PlanStep]:
        steps: list[PlanStep] = []
        for rule in self.rules:
            if not rule.enabled or (rule.fallback and steps) or not rule_matches(rule, event, lookup):
                continue
            prio = self.priority_for(rule, event)
            if rule.route is not None:
                steps.append(PlanStep(rule.id, "dispatch", prio, plan=self._dispatch_plan(rule, event, lookup, es)))
            else:
                assert rule.action is not None
                steps.append(PlanStep(rule.id, rule.action, prio, reason=rule.reason))
            if not rule.cont:
                break
        return steps

    def _dispatch_plan(
        self, rule: Rule, event: dict[str, Any], lookup: RouteLookup, es: EventsSettings
    ) -> dict[str, Any]:
        assert rule.route is not None
        route = rule.route
        graph = str(route["graph"])
        builder = INPUT_BUILDERS[str(route["input"])]
        b = BuildCtx(event=event, storm_alerts=lookup.storm_alerts() if route["input"] == "storm" else [])
        thread_id = det_thread_id(graph, _thread_key(str(route.get("thread", "by_event")), event))
        payload = event.get("payload") or {}
        incident_id = payload.get("incident_id")
        meta_extra: dict[str, Any] = {
            M_EVENT_ID: event["id"],
            M_CORRELATION_ID: event.get("correlation_id"),
            M_INCIDENT_ID: incident_id,
            M_SCENARIO_ID: (event.get("trace") or {}).get("scenario_id"),
            M_TRIGGER: trigger_for(event),
            M_EVAL_RUN_ID: payload.get("eval_run_id"),
        }
        thread_md = thread_metadata(str(route.get("kind", "triage")), graph, **meta_extra)
        ambient = bool((event.get("attributes") or {}).get("ambient"))
        chaos = (event.get("attributes") or {}).get("chaos")
        return {
            "graph_id": graph,
            "assistant_id": str(route.get("assistant") or graph),
            "thread_id": thread_id,
            "input": dict(builder(b)),
            "thread_metadata": thread_md,
            "context": run_context(event, lookup.launch_context(event), es),
            "multitask_strategy": str(route.get("multitask", "enqueue")),
            "on_completion": "delete" if ambient else "keep",
            "chaos": chaos if chaos in {"poison", "fail_once", "slow"} else None,
            "incident_id": incident_id,
            "trigger": thread_md[M_TRIGGER],
        }
