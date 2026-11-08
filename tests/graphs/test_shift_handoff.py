"""shift_handoff: deterministic, durable, no LLM. Happy path, crash-once-then-recover from the last checkpoint (for every
node), idempotent re-runs, the rendering golden, `tasks`/`checkpoints`/`updates` streams, fork before `publish`,
static breakpoints and the SDK-backed source. Offline and fast."""

from __future__ import annotations

from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from langgraph.types import Overwrite

from foghorn.common import lifecycle
from foghorn.contracts.inputs import ShiftHandoffInput
from foghorn.embeddings import aembed
from foghorn.flows.shift_handoff import metrics as m
from foghorn.flows.shift_handoff import nodes, ports
from foghorn.flows.shift_handoff.build import build_graph
from foghorn.flows.shift_handoff.render import render_handoff
from foghorn.flows.shift_handoff.state import NODES, append_audit

SHIFT_END = "2026-11-05T16:00:00Z"


# --------------------------------------------------------------------------------------------------- the fake world
class FakeShift:
    """Stand-in for the SDK client + data layer behind the `ShiftSource` port."""

    def __init__(
        self, threads: list[dict[str, Any]], interrupted: list[dict[str, Any]], alerts: list[dict[str, Any]]
    ) -> None:
        self.threads, self.interrupted, self.alerts = threads, interrupted, alerts
        self.open_times = {"S05": "2026-11-05T15:00:00Z"}
        self.fail: set[str] = set()

    async def incident_threads(self) -> list[dict[str, Any]]:
        if "threads" in self.fail:
            raise ConnectionError("agent server restarting")
        return self.threads

    async def interrupted_threads(self) -> list[dict[str, Any]]:
        if "interrupted" in self.fail:
            raise ConnectionError("agent server restarting")
        return self.interrupted

    async def alert_history(self, start: str, end: str) -> list[dict[str, Any]]:
        if "alerts" in self.fail:
            raise OSError("telemetry store offline")
        return [a for a in self.alerts if start < a["t"] <= end]

    async def scenario_open_time(self, scenario_id: str) -> str | None:
        return self.open_times.get(scenario_id)

    async def default_shift_end(self) -> str:
        return SHIFT_END


def thread(iid, sev, status, comp, *, run="idle", sid="S01", opened=None, report=None, alerts=True):
    vals: dict[str, Any] = {
        "incident": {"id": iid, "severity": sev, "status": status, "title": f"{comp} trouble"},
        "scenario_id": sid,
    }
    if opened:
        vals["opened_at"] = opened
    if report:
        vals["report"] = report
    if alerts:
        vals["alerts"] = [{"type": "alert.fired", "subject": {"component": comp}}]
    return {
        "thread_id": f"th-{iid}",
        "status": run,
        "metadata": {"foghorn_kind": "incident", "foghorn_incident_id": iid, "foghorn_scenario_id": sid},
        "values": vals,
        "interrupts": {},
    }


def interrupted(approval_id, iid, *, kind="approval", risk="high"):
    return {
        "thread_id": f"th-{iid}",
        "status": "interrupted",
        "metadata": {"foghorn_incident_id": iid, "foghorn_graph": "incident_commander"},
        "values": {},
        "interrupts": {
            "task1": [
                {
                    "id": "i1",
                    "value": {
                        "v": 1,
                        "kind": kind,
                        "approval_id": approval_id,
                        "title": "Roll back checkout",
                        "risk": risk,
                        "context": {"incident_id": iid},
                    },
                }
            ]
        },
    }


def alert(id_, t, comp, sev, rule="error_rate_high"):
    return {"id": id_, "t": t, "component": comp, "severity": sev, "rule": rule, "origin": "S01"}


RESOLVED_REPORT = {
    "scenario_id": "S10",
    "incident_id": "INC-2",
    "severity": "SEV2",
    "recovered": True,
    "root_cause": {"component": "pricing-service", "category": "config_change", "confidence": 0.8, "evidence_refs": []},
    "actions_executed": [
        {
            "action": "revert_flag",
            "target": "new_pricing_engine",
            "t_virtual": "2026-11-05T13:40:00Z",
            "outcome": "executed",
        }
    ],
    "abstained": False,
}


