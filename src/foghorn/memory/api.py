"""Long-term memory: one thin, typed layer over the LangGraph ``BaseStore`` for every graph that remembers.

Namespaces (the seeder in ``foghorn.events.seeding`` writes the first three; ``item.value["text"]`` is the indexed
field - ``langgraph.json`` ``store.index.fields == ["text"]``):

    ("incidents", <component>)   past incident summaries (semantic recall)
    ("lessons",   <component>)   distilled lessons ("roll back only the component that changed")
    ("prefs",     <team>)        team preferences and rules ("status update drafted for every SEV2 checkout incident")
    ("people",)                  responders and roles
    ("postmortems", <component>) published postmortems
    ("ledger", "events")         router's event ledger (dedupe / correlation)       - owned by event_router
    ("effects",)                 idempotency records for side effects               - owned by whoever acts

All functions are ``async`` (the Store API is async-first; call them from ``async def`` nodes). ``store=None`` (unit
tests, a graph run without a store) degrades to "no memory" instead of failing: ``recall`` returns ``[]``,
``remember`` returns ``False``. Never put untrusted free text in a namespace element (docs/design/02 section 4.3).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, TypedDict

from langgraph.store.base import BaseStore

NS_INCIDENTS = "incidents"
NS_LESSONS = "lessons"
NS_PREFS = "prefs"
NS_PEOPLE = "people"
NS_POSTMORTEMS = "postmortems"
NS_LEDGER = "ledger"
NS_EFFECTS = "effects"
RECALL_KINDS = (NS_INCIDENTS, NS_LESSONS, NS_PREFS)


class MemoryHit(TypedDict):
    ns: list[str]
    key: str
    score: float | None
    text: str
    value: dict[str, Any]

    # NOTE: ``ref`` strings used as evidence references are ``f"memory:{key}"``.


def memory_ref(hit: MemoryHit) -> str:
    return f"memory:{hit['key']}"


def _hit(item: Any) -> MemoryHit:
    value = dict(item.value or {})
    return {
        "ns": list(item.namespace),
        "key": item.key,
        "score": getattr(item, "score", None),
        "text": str(value.get("text", "")),
        "value": value,
    }


async def recall(
    store: BaseStore | None,
    query: str,
    *,
    kinds: Sequence[str] = RECALL_KINDS,
    component: str | None = None,
    k: int = 4,
) -> list[MemoryHit]:
    """Semantic recall across ``kinds``. With ``component`` the component's own namespace is searched first and the
    global pool second, results merged by score (component-local hits win ties) and truncated to ``k``."""
    if store is None or not query.strip():
        return []
    seen: dict[tuple[tuple[str, ...], str], MemoryHit] = {}
    for kind in kinds:
        prefixes: list[tuple[str, ...]] = [(kind, component)] if component else []
        prefixes.append((kind,))
        for rank, prefix in enumerate(prefixes):
            for item in await store.asearch(prefix, query=query, limit=k):
                hit = _hit(item)
                sig = (tuple(hit["ns"]), hit["key"])
                if sig not in seen:
                    bonus = 0.02 if component and rank == 0 else 0.0  # component-local gets a small edge
                    hit["score"] = (hit["score"] or 0.0) + bonus
                    seen[sig] = hit
    return sorted(seen.values(), key=lambda h: (-(h["score"] or 0.0), h["key"]))[:k]


async def get_prefs(store: BaseStore | None, team: str | None = None) -> list[MemoryHit]:
    """All preference items (optionally one team's), unranked, for rule application."""
    if store is None:
        return []
    prefix = (NS_PREFS, team) if team else (NS_PREFS,)
    return [_hit(i) for i in await store.asearch(prefix, limit=100)]


async def remember(
    store: BaseStore | None,
    kind: str,
    key: str,
    value: dict[str, Any],
    *,
    component: str | None = None,
) -> bool:
    """Write (upsert) one memory. ``value`` must carry the searchable ``text``. Idempotent per ``(kind, component, key)``."""
    if store is None:
        return False
    if not str(value.get("text", "")).strip():
        raise ValueError("memory value needs a non-empty 'text' (it is the indexed field)")
    ns = (kind, component) if component else (kind,)
    await store.aput(ns, key, value)
    return True


async def forget(store: BaseStore | None, ns: Sequence[str], key: str) -> None:
    if store is not None:
        await store.adelete(tuple(ns), key)


async def claim_effect(store: BaseStore | None, key: str, value: dict[str, Any] | None = None) -> bool:
    """Idempotency guard for side effects: True the first time ``key`` is claimed, False afterwards.

    Put the claim AFTER the last ``interrupt()`` and BEFORE acting. ``store=None`` always returns True (tests).
    """
    if store is None:
        return True
    if await store.aget((NS_EFFECTS,), key) is not None:
        return False
    await store.aput((NS_EFFECTS,), key, {"text": f"effect {key}", **(value or {})}, index=False)
    return True
