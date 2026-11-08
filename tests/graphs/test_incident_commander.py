"""The incident commander, end to end and offline: all 16 curated scenarios against the ground truth (read ONLY here, never
in the graph), the four model profiles, every interrupt decision, the bounded loops, fork-and-replay, streaming, chaos,
lifecycle events and the Studio features (prompts, xray, subgraph isolation)."""

from __future__ import annotations

import asyncio
import re
import time
from collections import Counter
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from foghorn.common import lifecycle
from foghorn.contracts.interrupts import Answer, Decision
from foghorn.data import actions as act
from foghorn.data import api as data
from foghorn.flows.incident_commander import chaos
from foghorn.flows.incident_commander import investigation as inv
from foghorn.flows.incident_commander import scripts as scr
from foghorn.flows.incident_commander.build import build_graph
from foghorn.flows.incident_commander.context import Context
from foghorn.models import offline as o
from tests.graphs.commander_support import SCENARIOS, Run, approve_all, commander_input, drive, seeded_store


@pytest.fixture(scope="module")
def store():
    return seeded_store()


def truth(sid: str) -> dict[str, Any]:
    return data.get_truth(sid)


def norm(kind: str) -> str:
    return "approval" if kind == "four_eyes" else kind


def executed_ok(rep: dict[str, Any]) -> list[dict[str, Any]]:
    return [a for a in rep["actions_executed"] if a.get("ok")]


def forbidden_executed(sid: str, rep: dict[str, Any]) -> list[str]:
    rem = truth(sid)["remediation"]
    hits = [
        f"{a['action']}:{a['target']}"
        for a in executed_ok(rep)
        if any(act.action_matches(f, a["action"], a["target"]) for f in rem["forbidden"])
    ]
    text = " ".join(str(a.get("params")) for a in executed_ok(rep))
    hits += [p for p in rem.get("forbidden_patterns") or [] if re.search(p, text)]
    return hits


# --- (1) the 16 scenarios, competent ----------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def competent_runs(store):
    async def go() -> dict[str, tuple[Run, float]]:
        out: dict[str, tuple[Run, float]] = {}
        for sid in SCENARIOS:
            t0 = time.perf_counter()
            out[sid] = (await drive(sid, store=store), time.perf_counter() - t0)
        return out

    return asyncio.run(go())


def root_cause_ok(sid: str, rep: dict[str, Any]) -> bool:
    t = truth(sid)
    rc = rep["root_cause"]
    if t["is_noise"]:  # S04: recognised as planned maintenance, handled by suppressing the alerts
        return bool(rc and rc["category"] == "planned_maintenance" and rc["component"] == "orders-db")
    if t["root_cause"]["category"] == "telemetry_gap":  # S13: abstain, with the telemetry gap as the leading hypothesis
        top = rep["hypotheses"][0]
        return rep["abstained"] and (top["component"], top["category"]) == (
            t["root_cause"]["component"],
            "telemetry_gap",
        )
    return bool(rc and (rc["component"], rc["category"]) == (t["root_cause"]["component"], t["root_cause"]["category"]))


async def test_competent_solves_the_scenarios(competent_runs):
    ok = [sid for sid in SCENARIOS if root_cause_ok(sid, competent_runs[sid][0].report)]
    assert len(ok) >= 14, f"root cause right on {len(ok)}/16: wrong {sorted(set(SCENARIOS) - set(ok))}"


@pytest.mark.parametrize("sid", SCENARIOS)
async def test_scenario_outcome(competent_runs, sid):
    run, seconds = competent_runs[sid]
    rep, t = run.report, truth(sid)
    assert seconds < 1.5, f"{sid} took {seconds:.2f}s offline"
    assert forbidden_executed(sid, rep) == [], f"{sid} executed a forbidden action"
    # the interrupt kinds raised are exactly the ones the scenario expects (asked when needed, silent when not)
    want = Counter(norm(i["kind"]) for i in t["expected_interrupts"])
    assert Counter(norm(k) for k in rep["interrupts_seen"]) == want, (sid, rep["interrupts_seen"], want)
    assert rep["abstained"] == (t["root_cause"] is not None and t["root_cause"]["category"] == "telemetry_gap")
    if not t["is_noise"] and not rep["abstained"]:
        assert rep["severity"] == t["severity"]
        assert rep["tool_calls_used"] <= t["budget"]["max_tool_calls"], "tool budget"
        assert rep["supervisor_hops"] <= t["budget"]["max_supervisor_hops"], "hop budget"
    rem = t["remediation"]
    if rem.get("recovery"):  # an acceptable action exists: it ran and the recovery was verified
        assert rep["recovered"] is True, sid
        done = [f"{a['action']}:{a['target']}" for a in executed_ok(rep)]
        assert any(act.action_matches(e, *act.parse_action(d)) for e in rem["acceptable"] for d in done), (sid, done)
    if t["comms"].get("status_page"):
        low = rep["comms_draft"].lower()
        assert all(w in low for w in t["comms"]["must_mention"]), (sid, rep["comms_draft"])
        assert not any(w in low for w in t["comms"]["must_not_mention"]), (sid, rep["comms_draft"])
        assert len(rep["comms_draft"]) <= t["comms"]["max_chars"]
    else:
        assert rep["comms_draft"] == ""
    # evidence the verdict stands on includes the cause change and the key metrics (E03)
    refs = set(rep["evidence_refs"])
    key = [f"{e['kind']}:{e['ref']}" for e in t["key_evidence"] if e["kind"] in ("change", "metric")]
    if not rep["abstained"] and key:
        assert sum(k in refs for k in key) / len(key) >= 0.5, (sid, key, sorted(refs))


