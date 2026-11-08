"""memory_keeper: learn (new vs reinforce over a REAL InMemoryStore with the hashing embedder, the three MemoryDecision
outcomes, recall ordering, idempotent re-runs, lifecycle events) and consolidate (merge duplicates, boost, stale, the
delete-only interrupt). Offline and deterministic."""

from __future__ import annotations

import re
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command

from foghorn.common import lifecycle
from foghorn.contracts.interrupts import MemoryDecision
from foghorn.data import api as data_api
from foghorn.embeddings import aembed
from foghorn.flows.memory_keeper import scripts
from foghorn.flows.memory_keeper.build import build_graph
from foghorn.flows.memory_keeper.consolidate import confidence_target
from foghorn.flows.memory_keeper.helpers import lesson_key, normalise
from foghorn.memory import api as mem
from foghorn.models import offline as o

LESSON_A = "Database load is the symptom: look at the cache hit ratio first, and warm the cache as part of the revert"
LESSON_B = "Alert on the sessions-cache hit ratio and add jitter to every TTL"


def new_store() -> InMemoryStore:
    return InMemoryStore(index={"dims": 256, "embed": aembed, "fields": ["text"]})


@pytest.fixture
async def store():
    s = new_store()
    for it in data_api.memory_seed_items():  # the real seed memories: 30 lessons, 120 incidents, 14 prefs, 8 people
        await s.aput(tuple(it["namespace"]), it["key"], it["value"])
    return s


@pytest.fixture
def graph(store):
    return build_graph(checkpointer=InMemorySaver(), store=store)


def pm(
    incident_id="INC-5",
    *,
    component="sessions-cache",
    category="cache_stampede",
    lessons=(LESSON_A, LESSON_B),
    status="approved",
    **kw,
):
    return {
        "incident_id": incident_id,
        "severity": "SEV2",
        "title": "Session cache stampede",
        "component": component,
        "category": category,
        "status": status,
        "summary": "A cache stampede hit the sessions cache after a TTL change.",
        "root_cause": "The cache entries expired together after a TTL change.",
        "lessons_learned": list(lessons),
        "timeline": [{"kind": "action", "summary": "executed revert_config on sessions-cache"}],
        "window": {"start": "2026-11-04T11:00:00Z", "end": "2026-11-04T11:40:00Z"},
        **kw,
    }


def cfg(t: str = "m1") -> dict[str, Any]:
    return {"configurable": {"thread_id": t}}


def save(by: str = "pat") -> dict[str, Any]:
    return {"decision": "save", "decided_by": by}


async def learn(
    g: Any, inp: dict[str, Any], decisions: list[Any], *, thread: str = "m1", context: dict[str, Any] | None = None
):
    queue, seen = list(decisions), []
    out = await g.ainvoke({"mode": "learn", **inp}, cfg(thread), context=context, version="v2")
    while out.interrupts:
        seen.extend(out.interrupts)
        out = await g.ainvoke(Command(resume=queue.pop(0)), cfg(thread), context=context, version="v2")
    assert not queue, f"unused decisions {queue}"
    return out, seen


def uniq(events: list[dict[str, Any]], type_: str) -> list[dict[str, Any]]:
    return list({e["idempotency_key"]: e for e in events if e["type"] == type_}.values())  # what the bus keeps