def world() -> FakeShift:
    return FakeShift(
        [
            thread("INC-1", "SEV1", "open", "checkout-service", opened="2026-11-05T14:06:30Z"),
            thread(
                "INC-2",
                "SEV2",
                "resolved",
                "pricing-service",
                sid="S10",
                opened="2026-11-05T13:20:00Z",
                report=RESOLVED_REPORT,
                alerts=False,
            ),
            thread("INC-3", "SEV3", "mitigating", "orders-db", run="error", sid="S05"),  # opened_at from the data layer
        ],
        [interrupted("apr_INC-1_1", "INC-1"), interrupted("apr_INC-2_9", "INC-2", kind="edit", risk="low")],
        [
            alert("evt_a", "2026-11-05T14:06:00Z", "checkout-service", "high"),
            alert("evt_b", "2026-11-05T14:07:00Z", "checkout-service", "medium", "p95"),
            alert("evt_c", "2026-11-05T09:00:00Z", "search-service", "high", "old"),
            alert("evt_d", "2026-11-05T15:01:00Z", "cart-service", "critical"),
            alert("evt_e", "2026-11-05T15:02:00Z", "cart-service", "high"),
            alert("evt_old", "2026-11-04T09:00:00Z", "search-service", "high"),  # before the window
        ],
    )


@pytest.fixture
def fake():
    w = world()
    ports.set_source_factory(lambda: w)
    nodes._LOCAL_CRASHES.clear()
    yield w
    ports.set_source_factory(None)


@pytest.fixture
def store():
    return InMemoryStore(index={"dims": 256, "embed": aembed, "fields": ["text"]})


@pytest.fixture
def graph(store, fake):
    return build_graph(checkpointer=InMemorySaver(), store=store)


def cfg(thread_id: str = "h1") -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}}


INPUT: ShiftHandoffInput = {"shift_end": SHIFT_END, "window_hours": 8}

GOLDEN = """# Shift handoff - 2026-11-05T16:00:00Z

Window: 2026-11-05T08:00:00Z -> 2026-11-05T16:00:00Z (8 h, virtual time, UTC). Incident source: sdk. Generated by `shift_handoff` (deterministic, no LLM).

## At a glance

| Open | Resolved | Pending approvals | Failed runs | Alerts | High alerts | MTTR (min) |
| --- | --- | --- | --- | --- | --- | --- |
| 2 | 1 | 2 | 1 | 5 | 4 | 20 |

Open by severity: SEV1=1, SEV2=0, SEV3=1, SEV4=0

## Open incidents

| Incident | Sev | Status | Component | Run | Title |
| --- | --- | --- | --- | --- | --- |
| INC-1 | SEV1 | open | checkout-service | idle | checkout-service trouble |
| INC-3 | SEV3 | mitigating | orders-db | error | orders-db trouble |

## Resolved this shift

| Incident | Sev | Component | Opened | Resolved |
| --- | --- | --- | --- | --- |
| INC-2 | SEV2 | pricing-service | 2026-11-05T13:20:00Z | 2026-11-05T13:40:00Z |

## Pending approvals

| Approval | Kind | Risk | Incident | Title |
| --- | --- | --- | --- | --- |
| apr_INC-1_1 | approval | high | INC-1 | Roll back checkout |
| apr_INC-2_9 | edit | low | INC-2 | Roll back checkout |

## Findings

- [warn] edit approval apr_INC-2_9 is still pending but INC-2 is resolved
- [alert] SEV1 INC-1 has been open for 113.5 minutes
- [alert] the commander run for INC-3 failed: resume it from its last checkpoint
- [info] 2 high-severity alerts on cart-service have no tracked incident

## Alert activity

| Component | Alerts | High/critical | First | Last |
| --- | --- | --- | --- | --- |
| cart-service | 2 | 2 | 2026-11-05T15:01:00Z | 2026-11-05T15:02:00Z |
| checkout-service | 2 | 1 | 2026-11-05T14:06:00Z | 2026-11-05T14:07:00Z |
| search-service | 1 | 1 | 2026-11-05T09:00:00Z | 2026-11-05T09:00:00Z |

## Run log

- collect_incidents: 3 incident(s) from sdk; 5 alert(s) in 2026-11-05T08:00:00Z -> 2026-11-05T16:00:00Z
- collect_approvals: 2 pending approval(s)
- compute_metrics: 2 open, 1 resolved, 5 alerts
- reconcile: 4 finding(s)
- render_report: markdown rendered
"""


