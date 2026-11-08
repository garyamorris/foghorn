"""postmortem_pipeline (Functional API): drafting from commander reports, the bounded review loop, the human decision,
`previous` accumulation, idempotent publishing, streaming and failure injection. Offline, deterministic, fast."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import NodeTimeoutError
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command, TimeoutPolicy

from foghorn.common import lifecycle
from foghorn.contracts.interrupts import Decision
from foghorn.contracts.meta import det_thread_id
from foghorn.data import api as data_api
from foghorn.embeddings import aembed
from foghorn.flows.postmortem_pipeline import ports, scripts, tasks
from foghorn.flows.postmortem_pipeline.build import build_graph
from foghorn.flows.postmortem_pipeline.checks import blockers, check_postmortem
from foghorn.flows.postmortem_pipeline.facts import build_facts
from foghorn.flows.postmortem_pipeline.models import MAX_REVISIONS
from foghorn.flows.postmortem_pipeline.render import render_markdown
from foghorn.flows.postmortem_pipeline.workflow import apply_edits
from foghorn.models import offline as o

ROOT = Path(__file__).resolve().parents[2]

# --- commander reports (what incident_commander leaves in its state under ``report``) ---------------------------------
CASES: dict[str, dict[str, Any]] = {
    "S01": {
        "component": "checkout-service",
        "category": "bad_deploy",
        "action": ("rollback_deploy", "checkout-service"),
    },
    "S05": {"component": "orders-db", "category": "db_pool_exhaustion", "action": ("kill_queries", "orders-db")},
    "S06": {"component": "sessions-cache", "category": "cache_stampede", "action": ("revert_config", "sessions-cache")},
    "S10": {
        "component": "pricing-service",
        "category": "config_change",
        "action": ("revert_flag", "new_pricing_engine"),
    },
}


def make_report(
    sid: str, *, incident_id: str = "INC-9001", recovered: bool | None = True, abstained: bool = False
) -> dict[str, Any]:
    case = CASES.get(sid, CASES["S01"])
    t0 = data_api.default_as_of(sid)
    executed = (
        []
        if abstained
        else [
            {
                "action": case["action"][0],
                "target": case["action"][1],
                "params": {},
                "t_virtual": t0,
                "outcome": "executed",
            }
        ]
    )
    return {
        "scenario_id": sid,
        "incident_id": incident_id,
        "severity": "SEV2",
        "root_cause": None
        if abstained
        else {
            "component": case["component"],
            "category": case["category"],
            "confidence": 0.91,
            "evidence_refs": ["metric:x/error_rate"],
        },
        "abstained": abstained,
        "hypotheses": [],
        "evidence_refs": [],
        "actions_proposed": [],
        "actions_executed": executed,
        "recovered": None if abstained else recovered,
        "approvals": [
            {"id": "apr_1", "kind": "approval", "status": "approved", "decided_by": "j.okafor", "action": None}
        ],
        "interrupts_seen": ["approval"],
        "comms_draft": "",
        "memory_recalled": [],
        "memory_written": [],
        "flags": ["abstained"] if abstained else [],
        "tool_calls_used": 7,
        "supervisor_hops": 4,
        "narrative": "",
    }


def run_input(sid: str = "S01", incident_id: str = "INC-9001", **kw: Any) -> dict[str, Any]:
    return {"incident_id": incident_id, "scenario_id": sid, "report": make_report(sid, incident_id=incident_id, **kw)}


class FakeSource:
    """Stand-in for the in-process SDK client: ``threads.get_state(commander_thread)["values"]``."""

    def __init__(self) -> None:
        self.states: dict[str, dict[str, Any]] = {}
        self.error: Exception | None = None
        self.asked: list[str] = []

    async def commander_state(self, thread_id: str) -> dict[str, Any] | None:
        self.asked.append(thread_id)
        if self.error:
            raise self.error
        return self.states.get(thread_id)


@pytest.fixture
def source():
    fake = FakeSource()
    ports.set_source_factory(lambda: fake)
    yield fake
    ports.set_source_factory(None)


@pytest.fixture
def store():
    return InMemoryStore(index={"dims": 256, "embed": aembed, "fields": ["text"]})


@pytest.fixture
def graph(store, source):
    return build_graph(checkpointer=InMemorySaver(), store=store)


def cfg(thread: str = "pm-t1") -> dict[str, Any]:
    return {"configurable": {"thread_id": thread}}


async def drive(
    g: Any, inp: Any, *, resumes: list[Any] | None = None, thread: str = "pm-t1", context: dict[str, Any] | None = None
):
    """Run to completion answering each interrupt from `resumes`; returns (final GraphOutput, interrupts seen)."""
    queue, seen = list(resumes or []), []
    out = await g.ainvoke(inp, cfg(thread), context=context, version="v2")
    while out.interrupts:
        seen.extend(out.interrupts)
        out = await g.ainvoke(Command(resume=queue.pop(0)), cfg(thread), context=context, version="v2")
    assert not queue, f"unused resumes {queue}"
    return out, seen


def approve(by: str = "pat") -> dict[str, Any]:
    return {"decision": "approve", "decided_by": by}


def published(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    uniq = {
        e["idempotency_key"]: e for e in events if e["type"] == "postmortem.published"
    }  # the bus dedupes on this key
    return list(uniq.values())


# ======================================================================================================== happy path
async def test_happy_path_one_revise_round_then_human_approval_publishes(graph, store):
    with lifecycle.capture() as events:
        out, seen = await drive(graph, run_input("S01"), resumes=[approve()])
    assert len(seen) == 1
    payload = seen[0].value
    assert payload["kind"] == "edit" and payload["options"] == ["approve", "edit", "reject"]
    assert payload["approval_id"] == "pm_INC-9001_r1" and payload["context"]["incident_id"] == "INC-9001"
    assert payload["proposed"]["review_rounds"] == 1  # the first draft forgot the owners; the revise pass fixed it
    assert payload["proposed"]["open_issues"] == []
    assert "## Action items" in payload["proposed"]["markdown"] and "UNASSIGNED" not in payload["proposed"]["markdown"]

    res = out.value["result"]
    assert res["graph_id"] == "postmortem_pipeline" and res["status"] == "ok"
    assert (
        res["metrics"]["review_rounds"] == 1.0
        and res["metrics"]["owned_action_items"] == res["metrics"]["action_items"]
    )
    assert (
        res["artifacts"][0]["kind"] == "report"
        and res["artifacts"][0]["ref"] == "store:postmortems/checkout-service/pm-INC-9001"
    )
    assert out.value["postmortem"]["status"] == "approved" and out.value["revision"] == 1

    item = await store.aget(("postmortems", "checkout-service"), "pm-INC-9001")
    assert item is not None and item.value["revision"] == 1 and item.value["postmortem"]["incident_id"] == "INC-9001"
    assert (
        item.value["provenance"]["written_by"] == "postmortem_pipeline"
        and item.value["provenance"]["thread_id"] == "pm-t1"
    )

    pubs = published(events)
    assert len(pubs) == 1 and pubs[0]["idempotency_key"] == "postmortem.published:INC-9001:r1"
    assert pubs[0]["payload"]["postmortem"]["incident_id"] == "INC-9001"  # memory_keeper reads it from here
    assert pubs[0]["payload"]["scenario_id"] == "S01" and pubs[0]["subject"]["component"] == "checkout-service"
    types = {e["type"] for e in events}
    assert {"approval.requested", "approval.decided", "postmortem.published"} <= types
    req = next(e for e in events if e["type"] == "approval.requested")
    assert req["payload"]["thread_id"] == "pm-t1" and req["payload"]["kind"] == "edit"


@pytest.mark.parametrize("sid", sorted(CASES))
async def test_several_commander_reports_produce_grounded_postmortems(graph, sid):
    out, seen = await drive(graph, run_input(sid), resumes=[approve()])
    pm = out.value["postmortem"]
    case = CASES[sid]
    assert (
        case["component"] in pm["root_cause"]
        and pm["component"] == case["component"]
        and pm["category"] == case["category"]
    )
    assert re.search(r"\d", pm["impact"])  # impact is quantified from alert data
    refs = {e["ref"] for e in pm["timeline"]}
    assert f"action:{case['action'][0]}:{case['action'][1]}" in refs and f"recovery:{case['component']}" in refs
    assert any(e["kind"] == "detection" for e in pm["timeline"])
    assert pm["window"]["start"] <= pm["window"]["end"] and all(
        e["t"] <= pm["window"]["end"] for e in pm["timeline"]
    )  # time fence
    owners = {a["owner"] for a in pm["action_items"]}
    assert None not in owners and len(owners) >= 2  # owners come from the on-call data, several roles
    assert out.value["review"]["ok"] and not blockers(check_postmortem(pm, build_facts(_incident_for(sid))))
    assert seen[0].value["proposed"]["review_rounds"] == 1


def _incident_for(sid: str) -> dict[str, Any]:
    return {"incident_id": "INC-9001", "scenario_id": sid, "report": make_report(sid), "source": "input"}


async def test_abstained_commander_gets_an_honest_undetermined_root_cause(graph):
    out, _ = await drive(graph, run_input("S13", abstained=True), resumes=[approve()])
    pm = out.value["postmortem"]
    assert "not determined" in pm["root_cause"].lower() and "abstained" in pm["root_cause"].lower()
    assert not any(e["kind"] == "recovery" for e in pm["timeline"])
    assert any("abstained" in x for x in pm["what_went_badly"]) and out.value["review"]["ok"]


# ======================================================================================== reading the commander thread
async def test_report_is_read_from_the_commander_thread_and_beats_the_input(graph, source):
    commander_thread = det_thread_id("incident_commander", "INC-9001")
    report = make_report("S05")  # SDK says orders-db ...
    source.states[commander_thread] = {
        "report": report,
        "incident": {"id": "INC-9001", "severity": "SEV1", "title": "Orders DB pool exhausted"},
    }
    out, _ = await drive(
        graph, run_input("S01"), resumes=[approve()]
    )  # ... the input still carries a checkout-service report
    pm = out.value["postmortem"]
    assert source.asked == [commander_thread]
    assert pm["component"] == "orders-db" and pm["title"] == "Orders DB pool exhausted" and pm["severity"] == "SEV1"


async def test_explicit_source_thread_id_is_honoured(graph, source):
    source.states["thread-from-dispatcher"] = {"report": make_report("S06")}
    inp = {"incident_id": "INC-9001", "scenario_id": "S06", "source_thread_id": "thread-from-dispatcher"}
    out, _ = await drive(graph, inp, resumes=[approve()])
    assert source.asked == ["thread-from-dispatcher"] and out.value["postmortem"]["component"] == "sessions-cache"


async def test_unreachable_commander_falls_back_to_the_report_in_the_input(graph, source):
    source.error = ConnectionError("agent server restarting")
    out, _ = await drive(graph, run_input("S10"), resumes=[approve()])
    assert out.value["postmortem"]["component"] == "pricing-service"


async def test_no_report_anywhere_still_yields_a_reviewable_draft(graph):
    out, seen = await drive(graph, {"incident_id": "INC-77", "scenario_id": "S01"}, resumes=[approve()])
    pm = out.value["postmortem"]
    assert "not determined" in pm["root_cause"].lower() and "not available" in pm["root_cause"].lower()
    assert seen[0].value["proposed"]["postmortem"]["incident_id"] == "INC-77"


# ============================================================================================= the bounded review loop
async def test_naive_model_exhausts_the_revise_bound_and_the_human_sees_the_open_issues(graph):
    out, seen = await drive(graph, run_input("S01"), resumes=[approve()], context={"model_profile": "naive"})
    prop = seen[0].value["proposed"]
    assert prop["review_rounds"] == MAX_REVISIONS == 2
    ids = {i["id"] for i in prop["open_issues"]}
    assert "action_items_owned" in ids and "timeline_complete" in ids
    assert "UNASSIGNED" in prop["markdown"] and "## Open review issues" in prop["markdown"]
    assert out.value["result"]["metrics"]["open_blockers"] >= 2  # the human still decides; publishing is allowed


@pytest.mark.parametrize(("asked", "expected"), [(0, 0), (1, 1), (2, 2), (9, 2)])
async def test_revise_rounds_are_capped_by_context_and_by_the_hard_bound(graph, asked, expected):
    out, seen = await drive(
        graph, run_input("S01"), resumes=[approve()], context={"model_profile": "naive", "max_revisions": asked}
    )
    assert seen[0].value["proposed"]["review_rounds"] == expected


async def test_review_calls_the_judge_role_and_the_loop_never_calls_it_more_than_bound_plus_one(graph):
    judge = scripts.review_script()
    o.SCRIPT_OVERRIDES["judge"] = judge
    await drive(graph, run_input("S01"), resumes=[approve()], context={"model_profile": "naive"})
    assert len(judge.calls) == MAX_REVISIONS + 1
    assert all(c.role == "judge" and c.schema_name == "Review" for c in judge.calls)


# ====================================================================================================== human decisions
async def test_edit_decision_applies_valid_edits_ignores_unknown_ones_and_publishes_the_edited_text(graph, store):
    edit = {
        "decision": "edit",
        "decided_by": "j.okafor",
        "note": "tighten the summary",
        "edits": {
            "summary": "Checkout broke after a bad release; rolled back in 4 minutes.",
            "bogus_field": 1,
            "action_items": "not-a-list",
        },
    }
    with lifecycle.capture() as events:
        out, _ = await drive(graph, run_input("S01"), resumes=[edit])
    pm = out.value["postmortem"]
    assert pm["status"] == "edited" and pm["summary"].startswith("Checkout broke")
    assert (
        out.value["result"]["metrics"]["edited_fields"] == 1.0
        and out.value["result"]["metrics"]["ignored_edits"] == 2.0
    )
    item = await store.aget(("postmortems", "checkout-service"), "pm-INC-9001")
    assert item is not None and item.value["postmortem"]["summary"].startswith("Checkout broke")
    assert published(events)[0]["payload"]["decided_by"] == "j.okafor"
    assert "edited by j.okafor" in out.value["result"]["summary"]


async def test_reject_publishes_nothing_and_leaves_a_note_for_the_next_draft(graph, store):
    with lifecycle.capture() as events:
        out, _ = await drive(
            graph,
            run_input("S01"),
            resumes=[{"decision": "reject", "decided_by": "pat", "note": "impact section is vague"}],
        )
    assert out.value["result"]["status"] == "skipped" and "impact section is vague" in out.value["result"]["summary"]
    assert await store.aget(("postmortems", "checkout-service"), "pm-INC-9001") is None
    assert published(events) == [] and any(e["type"] == "approval.decided" for e in events)


async def test_ask_is_not_supported_and_does_not_publish(graph, store):
    out, _ = await drive(graph, run_input("S01"), resumes=[{"decision": "ask", "question": "why rollback?"}])
    assert out.value["result"]["status"] == "skipped"
    assert await store.aget(("postmortems", "checkout-service"), "pm-INC-9001") is None


async def test_invalid_resume_value_is_rejected_and_the_interrupt_stays_answerable(graph):
    out = await graph.ainvoke(run_input("S01"), cfg(), version="v2")
    assert out.interrupts
    with pytest.raises(ValueError, match=r"(?i)decision"):
        await graph.ainvoke(Command(resume={"decision": "maybe"}), cfg(), version="v2")
    out = await graph.ainvoke(Command(resume=approve()), cfg(), version="v2")  # still answerable
    assert out.value["result"]["status"] == "ok"


# =========================================================================================== previous (revision history)
async def test_previous_accumulates_revision_history_across_runs_on_one_thread(graph, store):
    # run 1: rejected with a note -> the note must steer run 2
    out1, seen1 = await drive(
        graph, run_input("S01"), resumes=[{"decision": "reject", "decided_by": "pat", "note": "name the flag owner"}]
    )
    assert seen1[0].value["approval_id"] == "pm_INC-9001_r1" and out1.value["revision"] == 1
    # run 2 on the SAME thread: revision 2, sees the feedback, approved
    out2, seen2 = await drive(graph, run_input("S01"), resumes=[approve("sam")])
    assert seen2[0].value["approval_id"] == "pm_INC-9001_r2" and out2.value["revision"] == 2
    assert any("name the flag owner" in x for x in out2.value["postmortem"]["what_went_badly"])
    # run 3: an amendment of the published postmortem -> revision 3, history keeps all three
    out3, _ = await drive(graph, run_input("S01"), resumes=[approve("sam")])
    assert out3.value["revision"] == 3
    assert not any(
        "name the flag owner" in x for x in out3.value["postmortem"]["what_went_badly"]
    )  # feedback was consumed
    item = await store.aget(("postmortems", "checkout-service"), "pm-INC-9001")
    assert item is not None and item.value["revision"] == 3
    assert [h["status"] for h in item.value["history"]] == ["rejected", "published", "published"]
    assert [h["decided_by"] for h in item.value["history"]] == ["pat", "sam", "sam"]

    # `entrypoint.final`: what the caller sees (the output) differs from what is saved for `previous` (the memo)
    saver = graph.checkpointer
    tup = await saver.aget_tuple(cfg())
    memo = tup.checkpoint["channel_values"]["__previous__"]
    assert set(memo) == {"incident_id", "revisions", "last_published", "feedback"} and len(memo["revisions"]) == 3
    state = await graph.aget_state(cfg())
    assert set(state.values) == {"result", "postmortem", "markdown", "review", "revision"}


# =============================================================================================== idempotent publishing
async def test_double_resume_does_not_publish_twice(graph, store):
    with lifecycle.capture() as events:
        out, _ = await drive(graph, run_input("S01"), resumes=[approve()])
        again = await graph.ainvoke(Command(resume=approve()), cfg(), version="v2")  # a second click on "approve"
    assert again.value["result"]["status"] == "ok" and again.value["revision"] == out.value["revision"]
    assert len(published(events)) == 1 and len([e for e in events if e["type"] == "postmortem.published"]) == 1
    items = await store.asearch(("postmortems",), limit=10)
    assert len(items) == 1 and items[0].value["revision"] == 1
    claim = await store.aget(("effects",), "postmortem.publish:INC-9001:r1")
    assert claim is not None


async def test_publish_claims_the_effect_once_per_revision(store, source):
    """The task itself: re-running `publish` for the same revision (a retry, a replay) writes the same item and the same event."""
    g = build_graph(checkpointer=InMemorySaver(), store=store)
    with lifecycle.capture() as events:
        await drive(g, run_input("S01"), resumes=[approve()], thread="a")
        await drive(
            g, run_input("S01"), resumes=[approve()], thread="b"
        )  # another thread, same incident and revision 1
    assert len({e["idempotency_key"] for e in events if e["type"] == "postmortem.published"}) == 1
    assert len(await store.asearch(("postmortems",), limit=10)) == 1


# =================================================================================================== streaming
async def test_streaming_custom_events_updates_tasks_and_summary_tokens(graph):
    modes = ["updates", "custom", "messages", "tasks"]
    chunks: list[tuple[str, Any]] = []
    async for mode, data in graph.astream(run_input("S01"), cfg(), stream_mode=modes):
        chunks.append((mode, data))
    custom = [d for m, d in chunks if m == "custom"]
    kinds = [c["kind"] for c in custom]
    assert {"progress", "artifact", "decision", "note"} <= set(kinds)
    assert all(c["v"] == 1 and c["key"] and "data" in c for c in custom)
    keys = [c["key"] for c in custom]
    assert len(keys) == len(set(keys))  # deterministic and unique per stage
    art = next(c for c in custom if c["kind"] == "artifact")
    assert art["data"]["kind"] == "text" and art["data"]["name"].startswith("postmortem-INC-9001-draft")
    # tasks appear as `updates` entries {task_name: result}
    names = [next(iter(d)) for m, d in chunks if m == "updates" and "__interrupt__" not in d]
    assert names[:2] == ["load_incident", "build_timeline"] and names.count("draft") == 2 and names.count("review") == 2
    task_events = [d for m, d in chunks if m == "tasks"]
    assert {t["name"] for t in task_events} >= {
        "load_incident",
        "build_timeline",
        "draft",
        "review",
        "postmortem_pipeline",
    }
    # tokens: only the summary call streams; the structured calls are tagged `nostream`
    toks = [msg.content for m, (msg, _meta) in ((m, d) for m, d in chunks if m == "messages") if msg.content]
    text = "".join(toks)
    assert text.startswith("SEV2 incident INC-9001") and "{" not in text and "timeline" not in text
    assert any(meta["langgraph_node"] == "draft" for m, (_msg, meta) in ((m, d) for m, d in chunks if m == "messages"))
    assert any(isinstance(d, dict) and "__interrupt__" in d for m, d in chunks if m == "updates")


async def test_resume_re_emits_with_the_same_keys(graph):
    first = [d async for m, d in graph.astream(run_input("S01"), cfg(), stream_mode=["custom"]) if m == "custom"]  # noqa: F841
    after = [
        d async for m, d in graph.astream(Command(resume=approve()), cfg(), stream_mode=["custom"]) if m == "custom"
    ]
    keys = [c["key"] for c in after]
    assert "pm:INC-9001:r1:published" in keys
    mem = next(c for c in after if c["kind"] == "memory.written")
    assert mem["data"] == {"ns": ["postmortems", "checkout-service"], "key": "pm-INC-9001"}


# ============================================================================================== failure injection
def _flaky_draft(times: int, exc: BaseException) -> o.Script:
    base = scripts.draft_script()
    s = o.Script()
    s.when("flaky-body", lambda c: c.schema_name == "PostmortemBody", o.flaky(times, exc, lambda c: base.decide(c)[1]))
    s.rules.extend(base.rules)
    return s


@pytest.mark.parametrize("exc", [TimeoutError("llm timeout"), ConnectionError("provider reset")])
async def test_transient_model_errors_are_retried_by_the_task_policy(graph, exc):
    script = _flaky_draft(2, exc)
    o.SCRIPT_OVERRIDES["smart"] = script
    out, _ = await drive(graph, run_input("S01"), resumes=[approve()])
    assert out.value["result"]["status"] == "ok"
    body_calls = [c for c in script.calls if c.schema_name == "PostmortemBody"]
    assert len(body_calls) >= 3 + 1  # 2 failures + success of the first draft, plus the revise draft


async def test_exhausted_retries_fail_the_run_loudly(graph):
    o.SCRIPT_OVERRIDES["smart"] = _flaky_draft(10, TimeoutError("llm down"))
    with pytest.raises(TimeoutError):
        await graph.ainvoke(run_input("S01"), cfg(), version="v2")


async def test_non_retryable_errors_are_not_retried(graph):
    script = _flaky_draft(10, ValueError("schema bug"))
    o.SCRIPT_OVERRIDES["smart"] = script
    with pytest.raises(ValueError, match="schema bug"):
        await graph.ainvoke(run_input("S01"), cfg(), version="v2")
    assert len([c for c in script.calls if c.schema_name == "PostmortemBody"]) == 1


def test_tasks_carry_explicit_retry_and_timeout_policies():
    for t in (tasks.draft, tasks.review, tasks.publish):
        (policy,) = t.retry_policy
        assert (
            TimeoutError in policy.retry_on
            and ConnectionError in policy.retry_on
            and NodeTimeoutError in policy.retry_on
        )
        assert policy.max_attempts == 3
        assert isinstance(t.timeout, TimeoutPolicy) and t.timeout.run_timeout
    assert tasks.draft.timeout.run_timeout > tasks.load_incident.timeout.run_timeout


async def test_a_slow_model_trips_the_task_timeout_and_is_retried_then_fails(graph, monkeypatch):
    attempts: list[int] = []

    class Slow:
        def with_config(self, **_: Any) -> Slow:
            return self

        async def ainvoke(self, *_: Any, **__: Any) -> Any:
            attempts.append(1)
            await asyncio.sleep(5)

    monkeypatch.setattr(tasks, "structured", lambda *_a, **_k: Slow())
    monkeypatch.setattr(tasks.draft, "timeout", TimeoutPolicy(run_timeout=0.05))
    with pytest.raises(NodeTimeoutError):
        await graph.ainvoke(run_input("S01"), cfg(), version="v2")
    assert len(attempts) == 3  # RETRY.max_attempts


# ================================================================================================ pure pieces
def test_time_fence_the_timeline_never_contains_the_future():
    inc = {
        "incident_id": "INC-1",
        "scenario_id": "S01",
        "as_of": "2026-11-05T14:07:00Z",
        "report": make_report("S01", recovered=None),
        "source": "input",
    }
    inc["report"]["actions_executed"] = []
    facts = build_facts(inc)
    assert facts["window"]["end"] == "2026-11-05T14:07:00Z"
    assert facts["timeline"] and all(e["t"] <= "2026-11-05T14:07:00Z" for e in facts["timeline"])
    assert not any(e["kind"] in ("action", "recovery") for e in facts["timeline"])


def test_facts_degrade_gracefully_without_a_scenario():
    facts = build_facts({"incident_id": "INC-1", "scenario_id": "", "report": make_report("S01"), "source": "input"})
    assert facts["tool_errors"] and facts["timeline"][0]["kind"] == "action" and facts["oncall"] == {}


def test_checks_catch_each_defect():
    facts = build_facts(_incident_for("S01"))
    good = scripts.compose_body(facts, revision=1, issues=[], profile="competent")
    good.update(summary="Short summary.", incident_id="INC-9001")
    assert blockers(check_postmortem(good, facts)) == []
    bad = {
        **good,
        "root_cause": "Probably DNS.",
        "timeline": good["timeline"][:1],
        "impact": "It was bad.",
        "action_items": [],
    }
    ids = {i.id for i in check_postmortem(bad, facts)}
    assert {"root_cause_matches_report", "timeline_complete", "impact_quantified", "action_items_present"} <= ids
    blame = {**good, "summary": "This was the fault of the on-call engineer, who was careless."}
    assert "blameless" in {i.id for i in check_postmortem(blame, facts)}
    unowned = {**good, "action_items": [{**a, "owner": "someone.else"} for a in good["action_items"]]}
    assert "action_items_owned" in {i.id for i in check_postmortem(unowned, facts)}


def test_apply_edits_validates_each_key_independently():
    facts = build_facts(_incident_for("S01"))
    pm = scripts.compose_body(facts, revision=1, issues=[], profile="competent")
    pm.update(incident_id="INC-9001", summary="s")
    new, applied, rejected = apply_edits(pm, {"summary": "better", "impact": 5, "nope": 1, "lessons_learned": ["a"]})
    assert new["summary"] == "better" and new["lessons_learned"] == ["a"] and new["impact"] == pm["impact"]
    assert applied == ["summary", "lessons_learned"] and rejected == ["impact", "nope"]


def test_render_markdown_marks_unassigned_and_open_issues():
    facts = build_facts(_incident_for("S01"))
    pm = scripts.compose_body(facts, revision=0, issues=[], profile="competent")
    pm.update(
        incident_id="INC-9001", summary="s", severity="SEV2", component="checkout-service", revision=1, status="draft"
    )
    md = render_markdown(pm, open_issues=[{"id": "x", "severity": "blocker", "message": "m"}])
    assert md.startswith("# Postmortem INC-9001") and "UNASSIGNED" in md and "(blocker) x: m" in md


def test_offline_scripts_round_trip_the_message_envelope():
    msg = scripts.pack({"a": 1}, revision=2, issues=[{"id": "i"}], draft={"d": True})
    assert scripts.unpack(msg) == {"revision": 2, "facts": {"a": 1}, "issues": [{"id": "i"}], "draft": {"d": True}}


# ===================================================================================================== contract
def test_studio_schemas_and_editable_prompts(graph):
    name = "postmortem_pipeline"
    nodes = graph.get_graph().to_json()["nodes"]
    assert graph.name == name and [n["id"] for n in nodes if not n["id"].startswith("__")] == [
        name
    ]  # ONE node (gotcha 28)
    inp = graph.get_input_jsonschema()
    assert "incident_id" in json.dumps(inp) and inp["$ref"].endswith("PostmortemRunInput")
    out = json.dumps(graph.get_output_jsonschema())
    assert "GraphResult" in out and "postmortem" in out
    ctx = graph.get_context_jsonschema()["properties"]
    for prompt in ("draft_prompt", "review_prompt"):
        assert ctx[prompt]["langgraph_type"] == "prompt" and ctx[prompt]["langgraph_nodes"] == [name]
    assert {
        "model_profile",
        "approval_policy",
        "token_delay_ms",
        "breakpoints",
        "chaos",
        "as_of",
        "scenario_id",
    } <= set(ctx)
    Decision.model_validate(approve())


def test_graph_code_never_reads_ground_truth():
    forbidden = re.compile(r"get_truth|reveal_truth\s*=\s*True|access\.truth|\.truth\(")
    for pkg in ("postmortem_pipeline", "shift_handoff", "memory_keeper"):
        for f in (ROOT / "src/foghorn/flows" / pkg).glob("*.py"):
            assert not forbidden.search(f.read_text()), f"{f} reads ground truth"


def test_guide_parses_like_the_server_and_sample_inputs_fit_the_input_schema(graph):
    from foghorn.server.catalog import guide_entry

    entry, warning = guide_entry("postmortem_pipeline")
    assert warning is None and entry["has_guide"] and entry["graph_id"] == "postmortem_pipeline"
    assert len(entry["studio_steps"]) >= 5 and len(entry["sample_inputs"]) >= 3 and entry["features"]
    for sample in entry["sample_inputs"]:
        graph.get_input_schema().model_validate(sample["input"])  # a sample that does not fit would 422 in Studio