# ====================================================================================================== learn: happy
async def test_learn_reinforces_the_near_duplicate_and_adds_the_new_lesson(graph, store):
    with lifecycle.capture() as events:
        out, seen = await learn(graph, {"incident_id": "INC-5", "postmortem": pm()}, [save("pat"), save("pat")])
    assert [i.value["kind"] for i in seen] == ["memory_write", "memory_write"]
    first, second = (i.value for i in seen)
    assert first["proposed"]["action"] == "reinforce" and first["proposed"]["key"] == "les-004"
    assert first["proposed"]["near_duplicate"]["score"] >= 0.6 and first["context"]["evidence_refs"] == [
        "memory:les-004"
    ]
    assert first["options"] == ["save", "edit", "discard"] and first["approval_id"] == "mem_INC-5_c0"
    assert second["proposed"]["action"] == "new" and second["proposed"]["near_duplicate"] is None
    assert second["proposed"]["namespace"] == ["lessons", "sessions-cache"] and second["proposed"]["key"] == lesson_key(
        "sessions-cache", LESSON_B
    )

    # reinforcement: confidence +0.05, version bump, provenance of the evidence; text unchanged
    seed = await store.aget(("lessons", "sessions-cache"), "les-004")
    assert seed is not None and seed.value["reinforced_by"] == ["INC-5"] and seed.value["version"] == 2
    assert seed.value["confidence"] == 0.65 and seed.value["text"].startswith("Database load is the symptom")
    # the new lesson, with provenance and indexed text
    new = await store.aget(("lessons", "sessions-cache"), lesson_key("sessions-cache", LESSON_B))
    assert new is not None and new.value["text"] == LESSON_B and new.value["from_incident"] == "INC-5"
    assert new.value["provenance"] == {
        "thread_id": "m1",
        "event_id": None,
        "incident_id": "INC-5",
        "written_by": "memory_keeper",
    }
    assert new.value["confidence"] == 0.7 and new.value["learned_from_at"] == "2026-11-04T11:40:00Z"
    # the incident summary
    inc = await store.aget(("incidents", "sessions-cache"), "inc-5")
    assert inc is not None and "Root cause" in inc.value["text"] and inc.value["lessons"] == ["les-004", new.key]

    res = out.value["result"]
    assert res["graph_id"] == "memory_keeper" and res["status"] == "ok"
    assert res["metrics"]["candidates"] == 2 and res["metrics"]["saved"] == 2 and res["metrics"]["written"] == 3
    assert [w["action"] for w in out.value["written"]] == ["reinforced", "added", "added"]

    # lifecycle events carry idempotency keys; repeated emissions (resume re-runs) collapse on them
    recalled = uniq(events, "memory.recalled")
    assert {e["idempotency_key"] for e in recalled} >= {"memory.recalled:INC-5:c0:les-004"}
    written = uniq(events, "memory.written")
    assert {e["idempotency_key"] for e in written} == {
        "memory.written:lessons/sessions-cache/les-004:v2",
        f"memory.written:lessons/sessions-cache/{new.key}:v1",
        "memory.written:incidents/sessions-cache/inc-5:v1",
    }
    assert all(
        e["payload"]["incident_id"] == "INC-5" and e["subject"]["component"] == "sessions-cache" for e in written
    )
    assert {"approval.requested", "approval.decided"} <= {e["type"] for e in events}


async def test_a_new_lesson_for_a_component_without_lessons_is_added_not_reinforced(graph, store):
    p = pm(
        "INC-6",
        component="checkout-service",
        category="bad_deploy",
        lessons=("Check the deploy time against the first error rate increase",),
    )
    out, seen = await learn(graph, {"incident_id": "INC-6", "postmortem": p}, [save()])
    assert (
        seen[0].value["proposed"]["action"] == "new"
    )  # similar seed lessons live in OTHER components: never reinforced across
    assert seen[0].value["proposed"]["namespace"] == ["lessons", "checkout-service"]
    assert (await store.aget(("lessons", "inventory-service"), "les-001")).value.get(
        "reinforced_by"
    ) is None  # untouched
    assert out.value["result"]["metrics"]["recalled"] >= 1  # but the related hit was recalled and reported


# ============================================================================================ the three decisions
@pytest.mark.parametrize(
    ("decision", "expect_text"),
    [
        ({"decision": "save", "decided_by": "a"}, LESSON_B),
        (
            {"decision": "edit", "decided_by": "b", "text": "Add TTL jitter and alert on the hit ratio."},
            "Add TTL jitter and alert on the hit ratio.",
        ),
        ({"decision": "edit", "decided_by": "b", "text": "   "}, LESSON_B),  # blank edit = keep the proposal
        ({"decision": "discard", "decided_by": "c"}, None),
    ],
)
async def test_memory_decision_outcomes_for_a_new_lesson(graph, store, decision, expect_text):
    p = pm(lessons=(LESSON_B,))
    out, seen = await learn(graph, {"incident_id": "INC-5", "postmortem": p}, [decision])
    key = lesson_key("sessions-cache", LESSON_B)
    item = await store.aget(("lessons", "sessions-cache"), key)
    if expect_text is None:
        assert item is None and out.value["result"]["metrics"]["discarded"] == 1
    else:
        assert (
            item is not None
            and item.value["text"] == expect_text
            and item.value["decided_by"] == decision["decided_by"]
        )
    # the incident summary is a factual record and is written either way
    assert await store.aget(("incidents", "sessions-cache"), "inc-5") is not None