# ================================================================================================= happy path + golden
async def test_happy_path_collects_computes_reconciles_renders_and_publishes(graph, store):
    with lifecycle.capture() as events:
        out = await graph.ainvoke(INPUT, cfg())
    assert set(out) == {"result", "handoff", "report_md"}  # the output schema, not the whole state
    assert out["report_md"] == GOLDEN  # rendering golden
    res = out["result"]
    assert res["graph_id"] == "shift_handoff" and res["status"] == "ok" and res["artifacts"][0]["kind"] == "report"
    assert (
        res["metrics"]["incidents_open"] == 2.0
        and res["metrics"]["approvals_pending"] == 2.0
        and res["metrics"]["mttr_min_mean"] == 20.0
    )
    assert res["artifacts"][0]["ref"] == "store:handoffs/handoff-20261105-160000-8h"

    item = await store.aget(("handoffs",), "handoff-20261105-160000-8h")
    assert item is not None and item.value["text"] == GOLDEN and item.value["open_incidents"] == ["INC-1", "INC-3"]
    assert item.value["provenance"]["written_by"] == "shift_handoff"
    hits = await store.asearch(("handoffs",), query="open incidents checkout pending approvals", limit=1)
    assert hits and hits[0].key == item.key  # the handoff is searchable text

    (ev,) = [e for e in events if e["type"] == "handoff.published"]
    assert ev["idempotency_key"] == "handoff.published:handoff-20261105-160000-8h"
    assert ev["payload"]["open_incidents"] == ["INC-1", "INC-3"] and ev["payload"]["metrics"]["alerts_total"] == 5.0
    assert out["handoff"]["executions"] == dict.fromkeys(NODES, 1)


async def test_state_is_flat_and_meaningful_after_every_node(graph):
    await graph.ainvoke(INPUT, cfg())
    history = [s async for s in graph.aget_state_history(cfg())]
    by_next = {s.next: s.values for s in history}
    assert [i["incident_id"] for i in by_next[("collect_approvals",)]["incidents"]] == ["INC-1", "INC-2", "INC-3"]
    assert "approvals" in by_next[("compute_metrics",)] and "metrics" in by_next[("reconcile",)]
    assert "findings" in by_next[("render_report",)] and "report_md" in by_next[("publish",)]
    assert all(
        isinstance(v, str | int | float | list | dict | type(None)) for v in history[0].values.values()
    )  # plain JSON


async def test_window_semantics_open_always_resolved_only_if_in_window(graph, fake):
    fake.threads.append(
        thread(
            "INC-9",
            "SEV2",
            "resolved",
            "search-service",
            sid="S09",
            opened="2026-11-04T01:00:00Z",
            report={
                **RESOLVED_REPORT,
                "incident_id": "INC-9",
                "actions_executed": [
                    {"action": "x", "target": "y", "t_virtual": "2026-11-04T02:00:00Z", "outcome": "executed"}
                ],
            },
            alerts=False,
        )
    )
    out = await graph.ainvoke({"shift_end": SHIFT_END, "window_hours": 2}, cfg("w1"))  # 14:00 -> 16:00
    st = await graph.aget_state(cfg("w1"))
    ids = {i["incident_id"] for i in st.values["incidents"]}
    assert ids == {
        "INC-1",
        "INC-3",
    }  # INC-2 resolved 13:40 and INC-9 resolved yesterday are outside; open ones always shown
    assert out["result"]["metrics"]["alerts_total"] == 4.0  # 14:06, 14:07, 15:01, 15:02 only


