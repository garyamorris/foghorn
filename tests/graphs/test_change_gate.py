"""change_gate, the HITL gym: approve / edit / reject / ask / four-eyes (parallel interrupts) / gauntlet."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command
from pydantic import ValidationError

from foghorn.common import lifecycle
from foghorn.flows.change_gate import policy as pol
from foghorn.flows.change_gate.build import build_graph
from foghorn.models import offline as o
from foghorn.tools import api as tools_api
from tests.graphs.support_ingress import by_role, drive


async def state_of(graph: Any, thread_id: str = "gate-1") -> dict[str, Any]:
    """The full thread state (the graph's OUTPUT schema is deliberately narrow: result, status, change)."""
    return dict((await graph.aget_state({"configurable": {"thread_id": thread_id}})).values)


CHANGE: dict[str, Any] = {
    "id": "CHG-1",
    "kind": "deploy",
    "component": "checkout-service",
    "description": "Deploy checkout 2026.11.05.2",
    "risk": "medium",
    "requested_by": "a.dev",
    "environment": "prod",
    "diff": {"rollback_plan": "redeploy 2026.11.04.9"},
}
CRITICAL: dict[str, Any] = {
    "id": "CHG-9",
    "kind": "migration",
    "component": "orders-db",
    "description": "Add column orders.region",
    "risk": "high",
    "requested_by": "d.dba",
    "environment": "prod",
}
APPROVE = {"decision": "approve", "decided_by": "j.okafor"}


@pytest.fixture
def store():
    return InMemoryStore()


@pytest.fixture
def graph(store):
    return build_graph(checkpointer=InMemorySaver(), store=store)


def kinds(seen: list[list[Any]]) -> list[str]:
    return [i.value["kind"] for batch in seen for i in batch]


def events_of(events: list[dict[str, Any]], type_: str) -> list[dict[str, Any]]:
    return [e for e in events if e["type"] == type_]


# =========================================================================================== policy (pure)
@pytest.mark.parametrize(
    ("change", "tier"),
    [
        ({"kind": "flag", "component": "recommendation-service", "environment": "staging", "risk": "low"}, "low"),
        ({"kind": "deploy", "component": "checkout-service", "diff": {"rollback_plan": "x"}, "risk": "low"}, "medium"),
        ({"kind": "deploy", "component": "checkout-service", "risk": "low"}, "high"),  # no rollback plan
        ({"kind": "migration", "component": "orders-db", "risk": "low"}, "critical"),
        (
            {"kind": "flag", "component": "recommendation-service", "environment": "staging", "risk": "high"},
            "high",
        ),  # declared floor
    ],
)
def test_risk_assessment_is_deterministic_and_explains_itself(change, tier):
    r = pol.assess({"id": "c", **change}, requested_at=pol.DEFAULT_REQUESTED_AT, mode="standard", in_freeze=False)
    assert (
        r["tier"] == tier
        and r["factors"]
        and r["score"] == pol.assess({"id": "c", **change}, requested_at="x", mode="standard", in_freeze=False)["score"]
    )
    assert pol.tier_for(r["score"]) == r["computed_tier"]


def test_tier_thresholds_canary_and_freeze_terms():
    assert [pol.tier_for(s) for s in (0, 34.9, 35, 54.9, 55, 74.9, 75, 100)] == [
        "low",
        "low",
        "medium",
        "medium",
        "high",
        "high",
        "critical",
        "critical",
    ]
    base = {"id": "c", "kind": "deploy", "component": "checkout-service", "diff": {"rollback_plan": "x"}}
    plain = pol.assess(base, requested_at="", mode="standard", in_freeze=False)["score"]
    assert (
        pol.assess(
            {**base, "diff": {"rollback_plan": "x", "rollout": "canary 10%"}},
            requested_at="",
            mode="standard",
            in_freeze=False,
        )["score"]
        == plain - 10
    )
    assert pol.assess(base, requested_at="", mode="standard", in_freeze=True)["score"] == plain + 10
    assert pol.assess(base, requested_at="", mode="gauntlet", in_freeze=False)["tier"] == "critical"


def test_change_freeze_window_comes_from_the_policy_document():
    assert pol.freeze_window(pol.to_epoch("2026-11-05T14:00:00Z"))["name"] == "Peak trading"  # Thursday afternoon
    assert pol.freeze_window(pol.to_epoch("2026-11-05T03:00:00Z")) is None  # Thursday night
    assert pol.freeze_window(pol.to_epoch("2026-11-07T14:00:00Z")) is None  # Saturday


def test_approver_matrix_is_read_from_pol_005_and_strict_adds_a_level(monkeypatch):
    def ev(change, **kw):
        risk = pol.assess(
            change, requested_at=pol.DEFAULT_REQUESTED_AT, mode=kw.pop("mode", "standard"), in_freeze=False
        )
        return pol.evaluate(
            change,
            risk,
            requested_at=pol.DEFAULT_REQUESTED_AT,
            mode="standard",
            approval_policy=kw.get("approval_policy", "standard"),
            clarified=True,
        )

    low = {"id": "c", "kind": "flag", "component": "recommendation-service", "environment": "staging", "risk": "low"}
    assert ev(low)["path"] == "auto" and ev(low)["approvers_required"] == 0
    assert (
        ev(low, approval_policy="strict")["path"] == "approval"
        and ev(low, approval_policy="strict")["approvers_required"] == 1
    )
    assert ev(CHANGE)["approvers_required"] == 1 and ev(CHANGE)["roles"] == ["service_oncall"]
    crit = ev(CRITICAL)
    assert crit["path"] == "four_eyes" and crit["roles"] == [
        "service_oncall",
        "dba",
    ]  # a datastore change needs the dba
    edited = copy.deepcopy(pol.policies())  # never mutate the cached documents
    edited["POL-005"]["risk_levels"]["medium"]["approvers"] = 2
    monkeypatch.setattr(pol, "policies", lambda: edited)
    assert ev(CHANGE)["approvers_required"] == 2  # editing the policy document changes the gate


# =========================================================================================== approve
async def test_medium_change_one_approval_then_applied_with_lifecycle_events(graph, store):
    with lifecycle.capture() as events:
        out, seen = await drive(graph, {"change": CHANGE}, [APPROVE])
    assert kinds(seen) == ["approval"] and out["status"] == "applied"
    p = seen[0][0].value
    assert p["v"] == 1 and p["approval_id"] == "apr_CHG-1_0" and p["options"] == ["approve", "edit", "reject", "ask"]
    assert (
        p["policy"]["approvers_required"] == 1 and p["policy"]["roles"] == ["service_oncall"] and p["risk"] == "medium"
    )
    assert {c["id"] for c in p["policy"]["checks"]} == {"POL-003", "POL-005", "POL-006"}
    assert p["blast_radius"] and p["proposed"]["change"]["id"] == "CHG-1" and p["proposed"]["risk"]["factors"]
    res = out["result"]
    assert res["graph_id"] == "change_gate" and res["status"] == "ok" and res["metrics"]["applied"] == 1.0
    assert [e["type"] for e in events if e["type"] != "approval.requested"] == ["approval.decided", "action.executed"]
    req = events_of(events, "approval.requested")[0]
    assert (
        req["payload"]["thread_id"] == "gate-1"
        and req["payload"]["approval_id"] == "apr_CHG-1_0"
        and req["payload"]["kind"] == "approval"
    )
    assert req["idempotency_key"] == "approval.requested:apr_CHG-1_0"
    dec = events_of(events, "approval.decided")[0]["payload"]
    assert dec["decision"] == "approve" and dec["decided_by"] == "j.okafor" and dec["thread_id"] == "gate-1"
    assert events_of(events, "action.executed")[0]["idempotency_key"] == "action.executed:change:CHG-1"
    assert (await store.aget(("effects",), "change:CHG-1")) is not None  # the effect claim


async def test_resume_values_are_validated_against_the_decision_schema(graph):
    cfg = {"configurable": {"thread_id": "v-1"}}
    first = await graph.ainvoke({"change": CHANGE}, cfg, version="v2")
    assert first.interrupts
    with pytest.raises(ValidationError):
        await graph.ainvoke(Command(resume={"decision": "maybe"}), cfg)
    again = await graph.ainvoke(Command(resume=APPROVE), cfg, version="v2")  # the interrupt stayed answerable
    assert not again.interrupts and again.value["status"] == "applied"


async def test_low_risk_changes_are_auto_approved_with_no_interrupt(graph):
    change = {
        "id": "CHG-2",
        "kind": "flag",
        "component": "recommendation-service",
        "description": "flip a flag",
        "risk": "low",
        "requested_by": "a.dev",
        "environment": "staging",
    }
    with lifecycle.capture() as events:
        out, seen = await drive(graph, {"change": change}, [])
    assert not seen and out["status"] == "applied" and out["result"]["metrics"]["human_decisions"] == 0
    assert out["result"]["artifacts"][3]["data"][0]["decision"] == "auto_approve"
    assert [e["type"] for e in events] == ["action.executed"]


async def test_strict_policy_asks_even_for_low_risk(graph):
    change = {
        "id": "CHG-2",
        "kind": "flag",
        "component": "recommendation-service",
        "description": "flip a flag",
        "risk": "low",
        "requested_by": "a.dev",
        "environment": "staging",
    }
    _, seen = await drive(graph, {"change": change}, [APPROVE], context={"approval_policy": "strict"})
    assert kinds(seen) == ["approval"]


async def test_two_approver_changes_need_two_different_approvers_in_turn(graph):
    change = {**CHANGE, "id": "CHG-3", "diff": {}, "risk": "high"}  # no rollback plan -> clarify first
    out, seen = await drive(
        graph,
        {"change": change},
        [
            {"answer": "redeploy the previous image"},
            {"decision": "approve", "decided_by": "j.okafor"},
            {"decision": "approve", "decided_by": "j.okafor"},  # same person again: refused
            {"decision": "approve", "decided_by": "s.whitfield"},
        ],
    )
    assert kinds(seen) == ["question", "approval", "approval", "approval"] and out["status"] == "applied"
    titles = [b[0].value["title"] for b in seen]
    assert "approver 1 of 2" in titles[1] and "approver 2 of 2" in titles[2]
    assert "already approved" in seen[3][0].value["summary"]
    assert out["result"]["metrics"]["human_decisions"] == 4


# =========================================================================================== clarify (question)
async def test_high_risk_without_a_rollback_plan_asks_a_question_and_the_answer_lowers_the_risk(graph):
    change = {**CHANGE, "id": "CHG-4", "diff": {}}
    out, seen = await drive(
        graph, {"change": change}, [{"answer": "redeploy 2026.11.04.9", "decided_by": "a.dev"}, APPROVE]
    )
    q = seen[0][0].value
    assert (
        q["kind"] == "question"
        and q["options"] == ["answer"]
        and q["approval_id"] == "qst_CHG-4_0"
        and "rollback" in q["title"].lower()
    )
    assert out["change"]["diff"]["rollback_plan"] == "redeploy 2026.11.04.9"
    assert out["result"]["artifacts"][1]["data"]["rollback_plan"] is True and seen[1][0].value["risk"] == "medium"


# =========================================================================================== edit
async def test_edit_with_inline_edits_reassesses_and_comes_back_for_approval(graph):
    edit = {
        "decision": "edit",
        "decided_by": "j.okafor",
        "edits": {"description": "Deploy canary", "diff": {"rollout": "canary 10%"}, "bogus": 1, "risk": "nonsense"},
    }
    out, seen = await drive(graph, {"change": CHANGE}, [edit, APPROVE])
    assert kinds(seen) == ["approval", "approval"] and out["status"] == "applied"
    second = seen[1][0].value
    assert second["approval_id"] == "apr_CHG-1_1" and second["proposed"]["edits"] == 1
    assert (
        second["proposed"]["risk"]["score"] == seen[0][0].value["proposed"]["risk"]["score"] - 10
    )  # re-assessed: canary
    assert out["change"]["description"] == "Deploy canary" and out["change"]["diff"]["rollout"] == "canary 10%"
    assert out["change"]["diff"]["rollback_plan"] == "redeploy 2026.11.04.9"  # diff edits merge
    log = out["result"]["artifacts"][5]["data"][0]
    assert (
        log["by"] == "j.okafor"
        and log["applied"] == ["description", "diff"]
        and sorted(log["ignored"]) == ["bogus", "risk"]
    )
    assert out["result"]["artifacts"][0]["data"]["original"]["description"] == CHANGE["description"]


async def test_edit_without_edits_opens_an_edit_interrupt_that_asks_for_them(graph):
    out, seen = await drive(
        graph,
        {"change": CHANGE},
        [
            {"decision": "edit", "decided_by": "j.okafor"},
            {"decision": "edit", "decided_by": "j.okafor", "edits": {"environment": "staging"}},
            APPROVE,
        ],
    )
    assert kinds(seen) == ["approval", "edit", "approval"]
    e = seen[1][0].value
    assert (
        e["approval_id"] == "edt_CHG-1_1"
        and e["options"] == ["edit", "reject"]
        and e["proposed"]["editable"] == ["description", "risk", "environment", "diff"]
    )
    assert out["change"]["environment"] == "staging" and out["status"] == "applied"


async def test_cancelling_the_edit_interrupt_changes_nothing(graph):
    out, seen = await drive(
        graph, {"change": CHANGE}, [{"decision": "edit"}, {"decision": "reject", "note": "never mind"}, APPROVE]
    )
    assert kinds(seen) == ["approval", "edit", "approval"] and out["change"] == CHANGE | {"id": "CHG-1"}


async def test_edit_budget_is_bounded(graph):
    edits = [{"decision": "edit", "edits": {"description": f"v{i}"}} for i in range(4)]
    out, seen = await drive(graph, {"change": CHANGE}, [*edits, APPROVE], context={"max_edits": 2})
    last = seen[3][0].value
    assert "edit budget (2) used" in last["summary"] and out["status"] == "applied"
    assert out["result"]["metrics"]["edits"] == 2.0


# =========================================================================================== reject / revise
async def test_reject_revises_with_a_canary_and_a_lower_declared_risk_then_asks_again(graph):
    out, seen = await drive(
        graph,
        {"change": CHANGE},
        [{"decision": "reject", "note": "too big a blast radius", "decided_by": "j.okafor"}, APPROVE],
    )
    assert kinds(seen) == ["approval", "approval"] and out["status"] == "applied"
    revised = seen[1][0].value["proposed"]
    assert (
        revised["change"]["diff"]["rollout"] == "canary 10%"
        and revised["change"]["diff"]["revision_note"] == "too big a blast radius"
    )
    assert (
        revised["change"]["risk"] == "low"
        and "[revision 1" in revised["change"]["description"]
        and revised["revisions"] == 1
    )
    assert "revision 1" in seen[1][0].value["summary"]
    assert out["result"]["metrics"]["revisions"] == 1.0


async def test_reject_loop_is_bounded_and_the_final_rejection_applies_nothing(graph, store):
    rej = {"decision": "reject", "note": "no", "decided_by": "j.okafor"}
    with lifecycle.capture() as events:
        out, seen = await drive(graph, {"change": CHANGE}, [rej, rej, rej])
    assert len(seen) == 3 and out["status"] == "rejected" and out["result"]["status"] == "skipped"
    assert "rejected 3 times" in out["result"]["summary"]
    assert not events_of(events, "action.executed") and (await store.aget(("effects",), "change:CHG-1")) is None
    assert len(events_of(events, "approval.decided")) == 3


async def test_the_revision_bound_is_configurable(graph):
    rej = {"decision": "reject", "note": "no"}
    out, seen = await drive(graph, {"change": CHANGE}, [rej], context={"max_revisions": 0})
    assert len(seen) == 1 and out["status"] == "rejected"


# =========================================================================================== ask
async def test_ask_is_answered_by_the_smart_model_grounded_in_tool_results_then_the_card_returns(graph):
    ask = {"decision": "ask", "question": "What is the blast radius if this fails?", "decided_by": "j.okafor"}
    out, seen = await drive(graph, {"change": CHANGE}, [ask, APPROVE])
    assert kinds(seen) == ["approval", "approval"] and out["status"] == "applied"
    qa = seen[1][0].value["proposed"]["qa"]
    assert (
        len(qa) == 1
        and qa[0]["question"] == "What is the blast radius if this fails?"
        and qa[0]["asked_by"] == "j.okafor"
    )
    blast = tools_api.blast_radius({"scenario_id": "S01"}, "checkout-service")
    answer = qa[0]["answer"]
    assert (
        f"{blast['total_affected_if_down']} dependent component(s) are affected" in answer
    )  # numbers come from the tool
    assert "tier-0: " + ", ".join(blast["tier0_exposed"]) in answer and "revenue path is exposed" in answer
    books = tools_api.search_runbooks("deploy checkout-service What is the blast radius if this fails?", k=2)["hits"]
    assert books[0]["id"] in answer
    assert qa[0]["sources"] == ["blast_radius", "recall_incidents", "search_runbooks"]
    assert "Rollback plan on record: redeploy 2026.11.04.9" in answer
    assert "answer to your question" in seen[1][0].value["summary"]


async def test_the_answerer_is_a_smart_model_call_inside_the_node_and_streams_tokens(graph):
    script = o.Script()
    o.SCRIPT_OVERRIDES["smart"] = script
    ask = {"decision": "ask", "question": "who approves?"}
    cfg = {"configurable": {"thread_id": "tok-1"}}
    await graph.ainvoke({"change": CHANGE}, cfg)
    toks = []
    async for chunk, meta in graph.astream(Command(resume=ask), cfg, stream_mode="messages"):
        toks.append((meta["langgraph_node"], chunk.content))
    assert script.calls and script.calls[0].node == "answer_question" and script.calls[0].role == "smart"
    assert {n for n, _ in toks} == {"answer_question"} and "".join(c for _, c in toks)
    assert any(r.name == "blast_radius" for r in script.calls[0].tool_results)  # the grounding tools ran first


@pytest.mark.parametrize(
    ("profile", "needle"),
    [
        ("competent", "dependent component(s) are affected"),
        ("naive", "I would approve it"),
        ("cautious", "canary rollout"),
    ],
)
async def test_answers_differ_by_model_profile(graph, profile, needle):
    out, seen = await drive(
        graph,
        {"change": CHANGE},
        [{"decision": "ask", "question": "is it safe?"}, APPROVE],
        context={"model_profile": profile},
    )
    assert needle in seen[1][0].value["proposed"]["qa"][0]["answer"]


async def test_the_question_budget_is_bounded_and_ask_disappears_from_the_options(graph):
    ask = {"decision": "ask", "question": "again?"}
    out, seen = await drive(graph, {"change": CHANGE}, [ask, ask, APPROVE], context={"max_questions": 1})
    assert (
        seen[1][0].value["options"] == ["approve", "edit", "reject"]
        and "question budget used" in seen[2][0].value["summary"]
    )
    assert len((await state_of(graph))["qa"]) == 1


async def test_an_unreachable_grounding_tool_set_is_admitted_not_invented(graph, monkeypatch):
    o.SCRIPT_OVERRIDES["smart"] = o.Script().when("x", lambda c: True, "no tools were available")
    out, seen = await drive(graph, {"change": CHANGE}, [{"decision": "ask", "question": "x"}, APPROVE])
    assert seen[1][0].value["proposed"]["qa"][0]["answer"] == "no tools were available"


# =========================================================================================== four-eyes
async def test_critical_change_needs_two_parallel_reviewers_resumed_with_an_id_map(graph):
    resumes = [
        {"answer": "restore from the pre-migration snapshot"},
        APPROVE,
        by_role(
            {
                "service_oncall": {"decision": "approve", "decided_by": "j.okafor"},
                "dba": {"decision": "approve", "decided_by": "p.raman"},
            }
        ),
    ]
    with lifecycle.capture() as events:
        out, seen = await drive(graph, {"change": CRITICAL}, resumes)
    assert kinds(seen) == ["question", "approval", "four_eyes", "four_eyes"] and out["status"] == "applied"
    pair = seen[2]
    assert len(pair) == 2 and {i.value["policy"]["roles"][0] for i in pair} == {"service_oncall", "dba"}
    assert len({i.id for i in pair}) == 2 and all(len(i.id) == 32 for i in pair)  # distinct 32-hex ids, pending at once
    assert {i.value["approval_id"] for i in pair} == {"fe_CHG-9_2_service_oncall", "fe_CHG-9_2_dba"}
    assert all(
        i.value["options"] == ["approve", "reject"] and i.value["policy"]["approvers_required"] == 2 for i in pair
    )
    reviews = out["result"]["artifacts"][6]["data"]
    assert sorted(r["decided_by"] for r in reviews) == ["j.okafor", "p.raman"]
    decided = [e["payload"]["approval_id"] for e in events_of(events, "approval.decided")]
    assert "fe_CHG-9_2_dba" in decided and "fe_CHG-9_2_service_oncall" in decided


async def test_a_bare_value_with_two_pending_interrupts_is_refused_and_a_partial_map_leaves_one_pending(graph):
    cfg = {"configurable": {"thread_id": "fe-2"}}
    change = {**CRITICAL, "diff": {"rollback_plan": "snapshot"}}
    first = await graph.ainvoke({"change": change}, cfg, version="v2")
    out = await graph.ainvoke(Command(resume=APPROVE), cfg, version="v2")  # owner approval -> the pair
    assert len(out.interrupts) == 2
    with pytest.raises(RuntimeError):
        await graph.ainvoke(Command(resume=APPROVE), cfg)  # 02:15 - a bare value is ambiguous with 2 pending
    a, b = out.interrupts
    half = await graph.ainvoke(
        Command(resume={a.id: {"decision": "approve", "decided_by": "j.okafor"}}), cfg, version="v2"
    )
    assert [i.id for i in half.interrupts] == [b.id]  # reviewer B is still pending; the fan-in has not run
    assert (await graph.aget_state(cfg)).values.get("status") == "pending"
    done = await graph.ainvoke(
        Command(resume={b.id: {"decision": "approve", "decided_by": "p.raman"}}), cfg, version="v2"
    )
    assert not done.interrupts and done.value["status"] == "applied" and first.interrupts


async def test_one_reviewer_rejecting_sends_the_change_back_for_revision(graph):
    change = {**CRITICAL, "diff": {"rollback_plan": "snapshot"}}
    fe = by_role(
        {
            "service_oncall": APPROVE,
            "dba": {"decision": "reject", "decided_by": "p.raman", "note": "needs CONCURRENTLY"},
        }
    )
    out, seen = await drive(
        graph,
        {"change": change},
        [
            APPROVE,
            fe,
            APPROVE,
            by_role({"service_oncall": APPROVE, "dba": {"decision": "approve", "decided_by": "p.raman"}}),
        ],
    )
    assert (
        kinds(seen) == ["approval", "four_eyes", "four_eyes", "approval", "four_eyes", "four_eyes"]
        and out["status"] == "applied"
    )
    assert out["result"]["metrics"]["revisions"] == 1.0
    assert (
        "revision 1: dba: reject needs CONCURRENTLY" in seen[2][0].value["summary"]
    )  # the dba's note travels back to the owner
    assert seen[2][0].value["proposed"]["change"]["diff"]["rollout"] == "canary 10%"


async def test_the_same_person_cannot_be_both_four_eyes(graph):
    change = {**CRITICAL, "diff": {"rollback_plan": "snapshot"}}
    same = by_role(
        {
            "service_oncall": {"decision": "approve", "decided_by": "j.okafor"},
            "dba": {"decision": "approve", "decided_by": "j.okafor"},
        }
    )
    out, seen = await drive(
        graph, {"change": change}, [APPROVE, same] + [APPROVE, same] * 2, context={"max_revisions": 2}
    )
    assert out["status"] == "rejected" and "rejected 3 times" in out["result"]["summary"]
    assert "DIFFERENT reviewers" in out["result"]["summary"] and kinds(seen).count("four_eyes") == 6


# =========================================================================================== gauntlet
async def test_gauntlet_walks_through_every_interrupt_style_in_one_run(graph, store):
    resumes = [
        {"answer": "redeploy the previous image", "decided_by": "a.dev"},  # question
        APPROVE,  # too early: the challenges are not done
        {"decision": "ask", "question": "what is the blast radius?", "decided_by": "j.okafor"},  # ask
        {"decision": "edit", "decided_by": "j.okafor"},  # edit -> edit interrupt
        {"decision": "edit", "decided_by": "j.okafor", "edits": {"diff": {"rollout": "canary 10%"}}},
        {"decision": "reject", "note": "needs a canary", "decided_by": "j.okafor"},  # reject -> revise
        APPROVE,
        by_role(
            {
                "service_oncall": {"decision": "approve", "decided_by": "j.okafor"},
                "incident_commander": {"decision": "approve", "decided_by": "s.whitfield"},
            }
        ),
    ]
    with lifecycle.capture() as events:
        out, seen = await drive(graph, {"change": CHANGE, "mode": "gauntlet"}, resumes)
    flat = kinds(seen)
    assert flat == [
        "question",
        "approval",
        "approval",
        "approval",
        "edit",
        "approval",
        "approval",
        "four_eyes",
        "four_eyes",
    ]
    assert {"question", "approval", "edit", "four_eyes"} <= set(flat) and flat.count("four_eyes") == 2
    assert "Gauntlet: not yet - still to complete: ask, edit, reject" in seen[2][0].value["summary"]
    st = await state_of(graph)
    assert out["status"] == "applied" and st["risk"]["tier"] == "critical"
    m = out["result"]["metrics"]
    assert (m["edits"], m["revisions"], m["questions"]) == (1.0, 1.0, 1.0)
    assert st["remaining"] == [] and len(st["qa"]) == 1
    # every request has a decision, with the thread id on the request
    requested = {e["payload"]["approval_id"] for e in events_of(events, "approval.requested")}
    decided = {e["payload"]["approval_id"] for e in events_of(events, "approval.decided")}
    assert requested == decided and all(
        e["payload"]["thread_id"] == "gate-1" for e in events_of(events, "approval.requested")
    )
    assert {e["payload"]["kind"] for e in events_of(events, "approval.requested")} == {
        "question",
        "approval",
        "edit",
        "four_eyes",
    }
    assert len(events_of(events, "action.executed")) == 1


# =========================================================================================== idempotency / double resume
async def test_double_resume_applies_the_change_exactly_once(graph, store):
    cfg = {"configurable": {"thread_id": "dbl-1"}}
    await graph.ainvoke({"change": CHANGE}, cfg)
    with lifecycle.capture() as events:
        first = await graph.ainvoke(Command(resume=APPROVE), cfg)
        second = await graph.ainvoke(Command(resume=APPROVE), cfg)  # the user double-clicks
    assert first["status"] == "applied" and second["status"] == "applied"
    assert len(events_of(events, "action.executed")) == 1
    assert len(await store.asearch(("effects",), limit=10)) == 1


async def test_a_second_thread_for_the_same_change_id_is_a_duplicate_that_does_nothing(graph):
    with lifecycle.capture() as events:
        a, _ = await drive(graph, {"change": CHANGE}, [APPROVE], thread_id="t-a")
        b, _ = await drive(graph, {"change": CHANGE}, [APPROVE], thread_id="t-b")
    assert a["status"] == "applied" and b["status"] == "duplicate" and b["result"]["status"] == "duplicate"
    assert len(events_of(events, "action.executed")) == 1
    assert (await state_of(graph, "t-b"))["applied"]["outcome"] == "already_applied"


async def test_without_a_store_every_run_applies(store):
    g = build_graph(checkpointer=InMemorySaver(), store=None)
    out, _ = await drive(g, {"change": CHANGE}, [APPROVE])
    assert out["status"] == "applied"


async def test_static_breakpoint_pauses_before_the_side_effect_and_continue_resumes_it():
    g = build_graph(checkpointer=InMemorySaver(), store=InMemoryStore(), breakpoint=True)
    cfg = {"configurable": {"thread_id": "bp-1"}}
    await g.ainvoke({"change": CHANGE}, cfg)
    with lifecycle.capture() as events:
        paused = await g.ainvoke(Command(resume=APPROVE), cfg)
        st = await g.aget_state(cfg)
        assert st.next == ("apply_change",) and not events_of(events, "action.executed")  # approved, not yet applied
        assert "status" not in paused or paused.get("status") != "applied"
        done = await g.ainvoke(None, cfg)  # Studio's "Continue"
    assert done["status"] == "applied" and len(events_of(events, "action.executed")) == 1


async def test_per_run_interrupt_before_works_on_the_default_graph(graph):
    cfg = {"configurable": {"thread_id": "bp-2"}}
    await graph.ainvoke({"change": CHANGE}, cfg)
    await graph.ainvoke(Command(resume=APPROVE), cfg, interrupt_before=["apply_change"])
    assert (await graph.aget_state(cfg)).next == ("apply_change",)
    assert (await graph.ainvoke(None, cfg))["status"] == "applied"


# =========================================================================================== policy gate paths
FREEZE = "2026-11-05T14:00:00Z"


async def test_inside_the_freeze_tier_0_changes_need_two_approvers_and_a_rollback_is_exempt(graph):
    out, seen = await drive(
        graph,
        {"change": CHANGE, "requested_at": FREEZE},
        [APPROVE, {"decision": "approve", "decided_by": "s.whitfield"}],
    )
    policy = (await state_of(graph))["policy"]
    assert kinds(seen) == ["approval", "approval"] and policy["freeze"] is True
    checks = {c["id"]: c for c in policy["checks"]}
    assert checks["POL-003"]["status"] == "warn" and policy["approvers_required"] == 2
    rollback = {**CHANGE, "id": "CHG-5", "description": "Rollback checkout to 2026.11.04.9"}
    out2, _ = await drive(
        graph,
        {"change": rollback, "requested_at": FREEZE},
        [APPROVE, {"decision": "approve", "decided_by": "s.whitfield"}],
        thread_id="rb",
    )
    assert {c["id"]: c for c in (await state_of(graph, "rb"))["policy"]["checks"]}["POL-003"]["status"] == "pass"


async def test_break_glass_is_required_for_a_tier_0_datastore_migration_in_the_freeze(graph):
    change = {**CRITICAL, "diff": {"rollback_plan": "snapshot"}, "risk": "medium"}
    short = {"decision": "approve", "decided_by": "j.okafor", "note": "urgent"}
    out, seen = await drive(graph, {"change": change, "requested_at": FREEZE}, [short])
    assert (
        kinds(seen) == ["break_glass"] and out["status"] == "blocked" and "reason too short" in out["result"]["summary"]
    )
    ok = {
        "decision": "approve",
        "decided_by": "j.okafor",
        "note": "customer-facing schema bug, approved by the incident commander",
    }
    out2, seen2 = await drive(
        graph,
        {"change": change, "requested_at": FREEZE},
        [ok, APPROVE, by_role({"service_oncall": APPROVE, "dba": {"decision": "approve", "decided_by": "p.raman"}})],
        thread_id="bg-2",
    )
    assert kinds(seen2)[0] == "break_glass" and out2["status"] == "applied"
    bg = seen2[0][0].value
    assert bg["kind"] == "break_glass" and bg["options"] == ["approve", "reject"] and bg["reversible"] is False
    refused, _ = await drive(
        graph, {"change": change, "requested_at": FREEZE}, [{"decision": "reject"}], thread_id="bg-3"
    )
    assert refused["status"] == "blocked" and "refused" in refused["result"]["summary"]


async def test_an_unobservable_component_is_blocked_without_asking_anyone(graph):
    out, seen = await drive(graph, {"change": {**CHANGE, "component": "mystery-service"}}, [])
    assert (
        not seen
        and out["status"] == "blocked"
        and "POL-006" in out["result"]["summary"]
        and out["result"]["status"] == "skipped"
    )


# =========================================================================================== streaming / shapes
async def test_streaming_shows_policy_checks_decisions_and_the_pending_interrupt(graph):
    custom, updates, pending = [], [], []
    cfg = {"configurable": {"thread_id": "str-1"}}
    async for ns, mode, chunk in graph.astream(
        {"change": CHANGE}, cfg, stream_mode=["updates", "custom"], subgraphs=True
    ):
        (custom if mode == "custom" else updates).append(chunk)
    pending = [c["__interrupt__"] for c in updates if "__interrupt__" in c]
    assert pending and pending[0][0].value["kind"] == "approval"
    async for _, mode, chunk in graph.astream(
        Command(resume=APPROVE), cfg, stream_mode=["updates", "custom"], subgraphs=True
    ):
        if mode == "custom":
            custom.append(chunk)
    kinds_seen = {c["kind"] for c in custom}
    assert {"metric", "policy.check", "note", "decision"} <= kinds_seen
    assert {c["data"]["rule"] for c in custom if c["kind"] == "policy.check"} == {"POL-003", "POL-005", "POL-006"}
    keys = [c["key"] for c in custom]
    assert ("decision" in kinds_seen) and any(k.startswith("decided:apr_CHG-1_0") for k in keys)


def test_context_schema_graph_shape_and_input_schema():
    g = build_graph()
    props = g.get_context_jsonschema()["properties"]
    for key in (
        "model_profile",
        "approval_policy",
        "scenario_id",
        "as_of",
        "token_delay_ms",
        "breakpoints",
        "memory_enabled",
        "chaos",
        "max_questions",
        "max_edits",
        "max_revisions",
    ):
        assert key in props
    assert (
        props["answer_prompt"]["langgraph_nodes"] == ["answer_question"]
        and props["answer_prompt"]["langgraph_type"] == "prompt"
    )
    inp = g.get_input_jsonschema()["properties"]
    assert {"change", "mode", "requested_at", "scenario_id", "incident_id"} <= set(inp)
    assert "result" in g.get_output_jsonschema()["properties"] and g.name == "change_gate"
    nodes = set(g.get_graph().nodes)
    assert {
        "assess_risk",
        "policy_gate",
        "clarify",
        "break_glass",
        "request_approval",
        "edit_change",
        "revise_change",
        "answer_question",
        "four_eyes",
        "reviewer",
        "collect_reviews",
        "apply_change",
        "finish",
    } <= nodes


async def test_fork_before_the_approval_card_edits_the_change_and_replay_past_apply_is_a_duplicate():
    """Fork at the input checkpoint (successor = a static edge), because Command(goto) nodes have none (QA finding)."""
    g = build_graph(checkpointer=InMemorySaver(), store=InMemoryStore(), breakpoint=True)
    cfg = {"configurable": {"thread_id": "fk-1"}}
    await g.ainvoke({"change": CHANGE}, cfg)
    await g.ainvoke(Command(resume=APPROVE), cfg)  # paused before apply_change
    history = [s async for s in g.aget_state_history(cfg)]
    # NOT at ("request_approval",): its writer (policy_gate) has no static edge, so a fork there has no successor
    first = next(s for s in history if s.next == ("assess_risk",))
    edited = {
        **CHANGE,
        "description": "Deploy checkout (forked: smaller batch)",
        "diff": {"rollback_plan": "x", "rollout": "canary 5%"},
    }
    fork = await g.aupdate_state(first.config, {"change": edited}, as_node="__start__")
    again = await g.ainvoke(None, fork, version="v2")  # the card is shown again, now for the edited change
    card = again.interrupts[0].value
    assert card["kind"] == "approval" and card["proposed"]["change"]["description"].startswith(
        "Deploy checkout (forked"
    )
    await g.ainvoke(
        Command(resume=APPROVE), cfg
    )  # the thread now follows the fork: approve -> paused at the breakpoint
    done = await g.ainvoke(None, cfg)
    assert done["status"] == "applied" and done["change"]["diff"]["rollout"] == "canary 5%"
    # the original branch is still waiting at its breakpoint; applying it now is a no-op duplicate
    original = next(s for s in history if s.next == ("apply_change",))
    dup = await g.ainvoke(None, original.config)
    assert dup["status"] == "duplicate"


async def test_with_a_scenario_the_answer_is_grounded_in_the_scenarios_telemetry_too(graph):
    ask = {"decision": "ask", "question": "what else changed recently?"}
    change = {**CHANGE, "id": "CHG-6"}
    out, seen = await drive(
        graph,
        {"change": change, "scenario_id": "S01", "requested_at": "2026-11-05T14:00:00Z"},
        [ask, APPROVE, {"decision": "approve", "decided_by": "s.whitfield"}],
    )
    answer = seen[1][0].value["proposed"]["qa"][0]
    changes = tools_api.list_changes({"scenario_id": "S01", "as_of": "2026-11-05T14:00:00Z"}, window_minutes=240)
    assert f"{changes['count']} other change(s) landed in the last 240 minutes" in answer["answer"]
    assert "list_changes" in answer["sources"] and out["status"] == "applied"  # inside the freeze: two approvers
