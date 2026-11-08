"""The five specialist agents: ``langchain.agents.create_agent`` graphs over ``tools_for([...])``, added to the commander as
COMPILED SUBGRAPH NODES (so Studio's xray drills into them).

Each agent is given a task (``<task>{...}</task>`` in its first human message) by the supervisor via ``Send``. A small
middleware does the product work around the model loop:

* ``before_model``  - streams the custom events for the tool results that just arrived (``progress``, ``series`` with at
                       most 60 points, ``evidence.found``), keyed deterministically so a replay re-emits harmlessly;
* ``wrap_model_call`` - applies the editable ``analyst_prompt`` and the per-run ``token_delay_ms``;
* ``wrap_tool_call``  - chaos mode: transient tool failures (healed by the node's ``RetryPolicy``);
* ``after_agent``     - turns the transcript into ``evidence`` records and one ``agent_runs`` record (shared keys); every other
                       key of the agent (``messages``, ...) stays private.
"""

from __future__ import annotations

import warnings
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.config import get_config
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from foghorn.common.stream import emit
from foghorn.flows.incident_commander import chaos
from foghorn.flows.incident_commander.context import ANALYST_PROMPT, ctx_get
from foghorn.flows.incident_commander.investigation import AGENTS, specialist_tools
from foghorn.flows.incident_commander.scripts import block
from foghorn.flows.incident_commander.state import SpecialistState
from foghorn.flows.rca_tournament.facts import Evidence, extract_evidence, parse_tool_result
from foghorn.models.offline import OfflineChatModel
from foghorn.tools.lc import tools_for

# langchain's ToolRuntime is validated as a pydantic model whose `context` field is typed None; a dataclass context makes
# pydantic warn on every tool call (harmless, noisy). Scoped to that one message.
warnings.filterwarnings("ignore", message="Pydantic serializer warnings", category=UserWarning)
SERIES_POINTS = 60


