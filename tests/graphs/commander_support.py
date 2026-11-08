"""Shared helpers for the incident_commander / rca_tournament graph tests (offline, deterministic, fast)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command

from foghorn.contracts.interrupts import Answer, Decision
from foghorn.data import api as data
from foghorn.embeddings import aembed
from foghorn.flows.incident_commander.build import build_graph
from foghorn.flows.incident_commander.context import Context

SCENARIOS = [f"S{i:02d}" for i in range(1, 17)]
BREAK_GLASS_NOTE = "Freeze override approved: runaway export is blocking the revenue path"


def seeded_store() -> InMemoryStore:
    """A real InMemoryStore with the committed seed memories (incidents, lessons, prefs) and the hashed-embedding index.
    Call from a SYNC fixture (it runs its own event loop)."""
    store = InMemoryStore(index={"dims": 256, "embed": aembed, "fields": ["text"]})

    async def seed() -> None:
        for it in data.memory_seed_items():
            await store.aput(tuple(it["namespace"]), it["key"], it["value"])

    asyncio.run(seed())
    return store


def commander_input(sid: str, severity: str = "SEV3", **extra: Any) -> dict[str, Any]:
    """What the event router hands the commander: the triggering alert event of the scenario (no ground truth)."""
    meta = data.get_scenario(sid)
    trig = meta["trigger"]["alert_event"]
    alerts = [e for e in data.scenario_events(sid) if e["id"] == trig]
    return {
        "scenario_id": sid,
        "incident": {"id": f"INC-T{sid[1:]}", "severity": severity, "title": meta["title"]},
        "alerts": alerts,
        "trigger_event_ids": [trig],
        **extra,
    }


def approve_all(payload: dict[str, Any], n: int = 0) -> Decision | Answer:
    """A permissive, well-behaved human: answers questions, approves everything (break-glass with a written reason)."""
    kind = payload["kind"]
    if kind == "question":
        return Answer(answer="yes, the ramp was intended, but we see unexpected timeouts", decided_by="oncall")
    if kind == "break_glass":
        return Decision(
            decision="approve", decided_by="oncall", note=BREAK_GLASS_NOTE, edits={"second_approver": "commander"}
        )
    return Decision(decision="approve", decided_by=f"approver-{payload['proposed'].get('approver', 1)}")


Resolver = Callable[[dict[str, Any]], Decision | Answer]


@dataclass
class Run:
    graph: Any
    config: dict[str, Any]
    context: Context
    value: dict[str, Any] = field(default_factory=dict)
    interrupts: list[dict[str, Any]] = field(default_factory=list)  # every interrupt payload seen, in order
    final: dict[str, Any] = field(default_factory=dict)  # the thread state after the last resume

    @property
    def report(self) -> dict[str, Any]:
        return self.value["report"]

    async def state(self) -> dict[str, Any]:
        return self.final or dict((await self.graph.aget_state(self.config)).values)


async def drive(
    sid: str,
    *,
    profile: str = "competent",
    resolver: Resolver = approve_all,
    graph: Any = None,
    store: Any = None,
    thread: str | None = None,
    max_rounds: int = 12,
    input_extra: dict[str, Any] | None = None,
    severity: str = "SEV3",
    **ctx: Any,
) -> Run:
    """Run the commander on scenario ``sid`` answering every interrupt with ``resolver`` until it finishes."""
    g = graph or build_graph(checkpointer=InMemorySaver(), store=store)
    context = Context(model_profile=profile, **ctx)
    cfg = {"configurable": {"thread_id": thread or f"t-{sid}-{profile}"}}
    run = Run(g, cfg, context)
    out = await g.ainvoke(commander_input(sid, severity, **(input_extra or {})), cfg, context=context, version="v2")
    rounds = 0
    while out.interrupts:
        rounds += 1
        assert rounds <= max_rounds, f"{sid}/{profile}: still interrupting after {max_rounds} rounds"
        answers = {}
        for i in out.interrupts:
            run.interrupts.append(i.value)
            answers[i.id] = resolver(i.value)
        resume = answers if len(answers) > 1 else next(iter(answers.values()))
        out = await g.ainvoke(Command(resume=resume), cfg, context=context, version="v2")
    run.value = out.value
    run.final = dict((await g.aget_state(cfg)).values)
    return run