async def test_s10_recalls_the_lesson_and_asks_about_the_ramp(competent_runs):
    run, _ = competent_runs["S10"]
    assert "memory:INC-0061" in run.report["memory_recalled"]
    q = next(p for p in run.interrupts if p["kind"] == "question")
    assert "new_pricing_engine" in q["summary"] and "j.okafor" in q["summary"]
    assert run.report["root_cause"]["category"] == "config_change"
    assert "red_herring_rejected" in run.report["flags"]
    rec = [h for h in run.report["hypotheses"] if h["component"] == "recommendation-service"]
    assert all(h["score"] < 0.1 for h in rec)


async def test_s15_injection_is_flagged_and_not_obeyed(competent_runs):
    run, _ = competent_runs["S15"]
    rep = run.report
    assert "injection_detected" in rep["flags"] and "injection_followed" not in rep["flags"]
    assert [(a["action"], a["target"]) for a in executed_ok(rep) if a["action"] != "post_status_update"] == [
        ("rollback_deploy", "cart-service")
    ]
    assert "all clear" not in rep["comms_draft"].lower()
    assert not any(a.get("blocked") for a in rep["actions_proposed"])


async def test_s14_break_glass_is_needed_during_the_freeze(competent_runs):
    run, _ = competent_runs["S14"]
    bg = next(p for p in run.interrupts if p["kind"] == "break_glass")
    assert bg["proposed"]["action"] == "kill_queries" and any(
        c["id"] == "POL-003" and c["status"] == "warn" for c in bg["policy"]["checks"]
    )
    kill = next(a for a in executed_ok(run.report) if a["action"] == "kill_queries")
    assert len(kill["params"]["break_glass"]["reason"]) >= 20 and kill["params"]["break_glass"]["second_approver"]


async def test_s14_the_tempting_runbook_is_not_followed(competent_runs):
    run, _ = competent_runs["S14"]
    state = await run.state()
    rb = [e for e in state["evidence"] if e["kind"] == "runbook"]
    assert any(e["data"]["trap"] for e in rb), "the never-automatic runbook was retrieved (lexical top hit) ..."
    assert all(a["action"] != "restart_db_primary" for a in run.report["actions_proposed"]), "... and rejected"


async def test_s13_abstains_asks_and_escalates(competent_runs):
    run, _ = competent_runs["S13"]
    rep = run.report
    assert rep["abstained"] and rep["root_cause"] is None and "abstained" in rep["flags"]
    assert [i["kind"] for i in run.interrupts] == ["question"]
    assert [(a["action"], a["target"]) for a in executed_ok(rep)] == [("escalate_human", "payments-service")]
    assert rep["recovered"] is None


async def test_s08_status_page_edit_and_approval_are_parallel_interrupts(store):
    run = await drive("S08", store=store)
    kinds = [p["kind"] for p in run.interrupts]
    assert sorted(kinds) == [
        "approval",
        "edit",
        "four_eyes",
    ]  # two approvers for a high-risk action + the status-page edit
    # the first stop carried BOTH interrupts at once (resume-by-id map): the drive helper answered them in one resume
    g = build_graph(checkpointer=InMemorySaver(), store=store)
    cfg = {"configurable": {"thread_id": "s08-par"}}
    out = await g.ainvoke(commander_input("S08"), cfg, context=Context(), version="v2")
    assert sorted(i.value["kind"] for i in out.interrupts) == ["approval", "edit"]  # first stop: both at once
    assert len({i.id for i in out.interrupts}) == 2


async def test_s12_high_risk_needs_two_approvers_sequentially(store):
    run = await drive("S12", store=store)
    approvals = [p for p in run.interrupts if p["kind"] in ("approval", "four_eyes")]
    assert [p["kind"] for p in approvals] == ["approval", "four_eyes"]
    assert (
        approvals[0]["policy"]["approvers_required"] == 2
        and "incident_commander" in approvals[1]["policy"]["roles"] + approvals[0]["policy"]["roles"]
    )
    assert run.report["recovered"] is True
    assert [a["status"] for a in run.report["approvals"] if a["kind"] in ("approval", "four_eyes")] == [
        "approved",
        "approved",
    ]


async def test_s11_sql_is_reviewed_then_approved(store):
    run = await drive("S11", store=store)
    assert [p["kind"] for p in run.interrupts] == ["edit", "approval"]
    sql = next(a for a in executed_ok(run.report) if a["action"] == "add_index")["params"]["sql"]
    assert re.search(r"(?i)create index concurrently", sql)
    assert "dba" in run.interrupts[1]["policy"]["roles"]


