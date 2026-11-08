"""CONSOLIDATE mode nodes (cron): ``scan -> cluster -> (confirm_merge) -> apply``.

* ``scan`` pages through every lesson; ``cluster`` finds semantic duplicates WITHIN a component (union-find over search
  hits above ``merge_threshold``), computes confidence boosts for reinforced lessons and flags stale ones;
* ``confirm_merge`` is the only interrupt, and only when duplicates would be DELETED (``MemoryDecision``: save = merge and
  delete, anything else = keep them and just mark ``duplicate_of``);
* ``apply`` performs the writes. Every change is a pure function of what it reads (max-confidence rule, membership in
  ``merged_from``/``stale``), so running consolidation twice changes nothing the second time.
"""

from __future__ import annotations

import asyncio
import hashlib
from functools import lru_cache
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.runtime import get_runtime
from langgraph.store.base import BaseStore
from langgraph.types import interrupt

from foghorn.common.lifecycle import emit_event
from foghorn.contracts.interrupts import MemoryDecision, build_interrupt
from foghorn.data.util import to_epoch
from foghorn.flows.memory_keeper.context import KeeperContext
from foghorn.flows.memory_keeper.helpers import provenance, summarise_text, thread_id
from foghorn.flows.memory_keeper.learn import _announce
from foghorn.flows.postmortem_pipeline.streams import emit_safe
from foghorn.logging import get_logger
from foghorn.memory.api import NS_INCIDENTS, NS_LESSONS, forget, remember

from .state import KeeperState

log = get_logger("memory_keeper.consolidate")

CONFIDENCE_CAP = 0.95
REPORT_NS = ("reports", "consolidation")


def _ctx() -> KeeperContext:
    return get_runtime(KeeperContext).context or KeeperContext()


@lru_cache(maxsize=1)
def _past_incident_dates() -> dict[str, str]:
    """``from_incident`` of the seed lessons -> when that incident happened (data layer, read once)."""
    try:
        from foghorn.data import api

        return {r["id"]: r["opened_at"] for r in api.read_dataset("past_incidents", limit=500)["records"]}
    except Exception as exc:
        log.warning("past_incident_dates_unavailable", error=repr(exc)[:200])
        return {}


def last_seen(value: dict[str, Any], dates: dict[str, str]) -> str | None:
    return (
        value.get("last_reinforced_at")
        or value.get("last_seen")
        or value.get("learned_from_at")
        or dates.get(str(value.get("from_incident")))
    )


def confidence_target(value: dict[str, Any]) -> float:
    """Evidence accumulates: the originating incident plus every reinforcing one is a source."""
    sources = len({str(value.get("from_incident")), *map(str, value.get("reinforced_by") or [])} - {"None"})
    return round(min(CONFIDENCE_CAP, 0.5 + 0.1 * sources), 2)


# ------------------------------------------------------------------------------------------------------------- scan
async def scan(state: KeeperState, config: RunnableConfig, store: BaseStore) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    if store is not None:
        offset = 0
        while True:
            page = await store.asearch((NS_LESSONS,), limit=100, offset=offset)
            for it in page:
                v = it.value
                rows.append(
                    {
                        "ns": list(it.namespace),
                        "key": it.key,
                        "text": str(v.get("text", "")),
                        "confidence": float(v.get("confidence", 0.6)),
                        "reinforced_by": list(v.get("reinforced_by") or []),
                        "from_incident": v.get("from_incident"),
                        "last_seen": None,
                        "stale": bool(v.get("stale")),
                        "version": int(v.get("version", 1)),
                    }
                )
            if len(page) < 100:
                break
            offset += 100
    dates = await asyncio.to_thread(_past_incident_dates)
    if store is not None:
        for r in rows:
            item = await store.aget(tuple(r["ns"]), r["key"])
            r["last_seen"] = last_seen(item.value, dates) if item else None
    rows.sort(key=lambda r: (r["ns"], r["key"]))
    emit_safe("progress", "mem:consolidate:scan", agent="scan", label=f"{len(rows)} lesson(s) scanned", pct=20)
    return {"lessons": rows}


# ----------------------------------------------------------------------------------------------------------- cluster
def _find(parent: dict[str, str], x: str) -> str:
    while parent[x] != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x