async def test_edit_of_a_reinforcement_rewrites_the_existing_lesson_text(graph, store):
    edit = {
        "decision": "edit",
        "text": "Look at the cache hit ratio before the database; warm the cache while reverting.",
    }
    await learn(graph, {"incident_id": "INC-5", "postmortem": pm(lessons=(LESSON_A,))}, [edit])
    item = await store.aget(("lessons", "sessions-cache"), "les-004")
    assert (
        item is not None and item.value["text"].startswith("Look at the cache hit ratio") and item.value["version"] == 2
    )
    assert item.value["reinforced_by"] == ["INC-5"]


async def test_discarding_a_reinforcement_leaves_the_existing_lesson_untouched(graph, store):
    before = (await store.aget(("lessons", "sessions-cache"), "les-004")).value
    await learn(graph, {"incident_id": "INC-5", "postmortem": pm(lessons=(LESSON_A,))}, [{"decision": "discard"}])
    assert (await store.aget(("lessons", "sessions-cache"), "les-004")).value == before


async def test_invalid_decision_is_rejected_and_the_interrupt_stays_answerable(graph):
    out = await graph.ainvoke(
        {"mode": "learn", "incident_id": "INC-5", "postmortem": pm(lessons=(LESSON_B,))}, cfg("bad"), version="v2"
    )
    assert out.interrupts
    with pytest.raises(ValueError, match=r"(?i)decision"):
        await graph.ainvoke(Command(resume={"decision": "remember-forever"}), cfg("bad"), version="v2")
    out = await graph.ainvoke(Command(resume=save()), cfg("bad"), version="v2")
    assert out.value["result"]["status"] == "ok"
    MemoryDecision.model_validate(save())


# ================================================================================================ recall and search
async def test_recall_ordering_the_new_lesson_ranks_first_for_its_own_words_and_seeds_still_win_theirs(graph, store):
    await learn(graph, {"incident_id": "INC-5", "postmortem": pm()}, [save(), save()])
    key = lesson_key("sessions-cache", LESSON_B)
    hits = await mem.recall(
        store, "alert on the hit ratio and add jitter to the TTL", kinds=("lessons",), component="sessions-cache", k=3
    )
    assert hits[0]["key"] == key and hits[0]["score"] > hits[1]["score"]
    seed_hits = await mem.recall(store, "rotate logs first when the disk fills", kinds=("lessons",), k=3)
    assert seed_hits[0]["key"] == "les-015"
    # component-local lessons beat the global pool on ties
    local = await mem.recall(
        store, "warm the cache after a revert", kinds=("lessons",), component="sessions-cache", k=2
    )
    assert local[0]["ns"] == ["lessons", "sessions-cache"]


async def test_semantic_search_finds_the_new_lesson_through_the_store_api(graph, store):
    await learn(graph, {"incident_id": "INC-5", "postmortem": pm()}, [save(), save()])
    items = await store.asearch(("lessons",), query="jitter ttl hit ratio alert", limit=3)
    assert items[0].value["text"] == LESSON_B and items[0].score is not None and items[0].score > 0.35
    assert [i.key for i in await store.asearch(("incidents",), query="sessions cache stampede ttl", limit=2)][
        0
    ] == "inc-5"


# ================================================================================================== idempotency
async def test_learn_twice_is_idempotent_no_duplicates_no_double_reinforcement(graph, store):
    inp = {"incident_id": "INC-5", "postmortem": pm()}
    with lifecycle.capture() as events:
        await learn(graph, inp, [save(), save()], thread="first")
        snapshot = {(tuple(i.namespace), i.key): i.value for i in await store.asearch(("lessons",), limit=100)}
        out, _ = await learn(
            graph, inp, [save(), save()], thread="second"
        )  # same postmortem again (e.g. a replayed event)
    after = {(tuple(i.namespace), i.key): i.value for i in await store.asearch(("lessons",), limit=100)}
    assert len(after) == len(snapshot) == 31  # 30 seeds + 1 new lesson
    assert (
        after[(("lessons", "sessions-cache"), "les-004")] == snapshot[(("lessons", "sessions-cache"), "les-004")]
    )  # not bumped twice
    assert [w["action"] for w in out.value["written"] if w["kind"] == "lessons"] == [
        "unchanged",
        "unchanged",
    ]  # the second pass recognises its own lessons
    keys = [e["idempotency_key"] for e in events if e["type"] == "memory.written"]
    assert (
        len(set(keys)) == 3
    )  # lesson v2, new lesson v1, incident v1: the second pass can only repeat a key, never add one
    assert keys.count("memory.written:incidents/sessions-cache/inc-5:v1") == 2  # identical key: the bus keeps one
    assert len(await store.asearch(("incidents", "sessions-cache"), filter={"incident_id": "INC-5"}, limit=5)) == 1