# ============================================================================================= crash and recovery
@pytest.mark.parametrize("node", NODES)
async def test_crash_once_then_recover_from_the_last_checkpoint(graph, store, node):
    c = cfg(f"crash-{node}")
    with pytest.raises(nodes.SimulatedCrash, match=node):
        await graph.ainvoke({**INPUT, "crash_after": node}, c, durability="sync")
    st = await graph.aget_state(c)
    i = NODES.index(node)
    assert st.next == (node,)  # the checkpoint is right before the failed node
    assert [a["node"] for a in st.values["audit"]] == list(
        NODES[:i]
    )  # finished nodes are in the audit log, the failed one is not
    if node == "publish":  # the crash happens BEFORE the side effects: nothing half-published
        assert await store.asearch(("handoffs",), limit=5) == []

    out = await graph.ainvoke(None, c, durability="sync")  # resume from the checkpoint
    assert out["result"]["status"] == "ok" and out["report_md"] == GOLDEN
    # finished nodes were NOT redone; the crashed one ran twice (the crash + the retry)
    assert out["handoff"]["executions"] == {n: 2 if n == node else 1 for n in NODES}
    assert [a["node"] for a in (await graph.aget_state(c)).values["audit"]] == list(
        NODES
    )  # each node logged exactly once
    assert (await store.aget(("effects",), f"crash:crash-{node}:{node}")) is not None  # the crash is on record


async def test_a_crashed_node_does_not_crash_again_on_a_fork_or_second_resume(graph):
    c = cfg("crash-fork")
    with pytest.raises(nodes.SimulatedCrash):
        await graph.ainvoke({**INPUT, "crash_after": "reconcile"}, c, durability="sync")
    await graph.ainvoke(None, c, durability="sync")
    # fork from before `reconcile` on the same thread: the node runs again, and does not crash again
    before = next(s for s in [s async for s in graph.aget_state_history(c)] if s.next == ("reconcile",))
    out = await graph.ainvoke(None, before.config, durability="sync")
    assert out["result"]["status"] in ("ok", "duplicate")
    # a NEW thread with the same input crashes again (the record is per thread and node)
    with pytest.raises(nodes.SimulatedCrash):
        await graph.ainvoke({**INPUT, "crash_after": "reconcile"}, cfg("crash-new-thread"), durability="sync")


async def test_unknown_crash_after_names_are_ignored(graph):
    out = await graph.ainvoke({**INPUT, "crash_after": "no_such_node"}, cfg())
    assert out["result"]["status"] == "ok"


async def test_crash_without_a_store_still_crashes_only_once(fake):
    g = build_graph(checkpointer=InMemorySaver())  # no Store injected: the in-process fallback records the crash
    c = cfg("nostore")
    with pytest.raises(nodes.SimulatedCrash):
        await g.ainvoke({**INPUT, "crash_after": "compute_metrics"}, c)
    out = await g.ainvoke(None, c)
    assert out["result"]["status"] == "ok" and out["handoff"]["executions"] == {}  # no Store: nothing to count


# ================================================================================================== idempotency
async def test_rerun_is_idempotent_same_report_one_store_item_one_event_key(graph, store):
    with lifecycle.capture() as events:
        first = await graph.ainvoke(INPUT, cfg("a"))
        second = await graph.ainvoke(INPUT, cfg("b"))  # another thread, same shift
    assert first["report_md"] == second["report_md"]
    assert first["result"]["status"] == "ok" and second["result"]["status"] == "duplicate"
    assert len(await store.asearch(("handoffs",), limit=10)) == 1
    keys = [e["idempotency_key"] for e in events if e["type"] == "handoff.published"]
    assert len(keys) == 2 and len(set(keys)) == 1  # the bus drops the second delivery


async def test_every_node_is_idempotent_replaying_from_any_checkpoint_gives_the_same_report(graph):
    c = cfg("replay")
    out = await graph.ainvoke(INPUT, c)
    for snap in [s async for s in graph.aget_state_history(c)]:
        if not snap.next or snap.next == ("__start__",):
            continue
        replay = await graph.ainvoke(None, snap.config)
        assert replay["report_md"] == out["report_md"], f"replaying from before {snap.next} changed the report"


async def test_native_cron_input_without_shift_end_uses_the_default_window(graph):
    out = await graph.ainvoke({"window_hours": 8}, cfg("cron"))  # exactly what the seeded native cron sends
    assert (
        out["handoff"]["handoff_id"] == "handoff-20261105-160000-8h"
        and out["result"]["metrics"]["incidents_open"] == 2.0
    )