async def cluster(state: KeeperState, config: RunnableConfig, store: BaseStore) -> dict[str, Any]:
    ctx = _ctx()
    rows = state.get("lessons") or []
    by_id = {(tuple(r["ns"]), r["key"]): r for r in rows}
    parent = {f"{'/'.join(r['ns'])}/{r['key']}": f"{'/'.join(r['ns'])}/{r['key']}" for r in rows}
    scores: dict[tuple[str, str], float] = {}
    for r in rows:
        a = f"{'/'.join(r['ns'])}/{r['key']}"
        if store is None or not r["text"].strip():
            continue
        for hit in await store.asearch(tuple(r["ns"]), query=r["text"], limit=6):
            if hit.key == r["key"] or (tuple(hit.namespace), hit.key) not in by_id:
                continue
            if (hit.score or 0.0) >= ctx.merge_threshold:
                b = f"{'/'.join(hit.namespace)}/{hit.key}"
                parent[_find(parent, a)] = _find(parent, b)
                scores[tuple(sorted((a, b)))] = round(hit.score or 0.0, 3)  # type: ignore[index]
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault(_find(parent, f"{'/'.join(r['ns'])}/{r['key']}"), []).append(r)
    clusters: list[dict[str, Any]] = []
    for members in groups.values():
        if len(members) < 2:
            continue
        ranked = sorted(members, key=lambda r: (-r["confidence"], -len(r["reinforced_by"]), r["key"]))
        keep, drop = ranked[0], ranked[1:]
        clusters.append(
            {
                "ns": keep["ns"],
                "keep": keep["key"],
                "keep_text": summarise_text(keep["text"]),
                "drop": [d["key"] for d in drop],
                "drop_texts": [summarise_text(d["text"]) for d in drop],
                "max_score": max(scores.values(), default=0.0) if scores else 0.0,
            }
        )
    clusters.sort(key=lambda c: (c["ns"], c["keep"]))

    dropped = {(tuple(c["ns"]), k) for c in clusters for k in c["drop"]}
    boosts = []
    for r in rows:
        if (tuple(r["ns"]), r["key"]) in dropped:
            continue
        item = await store.aget(tuple(r["ns"]), r["key"]) if store is not None else None
        if item is None:
            continue
        target = confidence_target(item.value)
        if target > r["confidence"] and (item.value.get("reinforced_by")):
            boosts.append({"ns": r["ns"], "key": r["key"], "from": r["confidence"], "to": target})
    dates = [r["last_seen"] for r in rows if r["last_seen"]]
    now = ctx.as_of or await _newest_activity(store, dates)
    stale = []
    if now:
        horizon = to_epoch(now) - ctx.stale_days * 86400
        for r in rows:
            if r["stale"] or (tuple(r["ns"]), r["key"]) in dropped or not r["last_seen"] or r["reinforced_by"]:
                continue
            if to_epoch(r["last_seen"]) < horizon:
                stale.append({"ns": r["ns"], "key": r["key"], "last_seen": r["last_seen"]})
    emit_safe(
        "progress",
        "mem:consolidate:cluster",
        agent="cluster",
        label=f"{len(clusters)} duplicate group(s), {len(stale)} stale",
        pct=55,
    )
    return {"clusters": clusters, "boosts": boosts, "stale": stale, "merge_decision": "none"}


async def _newest_activity(store: BaseStore, lesson_dates: list[str]) -> str | None:
    """Without a clock in the input, "now" is the newest thing the memory knows about: a lesson date or an incident date."""
    dates = list(lesson_dates)
    if store is not None:
        offset = 0
        while True:
            page = await store.asearch((NS_INCIDENTS,), limit=100, offset=offset)
            dates += [str(it.value[k]) for it in page for k in ("opened_at", "resolved_at") if it.value.get(k)]
            if len(page) < 100:
                break
            offset += 100
    return max(dates, key=to_epoch) if dates else None


def needs_confirmation(state: KeeperState) -> str:
    """Conditional edge: only a plan that DELETES anything goes to a human."""
    return "confirm_merge" if any(c["drop"] for c in state.get("clusters") or []) else "apply"


# ------------------------------------------------------------------------------------------------------ confirm_merge
async def confirm_merge(state: KeeperState, config: RunnableConfig, store: BaseStore) -> dict[str, Any]:
    clusters = state.get("clusters") or []
    plan_id = hashlib.sha256(
        "|".join(f"{'/'.join(c['ns'])}:{c['keep']}:{','.join(c['drop'])}" for c in clusters).encode()
    ).hexdigest()[:8]
    aid = f"mem_consolidate_{plan_id}"
    n_drop = sum(len(c["drop"]) for c in clusters)
    emit_event(
        "approval.requested",
        idempotency_key=f"approval.requested:{aid}",
        payload={
            "approval_id": aid,
            "kind": "memory_write",
            "thread_id": thread_id(config),
            "title": "Merge duplicate lessons?",
            "summary": f"{n_drop} duplicate lesson(s) in {len(clusters)} group(s) would be merged into the best one and deleted",
            "risk": "medium",
        },
    )
    raw = interrupt(
        build_interrupt(
            "memory_write",
            approval_id=aid,
            title="Merge duplicate lessons?",
            summary=f"{n_drop} duplicate lesson(s) in {len(clusters)} group(s) would be merged into the best one and DELETED. "
            "save = merge and delete; discard = keep them (they are only marked duplicate_of).",
            risk="medium",
            reversible=False,
            proposed={"merges": clusters},
            options=["save", "discard"],
        ),
        response_schema=MemoryDecision,
    )
    d = raw if isinstance(raw, MemoryDecision) else MemoryDecision.model_validate(raw)
    emit_event(
        "approval.decided",
        idempotency_key=f"approval.decided:{aid}",
        payload={
            "approval_id": aid,
            "decision": d.decision,
            "decided_by": d.decided_by,
            "thread_id": thread_id(config),
        },
    )
    return {"merge_decision": "save" if d.decision == "save" else "discard"}