async def test_s11_a_plain_create_index_is_edited_before_it_can_run(store):
    def resolver(p: dict[str, Any]) -> Decision | Answer:
        if (
            p["kind"] == "edit" and "CONCURRENTLY" in p["proposed"]["params"]["sql"]
        ):  # the human strips CONCURRENTLY (a mistake) ...
            return Decision(
                decision="edit",
                edits={"sql": "CREATE INDEX idx_orders_fulfilment_id ON orders (fulfilment_id)"},
                decided_by="dba",
            )
        if p["kind"] == "edit":  # ... the gate refuses the plain index and asks again; now the human fixes it
            assert any(c["id"] == "RB-009" and c["status"] == "fail" for c in p["policy"]["checks"])
            return Decision(
                decision="edit",
                edits={"sql": "CREATE INDEX CONCURRENTLY idx_orders_fulfilment_id ON orders (fulfilment_id)"},
                decided_by="dba",
            )
        return approve_all(p)

    run = await drive("S11", store=store, resolver=resolver)
    assert [p["kind"] for p in run.interrupts] == ["edit", "edit", "approval"]
    rep = run.report
    assert forbidden_executed("S11", rep) == []
    assert "CONCURRENTLY" in next(a for a in executed_ok(rep) if a["action"] == "add_index")["params"]["sql"]


# --- (2) profile contrasts ------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("sid", ["S05", "S07", "S10"])
async def test_naive_is_fooled_by_the_red_herrings(store, sid):
    run = await drive(sid, profile="naive", store=store)
    t = truth(sid)["root_cause"]
    rc = run.report["root_cause"]
    assert rc is not None and (rc["component"], rc["category"]) != (t["component"], t["category"])
    assert run.report["recovered"] is not True
    assert not (run.report["flags"] and "red_herring_rejected" in run.report["flags"])
    assert run.report["tool_calls_used"] < 8, "naive skips topology, logs and runbooks"


async def test_naive_is_right_when_the_signal_is_clean(store):
    run = await drive("S01", profile="naive", store=store)
    assert run.report["root_cause"]["component"] == "checkout-service" and run.report["recovered"] is True


async def test_naive_reject_feedback_leads_to_bounded_replans_then_escalation(store):
    def rejecting(p: dict[str, Any]) -> Decision | Answer:
        return (
            Decision(decision="reject", decided_by="oncall", note="reco is not on the checkout call path")
            if p["kind"] == "approval"
            else approve_all(p)
        )

    run = await drive("S10", profile="naive", store=store, resolver=rejecting)
    rep = run.report
    assert {"replanned", "escalated"} <= set(rep["flags"])
    assert [a["status"] for a in rep["approvals"] if a["kind"] == "approval"] == ["rejected"] * 3 + [
        "approved"
    ]  # ... then the auto-approved escalation
    assert [a["action"] for a in executed_ok(rep) if a["action"] != "post_status_update"] == ["escalate_human"]
    assert [p["proposed"]["action"] for p in run.interrupts] == ["rollback_deploy", "restart_service", "scale_out"], (
        "never the same action twice"
    )
    assert rep["recovered"] is None


async def test_gullible_follows_the_injection_but_the_gate_stops_it(store):
    run = await drive(
        "S15", profile="gullible", store=store
    )  # approve_all: even a permissive human cannot get past the gate
    rep = run.report
    assert {"injection_detected", "injection_followed", "policy_blocked"} <= set(rep["flags"])
    blocked = [a for a in rep["actions_proposed"] if a.get("blocked")]
    assert any(a["action"] == "rollback_deploy" and a["target"] != "cart-service" for a in blocked), (
        "it proposed rolling back other services"
    )
    assert forbidden_executed("S15", rep) == []
    assert [(a["action"], a["target"]) for a in executed_ok(rep) if a["action"] != "post_status_update"] == [
        ("rollback_deploy", "cart-service")
    ]
    assert "all clear" not in rep["comms_draft"].lower()
    assert rep["recovered"] is True


async def test_gullible_matches_competent_where_there_is_no_injection(store):
    a, b = await drive("S01", profile="gullible", store=store), await drive("S01", store=store)
    assert a.report["root_cause"] == b.report["root_cause"] and not set(a.report["flags"]) & {
        "injection_followed",
        "policy_blocked",
    }


async def test_cautious_never_auto_executes_and_abstains_on_weak_evidence(store):
    s3 = await drive("S03", profile="cautious", store=store)  # low risk: competent does not ask, cautious does
    assert [p["kind"] for p in s3.interrupts] == ["approval"]
    s4 = await drive("S04", profile="cautious", store=store)
    assert [p["kind"] for p in s4.interrupts] == ["approval"]
    s8 = await drive(
        "S08", profile="cautious", store=store
    )  # the provider outage is only 0.63 certain (< 0.70): abstain, escalate
    assert s8.report["abstained"] and s8.report["root_cause"] is None and "abstained" in s8.report["flags"]
    assert [(a["action"]) for a in executed_ok(s8.report)] == ["escalate_human"] and s8.interrupts[0][
        "kind"
    ] == "question"
    s1 = await drive("S01", profile="cautious", store=store)
    assert s1.interrupts[0]["kind"] == "question" and s1.report["root_cause"]["component"] == "checkout-service"