# ================================================================================================== resilience
async def test_failing_sources_degrade_the_report_but_never_the_run(graph, fake):
    fake.fail = {"threads", "interrupted", "alerts"}
    out = await graph.ainvoke(INPUT, cfg())
    assert out["result"]["status"] == "ok"
    md = out["report_md"]
    assert "incident threads unavailable (ConnectionError)" in md and "thread search unavailable" in md
    assert "alert history unavailable (OSError)" in md and "no incidents, approvals or alerts" in md
    assert "Incident source: data_layer" in md


async def test_quiet_shift_is_a_friendly_report_not_an_empty_one(store):
    ports.set_source_factory(lambda: FakeShift([], [], []))
    try:
        out = await build_graph(checkpointer=InMemorySaver(), store=store).ainvoke(INPUT, cfg())
    finally:
        ports.set_source_factory(None)
    assert "no incidents, approvals or alerts were found in the window" in out["report_md"]
    assert "_no alerts in the window_" in out["report_md"]


async def test_sdk_source_reads_threads_by_flat_metadata_and_interrupted_status_and_data_layer_history(store):
    calls: list[dict[str, Any]] = []

    class Threads:
        async def search(self, **kw: Any) -> list[dict[str, Any]]:
            calls.append(kw)
            if kw.get("status") == "interrupted":
                return [interrupted("apr_INC-1_1", "INC-1")]
            return [thread("INC-1", "SEV1", "open", "checkout-service", opened="2026-11-05T14:06:30Z")]

    class Client:
        threads = Threads()

    ports.set_source_factory(lambda: ports.SdkShiftSource(lambda: Client()))
    try:
        out = await build_graph(checkpointer=InMemorySaver(), store=store).ainvoke(
            {"shift_end": "2026-11-05T16:00:00Z", "window_hours": 8}, cfg()
        )
    finally:
        ports.set_source_factory(None)
    by_kind = next(c for c in calls if c.get("metadata"))
    assert by_kind["metadata"] == {"foghorn_kind": "incident"} and "values" in by_kind["select"]
    assert any(c.get("status") == "interrupted" for c in calls)
    assert out["result"]["metrics"]["incidents_open"] == 1.0 and out["result"]["metrics"]["approvals_pending"] == 1.0
    assert (
        out["result"]["metrics"]["alerts_total"] > 0
    )  # the REAL data layer: stream_workday alerts fall in that window


async def test_demo_is_never_empty_when_the_server_knows_nothing(store):
    class Threads:
        async def search(self, **_: Any) -> list[dict[str, Any]]:
            return []

    class Client:
        threads = Threads()

    ports.set_source_factory(lambda: ports.SdkShiftSource(lambda: Client()))
    try:
        out = await build_graph(checkpointer=InMemorySaver(), store=store).ainvoke({"window_hours": 8}, cfg())
    finally:
        ports.set_source_factory(None)
    # no shift_end: the data layer decides (latest scenario trigger rounded up to the hour) and supplies the alert history
    assert out["handoff"]["handoff_id"] == "handoff-20261107-110000-8h"
    assert out["result"]["metrics"]["alerts_total"] > 0 and "Incident source: data_layer" in out["report_md"]
    assert "Alert activity" in out["report_md"] and "_no alerts in the window_" not in out["report_md"]


