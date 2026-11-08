"""The ``@task`` units of the pipeline. Tasks return plain JSON dicts (they are checkpointed), run their blocking reads in
threads, stream progress/artifact/note events with deterministic keys and are the retry / timeout boundary.

Retries use an EXPLICIT ``retry_on``: the default policy never retries ``RuntimeError``/``OSError`` (gotcha 11). Timeouts
(``TimeoutPolicy``) are async-only (gotcha 19), which is why every model call here is awaited.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, ParamSpec, TypeVar, cast

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.errors import NodeTimeoutError
from langgraph.func import task
from langgraph.runtime import get_runtime
from langgraph.types import RetryPolicy, TimeoutPolicy

from foghorn.common.lifecycle import emit_event
from foghorn.contracts.meta import det_thread_id
from foghorn.flows.postmortem_pipeline.checks import check_postmortem
from foghorn.flows.postmortem_pipeline.facts import build_facts
from foghorn.flows.postmortem_pipeline.models import Postmortem, PostmortemBody, Review
from foghorn.flows.postmortem_pipeline.ports import get_source
from foghorn.flows.postmortem_pipeline.scripts import draft_script, pack, review_script
from foghorn.flows.postmortem_pipeline.streams import emit_safe
from foghorn.logging import get_logger
from foghorn.memory.api import claim_effect, remember
from foghorn.models.factory import get_chat_model, structured

log = get_logger("postmortem.tasks")

P = ParamSpec("P")
T = TypeVar("T")


def typed_task(
    *, retry_policy: RetryPolicy | None = None, timeout: TimeoutPolicy | None = None
) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
    """``langgraph.func.task`` with a signature mypy can follow (its decorator-factory form loses ``P``/``T``). At runtime the
    result is the real ``_TaskFunction`` (``.retry_policy`` / ``.timeout`` are inspectable in tests)."""

    def deco(fn: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        return cast("Callable[P, Awaitable[T]]", task(retry_policy=retry_policy, timeout=timeout)(fn))

    return deco


RETRYABLE: tuple[type[Exception], ...] = (TimeoutError, ConnectionError, OSError, NodeTimeoutError)
RETRY = RetryPolicy(
    max_attempts=3, initial_interval=0.1, backoff_factor=2.0, max_interval=2.0, jitter=False, retry_on=RETRYABLE
)
MODEL_TIMEOUT = TimeoutPolicy(run_timeout=120.0)
IO_TIMEOUT = TimeoutPolicy(run_timeout=30.0)


def _key(incident_id: str, revision: int, stage: str) -> str:
    return f"pm:{incident_id}:r{revision}:{stage}"


# ------------------------------------------------------------------------------------------------ load_incident
@typed_task(timeout=IO_TIMEOUT)
async def load_incident(inp: dict[str, Any]) -> dict[str, Any]:
    """The commander's report: from the commander thread's state through the SDK (``values["report"]``), else from the report
    handed over in the input, else nothing (the draft then says the cause is undetermined)."""
    incident_id = str(inp["incident_id"])
    thread_id = inp.get("source_thread_id") or det_thread_id("incident_commander", incident_id)
    values: dict[str, Any] | None = None
    try:
        values = await get_source().commander_state(str(thread_id))
    except Exception as exc:  # unreachable commander thread must not stop a postmortem
        log.warning("commander_state_unavailable", incident=incident_id, thread=thread_id, error=repr(exc)[:200])
    report = (values or {}).get("report")
    source = "sdk" if isinstance(report, dict) and report else "none"
    if source == "none" and isinstance(inp.get("report"), dict):
        report, source = inp["report"], "input"
    if source == "none":
        report = None
    inc = (values or {}).get("incident") or {}
    out = {
        "incident_id": incident_id,
        "scenario_id": str(
            inp.get("scenario_id") or (report or {}).get("scenario_id") or (values or {}).get("scenario_id") or ""
        ),
        "severity": inc.get("severity") or (report or {}).get("severity"),
        "title": inc.get("title"),
        "as_of": (values or {}).get("as_of"),
        "report": report,
        "source": source,
        "thread_id": str(thread_id),
    }
    emit_safe(
        "progress", f"pm:{incident_id}:load", agent="load_incident", label=f"loaded commander report ({source})", pct=10
    )
    return out


# ---------------------------------------------------------------------------------------------- build_timeline
@typed_task(timeout=IO_TIMEOUT)
async def build_timeline(incident: dict[str, Any]) -> dict[str, Any]:
    """Facts bundle: timeline (alerts + changes + actions, fenced to the incident window), impact and on-call owners."""
    facts = await asyncio.to_thread(build_facts, incident)
    iid = incident["incident_id"]
    emit_safe(
        "progress",
        f"pm:{iid}:timeline",
        agent="build_timeline",
        label=f"timeline built: {len(facts['timeline'])} entries from {facts['window'].get('start') or 'n/a'}",
        pct=25,
    )
    for note in facts["tool_errors"]:
        emit_safe("note", f"pm:{iid}:timeline:{note[:24]}", agent="build_timeline", text=note)
    return facts


# --------------------------------------------------------------------------------------------------------- draft
@typed_task(retry_policy=RETRY, timeout=MODEL_TIMEOUT)
async def draft(facts: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """Two model calls: structured body (``nostream``) then the summary paragraph, whose tokens stream to the user."""
    iid, rnd, revision = facts["incident"]["id"], int(spec["round"]), int(spec["revision"])
    llm = get_chat_model("smart", offline_script=draft_script())
    body_llm = structured(llm, PostmortemBody).with_config(tags=["nostream"])
    prompt = str(spec["prompt"])
    issues = list(spec.get("issues") or [])
    body: PostmortemBody = await body_llm.ainvoke(
        [SystemMessage(prompt), HumanMessage(pack(facts, revision=rnd, issues=issues))]
    )
    body_d = body.model_dump(mode="json")
    note = "revising after review" if rnd else "first draft"
    emit_safe(
        "progress",
        _key(iid, revision, f"draft{rnd}"),
        agent="draft",
        label=f"{note}: sections written",
        pct=45 + 10 * rnd,
    )
    summary_msg = await llm.ainvoke(
        [
            SystemMessage(prompt + "\nWrite the executive summary (three sentences)."),
            HumanMessage(pack(facts, revision=rnd, draft=body_d)),
        ]
    )
    pm = Postmortem(
        **body_d,
        incident_id=iid,
        severity=facts["incident"]["severity"],
        scenario_id=facts["incident"].get("scenario_id"),
        component=facts.get("component"),
        category=facts.get("category"),
        summary=str(summary_msg.text).strip(),
        revision=revision,
        window=facts.get("window") or {},
    )
    out = pm.model_dump(mode="json")
    emit_safe(
        "artifact",
        _key(iid, revision, f"artifact{rnd}"),
        agent="draft",
        name=f"postmortem-{iid}-draft-{rnd}",
        kind="text",
        ref=None,
    )
    return out


# -------------------------------------------------------------------------------------------------------- review
@typed_task(retry_policy=RETRY, timeout=MODEL_TIMEOUT)
async def review(facts: dict[str, Any], pm: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """The ``judge``-role critic plus the deterministic checks. A draft is ``ok`` only if NEITHER finds a blocker."""
    iid, revision, rnd = facts["incident"]["id"], int(pm.get("revision", 1)), int(spec["round"])
    critic_llm = structured(get_chat_model("judge", offline_script=review_script()), Review).with_config(
        tags=["nostream"]
    )
    critic: Review = await critic_llm.ainvoke(
        [SystemMessage(str(spec["prompt"])), HumanMessage(pack(facts, revision=rnd, draft=pm))]
    )
    merged = {i.id: i for i in check_postmortem(pm, facts)}
    for i in critic.issues:
        merged.setdefault(i.id, i)
    issues = list(merged.values())
    blockers = [i for i in issues if i.severity == "blocker"]
    emit_safe(
        "decision",
        _key(iid, revision, f"review{rnd}"),
        agent="review",
        what="review",
        why=f"{len(blockers)} blocking issue(s), {len(issues) - len(blockers)} warning(s)",
    )
    return {"ok": not blockers, "issues": [i.model_dump(mode="json") for i in issues], "summary": critic.summary}


# ------------------------------------------------------------------------------------------------------- publish
@typed_task(retry_policy=RETRY, timeout=IO_TIMEOUT)
async def publish(pm: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    """Side effects, AFTER the human decision and idempotent twice over: the Store write is an upsert on a business key and
    the lifecycle event carries an idempotency key; ``claim_effect`` only tells us whether this is the first publication."""
    store = get_runtime().store
    iid, revision = pm["incident_id"], int(pm["revision"])
    component = pm.get("component") or "unknown"
    key = f"pm-{iid}"
    ns = ["postmortems", component]
    history = list(meta.get("history") or [])
    value = {
        "text": f"Postmortem {iid} ({pm.get('severity')}, {component}): {pm.get('summary', '')} Root cause: {pm.get('root_cause', '')}",
        "incident_id": iid,
        "revision": revision,
        "status": pm.get("status"),
        "postmortem": pm,
        "history": history,
        "provenance": {
            "thread_id": meta.get("thread_id"),
            "event_id": None,
            "incident_id": iid,
            "written_by": "postmortem_pipeline",
        },
    }
    first = await claim_effect(
        store, f"postmortem.publish:{iid}:r{revision}", {"incident_id": iid, "revision": revision}
    )
    stored = await remember(store, "postmortems", key, value, component=component)
    emit_event(
        "postmortem.published",
        idempotency_key=f"postmortem.published:{iid}:r{revision}",
        component=pm.get("component"),
        payload={
            "incident_id": iid,
            "scenario_id": pm.get("scenario_id"),
            "postmortem": pm,
            "revision": revision,
            "decided_by": meta.get("decided_by"),
            "thread_id": meta.get("thread_id"),
            "store": {"namespace": ns, "key": key},
        },
        scenario_id=pm.get("scenario_id"),
    )
    if stored:
        emit_safe("memory.written", f"pm:{iid}:r{revision}:memory", agent="publish", ns=ns, key=key)
    emit_safe(
        "progress", _key(iid, revision, "published"), agent="publish", label=f"published revision {revision}", pct=100
    )
    return {"stored": stored, "first": first, "key": key, "ns": ns, "revision": revision}