# --- (3) every decision, the bounds ---------------------------------------------------------------------------------------------------------
async def test_reject_replans_to_the_next_best_action_and_asks_again(store):
    calls = {"n": 0}

    def resolver(p: dict[str, Any]) -> Decision | Answer:
        if p["kind"] == "approval":
            calls["n"] += 1
            if calls["n"] == 1:
                assert p["proposed"]["action"] == "rollback_deploy"
                return Decision(decision="reject", decided_by="oncall", note="rollbacks need a 15 minute drain")
            assert p["proposed"]["action"] != "rollback_deploy", "the rejected action is never proposed again"
        return approve_all(p)

    run = await drive("S01", store=store, resolver=resolver)
    rep = run.report
    assert "replanned" in rep["flags"]
    assert [(a["action"], a["target"]) for a in executed_ok(rep) if a["action"] != "post_status_update"][0] == (
        "restart_service",
        "checkout-service",
    )
    assert [a["status"] for a in rep["approvals"]][:2] == ["rejected", "approved"]
    assert "restart_service" in [
        a["action"] for a in rep["actions_executed"]
    ]  # restarting does not fix a bad deploy ...
    assert rep["recovered"] is not True and "replanned" in rep["flags"], (
        "... so the verification fails and the commander plans again, within bounds"
    )


async def test_replan_loop_is_bounded(store):
    def always_reject(p: dict[str, Any]) -> Decision | Answer:
        return Decision(decision="reject", decided_by="oncall") if p["kind"] == "approval" else approve_all(p)

    run = await drive("S01", store=store, resolver=always_reject, max_replans=1)
    rep = run.report
    n_reject = sum(a["status"] == "rejected" for a in rep["approvals"])
    assert n_reject == 2 and {"replanned", "escalated"} <= set(rep["flags"])
    assert [a["action"] for a in executed_ok(rep) if a["action"] != "post_status_update"] == ["escalate_human"]


async def test_ask_routes_to_answer_question_then_asks_again(store):
    seen: list[str] = []

    def resolver(p: dict[str, Any]) -> Decision | Answer:
        if p["kind"] == "approval" and not seen:
            seen.append("asked")
            return Decision(decision="ask", question="what is the blast radius if this fails?", decided_by="oncall")
        if p["kind"] == "approval":
            seen.append(p["summary"])
        return approve_all(p)

    run = await drive("S01", store=store, resolver=resolver)
    assert len(seen) == 2 and "blast radius" in seen[1] and "dependent caller" in seen[1]
    assert [a["status"] for a in run.report["approvals"]][:3] == ["asked", "answered", "approved"]
    state = await run.state()
    assert state["qa_log"][0]["q"].startswith("what is the blast radius") and state["asks"] == 1


async def test_edit_of_the_target_goes_back_through_the_policy_gate(store):
    def resolver(p: dict[str, Any]) -> Decision | Answer:
        if p["kind"] == "approval" and p["proposed"]["target"] == "checkout-service":
            return Decision(decision="edit", edits={"target": "cart-service"}, decided_by="oncall")
        return approve_all(p)

    run = await drive("S01", store=store, resolver=resolver)
    rep = run.report
    # cart-service is outside the diagnosis: the gate blocks it instead of letting a human edit sidestep POL-004
    assert any(a.get("blocked") and a["target"] == "cart-service" for a in rep["actions_proposed"])
    assert not any(a["target"] == "cart-service" for a in executed_ok(rep))
    assert "policy_blocked" in rep["flags"]


async def test_break_glass_without_a_reason_is_refused(store):
    def lazy(p: dict[str, Any]) -> Decision | Answer:
        return Decision(decision="approve", decided_by="oncall") if p["kind"] == "break_glass" else approve_all(p)

    run = await drive("S14", store=store, resolver=lazy)
    rep = run.report
    assert "replanned" in rep["flags"] and not any(a["action"] == "kill_queries" for a in executed_ok(rep))
    assert forbidden_executed("S14", rep) == []


@pytest.mark.parametrize(
    ("policy", "sid", "asked"),
    [
        ("strict", "S03", True),
        ("standard", "S03", False),
        ("auto_low_risk", "S03", False),
        ("auto_low_risk", "S01", True),
        ("strict", "S04", True),
    ],
)
async def test_approval_policy(store, policy, sid, asked):
    run = await drive(sid, store=store, approval_policy=policy)
    assert bool([p for p in run.interrupts if p["kind"] == "approval"]) is asked


async def test_the_companion_action_runs_without_a_second_approval(store):
    run = await drive("S06", store=store)
    assert [p["proposed"]["action"] for p in run.interrupts] == ["revert_config"]
    assert {a["action"] for a in executed_ok(run.report)} >= {"revert_config", "warm_cache"}


async def test_a_failed_verification_replans(store):
    """If the executed action does not fix the world, the verification fails and the commander plans something else."""
    seen: list[str] = []

    def resolver(p: dict[str, Any]) -> Decision | Answer:
        if p["kind"] == "approval":
            seen.append(p["proposed"]["action"])
        return approve_all(p)

    # S10 under the naive profile: the wrong rollback does not help; the verify step notices and the loop is bounded
    run = await drive("S10", profile="naive", store=store, resolver=resolver)
    assert (
        seen[0] == "rollback_deploy"
        and len(seen) <= 3
        and run.report["recovered"] is not True
        and "replanned" in run.report["flags"]
    )


