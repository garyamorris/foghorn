"""`model_profile` / `scenario_id` from the RUN CONTEXT reach scripted rules (this is how one graph behaves
competently, naively, cautiously or gullibly offline, and how Studio's assistant 'Context' pane steers demos)."""

from __future__ import annotations

from dataclasses import dataclass

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from typing_extensions import TypedDict

from foghorn.models import offline as o


@dataclass
class Ctx:
    model_profile: str = "competent"
    scenario_id: str | None = None


class S(TypedDict, total=False):
    text: str


def _graph(script: o.Script):
    llm = o.OfflineChatModel(role="smart", script=script)

    async def think(_: S, runtime: Runtime[Ctx]) -> S:
        msg = await llm.ainvoke([("user", "what happened?")])
        return {"text": str(msg.content)}

    g = StateGraph(S, context_schema=Ctx)
    g.add_node("think", think)
    g.add_edge(START, "think")
    g.add_edge("think", END)
    return g.compile()


async def test_rules_can_branch_on_profile_and_scenario_from_runtime_context():
    script = (
        o.Script()
        .when("naive-s01", o.all_(o.profile("naive"), o.scenario("S01")), "blame the deploy")
        .when("any-naive", o.profile("naive", "gullible"), "blame the first alert")
        .when("default", lambda _c: True, "follow the evidence")
    )
    graph = _graph(script)
    assert (await graph.ainvoke({}, context=Ctx("naive", "S01")))["text"] == "blame the deploy"
    assert (await graph.ainvoke({}, context=Ctx("gullible", "S05")))["text"] == "blame the first alert"
    assert (await graph.ainvoke({}, context=Ctx()))["text"] == "follow the evidence"
    last = script.calls[-1]
    assert last.profile == "competent" and last.context["model_profile"] == "competent"


async def test_profile_defaults_to_competent_when_no_context_is_given():
    script = o.Script().when("p", o.profile("competent"), "ok")
    llm = o.OfflineChatModel(role="fast", script=script)
    assert (await llm.ainvoke([("user", "hi")])).content == "ok"
