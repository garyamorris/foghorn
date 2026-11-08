"""The LangChain wrappers inside ``langchain.agents.create_agent`` with a state_schema that carries the three state keys."""

from __future__ import annotations

import json
import warnings

import pytest
from langchain.agents import create_agent
from langchain_core.messages import ToolMessage

from foghorn.data import api as data
from foghorn.models import offline as o
from foghorn.tools import lc
from foghorn.tools.context import advance_as_of

warnings.filterwarnings("ignore")
CORE = {
    "query_metric",
    "compare_baseline",
    "search_logs",
    "list_changes",
    "get_topology",
    "blast_radius",
    "search_runbooks",
    "recall_incidents",
    "get_oncall",
    "execute_action",
    "verify_recovery",
}


def agent_for(script: o.Script, names: list[str]):
    return create_agent(o.OfflineChatModel(script=script), lc.tools_for(names), state_schema=lc.ToolState)


def tool_json(res: dict, name: str) -> dict:
    msg = next(m for m in res["messages"] if isinstance(m, ToolMessage) and m.name == name)
    return json.loads(msg.content)


def initial(sid: str, **extra):
    return {"messages": [("user", "investigate")], "scenario_id": sid, "as_of": None, "applied_actions": [], **extra}


def test_registry_and_schemas():
    assert set(lc.TOOLS_BY_NAME) >= CORE and set(lc.CORE_TOOL_NAMES) == CORE
    assert {"list_alerts", "advance_time"} <= set(lc.TOOLS_BY_NAME)
    for name, t in lc.TOOLS_BY_NAME.items():
        assert t.description and len(t.description) > 40, name
        assert "runtime" not in t.args, name  # the ToolRuntime is injected, never shown to the model
    assert set(lc.TOOLS_BY_NAME["query_metric"].args) == {"service", "metric", "window_minutes"}
    assert [t.name for t in lc.tools_for(["verify_recovery", "query_metric"])] == ["verify_recovery", "query_metric"]
    with pytest.raises(KeyError, match="nope"):
        lc.tools_for(["nope"])


def test_full_investigation_remediation_and_verification_loop_in_an_agent():
    s = (
        o.Script()
        .when(
            "1", o.step(0), o.call_tool("query_metric", service="pricing-service", metric="p95_ms", window_minutes=10)
        )
        .when(
            "2",
            o.step(1),
            o.call_tool(
                "search_logs", service="pricing-service", pattern="TaxClient", level="ERROR", window_minutes=30
            ),
        )
        .when("3", o.step(2), o.call_tool("execute_action", action="revert_flag", target="new_pricing_engine"))
        .when("4", o.step(3), o.call_tool("advance_time", minutes=15))
        .when("5", o.step(4), o.call_tool("verify_recovery", service="pricing-service"))
        .when("6", o.step(5), o.say("pricing recovered"))
    )
    agent = agent_for(s, ["query_metric", "search_logs", "execute_action", "advance_time", "verify_recovery"])
    default = data.default_as_of("S10")
    res = agent.invoke(initial("S10"))
    q = tool_json(res, "query_metric")
    assert q["as_of"] == default and q["points"][-1][0] <= default and q["z_score"] > 40
    logs = tool_json(res, "search_logs")
    assert "lines" not in logs and logs["text"].startswith("<untrusted") and logs["total_matches"] > 0
    act = tool_json(res, "execute_action")
    assert act["outcome"] == "executed" and act["applied_at"] == default
    assert [a["id"] for a in res["applied_actions"]] == [act["id"]]  # recorded in graph state by the tool's Command
    assert res["as_of"] == advance_as_of({"scenario_id": "S10", "as_of": default}, 15)
    assert tool_json(res, "advance_time")["as_of"] == res["as_of"]
    v = tool_json(res, "verify_recovery")
    assert v["status"] == "recovered" and v["actions_considered"] == [act["id"]]
    assert res["messages"][-1].content == "pricing recovered"


def test_tools_read_as_of_from_state_so_a_fork_can_edit_it():
    s = (
        o.Script()
        .when(
            "q", o.step(0), o.call_tool("query_metric", service="pricing-service", metric="p95_ms", window_minutes=60)
        )
        .when("d", o.step(1), o.say("ok"))
    )
    agent = agent_for(s, ["query_metric"])
    late = tool_json(agent.invoke(initial("S10")), "query_metric")
    early = tool_json(agent.invoke(initial("S10", as_of="2026-11-05T10:30:00Z")), "query_metric")
    assert (
        early["as_of"] == "2026-11-05T10:30:00Z"
        and early["n"] < late["n"] + 60
        and early["max_z"] < 6
        and late["max_z"] > 40
    )
    assert all(p[0] <= "2026-11-05T10:30:00Z" for p in early["points"])


def test_applied_actions_in_state_drive_the_recovery_overlay():
    sid = "S10"
    s = (
        o.Script()
        .when("v", o.step(0), o.call_tool("verify_recovery", service="pricing-service"))
        .when("d", o.step(1), o.say("ok"))
    )
    agent = agent_for(s, ["verify_recovery"])
    from foghorn.tools import api

    base = {"scenario_id": sid, "as_of": data.default_as_of(sid)}
    rec = api.execute_action(base, "revert_flag", "new_pricing_engine")
    later = advance_as_of(base, 20)
    assert tool_json(agent.invoke(initial(sid, as_of=later)), "verify_recovery")["status"] == "degraded"
    assert (
        tool_json(agent.invoke(initial(sid, as_of=later, applied_actions=[rec])), "verify_recovery")["status"]
        == "recovered"
    )