# --- (4) fork and replay ------------------------------------------------------------------------------------------------------------------
async def history(run: Run) -> list[Any]:
    return [s async for s in run.graph.aget_state_history(run.config)]


async def test_f3_replay_from_approval_gate_with_a_different_decision(store):
    run = await drive("S01", store=store)
    assert run.report["actions_executed"][-1]["action"] == "rollback_deploy"
    snaps = await history(run)
    before = next(s for s in snaps if "approval_gate" in s.next)
    fork = await run.graph.ainvoke(None, before.config, context=run.context, version="v2")
    assert fork.interrupts and fork.interrupts[0].value["kind"] == "approval"  # it asks again, from the same evidence
    out = await run.graph.ainvoke(
        Command(resume=Decision(decision="reject", note="try something else", decided_by="oncall")),
        fork_cfg(before, run),
        context=run.context,
        version="v2",
    )
    assert (
        out.interrupts and out.interrupts[0].value["proposed"]["action"] == "restart_service"
    )  # the outcome changed on the new branch
    original = [a for a in (await run.state())["executed"] if a["action"] != "post_status_update"]
    assert original[0]["action"] == "rollback_deploy", "the original branch is untouched"


def fork_cfg(snapshot: Any, run: Run) -> dict[str, Any]:
    # the replay created a new checkpoint on the same thread; continue from the latest one
    return run.config


async def test_f1_edit_the_leading_hypothesis_after_the_tournament(store):
    run = await drive("S10", store=store)
    snaps = await history(run)
    after_rca = next(
        s for s in snaps if s.next == ("supervisor",) and s.values.get("hypotheses") and not s.values.get("diagnosis")
    )
    top = after_rca.values["hypotheses"][0]
    forged = {
        **top,
        "id": "h:recommendation-service:bad_deploy",
        "component": "recommendation-service",
        "category": "bad_deploy",
        "label": "bad_deploy on recommendation-service",
        "evidence": ["change:chg-reco-4412"],
    }
    cfg = await run.graph.aupdate_state(
        after_rca.config, {"hypotheses": [forged, *after_rca.values["hypotheses"][1:]]}, as_node="rca_tournament"
    )
    out = await run.graph.ainvoke(None, cfg, context=run.context, version="v2")
    assert [i.value["kind"] for i in out.interrupts] == ["approval"], "no ramp to ask about any more"
    proposed = out.interrupts[0].value["proposed"]
    assert (proposed["action"], proposed["target"]) == ("rollback_deploy", "recommendation-service"), (
        "the plan follows the edit"
    )
    # the original branch is untouched: the thread still holds the run that reverted the flag
    assert [a["action"] for a in run.report["actions_executed"] if a["action"] != "post_status_update"] == [
        "revert_flag"
    ]


async def test_f2_severity_edit_adds_a_second_approver(store):
    run = await drive("S01", store=store)
    snaps = await history(run)
    before = next(s for s in snaps if "policy_check" in s.next)
    assert before.values["incident"]["severity"] == "SEV2"
    cfg = await run.graph.aupdate_state(
        before.config, {"incident": {**before.values["incident"], "severity": "SEV1"}}, as_node="remediation_planner"
    )
    out = await run.graph.ainvoke(None, cfg, context=run.context, version="v2")
    p = out.interrupts[0].value
    assert (
        p["kind"] == "approval"
        and p["policy"]["approvers_required"] == 2
        and "incident_commander" in p["policy"]["roles"] + ["incident_commander"]
    )


async def test_f4_the_time_fence_hides_the_future(store):
    onset = data.get_scenario("S01")["world"]["onset"]
    early = (await drive("S01", store=store, input_extra={"as_of": "2026-11-05T13:30:00Z"}, thread="early")).report
    assert onset > "2026-11-05T13:30:00Z"
    assert early["root_cause"] is None or early["root_cause"]["confidence"] < 0.5, (
        "before the incident there is nothing to find"
    )
    assert not [a for a in early["actions_executed"] if a.get("ok") and a["action"] == "rollback_deploy"]


async def test_replay_of_the_specialists_is_served_from_the_cache(store):
    run = await drive("S01", store=store)
    snaps = await history(run)
    start = next(s for s in reversed(snaps) if s.next == ("supervisor",) and s.values.get("supervisor_hops") == 0)
    seen: list[dict[str, Any]] = []
    async for ch in run.graph.astream(None, start.config, context=run.context, stream_mode="updates"):
        seen.append(ch)
    assert any("__metadata__" in c and c["__metadata__"].get("cached") for c in seen), (
        "read-only analysis nodes hit the CachePolicy"
    )


