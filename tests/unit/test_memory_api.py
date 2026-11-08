"""The shared memory layer (semantic recall, upserts, idempotent effect claims) over a real InMemoryStore."""

from __future__ import annotations

import pytest
from langgraph.store.memory import InMemoryStore

from foghorn.embeddings import aembed
from foghorn.memory import api as mem


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore(index={"dims": 256, "embed": aembed, "fields": ["text"]})


async def _seed(store: InMemoryStore) -> None:
    await mem.remember(
        store,
        "lessons",
        "les-1",
        {"text": "roll back only the component that changed after a bad deploy"},
        component="checkout-service",
    )
    await mem.remember(
        store,
        "lessons",
        "les-2",
        {"text": "expand the volume before the disk fills up on the database"},
        component="orders-db",
    )
    await mem.remember(
        store,
        "incidents",
        "inc-1",
        {"text": "connection pool exhausted on payments after slow queries"},
        component="payments-service",
    )
    await mem.remember(
        store,
        "prefs",
        "pref-1",
        {"text": "commerce on-call wants a status update drafted for every SEV2 checkout incident"},
    )


async def test_recall_ranks_the_relevant_memory_first(store):
    await _seed(store)
    hits = await mem.recall(store, "roll back the deploy that broke checkout", k=3)
    assert hits and hits[0]["key"] == "les-1"
    assert hits[0]["text"].startswith("roll back") and hits[0]["ns"] == ["lessons", "checkout-service"]
    assert mem.memory_ref(hits[0]) == "memory:les-1"


async def test_component_local_memory_gets_priority_and_kinds_filter(store):
    await _seed(store)
    only_incidents = await mem.recall(
        store, "connection pool exhausted", kinds=("incidents",), component="payments-service"
    )
    assert [h["key"] for h in only_incidents] == ["inc-1"]
    lessons = await mem.recall(store, "connection pool exhausted", kinds=("lessons",), component="payments-service")
    assert all(h["ns"][0] == "lessons" for h in lessons)


async def test_remember_requires_indexed_text_and_is_an_upsert(store):
    with pytest.raises(ValueError):
        await mem.remember(store, "lessons", "bad", {"note": "no text"})
    await mem.remember(store, "lessons", "k", {"text": "first"}, component="c")
    await mem.remember(store, "lessons", "k", {"text": "second"}, component="c")
    item = await store.aget(("lessons", "c"), "k")
    assert item is not None and item.value["text"] == "second"


async def test_get_prefs_and_forget(store):
    await _seed(store)
    prefs = await mem.get_prefs(store)
    assert [p["key"] for p in prefs] == ["pref-1"]
    await mem.forget(store, ["prefs"], "pref-1")
    assert await mem.get_prefs(store) == []


async def test_claim_effect_is_idempotent(store):
    assert await mem.claim_effect(store, "rollback:INC-1:checkout-service") is True
    assert await mem.claim_effect(store, "rollback:INC-1:checkout-service") is False
    assert await mem.claim_effect(store, "rollback:INC-2:checkout-service") is True


async def test_no_store_degrades_gracefully():
    assert await mem.recall(None, "anything") == []
    assert await mem.remember(None, "lessons", "k", {"text": "x"}) is False
    assert await mem.claim_effect(None, "k") is True
    assert await mem.get_prefs(None) == []
