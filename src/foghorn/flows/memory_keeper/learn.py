"""LEARN mode nodes: ``load_source -> distill -> dedupe -> curate -> write``.

* ``distill`` (``smart`` role, structured ``Distillation``) turns a published postmortem into lesson candidates;
* ``dedupe`` recalls near-duplicates by SEMANTIC search in the component's own lesson namespace: reinforce or new;
* ``curate`` asks a human, one ``memory_write`` interrupt per candidate (``MemoryDecision``: save / edit / discard);
* ``write`` is the only node with side effects, runs after every interrupt and is idempotent (business keys, versions,
  ``reinforced_by`` membership, lifecycle events with idempotency keys).

Store access is ``async`` through the injected ``store`` (the Store API is async-first).
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import get_runtime
from langgraph.store.base import BaseStore
from langgraph.types import interrupt

from foghorn.common.lifecycle import emit_event
from foghorn.contracts.interrupts import MemoryDecision, build_interrupt
from foghorn.flows.memory_keeper.context import KeeperContext
from foghorn.flows.memory_keeper.helpers import GENERAL, lesson_key, normalise, provenance, summarise_text, thread_id
from foghorn.flows.memory_keeper.models import Distillation
from foghorn.flows.memory_keeper.scripts import distiller_script, pack
from foghorn.flows.postmortem_pipeline.streams import emit_safe
from foghorn.logging import get_logger
from foghorn.memory.api import NS_INCIDENTS, NS_LESSONS, NS_POSTMORTEMS, recall, remember
from foghorn.models.factory import get_chat_model, structured

from .state import KeeperState

log = get_logger("memory_keeper.learn")

RELATED_MIN_SCORE = 0.35  # below this a cross-component hit is noise, not worth recalling


def _ctx() -> KeeperContext:
    return get_runtime(KeeperContext).context or KeeperContext()


# -------------------------------------------------------------------------------------------------------- load_source
async def load_source(state: KeeperState, config: RunnableConfig, store: BaseStore) -> dict[str, Any]:
    """The postmortem: from the event payload (``postmortem`` input) or, failing that, the Store ``postmortems`` namespace."""
    pm = state.get("postmortem") or None
    source = "input" if pm else "none"
    iid = state.get("incident_id") or (pm or {}).get("incident_id")
    if pm is None and iid and store is not None:
        hits = await store.asearch((NS_POSTMORTEMS,), filter={"incident_id": iid}, limit=1)
        if hits and isinstance(hits[0].value.get("postmortem"), dict):
            pm, source = dict(hits[0].value["postmortem"]), "store"
    update: dict[str, Any] = {"source": source, "component": (pm or {}).get("component")}
    if pm is not None:
        update["postmortem"] = pm
        update["incident_id"] = iid or pm.get("incident_id")
    emit_safe("progress", f"mem:{iid}:load", agent="load_source", label=f"postmortem loaded from {source}", pct=10)
    return update


# ----------------------------------------------------------------------------------------------------------- distill
async def distill(state: KeeperState, config: RunnableConfig, store: BaseStore) -> dict[str, Any]:
    ctx = _ctx()
    pm = state["postmortem"]
    limit = max(1, min(int(ctx.max_lessons), 5))
    llm = structured(get_chat_model("smart", offline_script=distiller_script()), Distillation).with_config(
        tags=["nostream"]
    )
    out: Distillation = await llm.ainvoke([SystemMessage(ctx.lesson_distiller_prompt), HumanMessage(pack(pm, limit))])
    seen: set[str] = set()
    cands: list[dict[str, Any]] = []
    for lesson in out.lessons:
        norm = normalise(lesson.text)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        cands.append(
            {
                "id": f"c{len(cands)}",
                "text": lesson.text.strip(),
                "category": lesson.category or pm.get("category"),
                "confidence": round(float(lesson.confidence), 2),
            }
        )
        if len(cands) >= limit:
            break
    emit_safe(
        "progress",
        f"mem:{state.get('incident_id')}:distill",
        agent="distill",
        label=f"{len(cands)} lesson candidate(s)",
        pct=35,
    )
    return {"candidates": cands, "incident_summary": out.incident_summary or pm.get("summary", "")}


# ------------------------------------------------------------------------------------------------------------ dedupe
async def dedupe(state: KeeperState, config: RunnableConfig, store: BaseStore) -> dict[str, Any]:
    """New or reinforce? Semantic search over the component's lesson namespace; the nearest hit at or above the threshold
    is a near-duplicate. Cross-component hits are reported as ``related`` but never reinforced."""
    ctx = _ctx()
    component = state.get("component") or GENERAL
    iid = state.get("incident_id")
    out: list[dict[str, Any]] = []
    recalled: list[dict[str, Any]] = []
    for cand in state.get("candidates") or []:
        hits = await recall(store, cand["text"], kinds=(NS_LESSONS,), component=state.get("component"), k=4)
        local = [h for h in hits if h["ns"] == [NS_LESSONS, component]]
        other = [h for h in hits if h["ns"] != [NS_LESSONS, component]]
        best = local[0] if local else None
        near = best if best is not None and (best["score"] or 0.0) >= ctx.reinforce_threshold else None
        related = [h for h in other[:1] if (h["score"] or 0.0) >= RELATED_MIN_SCORE]
        shown = [h for h in (near, *related) if h is not None]
        for h in shown:
            rec = {
                "for": cand["id"],
                "ns": h["ns"],
                "key": h["key"],
                "score": round(h["score"] or 0.0, 3),
                "summary": summarise_text(h["text"]),
            }
            recalled.append(rec)
            emit_safe(
                "memory.recalled",
                f"mem:{iid}:{cand['id']}:{h['key']}",
                agent="dedupe",
                ns=h["ns"],
                key=h["key"],
                score=rec["score"],
                summary=rec["summary"],
            )
            emit_event(
                "memory.recalled",
                idempotency_key=f"memory.recalled:{iid}:{cand['id']}:{h['key']}",
                component=state.get("component"),
                payload={"ns": h["ns"], "key": h["key"], "score": rec["score"], "incident_id": iid},
            )
        out.append(
            {
                **cand,
                "action": "reinforce" if near else "new",
                "ns": [NS_LESSONS, component],
                "key": near["key"] if near else lesson_key(component, cand["text"]),
                "near": {"key": near["key"], "score": round(near["score"] or 0.0, 3), "text": near["text"]}
                if near
                else None,
            }
        )
    return {"candidates": out, "recalled": recalled}


# ------------------------------------------------------------------------------------------------------------ curate
def _approval_id(iid: str | None, cand_id: str) -> str:
    return f"mem_{iid}_{cand_id}"


async def curate(state: KeeperState, config: RunnableConfig, store: BaseStore) -> dict[str, Any]:
    """One ``memory_write`` interrupt per candidate. The node restarts from line 1 on every resume: answered interrupts
    return instantly, events are emitted with idempotency keys, and NOTHING is written here."""
    iid = state.get("incident_id")
    tid = thread_id(config)
    decisions: list[dict[str, Any]] = []
    for cand in state.get("candidates") or []:
        aid = _approval_id(iid, cand["id"])
        reinforce = cand["action"] == "reinforce"
        title = f"Reinforce lesson {cand['key']}?" if reinforce else "Remember this lesson?"
        emit_event(
            "approval.requested",
            idempotency_key=f"approval.requested:{aid}",
            component=state.get("component"),
            payload={
                "approval_id": aid,
                "kind": "memory_write",
                "thread_id": tid,
                "title": title,
                "summary": cand["text"],
                "risk": "low",
                "incident_id": iid,
            },
        )
        raw = interrupt(
            build_interrupt(
                "memory_write",
                approval_id=aid,
                title=title,
                summary=cand["text"],
                risk="low",
                incident_id=iid,
                evidence_refs=[f"memory:{cand['near']['key']}"] if cand["near"] else [],
                proposed={
                    "text": cand["text"],
                    "action": cand["action"],
                    "namespace": cand["ns"],
                    "key": cand["key"],
                    "confidence": cand["confidence"],
                    "near_duplicate": cand["near"],
                },
                options=["save", "edit", "discard"],
            ),
            response_schema=MemoryDecision,
        )
        d = raw if isinstance(raw, MemoryDecision) else MemoryDecision.model_validate(raw)
        emit_event(
            "approval.decided",
            idempotency_key=f"approval.decided:{aid}",
            component=state.get("component"),
            payload={
                "approval_id": aid,
                "decision": d.decision,
                "decided_by": d.decided_by,
                "thread_id": tid,
                "incident_id": iid,
            },
        )
        text = (d.text or "").strip() if d.decision == "edit" else ""
        decisions.append(
            {
                "id": cand["id"],
                "action": cand["action"],
                "key": cand["key"],
                "ns": cand["ns"],
                "decision": d.decision,
                "text": text or cand["text"],
                "edited": bool(text and text != cand["text"]),
                "decided_by": d.decided_by,
                "confidence": cand["confidence"],
                "category": cand["category"],
            }
        )
    return {"decisions": decisions}


# ------------------------------------------------------------------------------------------------------------- write
async def write(state: KeeperState, config: RunnableConfig, store: BaseStore) -> dict[str, Any]:
    iid = str(state.get("incident_id"))
    pm = state["postmortem"]
    component = state.get("component")
    comp = component or GENERAL
    prov = provenance(config, iid)
    last_seen = (pm.get("window") or {}).get("end") or None
    written: list[dict[str, Any]] = []
    saved_keys: list[str] = []

    for d in state.get("decisions") or []:
        if d["decision"] == "discard":
            continue
        ns = tuple(d["ns"])
        existing = await store.aget(ns, d["key"]) if store is not None else None
        if d["action"] == "reinforce" and existing is not None:
            v = dict(existing.value)
            changed = False
            rb = list(v.get("reinforced_by") or [])
            if iid not in rb and iid != v.get("from_incident"):
                rb.append(iid)
                v["reinforced_by"] = rb
                v["confidence"] = round(min(0.99, float(v.get("confidence", 0.6)) + 0.05), 2)
                v["last_reinforced_at"] = last_seen
                changed = True
            if d["edited"] and d["text"] != v.get("text"):
                v["text"], changed = d["text"], True
            if changed:
                v["version"] = int(v.get("version", 1)) + 1
                v["stale"] = False
                if not await remember(store, NS_LESSONS, d["key"], v, component=comp):
                    continue
            version, action = int(v.get("version", 1)), "reinforced" if changed else "unchanged"
        else:
            v = {
                "text": d["text"],
                "category": d["category"],
                "source": "postmortem",
                "from_incident": iid,
                "confidence": d["confidence"],
                "reinforced_by": [],
                "version": 1,
                "learned_from_at": last_seen,
                "decided_by": d["decided_by"],
                "provenance": prov,
            }
            if existing is not None:  # a re-run of the same learn pass: keep what a later pass added
                v = {**existing.value, "text": d["text"]}
            if not await remember(store, NS_LESSONS, d["key"], v, component=comp):
                continue  # no Store injected: nothing was remembered
            version, action = int(v.get("version", 1)), "added"
        saved_keys.append(d["key"])
        row = {"kind": NS_LESSONS, "ns": d["ns"], "key": d["key"], "action": action, "version": version}
        written.append(row)
        if action != "unchanged":
            _announce(row, comp, iid, v["text"])

    inc_key = iid.lower()
    root = pm.get("root_cause", "")
    summary_value = {
        "text": state.get("incident_summary") or pm.get("summary", ""),
        "incident_id": iid,
        "severity": pm.get("severity"),
        "root_cause_category": pm.get("category"),
        "root_cause_component": component,
        "root_cause": root,
        "opened_at": (pm.get("window") or {}).get("start") or None,
        "resolved_at": last_seen,
        "lessons": saved_keys,
        "source": "postmortem",
        "provenance": prov,
    }
    if await remember(store, NS_INCIDENTS, inc_key, summary_value, component=comp):
        row = {"kind": NS_INCIDENTS, "ns": [NS_INCIDENTS, comp], "key": inc_key, "action": "added", "version": 1}
        written.append(row)
        _announce(row, comp, iid, summary_value["text"])
    return {"written": written}


def _announce(row: dict[str, Any], comp: str, iid: str, text: str) -> None:
    """Stream + lifecycle event for one written item (both idempotent: keys carry the item version)."""
    emit_safe(
        "memory.written", f"mem:{iid}:{row['key']}:v{row['version']}", agent="write", ns=row["ns"], key=row["key"]
    )
    emit_event(
        "memory.written",
        idempotency_key=f"memory.written:{'/'.join(row['ns'])}/{row['key']}:v{row['version']}",
        component=None if comp == GENERAL else comp,
        payload={
            "ns": row["ns"],
            "key": row["key"],
            "action": row["action"],
            "incident_id": iid,
            "text": summarise_text(text),
        },
    )