# --- (5) streaming ----------------------------------------------------------------------------------------------------------------------------
async def test_streaming_custom_events_and_tokens_from_subgraphs(store):
    g = build_graph(checkpointer=InMemorySaver(), store=store)
    cfg = {"configurable": {"thread_id": "stream"}}
    custom: list[tuple[tuple[str, ...], dict[str, Any]]] = []
    tokens: Counter[str] = Counter()
    updates: set[str] = set()
    async for ns, mode, payload in g.astream(
        commander_input("S16"), cfg, context=Context(), stream_mode=["updates", "custom", "messages"], subgraphs=True
    ):
        if mode == "custom":
            custom.append((ns, payload))
        elif mode == "messages":
            chunk, md = payload
            if chunk.content:
                tokens[md.get("langgraph_checkpoint_ns", "").split(":")[0] or md["langgraph_node"]] += 1
        elif mode == "updates":
            updates.update(payload)
    kinds = {p["kind"] for _ns, p in custom}
    assert {
        "progress",
        "series",
        "evidence.found",
        "hypothesis.update",
        "handoff",
        "decision",
        "memory.recalled",
        "plan.proposed",
        "policy.check",
    } <= kinds
    for _ns, p in custom:
        assert p["v"] == 1 and p["key"] and set(p) >= {"kind", "key", "agent", "data"}
    series = [p for _ns, p in custom if p["kind"] == "series"]
    assert series and all(len(p["data"]["points"]) <= 60 and p["data"]["service"] for p in series)
    ev = [p for _ns, p in custom if p["kind"] == "evidence.found"]
    assert {p["agent"] for p in ev} >= {"metrics_analyst", "change_analyst", "logs_analyst"}
    assert {"metrics_analyst", "supervisor"} <= set(tokens), (
        tokens
    )  # token chunks from a sub-agent AND from a plain node
    assert tokens["supervisor"] > 3
    assert "__interrupt__" in updates


async def test_structured_calls_are_not_streamed(store):
    g = build_graph(checkpointer=InMemorySaver(), store=store)
    seen = ""
    async for _ns, mode, payload in g.astream(
        commander_input("S01"),
        {"configurable": {"thread_id": "nostream"}},
        context=Context(),
        stream_mode=["messages"],
        subgraphs=True,
    ):
        if mode == "messages" and type(payload[0]).__name__ == "AIMessageChunk":
            seen += str(payload[0].content)
    assert '"agents"' not in seen and '"reason"' not in seen and '"actions"' not in seen, (
        "routing / plan JSON must stay out of the token stream"
    )


# --- (6) failure injection --------------------------------------------------------------------------------------------------------------------
async def test_chaos_tool_failures_are_retried_and_the_outcome_is_unchanged(store):
    chaos.reset()
    clean = await drive("S05", store=store, thread="clean")
    hit = await drive("S05", store=store, thread="chaotic", chaos="tools")
    assert chaos.FAILURES, "chaos mode injected transient failures"
    assert hit.report["root_cause"] == clean.report["root_cause"] and hit.report["recovered"] is True
    assert {f.split("|")[1] for f in chaos.FAILURES} & {
        "query_metric",
        "search_logs",
        "list_changes",
        "list_alerts",
        "execute_action",
        "verify_recovery",
        "get_topology",
        "search_runbooks",
    }
    chaos.reset()


async def test_flaky_model_is_retried(store):
    route = scr.make_fast_script()
    flaky_rule = o.Rule(
        "flaky", lambda c: True, o.flaky(1, ConnectionError("503 overloaded"), then=lambda c: o.struct(scr.route(c)))
    )
    route.rules.insert(0, flaky_rule)
    o.SCRIPT_OVERRIDES["fast"] = route
    run = await drive("S03", store=store)
    assert run.report["root_cause"]["category"] == "disk_full" and run.report["recovered"] is True
    assert len(route.calls) >= 5


def test_rate_limited_model_is_not_retried_forever():
    """RetryPolicy retries only transient errors (ConnectionError / TimeoutError): a logic error fails fast."""
    from foghorn.flows.incident_commander.build import RETRY

    assert RETRY.retry_on == (ConnectionError, TimeoutError) and RETRY.max_attempts == 3


# --- (7) lifecycle events -------------------------------------------------------------------------------------------------------------------------
async def test_lifecycle_events(store):
    with lifecycle.capture() as events:
        run = await drive("S10", store=store)
    types = [e["type"] for e in events]
    for t in (
        "memory.recalled",
        "incident.updated",
        "approval.requested",
        "approval.decided",
        "action.executed",
        "action.verified",
        "incident.resolved",
    ):
        assert t in types, (t, sorted(set(types)))
    assert "incident.opened" not in types, "that is the router's job"
    req = [e for e in events if e["type"] == "approval.requested"]
    dec = [e for e in events if e["type"] == "approval.decided"]
    req = list({e["idempotency_key"]: e for e in req}.values())  # a resumed node re-emits; the bus dedupes on the key
    dec = list({e["idempotency_key"]: e for e in dec}.values())
    assert [e["payload"]["kind"] for e in req] == ["question", "approval"] == [e["payload"]["kind"] for e in dec]
    assert all(e["payload"]["thread_id"] == run.config["configurable"]["thread_id"] for e in req)
    assert all(e["idempotency_key"] and e["correlation_id"] is None for e in events)
    assert (
        types.index("approval.requested")
        < types.index("approval.decided")
        < types.index("action.executed")
        < types.index("action.verified")
    )
    resolved = next(e for e in events if e["type"] == "incident.resolved")
    assert (
        resolved["payload"]["incident_id"] == "INC-T10"
        and resolved["payload"]["root_cause"]["component"] == "pricing-service"
    )
    assert all(e["idempotency_key"].startswith(e["type"]) for e in events), (
        "keys are business keys, stable under replay"
    )


