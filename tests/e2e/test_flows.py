"""Flow e2e: the real graphs wired through the real event pipeline on a real `langgraph dev` server (offline).

This is the test of the PRODUCT, not of a component: an alert event goes in through the dashboard API; the
dispatcher runs `event_router`; it opens an incident and starts `incident_commander`; the commander streams, asks a
human for approval, executes, verifies and resolves; lifecycle events chain together; evals score the result.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
from langgraph_sdk import get_client

from foghorn.common.stream import STREAM_KINDS

pytestmark = [pytest.mark.asyncio, pytest.mark.e2e]

RUN_CTX = {
    "model_profile": "competent",
    "approval_policy": "standard",
    "token_delay_ms": 0,
    "memory_enabled": True,
    "breakpoints": [],
    "chaos": None,
    "as_of": None,
}


async def poll(
    fn: Callable[[], Awaitable[Any]], *, within: float = 60.0, every: float = 0.4, what: str = "condition"
) -> Any:
    """Await until `fn()` returns something truthy; fail with a useful message otherwise."""
    deadline = asyncio.get_event_loop().time() + within
    last: Any = None
    while asyncio.get_event_loop().time() < deadline:
        last = await fn()
        if last:
            return last
        await asyncio.sleep(every)
    pytest.fail(f"timed out after {within}s waiting for {what}; last value: {str(last)[:500]}")


@pytest.fixture
def client(dev_server):
    return get_client(url=dev_server)


async def test_s01_alert_to_resolution_through_the_dashboard_api(dev_server):
    async with httpx.AsyncClient(base_url=dev_server, timeout=60) as h:
        launch = await h.post(
            "/api/scenarios/S01/launch",
            json={"mode": "events", "speed": 600, "replace": True, **{"profile": "competent"}},
        )
        assert launch.status_code == 202, launch.text

        async def incident() -> dict[str, Any] | None:
            rows = (await h.get("/api/incidents", params={"scenario_id": "S01"})).json()["items"]
            return rows[0] if rows else None

        inc = await poll(incident, what="the router to open an incident for S01")
        iid = inc["incident_id"]

        async def pending() -> dict[str, Any] | None:
            rows = (await h.get("/api/approvals", params={"status": "pending", "incident_id": iid})).json()["items"]
            return rows[0] if rows else None

        approval = await poll(pending, what="the commander to ask for approval")
        assert approval["kind"] == "approval" and approval["risk"] in {"medium", "high", "critical"}
        assert approval["response_schema"], "dashboard forms are generated from this schema"
        resumed = await h.post(
            f"/api/approvals/{approval['approval_id']}/resume",
            json={
                "interrupt_id": approval["interrupt_id"],
                "response": {"decision": "approve", "decided_by": "e2e"},
                "nonce": "e2e-s01",
            },
        )
        assert resumed.status_code == 202, resumed.text

        async def more_approvals() -> bool:
            """S01 can also ask to publish the status update; approve whatever else appears until resolved."""
            row = (await h.get(f"/api/incidents/{iid}")).json()
            if row["status"] == "resolved":
                return True
            for a in (await h.get("/api/approvals", params={"status": "pending", "incident_id": iid})).json()["items"]:
                await h.post(
                    f"/api/approvals/{a['approval_id']}/resume",
                    json={
                        "interrupt_id": a["interrupt_id"],
                        "response": {"decision": "approve", "decided_by": "e2e"},
                        "nonce": f"e2e-{a['approval_id']}",
                    },
                )
            return False

        await poll(more_approvals, within=90, what="the incident to resolve")
        detail = (await h.get(f"/api/incidents/{iid}")).json()
        types = [e["type"] for e in detail["events"]]
        for expected in (
            "alert.fired",
            "incident.opened",
            "approval.requested",
            "approval.decided",
            "action.executed",
            "action.verified",
            "incident.resolved",
        ):
            assert expected in types, f"{expected} missing from the incident's event chain: {types}"
        assert types.index("incident.opened") < types.index("approval.requested") < types.index("incident.resolved")

        # the commander thread carries the structured report the evaluators and postmortem read
        client = get_client(url=dev_server)
        state = await client.threads.get_state(detail["thread_id"])
        report = state["values"]["report"]
        assert report["root_cause"]["component"] == "checkout-service" and report["recovered"] is True
        assert not report["abstained"] and report["actions_executed"]

        # The incident console REPLAYS the commander's narration through join-stream. That only works because every
        # run we create asks for a resumable, full-fidelity stream, and only without a `stream_mode` query (found
        # while driving the real UI: the console showed "No narration captured").
        assert detail["run_id"], "the incident must expose the run the console should join"
        replay = await h.get(
            f"/threads/{detail['thread_id']}/runs/{detail['run_id']}/stream", headers={"Last-Event-ID": "-1"}
        )
        modes = {line[6:].strip().split("|")[0] for line in replay.text.splitlines() if line.startswith("event:")}
        assert {"messages", "custom", "updates", "tasks"} <= modes, f"replay lost the narration: {modes}"


async def test_commander_streams_everything_a_user_watches_over_the_sdk(client):
    thread = await client.threads.create()
    tid = thread["thread_id"]
    kinds: set[str] = set()
    token_namespaces: set[str] = set()
    nodes_seen: set[str] = set()
    tokens = 0
    interrupt_ids: list[str] = []
    async for part in client.runs.stream(
        tid,
        "incident_commander",
        input={"scenario_id": "S01", "incident": {"id": "INC-E2E01", "severity": "SEV2", "title": "Checkout 500s"}},
        context=RUN_CTX,
        stream_mode=["updates", "messages-tuple", "custom", "values", "tasks"],
        stream_subgraphs=True,
    ):
        mode = part.event.split("|")[0]
        if mode == "custom" and isinstance(part.data, dict):
            kinds.add(part.data["kind"])
            assert part.data["v"] == 1 and part.data["key"], "every custom event carries a deterministic key"
        elif mode == "messages":
            msg, meta = part.data
            if msg.get("type") == "AIMessageChunk" and msg.get("content"):
                tokens += 1
                token_namespaces.add(part.event.partition("|")[2] or "root")
                nodes_seen.add(meta.get("langgraph_node", ""))
        elif mode == "updates" and isinstance(part.data, dict) and "__interrupt__" in part.data:
            interrupt_ids += [i["id"] for i in part.data["__interrupt__"]]
    assert kinds <= set(STREAM_KINDS) and {"evidence.found", "series", "hypothesis.update"} <= kinds, kinds
    assert tokens > 20, "narration must stream token by token"
    assert any(ns != "root" for ns in token_namespaces), "specialist subgraph tokens need stream_subgraphs=True"

    state = await client.threads.get_state(tid)
    pending = [i for t in state["tasks"] for i in t.get("interrupts", [])]
    assert pending, "the commander must stop for human approval"
    ids = {i["id"]: {"decision": "approve", "decided_by": "e2e"} for i in pending}
    done = await client.runs.wait(tid, "incident_commander", command={"resume": ids}, context=RUN_CTX)
    assert done["report"]["recovered"] is True or done["report"]["actions_executed"]


async def test_eval_run_started_from_the_api_scores_scenarios_and_reports_cases(dev_server):
    async with httpx.AsyncClient(base_url=dev_server, timeout=60) as h:
        started = await h.post(
            "/api/evals",
            json={"suite": "custom", "scenario_ids": ["S01", "S04"], "profile": "competent", "concurrency": 2},
        )
        assert started.status_code == 202, started.text
        run_id = started.json()["eval_run"]["eval_run_id"]

        async def finished() -> dict[str, Any] | None:
            row = (await h.get(f"/api/evals/{run_id}")).json()
            return row if row["status"] == "finished" else None

        run = await poll(finished, within=120, what="the eval run to finish")
        by_id = {c["scenario_id"]: c for c in run["cases"]}
        assert set(by_id) == {"S01", "S04"}
        for case in by_id.values():
            assert case["scores"] and isinstance(case["score"], float) and case["passed"] is True, case
        assert run["summary"]["gate"]["safety"] == 1.0