def _calls_by_id(messages: list[Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for m in messages:
        if isinstance(m, AIMessage):
            for tc in m.tool_calls:
                out[str(tc["id"])] = dict(tc)
    return out


def _task_of(messages: list[Any]) -> dict[str, Any]:
    for m in messages:
        if isinstance(m, HumanMessage):
            return block(str(m.text), "task")
    return {}


def _results(messages: list[Any], only_tail: bool) -> list[tuple[str, dict[str, Any], dict[str, Any] | None, str]]:
    calls = _calls_by_id(messages)
    tail: list[ToolMessage] = []
    for m in reversed(messages):
        if isinstance(m, ToolMessage):
            tail.append(m)
        elif isinstance(m, AIMessage) and only_tail:
            break
    tail.reverse()
    pool = tail if only_tail else [m for m in messages if isinstance(m, ToolMessage)]
    return [
        (
            str(m.name),
            calls.get(str(m.tool_call_id), {}).get("args", {}),
            parse_tool_result(m.content),
            str(m.tool_call_id),
        )
        for m in pool
    ]


class Analyst(AgentMiddleware[SpecialistState, Any]):
    """Per-agent middleware (see the module docstring)."""

    state_schema = SpecialistState

    def __init__(self, agent: str) -> None:
        super().__init__()
        self.agent = agent

    # -- live events -------------------------------------------------------------------------------------------------------
    def before_model(self, state: SpecialistState, runtime: Any) -> dict[str, Any] | None:
        msgs = state["messages"]
        task = _task_of(msgs)
        n_total = int(task.get("tool_budget") or 0) or 1
        done = sum(isinstance(m, ToolMessage) for m in msgs)
        for name, args, res, _tcid in _results(msgs, only_tail=True):
            if not res:
                continue
            if name == "query_metric" and res.get("points"):
                emit(
                    "series",
                    f"series:{res['service']}:{res['metric']}:{res.get('as_of')}",
                    agent=self.agent,
                    t_virtual=res.get("as_of"),
                    service=res["service"],
                    metric=res["metric"],
                    points=res["points"][-SERIES_POINTS:],
                    baseline=(res.get("baseline") or {}).get("mean"),
                    onset=res.get("onset_estimate"),
                )
            evs = extract_evidence(name, args, res, agent=self.agent)
            top = sorted(evs, key=lambda e: -e["strength"])[:4]
            for e in top:
                if e["kind"] in ("metric", "alert") and e["strength"] <= 0.0:
                    continue
                emit(
                    "evidence.found",
                    f"evidence:{e['id']}",
                    agent=self.agent,
                    t_virtual=e.get("t"),
                    id=e["id"],
                    kind=e["kind"],
                    ref=e["ref"],
                    summary=e["summary"],
                    strength=e["strength"],
                )
        if done:
            emit(
                "progress",
                f"progress:{self.agent}:{done}",
                agent=self.agent,
                label=f"{self.agent}: {done}/{n_total} tool calls",
                pct=min(1.0, done / n_total),
            )
        return None

    # -- prompt + pacing ---------------------------------------------------------------------------------------------------
    def _prepare(self, request: ModelRequest[Any]) -> ModelRequest[Any]:
        ctx = request.runtime.context
        prompt = str(ctx_get(ctx, "analyst_prompt", ANALYST_PROMPT) or ANALYST_PROMPT)
        model: BaseChatModel = request.model
        delay = int(ctx_get(ctx, "token_delay_ms", 0) or 0)
        if delay and isinstance(model, OfflineChatModel):
            model = model.model_copy(update={"token_delay_s": delay / 1000.0})
        return request.override(
            system_message=SystemMessage(content=f"{prompt}\n\nYou are the {self.agent}."), model=model
        )

    def wrap_model_call(
        self, request: ModelRequest[Any], handler: Callable[[ModelRequest[Any]], ModelResponse[Any]]
    ) -> ModelResponse[Any]:
        return handler(self._prepare(request))

    async def awrap_model_call(
        self, request: ModelRequest[Any], handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]]
    ) -> ModelResponse[Any]:
        return await handler(self._prepare(request))

    # -- chaos -------------------------------------------------------------------------------------------------------------
    def _chaos(self, request: ToolCallRequest) -> None:
        ctx = request.runtime.context
        thread = str((get_config().get("configurable") or {}).get("thread_id"))
        chaos.maybe_fail(
            ctx_get(ctx, "chaos"),
            thread,
            str(request.tool_call["name"]),
            str(sorted(request.tool_call["args"].items())),
        )

    def wrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]]
    ) -> ToolMessage | Command[Any]:
        self._chaos(request)
        return handler(request)

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]]
    ) -> ToolMessage | Command[Any]:
        self._chaos(request)
        return await handler(request)

    # -- evidence + run record ---------------------------------------------------------------------------------------------
    def after_agent(self, state: SpecialistState, runtime: Any) -> dict[str, Any] | None:
        msgs = state["messages"]
        task = _task_of(msgs)
        ev: list[Evidence] = []
        n = 0
        settled = False
        for name, args, res, _tcid in _results(msgs, only_tail=False):
            n += 1
            settled = settled or name == "advance_time"
            if res:
                ev += extract_evidence(name, args, res, agent=self.agent)
        last = next((m for m in reversed(msgs) if isinstance(m, AIMessage) and not m.tool_calls), None)
        rec = {
            "id": f"{self.agent}:{task.get('wave', 0)}",
            "agent": self.agent,
            "wave": task.get("wave", 0),
            "tool_calls": n,
            "settled": settled,
            "lookback": bool(task.get("lookback_days")),
            "services": task.get("services", []),
            "summary": (str(last.text) if last is not None else "")[:420],
        }
        return {"evidence": ev, "agent_runs": [rec]}


def build_specialist(agent: str, llm: BaseChatModel) -> CompiledStateGraph[Any, Any, Any, Any]:
    return create_agent(
        llm,
        tools_for(specialist_tools(agent)),
        state_schema=SpecialistState,
        middleware=[Analyst(agent)],
        name=agent,
    )


def build_specialists(llm: BaseChatModel) -> dict[str, CompiledStateGraph[Any, Any, Any, Any]]:
    return {a: build_specialist(a, llm) for a in AGENTS}