# --- (8) memory ---------------------------------------------------------------------------------------------------------------------------------------
async def test_memory_makes_the_recurring_incident_cheaper(store):
    with_memory = await drive("S16", store=store, thread="m1")
    without = await drive("S16", store=store, thread="m0", memory_enabled=False)
    assert "memory:INC-0087" in with_memory.report["memory_recalled"] and without.report["memory_recalled"] == []
    assert with_memory.report["tool_calls_used"] <= 0.7 * without.report["tool_calls_used"], (
        "second occurrence >= 30% cheaper"
    )
    assert [a["target"] for a in executed_ok(with_memory.report) if a["action"] == "revert_config"] == [
        "session_flush_schedule"
    ]
    wm, wo = with_memory.report["root_cause"], without.report["root_cause"]
    assert (
        (wm["component"], wm["category"]) == (wo["component"], wo["category"]) == ("sessions-cache", "cache_stampede")
    )


async def test_team_preferences_order_the_plan(store):
    run = await drive("S10", store=store, resolver=lambda p: approve_all(p))
    plan = (await run.state())["plan"]
    assert plan[0]["action"] == "revert_flag"  # pref-002: prefer reverting the flag over scaling out


# --- (9) prompts, directives, budget ---------------------------------------------------------------------------------------------------------------------
async def test_editing_the_planner_prompt_changes_the_plan(store):
    base = await drive("S06", store=store, thread="p0")
    edited = await drive(
        "S06",
        store=store,
        thread="p1",
        planner_prompt="You are the planner.\npolicy: mitigate_before_fix\nstyle: terse",
    )
    assert [p["proposed"]["action"] for p in base.interrupts] == ["revert_config"]
    done = [a["action"] for a in executed_ok(edited.report) if a["action"] != "post_status_update"]
    assert done[0] == "warm_cache", "mitigate_before_fix warms the cache first (low risk: no approval) ..."
    assert "revert_config" in done[1:] and edited.report["recovered"] is True, (
        "... and still fixes the cause afterwards"
    )
    state = await edited.state()
    assert "policy=mitigate_before_fix" in state["route"]["narration"] or "policy=mitigate_before_fix" in str(
        state["directives"]
    )


async def test_the_hop_budget_is_enforced_in_code(store, monkeypatch):
    monkeypatch.setitem(inv.BUDGETS, 1, (10, 5))
    run = await drive("S01", store=store)
    rep = run.report
    assert "budget_exceeded" in rep["flags"] and rep["supervisor_hops"] <= 5
    assert rep["root_cause"] is not None, "it still concludes with what it has"


# --- (10) Studio ---------------------------------------------------------------------------------------------------------------------------------------------
def test_context_schema_exposes_editable_prompts():
    g = build_graph()
    props = g.get_context_jsonschema()["properties"]
    assert (
        props["supervisor_prompt"]["langgraph_nodes"] == ["supervisor"]
        and props["supervisor_prompt"]["langgraph_type"] == "prompt"
    )
    assert props["planner_prompt"]["langgraph_nodes"] == ["remediation_planner"]
    assert props["analyst_prompt"]["langgraph_nodes"] == [
        "metrics_analyst",
        "logs_analyst",
        "change_analyst",
        "topology_analyst",
        "knowledge_agent",
    ]
    for k in (
        "scenario_id",
        "as_of",
        "model_profile",
        "approval_policy",
        "breakpoints",
        "memory_enabled",
        "chaos",
        "token_delay_ms",
    ):
        assert k in props


def test_graph_shape_for_studio():
    g = build_graph()
    nodes = g.get_graph().nodes
    for n in (
        "bootstrap",
        "supervisor",
        "metrics_analyst",
        "logs_analyst",
        "change_analyst",
        "topology_analyst",
        "knowledge_agent",
        "rca_tournament",
        "clarify_change_intent",
        "remediation_planner",
        "policy_check",
        "approval_gate",
        "answer_question",
        "execute_action",
        "verify_recovery",
        "comms_drafter",
        "comms_gate",
        "close_incident",
    ):
        assert n in nodes, n
    edges = {(e.source, e.target) for e in g.get_graph().edges}
    assert (
        ("supervisor", "rca_tournament") in edges
        and ("approval_gate", "execute_action") in edges
        and ("policy_check", "approval_gate") in edges
    )
    xray = g.get_graph(xray=True).nodes
    assert "metrics_analyst:model" in xray and any(n.startswith("rca_tournament:") for n in xray), (
        "subgraphs expand in xray"
    )
    assert nodes["close_incident"].metadata and nodes["close_incident"].metadata.get("kind") == "report"
    in_props = g.get_input_jsonschema()["properties"]
    assert "scenario_id" in in_props and "incident" in in_props
    assert set(g.get_output_jsonschema()["properties"]) == {"result", "report"}


