"""The copilot's own tools (the 12 read-only investigation tools come from ``foghorn.tools.lc.tools_for``).

* ``propose_action``      side-effecting: reviewed by a human BEFORE it runs (``ToolReviewMiddleware``), then executed through the
                          pure ``execute_action`` simulation and recorded in ``applied_actions``
* ``remember_preference`` side-effecting (Store write): reviewed, then ``memory.api.remember`` into ``("prefs", team)``
* ``recall_memory``       read-only Store search (``memory.api.recall``)
* ``incident_status``     read-only summary of where the investigation stands
* ``ask_user``            an IN-TOOL ``interrupt()``: asks the operator a clarifying question (e.g. which scenario?)

Tool bodies that call ``interrupt()`` re-run from line 1 on resume (docs/design/02 gotcha 14): everything before it is pure.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from langchain.tools import ToolRuntime, tool
from langchain_core.messages import ToolMessage
from langgraph.config import get_config, get_stream_writer
from langgraph.types import Command, interrupt

from foghorn.common.lifecycle import emit_event
from foghorn.common.stream import emit, stream_payload
from foghorn.contracts.interrupts import Answer, build_interrupt
from foghorn.data import actions as act
from foghorn.data import api as data
from foghorn.data.access import ScenarioNotFound
from foghorn.flows.ops_copilot.context import ctx_get
from foghorn.memory import api as mem
from foghorn.tools import api as tools_api
from foghorn.tools.context import ToolError
from foghorn.tools.lc import tool_context

#: tool names whose calls change the world (or the Store) and therefore go through the human review
REVIEWED_TOOLS = ("propose_action", "remember_preference")
READ_ONLY_TOOLS = (
    "query_metric",
    "compare_baseline",
    "search_logs",
    "list_changes",
    "get_topology",
    "blast_radius",
    "search_runbooks",
    "recall_incidents",
    "get_oncall",
    "list_alerts",
    "verify_recovery",
    "advance_time",
)
_SCENARIO_ID = re.compile(r"\b(S\d{2}|X-\d{4})\b", re.I)


def _dump(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), default=str)


def emit_memory(kind: str, dedupe_key: str, mem_key: str, **data: Any) -> None:
    """``memory.written`` / ``memory.recalled`` stream events carry a data field named ``key`` (contracts/stream.py), which
    ``emit(kind, key, **data)`` cannot take (the parameter name collides): build the payload by hand."""
    try:
        writer = get_stream_writer()
    except RuntimeError:  # outside a run
        return
    payload = stream_payload(kind, dedupe_key, agent=kind.split(".")[0], **data)  # type: ignore[arg-type]
    payload["data"]["key"] = mem_key
    writer(payload)


def thread_id() -> str:
    try:
        return str((get_config().get("configurable") or {}).get("thread_id") or "")
    except RuntimeError:  # outside a run (unit tests calling a tool function directly)
        return ""


def team_slug(raw: str | None, default: str = "team-sre") -> str:
    """A namespace-safe team id: ``'the DBA team'`` -> ``team-dba``. Free text never goes into a namespace (POL-004)."""
    if not raw:
        return default
    s = re.sub(r"[^a-z0-9]+", "-", raw.lower().replace("the ", "").replace(" team", "")).strip("-")[:32]
    if not s:
        return default
    return s if s.startswith(("team-", "user-")) else f"team-{s}"


def _ctx_team(runtime: ToolRuntime) -> str:
    return str(ctx_get(getattr(runtime, "context", None), "team", "team-sre"))


def needs_review(name: str, args: dict[str, Any], policy: str = "standard") -> bool:
    """Should this call be shown to a human first? Used by the review middleware (and must be pure: it runs again on resume)."""
    if name == "remember_preference":
        return True
    if name != "propose_action":
        return False
    spec = act.ACTIONS.get(str(args.get("action", "")))
    if spec is None or spec.approvers >= 2 or spec.never_automatic:
        return False  # the tool refuses these itself: asking a human to approve a refusal is noise
    return not (policy == "auto_low_risk" and spec.risk == "low" and spec.kind == "state_changing")


@tool
def propose_action(
    action: str, target: str, rationale: str = "", params: dict[str, Any] | None = None, *, runtime: ToolRuntime
) -> Command[Any] | str:
    """Propose a remediation and, once a human reviewer approves it, run it (simulated) and record it. A reviewer can approve,
    edit the arguments or reject; nothing runs before that. action: rollback_deploy, revert_flag, revert_config, restart_service,
    scale_out, renew_cert, rotate_logs, expand_volume, kill_queries, add_index, warm_cache, pause_consumer, quarantine_message,
    suppress_alert, post_status_update, open_ticket, escalate_human. target: component, flag/config key or zone. rationale: one
    sentence of evidence. High-risk actions (two approvers: failovers, fallback provider, zone drain) and never-automatic ones
    (restart_db_primary) are refused here: they belong to the incident commander. During the change freeze kill_queries on a
    datastore and add_index need params.break_glass {reason (20+ chars), second_approver}."""
    spec = act.ACTIONS.get(action)
    if spec is None:
        return _dump(
            {"outcome": "rejected", "reason": f"unknown action {action!r}; valid: {', '.join(sorted(act.ACTIONS))}"}
        )
    if spec.never_automatic or spec.approvers >= 2:
        why = "never automatic" if spec.never_automatic else f"needs {spec.approvers} approvers"
        return _dump(
            {
                "outcome": "needs_incident_commander",
                "action": action,
                "target": target,
                "reason": f"{action} is {why} (POL-005): start an incident_commander run or ask the on-call; the copilot does not run it",
            }
        )
    try:
        rec = tools_api.execute_action(tool_context(runtime), action, target, params)
    except (ToolError, ScenarioNotFound) as e:
        return _dump({"outcome": "rejected", "reason": str(e.args[0] if e.args else e)})
    rec = {**rec, "proposed_by": "ops_copilot", "rationale": rationale}
    if rec.get("outcome") == "executed":
        emit_event(
            "action.executed",
            idempotency_key=f"action.executed:{rec['id']}",
            payload={
                "action": action,
                "target": target,
                "outcome": "executed",
                "thread_id": thread_id(),
                "source": "ops_copilot",
            },
            scenario_id=str(rec.get("scenario_id") or ""),
        )
        emit("note", f"action:{rec['id']}", agent="propose_action", text=f"{action} on {target} executed (simulated)")
    msg = ToolMessage(content=_dump(rec), tool_call_id=runtime.tool_call_id or "", name="propose_action")
    return Command(update={"applied_actions": [rec], "messages": [msg]})


@tool
async def remember_preference(text: str, team: str | None = None, *, runtime: ToolRuntime) -> str:
    """Save a team preference or rule to long-term memory so every future conversation can use it (e.g. 'the DBA team prefers
    kill_queries before any restart'). A human reviews the text before it is saved. team: who it applies to (default: the
    configured team)."""
    ns_team = team_slug(team, _ctx_team(runtime))
    if not ctx_get(getattr(runtime, "context", None), "memory_enabled", True):
        return _dump({"saved": False, "reason": "memory is disabled for this run (memory_enabled=false)"})
    text = " ".join(text.split())
    key = "pref-" + hashlib.sha1(f"{ns_team}|{text.lower()}".encode()).hexdigest()[:10]  # noqa: S324 - a stable id, not security
    value = {
        "text": text,
        "source": "ops_copilot",
        "confidence": 0.7,
        "provenance": {"thread_id": thread_id(), "tool_call_id": runtime.tool_call_id, "written_by": "ops_copilot"},
    }
    saved = await mem.remember(runtime.store, "prefs", key, value, component=ns_team)
    if not saved:
        return _dump({"saved": False, "reason": "no long-term store is configured for this run"})
    emit_memory("memory.written", f"memory:prefs:{ns_team}:{key}", key, ns=["prefs", ns_team])
    emit_event(
        "memory.written",
        idempotency_key=f"memory.written:prefs/{ns_team}/{key}",
        payload={"ns": ["prefs", ns_team], "key": key, "thread_id": thread_id()},
    )
    return _dump({"saved": True, "ns": ["prefs", ns_team], "key": key, "text": text})


@tool
async def recall_memory(
    query: str, kinds: list[str] | None = None, component: str | None = None, k: int = 4, *, runtime: ToolRuntime
) -> str:
    """Search long-term memory: team preferences (prefs), distilled lessons (lessons) and past incidents (incidents). Use it
    for 'what does the DBA team prefer?', 'have we seen this before?' and before proposing an action."""
    if not ctx_get(getattr(runtime, "context", None), "memory_enabled", True):
        return _dump({"query": query, "hits": [], "note": "memory is disabled for this run (memory_enabled=false)"})
    use = tuple(x for x in (kinds or mem.RECALL_KINDS) if x in mem.RECALL_KINDS) or mem.RECALL_KINDS
    hits = await mem.recall(runtime.store, query, kinds=use, component=component, k=max(1, min(int(k), 8)))
    for h in hits:
        emit_memory(
            "memory.recalled",
            f"memory:{'/'.join(h['ns'])}:{h['key']}",
            h["key"],
            ns=h["ns"],
            score=h["score"],
            summary=h["text"][:120],
        )
    return _dump(
        {
            "query": query,
            "hits": [
                {
                    "ns": h["ns"],
                    "key": h["key"],
                    "ref": mem.memory_ref(h),
                    "score": round(h["score"] or 0.0, 4),
                    "text": h["text"],
                }
                for h in hits
            ],
        }
    )


@tool
def incident_status(*, runtime: ToolRuntime) -> str:
    """Where the investigation stands: the scenario's public summary, the virtual time, the active alerts, the actions applied in
    this conversation and whether the affected service recovered. Read-only."""
    ctx = tool_context(runtime)
    if not ctx.get("scenario_id"):
        return _dump({"error": "no scenario selected; ask the operator (ask_user) or set it in the assistant context"})
    try:
        pub = data.get_scenario(str(ctx["scenario_id"]))
        alerts = tools_api.list_alerts(ctx, None, 60, True)
        applied = [
            {k: a.get(k) for k in ("id", "action", "target", "outcome", "applied_at")} for a in ctx["applied_actions"]
        ]
        recovery: dict[str, Any] = {}
        for a in alerts["alerts"][:2]:
            comp = str(a["component"])
            if comp not in recovery:
                v = tools_api.verify_recovery(ctx, comp)
                recovery[comp] = {"status": v["status"], "reason": v["reason"]}
    except (ToolError, ScenarioNotFound) as e:
        return _dump({"error": str(e.args[0] if e.args else e)})
    return _dump(
        {
            "scenario": {k: pub.get(k) for k in ("scenario_id", "title", "tier", "summary")},
            "as_of": alerts["as_of"],
            "active_alerts": [
                {
                    "rule": a["rule"],
                    "component": a["component"],
                    "metric": a["metric"],
                    "severity": a["severity"],
                    "fired_at": a["fired_at"],
                }
                for a in alerts["alerts"]
            ],
            "applied_actions": applied,
            "recovery": recovery,
        }
    )


@tool
def ask_user(question: str, *, runtime: ToolRuntime) -> Command[Any]:
    """Ask the operator a clarifying question and WAIT for the answer (a human-in-the-loop pause). Use it when something you need
    is missing - most often which scenario / incident to look at (S01-S16). If the answer names a scenario it is selected for
    the rest of the conversation."""
    call_id = runtime.tool_call_id or "q"
    payload = build_interrupt(
        "question",
        approval_id=f"q_{call_id}",
        title="The copilot has a question",
        summary=question,
        options=["answer"],
    )
    raw = interrupt(payload, response_schema=Answer)
    answer = raw if isinstance(raw, Answer) else Answer.model_validate(raw)
    update: dict[str, Any] = {}
    sid: str | None = None
    m = _SCENARIO_ID.search(answer.answer)
    if m:
        cand = m.group(1).upper()
        try:
            data.get_scenario(cand)
            sid = cand
        except ScenarioNotFound:
            sid = None
    if sid:
        update["scenario_id"] = sid
    msg = ToolMessage(
        content=_dump({"answer": answer.answer, "scenario_id": sid}), tool_call_id=call_id, name="ask_user"
    )
    return Command(update={**update, "messages": [msg]})


OWN_TOOLS = (propose_action, remember_preference, recall_memory, incident_status, ask_user)