# --------------------------------------------------------------------------------------------------------- apply
async def apply(state: KeeperState, config: RunnableConfig, store: BaseStore) -> dict[str, Any]:
    prov = provenance(config, None)
    merged = deleted = kept_dups = boosted = flagged = 0
    changed: list[dict[str, Any]] = []
    decision = state.get("merge_decision", "none")
    boost_map = {(tuple(b["ns"]), b["key"]): b["to"] for b in state.get("boosts") or []}

    for c in state.get("clusters") or []:
        ns, keep = tuple(c["ns"]), c["keep"]
        survivor = await store.aget(ns, keep) if store is not None else None
        if survivor is None:
            continue
        v = dict(survivor.value)
        if decision == "save":
            union = set(v.get("reinforced_by") or [])
            sources = {str(v.get("from_incident"))}
            for k in c["drop"]:
                item = await store.aget(ns, k)
                if item is None:
                    continue
                union |= set(item.value.get("reinforced_by") or [])
                sources.add(str(item.value.get("from_incident")))
            union.discard(str(v.get("from_incident")))
            mf = sorted(set(v.get("merged_from") or []) | set(c["drop"]))
            v.update(reinforced_by=sorted(union), merged_from=mf, version=int(v.get("version", 1)) + 1)
            v["confidence"] = round(
                min(CONFIDENCE_CAP, max(float(v.get("confidence", 0.6)), confidence_target(v)) + 0.03 * len(c["drop"])),
                2,
            )
            await remember(store, NS_LESSONS, keep, v, component=ns[1])
            for k in c["drop"]:
                await forget(store, ns, k)
                deleted += 1
            merged += 1
            row = {"kind": NS_LESSONS, "ns": list(ns), "key": keep, "action": "merged", "version": v["version"]}
            changed.append(row)
            _announce(row, ns[1], "consolidation", v["text"])
        else:  # nothing is deleted: mark the duplicates so a human can find them
            for k in c["drop"]:
                item = await store.aget(ns, k)
                if item is not None and item.value.get("duplicate_of") != keep:
                    await remember(store, NS_LESSONS, k, {**item.value, "duplicate_of": keep}, component=ns[1])
                    kept_dups += 1

    for (ns, key), to in boost_map.items():
        item = await store.aget(ns, key) if store is not None else None
        if item is None or float(item.value.get("confidence", 0.0)) >= to:
            continue
        v = {**item.value, "confidence": to, "version": int(item.value.get("version", 1)) + 1}
        await remember(store, NS_LESSONS, key, v, component=ns[1])
        boosted += 1
        row = {"kind": NS_LESSONS, "ns": list(ns), "key": key, "action": "boosted", "version": v["version"]}
        changed.append(row)
        _announce(row, ns[1], "consolidation", v["text"])

    for s in state.get("stale") or []:
        ns = tuple(s["ns"])
        item = await store.aget(ns, s["key"]) if store is not None else None
        if item is None or item.value.get("stale"):
            continue
        await remember(
            store, NS_LESSONS, s["key"], {**item.value, "stale": True, "stale_since": s["last_seen"]}, component=ns[1]
        )
        flagged += 1

    report = {
        "scanned": len(state.get("lessons") or []),
        "duplicate_groups": len(state.get("clusters") or []),
        "merged_groups": merged,
        "deleted": deleted,
        "duplicates_marked": kept_dups,
        "boosted": boosted,
        "stale_flagged": flagged,
        "merge_decision": decision,
        "clusters": state.get("clusters") or [],
        "boosts": state.get("boosts") or [],
        "stale": state.get("stale") or [],
    }
    tid = thread_id(config) or "no-thread"
    key = f"consolidation-{hashlib.sha256(tid.encode()).hexdigest()[:8]}"
    text = (
        f"Memory consolidation: scanned {report['scanned']} lessons, merged {merged} duplicate group(s) "
        f"({deleted} deleted, {kept_dups} marked), boosted {boosted}, flagged {flagged} stale."
    )
    await remember(store, REPORT_NS[0], key, {"text": text, **report, "provenance": prov}, component=REPORT_NS[1])
    report["report_key"] = key
    emit_safe("progress", "mem:consolidate:apply", agent="apply", label=text, pct=100)
    return {"report": report, "written": changed}