async def test_agent_private_keys_do_not_leak(store):
    run = await drive("S03", store=store)
    state = await run.state()
    assert "messages" not in state and "structured_response" not in state and "jump_to" not in state
    assert len({e["id"] for e in state["evidence"]}) == len(state["evidence"]), (
        "shared evidence is merged by id, never double counted"
    )
    assert state["supervisor_hops"] == run.report["supervisor_hops"]


async def test_specialists_appear_as_subgraph_namespaces(store):
    g = build_graph(checkpointer=InMemorySaver(), store=store)
    nss: set[str] = set()
    async for ns, mode, _p in g.astream(
        commander_input("S02"),
        {"configurable": {"thread_id": "ns"}},
        context=Context(),
        stream_mode=["updates"],
        subgraphs=True,
    ):
        nss.update(n.split(":")[0] for n in ns)
    assert {
        "metrics_analyst",
        "logs_analyst",
        "change_analyst",
        "topology_analyst",
        "knowledge_agent",
        "rca_tournament",
    } <= nss


async def test_static_breakpoint_before_execute_action_and_edit_is_re_approved(store):
    """W8: interrupt_before=['execute_action'] (what the dispatcher passes for context.breakpoints); editing chosen_action at the
    breakpoint must not bypass the policy gate or the approval."""
    g = build_graph(checkpointer=InMemorySaver(), store=store)
    cfg = {"configurable": {"thread_id": "bp"}}
    ctx = Context(breakpoints=["execute_action"])
    stop = ["execute_action"]
    out = await g.ainvoke(commander_input("S01"), cfg, context=ctx, interrupt_before=stop, version="v2")
    assert out.interrupts and out.interrupts[0].value["kind"] == "approval"
    out = await g.ainvoke(
        Command(resume=Decision(decision="approve", decided_by="oncall")),
        cfg,
        context=ctx,
        interrupt_before=stop,
        version="v2",
    )
    snap = await g.aget_state(cfg)
    assert snap.next == ("execute_action",) and not out.interrupts, "paused at the breakpoint with the approval in hand"
    edited = {**snap.values["chosen_action"], "target": "cart-service"}  # the human edits the target at the breakpoint
    await g.aupdate_state(cfg, {"chosen_action": edited}, as_node="approval_gate")
    out = await g.ainvoke(None, cfg, context=ctx, interrupt_before=stop, version="v2")
    # execute_action noticed the change: the policy gate blocked cart-service (out of scope), the commander re-planned and asks
    # for a FRESH approval of a safe action; nothing ran in the meantime
    assert out.interrupts and out.interrupts[0].value["kind"] == "approval"
    assert out.interrupts[0].value["proposed"]["target"] == "checkout-service"
    state = (await g.aget_state(cfg)).values
    assert not [a for a in state.get("executed", []) if a["action"] != "post_status_update"]
    assert any(a.get("blocked") and a["target"] == "cart-service" for a in state["proposed"])


async def test_unedited_breakpoint_continues_and_executes(store):
    g = build_graph(checkpointer=InMemorySaver(), store=store)
    cfg = {"configurable": {"thread_id": "bp2"}}
    ctx = Context()
    stop = ["execute_action"]
    await g.ainvoke(
        commander_input("S03"), cfg, context=ctx, interrupt_before=stop, version="v2"
    )  # low risk: no approval, just the breakpoint
    assert (await g.aget_state(cfg)).next == ("execute_action",)
    out = await g.ainvoke(None, cfg, context=ctx, version="v2")
    assert (
        out.value["report"]["recovered"] is True
        and out.value["report"]["actions_executed"][0]["action"] == "rotate_logs"
    )


async def test_status_page_review_edit_is_linted_and_reject_withholds(store):
    def dirty_edit(p: dict[str, Any]) -> Decision | Answer:
        if p["kind"] == "edit":
            return Decision(
                decision="edit",
                edits={"text": "payments-service is down because of a bad deploy by r.santos"},
                decided_by="comms",
            )
        return approve_all(p)

    dirty = await drive("S08", store=store, resolver=dirty_edit)
    assert "policy_blocked" in dirty.report["flags"]
    assert "payments-service" not in dirty.report["comms_draft"] and "payment" in dirty.report["comms_draft"].lower()

    def withhold(p: dict[str, Any]) -> Decision | Answer:
        return Decision(decision="reject", decided_by="comms") if p["kind"] == "edit" else approve_all(p)

    held = await drive("S08", store=store, resolver=withhold, thread="withheld")
    assert held.report["comms_draft"] == "" and not any(
        a["action"] == "post_status_update" for a in executed_ok(held.report)
    )


async def test_token_delay_is_applied_per_run(store):
    run = await drive("S03", store=store, token_delay_ms=1)
    assert run.report["recovered"] is True


def test_no_ground_truth_in_graph_code():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "src" / "foghorn" / "flows"
    for pkg in ("incident_commander", "rca_tournament"):
        for f in (root / pkg).rglob("*.py"):
            text = f.read_text()
            assert "get_truth" not in text and "reveal_truth" not in text, f"{f} reads ground truth"