# =============================================================================================== sources of the postmortem
async def test_postmortem_is_read_from_the_store_when_the_event_did_not_carry_it(graph, store):
    await store.aput(
        ("postmortems", "sessions-cache"), "pm-INC-5", {"text": "pm", "incident_id": "INC-5", "postmortem": pm()}
    )
    out, seen = await learn(graph, {"incident_id": "INC-5"}, [save(), save()])
    st = await graph.aget_state(cfg())
    assert st.values["source"] == "store" and len(seen) == 2 and out.value["result"]["status"] == "ok"


async def test_nothing_to_learn_is_a_skipped_run_not_a_failure(graph):
    out, seen = await learn(graph, {"incident_id": "INC-404"}, [])
    assert (
        seen == []
        and out.value["result"]["status"] == "skipped"
        and "nothing to learn" in out.value["result"]["summary"]
    )


async def test_no_store_degrades_to_no_memory_without_failing():
    g = build_graph(checkpointer=InMemorySaver())
    out, _ = await learn(g, {"incident_id": "INC-5", "postmortem": pm(lessons=(LESSON_B,))}, [save()])
    assert out.value["result"]["status"] == "ok" and out.value["written"] == []


# ================================================================================================== profiles + limits
async def test_profile_contrast_naive_proposes_a_platitude_competent_the_postmortems_lessons(graph):
    naive, seen_n = await learn(
        graph,
        {"incident_id": "INC-5", "postmortem": pm()},
        [{"decision": "discard"}],
        thread="n",
        context={"model_profile": "naive"},
    )
    assert (
        len(seen_n) == 1
        and seen_n[0].value["proposed"]["text"].startswith("Communicate early")
        and seen_n[0].value["proposed"]["confidence"] == 0.4
    )
    _, seen_c = await learn(graph, {"incident_id": "INC-5", "postmortem": pm()}, [save(), save()], thread="c")
    assert len(seen_c) == 2 and seen_c[0].value["proposed"]["text"] == LESSON_A


async def test_cautious_profile_drops_lessons_it_is_not_confident_about(graph):
    out, seen = await learn(
        graph, {"incident_id": "INC-5", "postmortem": pm(status="draft")}, [], context={"model_profile": "cautious"}
    )
    assert (
        seen == [] and out.value["result"]["metrics"]["candidates"] == 0
    )  # draft postmortem -> 0.6 < 0.7: nothing to curate
    assert await graph.checkpointer.aget_tuple(cfg()) is not None


@pytest.mark.parametrize(("limit", "expected"), [(1, 1), (2, 2), (5, 3)])
async def test_max_lessons_caps_the_candidates_and_duplicates_are_dropped(graph, limit, expected):
    lessons = (
        LESSON_A,
        LESSON_B,
        "Third lesson about jitter.",
        LESSON_A.upper() + "!!",
    )  # the last is a duplicate of the first
    out, seen = await learn(
        graph,
        {"incident_id": "INC-5", "postmortem": pm(lessons=lessons)},
        [{"decision": "discard"}] * expected,
        context={"max_lessons": limit},
    )
    assert len(seen) == expected and out.value["result"]["metrics"]["discarded"] == expected