# ===================================================================================================== streams
async def test_tasks_checkpoints_updates_and_custom_streams(graph):
    chunks: list[tuple[str, Any]] = []
    async for mode, data in graph.astream(
        INPUT, cfg("s1"), stream_mode=["tasks", "checkpoints", "updates", "custom"], durability="sync"
    ):
        chunks.append((mode, data))
    starts = [d["name"] for m_, d in chunks if m_ == "tasks" and "input" in d]
    finishes = [d for m_, d in chunks if m_ == "tasks" and "result" in d]
    assert starts == list(NODES) and [d["name"] for d in finishes] == list(NODES)
    assert all(d["error"] is None for d in finishes)
    updated = [next(iter(d)) for m_, d in chunks if m_ == "updates"]
    assert updated == list(NODES)
    cps = [d for m_, d in chunks if m_ == "checkpoints"]
    assert len(cps) == len(NODES) + 2  # the input checkpoint, the START superstep and one per node
    assert {c["config"]["configurable"]["checkpoint_id"] for c in cps} and cps[-1]["next"] == []
    assert all(c["metadata"]["step"] == i - 1 for i, c in enumerate(cps))
    custom = [d for m_, d in chunks if m_ == "custom"]
    assert {c["kind"] for c in custom} >= {"progress", "metric", "note", "artifact", "memory.written"}
    assert all(c["v"] == 1 and c["key"] for c in custom)
    assert [c for c in custom if c["kind"] == "artifact"][0]["data"]["kind"] == "text"  # field named `kind` survives


# ============================================================================== time travel, breakpoints, DeltaChannel
async def test_fork_before_publish_with_an_edited_report_publishes_the_edit(graph, store):
    c = cfg("fork")
    await graph.ainvoke(INPUT, c)
    snap = next(s for s in [s async for s in graph.aget_state_history(c)] if s.next == ("publish",))
    forked = await graph.aupdate_state(snap.config, {"report_md": "# EDITED BY THE ON-CALL\n"}, as_node="render_report")
    out = await graph.ainvoke(None, forked)
    assert out["report_md"].startswith("# EDITED BY THE ON-CALL")
    item = await store.aget(("handoffs",), "handoff-20261105-160000-8h")
    assert item is not None and item.value["text"].startswith(
        "# EDITED"
    )  # the fork overwrote the upsert; same business key
    heads = {
        s.config["configurable"]["checkpoint_id"] for s in [s async for s in graph.aget_state_history(c)] if not s.next
    }
    assert len(heads) == 2  # two terminal checkpoints: the original branch and the fork


async def test_static_breakpoint_before_publish_then_edit_state_then_continue(graph, store):
    c = cfg("bp")
    await graph.ainvoke(INPUT, c, interrupt_before=["publish"], durability="sync")
    st = await graph.aget_state(c)
    assert st.next == ("publish",) and "handoff" not in st.values  # paused: nothing published yet
    assert await store.asearch(("handoffs",), limit=5) == []
    await graph.aupdate_state(
        c, {"report_md": st.values["report_md"] + "\nReviewed by the duty manager.\n"}, as_node="render_report"
    )
    final = await graph.ainvoke(None, c, durability="sync")
    assert final["report_md"].endswith("Reviewed by the duty manager.\n")
    assert len(await store.asearch(("handoffs",), limit=5)) == 1


async def test_audit_log_is_reducer_backed_appends_and_overwrites(graph):
    c = cfg("audit")
    await graph.ainvoke(INPUT, c)
    hist = [s async for s in graph.aget_state_history(c)]  # newest first
    lengths = [len(s.values.get("audit") or []) for s in hist]
    assert lengths == sorted(lengths, reverse=True) and lengths[0] == len(NODES) and lengths[-1] == 0
    await graph.aupdate_state(c, {"audit": [{"node": "manual", "note": "added in Studio"}]}, as_node="publish")
    assert (await graph.aget_state(c)).values["audit"][-1] == {"node": "manual", "note": "added in Studio"}
    await graph.aupdate_state(c, {"audit": Overwrite([{"node": "only", "note": "replaced"}])}, as_node="publish")
    assert (await graph.aget_state(c)).values["audit"] == [{"node": "only", "note": "replaced"}]


def test_append_audit_is_batching_invariant():
    xs, ys = [{"n": 1}, [{"n": 2}]], [{"n": 3}]
    one = append_audit(append_audit(None, xs), ys)
    assert one == append_audit(None, xs + ys) == [{"n": 1}, {"n": 2}, {"n": 3}]
    assert append_audit([{"n": 0}], [Overwrite([{"n": 9}])]) == [{"n": 9}]