def test_bad_arguments_come_back_as_an_error_payload_not_an_exception():
    s = (
        o.Script()
        .when("q", o.step(0), o.call_tool("query_metric", service="no-such", metric="rps"))
        .when("t", o.step(1), o.call_tool("get_topology", service="checkout-service"))
        .when("d", o.step(2), o.say("ok"))
    )
    res = agent_for(s, ["query_metric", "get_topology"]).invoke(initial("S10"))
    assert "unknown service" in tool_json(res, "query_metric")["error"]
    assert tool_json(res, "get_topology")["tier"] == 0
    # no scenario anywhere -> a clear error too
    s2 = (
        o.Script()
        .when("q", o.step(0), o.call_tool("query_metric", service="checkout-service", metric="rps"))
        .when("d", o.step(1), o.say("ok"))
    )
    res2 = agent_for(s2, ["query_metric"]).invoke({"messages": [("user", "hi")], "applied_actions": []})
    assert "no scenario" in tool_json(res2, "query_metric")["error"]


def test_scenario_falls_back_to_configurable_when_state_has_none():
    s = (
        o.Script()
        .when("q", o.step(0), o.call_tool("query_metric", service="checkout-service", metric="rps", window_minutes=3))
        .when("d", o.step(1), o.say("ok"))
    )
    agent = agent_for(s, ["query_metric"])
    res = agent.invoke({"messages": [("user", "hi")], "applied_actions": []}, {"configurable": {"scenario_id": "S01"}})
    assert tool_json(res, "query_metric")["as_of"] == data.default_as_of("S01")


def test_duplicate_parallel_actions_are_recorded_once_and_unknown_actions_are_rejected():
    s = (
        o.Script()
        .when(
            "a",
            o.step(0),
            o.call_tools(
                ("execute_action", {"action": "rollback_deploy", "target": "checkout-service"}),
                ("execute_action", {"action": "rollback_deploy", "target": "checkout-service"}),
            ),
        )
        .when("b", o.step(1), o.call_tool("execute_action", action="teleport", target="checkout-service"))
        .when("d", o.step(2), o.say("ok"))
    )
    res = agent_for(s, ["execute_action"]).invoke(initial("S01"))
    outcomes = [json.loads(m.content)["outcome"] for m in res["messages"] if isinstance(m, ToolMessage)]
    assert outcomes == ["executed", "executed", "rejected"]
    assert (
        len(res["applied_actions"]) == 2
    )  # two distinct records (the duplicate shares an id); the rejected one is recorded too
    assert sum(a["outcome"] == "executed" for a in res["applied_actions"]) == 1


def test_untrusted_text_stays_wrapped_through_the_tools():
    s = (
        o.Script()
        .when(
            "a",
            o.step(0),
            o.call_tool("search_logs", service="cart-service", pattern="coupon", level="ERROR", window_minutes=30),
        )
        .when("b", o.step(1), o.call_tool("list_changes", window_minutes=240))
        .when("d", o.step(2), o.say("ok"))
    )
    res = agent_for(s, ["search_logs", "list_changes"]).invoke(
        initial("S15", as_of=advance_as_of({"scenario_id": "S15"}, 10))
    )
    logs = tool_json(res, "search_logs")
    assert logs["injection_suspected"] and 'flagged="possible-injection"' in logs["text"]
    ch = tool_json(res, "list_changes")
    assert ch["wrapped"].startswith('<untrusted source="changes"') and "deploy 2026.11.07.1" in ch["wrapped"]


def test_corpus_tools_need_no_scenario():
    s = (
        o.Script()
        .when(
            "a",
            o.step(0),
            o.call_tools(
                ("search_runbooks", {"query": "tls handshake certificate expired"}),
                ("recall_incidents", {"query": "weekly cache flush stampede"}),
            ),
        )
        .when("d", o.step(1), o.say("ok"))
    )
    res = agent_for(s, ["search_runbooks", "recall_incidents"]).invoke(
        {"messages": [("user", "x")], "applied_actions": []}
    )
    assert tool_json(res, "search_runbooks")["hits"][0]["id"] == "RB-005"
    assert "INC-0087" in [h["id"] for h in tool_json(res, "recall_incidents")["hits"][:3]]


def test_remaining_wrappers_work_in_an_agent():
    calls = [
        ("compare_baseline", {"service": "pricing-service", "metric": "p95_ms"}),
        ("get_oncall", {"component": "pricing-service"}),
        ("blast_radius", {"service": "pricing-service"}),
        ("list_alerts", {"window_minutes": 60}),
    ]
    s = o.Script().when("a", o.step(0), o.call_tools(*calls)).when("d", o.step(1), o.say("ok"))
    res = agent_for(s, [c[0] for c in calls]).invoke(initial("S10"))
    assert tool_json(res, "compare_baseline")["verdict"] in ("degraded", "critical")
    assert tool_json(res, "get_oncall")["rota"] == "rota-commerce"
    assert "cart-service" in tool_json(res, "blast_radius")["direct_callers"]
    assert tool_json(res, "list_alerts")["count"] >= 1


def test_advance_time_is_clamped_and_never_negative():
    s = (
        o.Script()
        .when("a", o.step(0), o.call_tool("advance_time", minutes=500))
        .when("b", o.step(1), o.call_tool("advance_time", minutes=-5))
        .when("d", o.step(2), o.say("ok"))
    )
    res = agent_for(s, ["advance_time"]).invoke(initial("S10"))
    first, second = (json.loads(m.content)["as_of"] for m in res["messages"] if isinstance(m, ToolMessage))
    assert first == advance_as_of({"scenario_id": "S10", "as_of": data.default_as_of("S10")}, 30) and second == first