# ================================================================================================== streaming
async def test_streaming_updates_custom_events_and_resume_keys(graph):
    inp = {"mode": "learn", "incident_id": "INC-5", "postmortem": pm()}
    first = [(m, d) async for m, d in graph.astream(inp, cfg("s"), stream_mode=["updates", "custom"])]
    order = [next(iter(d)) for m, d in first if m == "updates" and "__interrupt__" not in d]
    assert order == ["load_source", "distill", "dedupe"]
    custom = [d for m, d in first if m == "custom"]
    kinds = [c["kind"] for c in custom]
    assert "memory.recalled" in kinds and "progress" in kinds
    rec = next(c for c in custom if c["kind"] == "memory.recalled")
    assert (
        rec["data"]["key"] == "les-004"
        and rec["data"]["ns"] == ["lessons", "sessions-cache"]
        and rec["data"]["score"] >= 0.6
    )
    assert all(c["v"] == 1 and c["key"] for c in custom)

    after: list[dict[str, Any]] = []
    for resume in (save(), save()):
        after += [
            d async for m, d in graph.astream(Command(resume=resume), cfg("s"), stream_mode=["custom"]) if m == "custom"
        ]
    written = [c for c in after if c["kind"] == "memory.written"]
    assert {(tuple(c["data"]["ns"]), c["data"]["key"]) for c in written} >= {(("lessons", "sessions-cache"), "les-004")}
    assert len({c["key"] for c in written}) == len(written)  # deterministic keys, one per item version


async def test_distiller_tokens_are_never_streamed_to_the_user(graph):
    chunks = [
        d
        async for m, d in graph.astream(
            {"mode": "learn", "incident_id": "INC-5", "postmortem": pm()}, cfg("t"), stream_mode=["messages"]
        )
    ]
    assert chunks == []  # the structured call is tagged `nostream`


# =================================================================================================== failure injection
def _flaky_distiller(times: int, exc: BaseException) -> o.Script:
    base = scripts.distiller_script()
    s = o.Script()
    s.when("flaky", lambda c: c.schema_name == "Distillation", o.flaky(times, exc, lambda c: base.decide(c)[1]))
    return s


async def test_distiller_timeouts_are_retried_by_the_node_policy(graph):
    script = _flaky_distiller(2, TimeoutError("llm timeout"))
    o.SCRIPT_OVERRIDES["smart"] = script
    out, _ = await learn(graph, {"incident_id": "INC-5", "postmortem": pm(lessons=(LESSON_B,))}, [save()])
    assert out.value["result"]["status"] == "ok" and len(script.calls) == 3


async def test_exhausted_retries_and_non_retryable_errors(graph):
    o.SCRIPT_OVERRIDES["smart"] = _flaky_distiller(9, ConnectionError("provider down"))
    with pytest.raises(ConnectionError):
        await graph.ainvoke({"mode": "learn", "incident_id": "INC-5", "postmortem": pm()}, cfg("x"))
    script = _flaky_distiller(9, ValueError("bad schema"))
    o.SCRIPT_OVERRIDES["smart"] = script
    with pytest.raises(ValueError, match="bad schema"):
        await graph.ainvoke({"mode": "learn", "incident_id": "INC-5", "postmortem": pm()}, cfg("y"))
    assert len(script.calls) == 1


# ======================================================================================================= consolidate
DUP_1 = "Warm the cache as part of the revert, because reverting alone leaves it cold"
DUP_2 = "Warm the cache as part of the revert because reverting alone leaves it cold"
DUP_3 = "warm the cache as part of the revert; reverting alone leaves it cold"


async def lesson(store: InMemoryStore, comp: str, key: str, text: str, **extra: Any) -> None:
    await store.aput(
        ("lessons", comp),
        key,
        {"text": text, "confidence": 0.6, "source": "postmortem", "from_incident": f"INC-{key}", **extra},
    )


async def consolidate(
    g: Any, decisions: list[Any] | None = None, *, thread: str = "c1", context: dict[str, Any] | None = None
):
    out = await g.ainvoke({"mode": "consolidate"}, cfg(thread), context=context, version="v2")
    seen = []
    for d in decisions or []:
        seen.extend(out.interrupts)
        out = await g.ainvoke(Command(resume=d), cfg(thread), context=context, version="v2")
    seen.extend(out.interrupts)
    return out, seen