# ================================================================================================ pure pieces
def test_metrics_mttr_ages_and_severity_split():
    incs = [
        {
            "incident_id": "A",
            "severity": "SEV1",
            "status": "open",
            "run_status": "idle",
            "opened_at": "2026-11-05T14:00:00Z",
            "resolved_at": None,
        },
        {
            "incident_id": "B",
            "severity": "SEV2",
            "status": "resolved",
            "run_status": "idle",
            "opened_at": "2026-11-05T10:00:00Z",
            "resolved_at": "2026-11-05T10:30:00Z",
        },
        {
            "incident_id": "C",
            "severity": "SEV2",
            "status": "resolved",
            "run_status": "error",
            "opened_at": "2026-11-05T11:00:00Z",
            "resolved_at": "2026-11-05T11:10:00Z",
        },
        {
            "incident_id": "D",
            "severity": "SEV3",
            "status": "resolved",
            "run_status": "idle",
            "opened_at": "2026-11-05T12:00:00Z",
            "resolved_at": "2026-11-05T11:00:00Z",
        },  # nonsense: ignored
    ]
    out = m.compute_metrics(incs, [], [], shift_end=SHIFT_END, window_hours=4)
    assert (
        out["incidents_open"] == 1
        and out["incidents_resolved"] == 3
        and out["open_sev1"] == 1
        and out["open_sev2"] == 0
    )
    assert (
        out["mttr_min_mean"] == 20.0
        and out["mttr_min_max"] == 30.0
        and out["oldest_open_min"] == 120.0
        and out["runs_failed"] == 1
    )
    assert out["alerts_per_hour"] == 0.0


def test_thread_parsing_is_tolerant():
    assert m.incident_from_thread({"thread_id": "x"}) is None  # unidentifiable
    inc = m.incident_from_thread(
        {
            "thread_id": "t",
            "metadata": {"foghorn_incident_id": "INC-5"},
            "values": {
                "report": {
                    "recovered": True,
                    "severity": "SEV2",
                    "actions_executed": [{"t_virtual": "2026-11-05T10:00:00Z"}],
                }
            },
        }
    )
    assert (
        inc is not None
        and inc["status"] == "resolved"
        and inc["severity"] == "SEV2"
        and inc["resolved_at"] == "2026-11-05T10:00:00Z"
    )
    flat = m.approval_from_thread(
        {
            "thread_id": "t",
            "metadata": {},
            "interrupts": [{"id": "z", "value": {"kind": "question", "title": "q"}}, {"id": "bad", "value": "str"}],
        }
    )
    assert len(flat) == 1 and flat[0]["kind"] == "question" and flat[0]["approval_id"] == "z"
    assert m.approval_from_thread({"thread_id": "t", "interrupts": {}}) == []


def test_render_handles_an_empty_state():
    md = render_handoff(
        {"shift_end": SHIFT_END, "window_start": "2026-11-05T08:00:00Z", "window_hours": 8, "source": "data_layer"}
    )
    assert md.count("_none_") >= 4 and "n/a" in md


# ================================================================================================= contract
def test_studio_shape_and_schemas(graph):
    g = graph.get_graph()
    assert [n for n in g.nodes if not n.startswith("__")] == list(NODES)
    assert sorted((e.source, e.target) for e in g.edges) == sorted(
        [("__start__", NODES[0]), *zip(NODES, NODES[1:], strict=False), (NODES[-1], "__end__")]
    )
    assert set(graph.get_input_jsonschema()["properties"]) == {"shift_end", "window_hours", "crash_after"}
    assert set(graph.get_output_jsonschema()["properties"]) == {"result", "handoff", "report_md"}
    assert graph.name == "shift_handoff"
    assert {"default_window_hours", "stale_sev1_minutes", "model_profile"} <= set(
        graph.get_context_jsonschema()["properties"]
    )


def test_guide_parses_like_the_server_and_sample_inputs_fit_the_input_schema(graph):
    from foghorn.server.catalog import guide_entry

    entry, warning = guide_entry("shift_handoff")
    assert warning is None and entry["has_guide"] and entry["graph_id"] == "shift_handoff"
    assert len(entry["studio_steps"]) >= 5 and len(entry["sample_inputs"]) >= 3 and entry["features"]
    for sample in entry["sample_inputs"]:
        graph.get_input_schema().model_validate(sample["input"])  # a sample that does not fit would 422 in Studio