async def test_consolidate_merges_semantic_duplicates_after_a_delete_confirmation():
    store = new_store()
    await lesson(store, "sessions-cache", "a", DUP_1, confidence=0.7, reinforced_by=["INC-9"])
    await lesson(store, "sessions-cache", "b", DUP_2, confidence=0.6, reinforced_by=["INC-8"])
    await lesson(store, "sessions-cache", "c", DUP_3, confidence=0.6)
    await lesson(store, "sessions-cache", "other", "Alert on certificate expiry fourteen days ahead")
    await lesson(store, "orders-db", "same-text-elsewhere", DUP_1)  # other component: never merged across namespaces
    g = build_graph(checkpointer=InMemorySaver(), store=store)
    with lifecycle.capture() as events:
        out, seen = await consolidate(g, [save()])
    assert (
        len(seen) == 1 and seen[0].value["kind"] == "memory_write" and seen[0].value["options"] == ["save", "discard"]
    )
    (cl,) = seen[0].value["proposed"]["merges"]
    assert cl["keep"] == "a" and cl["drop"] == ["b", "c"] and cl["ns"] == ["lessons", "sessions-cache"]
    assert seen[0].value["reversible"] is False

    assert (
        await store.aget(("lessons", "sessions-cache"), "b") is None
        and await store.aget(("lessons", "sessions-cache"), "c") is None
    )
    kept = await store.aget(("lessons", "sessions-cache"), "a")
    assert (
        kept is not None
        and kept.value["merged_from"] == ["b", "c"]
        and kept.value["reinforced_by"] == ["INC-8", "INC-9"]
    )
    assert kept.value["confidence"] > 0.7 and kept.value["version"] == 2
    assert await store.aget(("lessons", "orders-db"), "same-text-elsewhere") is not None
    assert await store.aget(("lessons", "sessions-cache"), "other") is not None
    r = out.value["report"]
    assert (
        r["merged_groups"] == 1
        and r["deleted"] == 2
        and r["merge_decision"] == "save"
        and out.value["result"]["status"] == "ok"
    )
    assert [e["payload"]["action"] for e in uniq(events, "memory.written")] == ["merged"]
    rep = await store.aget(("reports", "consolidation"), r["report_key"])
    assert (
        rep is not None
        and rep.value["text"].startswith("Memory consolidation")
        and rep.value["provenance"]["written_by"] == "memory_keeper"
    )


async def test_consolidate_declined_merge_keeps_everything_and_only_marks_duplicates():
    store = new_store()
    await lesson(store, "sessions-cache", "a", DUP_1, confidence=0.7)
    await lesson(store, "sessions-cache", "b", DUP_2)
    g = build_graph(checkpointer=InMemorySaver(), store=store)
    out, seen = await consolidate(g, [{"decision": "discard"}])
    assert len(seen) == 1 and out.value["report"]["deleted"] == 0 and out.value["report"]["duplicates_marked"] == 1
    assert (await store.aget(("lessons", "sessions-cache"), "b")).value["duplicate_of"] == "a"
    assert (await store.aget(("lessons", "sessions-cache"), "a")).value.get("merged_from") is None


async def test_consolidate_without_deletions_never_interrupts_boosts_confidence_and_flags_stale():
    store = new_store()
    await lesson(
        store,
        "orders-db",
        "hot",
        "Kill the long query before any restart of the primary",
        reinforced_by=["INC-7", "INC-8"],
        last_seen="2026-11-01T00:00:00Z",
    )
    await lesson(store, "orders-db", "old", "Always build indexes concurrently", last_seen="2024-01-01T00:00:00Z")
    await lesson(
        store, "orders-db", "fresh", "Check the change feed for maintenance windows", last_seen="2026-10-01T00:00:00Z"
    )
    g = build_graph(checkpointer=InMemorySaver(), store=store)
    out, seen = await consolidate(g, context={"stale_days": 365})
    assert seen == []  # nothing is deleted -> no human needed
    r = out.value["report"]
    assert r["boosted"] == 1 and r["stale_flagged"] == 1 and r["merged_groups"] == 0
    hot = (await store.aget(("lessons", "orders-db"), "hot")).value
    assert hot["confidence"] == confidence_target(hot) == 0.8 and hot["version"] == 2
    old = (await store.aget(("lessons", "orders-db"), "old")).value
    assert (
        old["stale"] is True
        and old["stale_since"] == "2024-01-01T00:00:00Z"
        and "stale" not in (await store.aget(("lessons", "orders-db"), "fresh")).value
    )


async def test_consolidate_is_idempotent_the_second_run_changes_nothing():
    store = new_store()
    await lesson(
        store, "sessions-cache", "a", DUP_1, confidence=0.7, reinforced_by=["INC-9"], last_seen="2026-11-01T00:00:00Z"
    )
    await lesson(store, "sessions-cache", "b", DUP_2)
    await lesson(
        store, "sessions-cache", "old", "Prefer feature flags over config deploys", last_seen="2024-01-01T00:00:00Z"
    )
    g = build_graph(checkpointer=InMemorySaver(), store=store)
    await consolidate(g, [save()], thread="first")
    before = {(tuple(i.namespace), i.key): i.value for i in await store.asearch(("lessons",), limit=100)}
    with lifecycle.capture() as events:
        out, seen = await consolidate(g, thread="second")
    after = {(tuple(i.namespace), i.key): i.value for i in await store.asearch(("lessons",), limit=100)}
    assert seen == [] and before == after
    r = out.value["report"]
    assert (r["merged_groups"], r["deleted"], r["boosted"], r["stale_flagged"]) == (0, 0, 0, 0)
    assert uniq(events, "memory.written") == []


async def test_consolidate_over_the_real_seed_lessons_finds_no_false_duplicates(store, graph):
    out, seen = await consolidate(graph)
    assert seen == [] and out.value["report"]["scanned"] == 30 and out.value["report"]["duplicate_groups"] == 0
    assert out.value["report"]["stale_flagged"] > 0  # the oldest seed lessons come from 2025 incidents
    again, _ = await consolidate(graph, thread="again")
    assert again.value["report"]["stale_flagged"] == 0


async def test_mode_is_inferred_when_missing(graph):
    out = await graph.ainvoke({}, cfg("auto1"), version="v2")  # no incident, no postmortem -> consolidate
    assert out.value["result"]["summary"].startswith("Consolidated")
    out = await graph.ainvoke(
        {"incident_id": "INC-404"}, cfg("auto2"), version="v2"
    )  # an incident id -> learn (skipped: unknown)
    assert out.value["result"]["status"] == "skipped"


# ===================================================================================================== pure + contract
def test_lesson_keys_are_stable_and_text_normalised():
    assert lesson_key("c", "Warm the CACHE!") == lesson_key("c", "warm the cache") != lesson_key("d", "warm the cache")
    assert normalise("  A,  b?\n c ") == "a b c"
    assert re.fullmatch(r"les-[0-9a-f]{10}", lesson_key(None, "x"))


def test_distiller_script_envelope_and_profiles():
    msg = scripts.pack(pm(), 2)
    assert scripts.unpack(msg)[1] == 2 and scripts.unpack(msg)[0]["incident_id"] == "INC-5"
    d = scripts.distill(pm(lessons=()), 3, "competent")
    assert len(d.lessons) == 1 and "Check this first" in d.lessons[0].text  # fallback from the root cause
    assert scripts.distill(pm(status="edited"), 3, "competent").lessons[0].confidence == 0.8
    assert "Mitigation: executed revert_config" in scripts.incident_summary(pm())


def test_studio_shape_context_prompt_and_schemas(graph):
    g = graph.get_graph()
    names = {n for n in g.nodes if not n.startswith("__")}
    assert names == {
        "load_source",
        "distill",
        "dedupe",
        "curate",
        "write",
        "scan",
        "cluster",
        "confirm_merge",
        "apply",
        "finish",
    }
    edges = {(e.source, e.target) for e in g.edges}
    assert {
        ("__start__", "load_source"),
        ("__start__", "scan"),
        ("cluster", "confirm_merge"),
        ("cluster", "apply"),
        ("load_source", "finish"),
    } <= edges
    ctx = graph.get_context_jsonschema()["properties"]["lesson_distiller_prompt"]
    assert ctx["langgraph_type"] == "prompt" and ctx["langgraph_nodes"] == ["distill"]
    assert {"mode", "incident_id", "scenario_id", "postmortem"} <= set(graph.get_input_jsonschema()["properties"])
    assert {"result", "written", "report"} == set(graph.get_output_jsonschema()["properties"])
    assert graph.name == "memory_keeper"


def test_guide_parses_like_the_server_and_sample_inputs_fit_the_input_schema(graph):
    from foghorn.server.catalog import guide_entry

    entry, warning = guide_entry("memory_keeper")
    assert warning is None and entry["has_guide"] and entry["graph_id"] == "memory_keeper"
    assert len(entry["studio_steps"]) >= 5 and len(entry["sample_inputs"]) >= 3 and entry["features"]
    for sample in entry["sample_inputs"]:
        graph.get_input_schema().model_validate(sample["input"])  # a sample that does not fit would 422 in Studio
