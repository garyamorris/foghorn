"""Approvals inbox: scan interrupted threads, expose each pending interrupt, resume it safely.

The LangGraph thread state is the source of truth for interrupts; the ``approvals`` table is a projection that makes
history/KPIs cheap and survives restarts (startup reconciliation re-scans). Resume rules (docs/design/02 gotcha 15):
the server does NOT reject a stale/unknown interrupt id (it silently re-interrupts), so the id is validated here
against the thread's CURRENT pending interrupts before ``Command(resume={interrupt_id: value})`` is sent.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from foghorn import clock
from foghorn.contracts.interrupts import RESPONSE_SCHEMAS
from foghorn.contracts.meta import M_GRAPH, M_INCIDENT_ID
from foghorn.events.config import EventsSettings
from foghorn.events.db import StateDB
from foghorn.events.dispatcher import Dispatcher, DispatchError
from foghorn.events.pipeline import EventBus
from foghorn.events.sdk import ClientFactory, flatten_interrupts
from foghorn.events.vclock import to_iso
from foghorn.logging import get_logger

log = get_logger("events.approvals")


@dataclass
class ScanResult:
    items: list[dict[str, Any]] = field(default_factory=list)
    scanned_threads: int = 0
    ok: bool = True


def schema_for_kind(kind: str) -> dict[str, Any] | None:
    model = RESPONSE_SCHEMAS.get(kind)
    return model.model_json_schema() if model else None


def approval_id_for(value: Any, interrupt_id: str) -> str:
    if isinstance(value, dict) and value.get("approval_id"):
        return str(value["approval_id"])
    return f"apr_{interrupt_id[:8]}"


class ApprovalService:
    def __init__(
        self,
        *,
        db: StateDB,
        es: EventsSettings,
        bus: EventBus,
        dispatcher: Dispatcher,
        client_factory: ClientFactory,
        wall: Callable[[], datetime] | None = None,
    ) -> None:
        self.db, self.es, self.bus, self.dispatcher = db, es, bus, dispatcher
        self._client_factory = client_factory
        self._client: Any = None
        self._wall = wall or clock.now
        self._locks: dict[str, asyncio.Lock] = {}
        self._confirmed: set[str] = set()  # approval ids a scan has seen pending (grace for not-yet-visible interrupts)
        dispatcher.resume_handler = self.resume_from_plan
        dispatcher.on_interrupted = self.reconcile_thread

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    def _now_iso(self) -> str:
        return to_iso(self._wall(), ms=True)

    # --- scanning ----------------------------------------------------------------------------------------------
    def _row_for(self, thread: dict[str, Any], it: dict[str, Any], existing: dict[str, Any] | None) -> dict[str, Any]:
        value = it.get("value")
        payload = value if isinstance(value, dict) else {"value": value}
        kind = str(payload.get("kind") or "approval")
        md = thread.get("metadata") or {}
        ctx = payload.get("context") or {}
        return {
            "approval_id": approval_id_for(payload, str(it["id"])),
            "kind": kind,
            "status": "pending",
            "thread_id": thread.get("thread_id"),
            "interrupt_id": str(it["id"]),
            "title": payload.get("title"),
            "summary": payload.get("summary"),
            "risk": payload.get("risk"),
            "reversible": payload.get("reversible"),
            "incident_id": ctx.get("incident_id") or md.get(M_INCIDENT_ID),
            "graph_id": md.get(M_GRAPH) or md.get("graph_id"),
            "requested_at": None
            if existing and existing.get("requested_at")
            else (str(thread.get("updated_at") or self._now_iso())),
            "payload": payload,
            "response_schema": it.get("response_schema") or schema_for_kind(kind),
            "options": payload.get("options"),
        }

    def _sync_scan(self, threads: list[dict[str, Any]], *, complete: bool, startup: bool) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        with self.db.transaction():
            for th in threads:
                for it in flatten_interrupts(th):
                    aid = approval_id_for(it.get("value"), str(it["id"]))
                    existing = self.db.get_approval(aid)
                    spec = self._row_for(th, it, existing)
                    spec.pop("approval_id")
                    row = self.db.upsert_approval(aid, **spec)
                    if existing and existing["status"] != "pending":  # the thread says it is pending again
                        self.db.execute(
                            "UPDATE approvals SET status = 'pending', decided_at = NULL, decided_by = NULL, decision = NULL WHERE approval_id = ?",
                            (aid,),
                        )
                        row = self.db.get_approval(aid) or row
                    seen.add(aid)
                    out.append(row)
            if complete:
                for r in self.db.list_approvals(statuses=("pending",), limit=1000):
                    if r["approval_id"] in seen:
                        continue
                    if startup or r["approval_id"] in self._confirmed:
                        self.db.upsert_approval(
                            r["approval_id"],
                            status="decided",
                            decided_at=self._now_iso(),
                            decided_by="external",
                            decision={"via": "external"},
                        )
                        self.db.execute(
                            "UPDATE approvals SET payload = NULL, response_schema = NULL WHERE approval_id = ?",
                            (r["approval_id"],),
                        )
        self._confirmed = set(seen) if complete else (self._confirmed | seen)
        return out

    async def scan(self, *, startup: bool = False) -> ScanResult:
        try:
            threads = await self.client.threads.search(status="interrupted", limit=self.es.scan_limit)
        except Exception as exc:
            log.warning("approvals_scan_failed", error=repr(exc))
            rows = await self.db.run(self.db.list_approvals, statuses=("pending",))
            return ScanResult(items=rows, scanned_threads=0, ok=False)
        threads = [dict(t) for t in threads]
        rows = await self.db.run(self._sync_scan, threads, complete=len(threads) < self.es.scan_limit, startup=startup)
        return ScanResult(items=rows, scanned_threads=len(threads), ok=True)

    async def reconcile_thread(self, thread_id: str) -> None:
        """Called when a run ends interrupted: make sure its interrupts are in the projection even if the graph
        forgot to emit ``approval.requested``."""
        thread = dict(await self.client.threads.get(thread_id))
        await self.db.run(self._sync_scan, [thread], complete=False, startup=False)

    # --- reading -----------------------------------------------------------------------------------------------
    async def pending(self) -> ScanResult:
        return await self.scan()

    async def get(self, approval_id: str) -> dict[str, Any] | None:
        row = await self.db.run(self.db.get_approval, approval_id)
        if row is None:
            await self.scan()
            row = await self.db.run(self.db.get_approval, approval_id)
        return row

    # --- resuming ----------------------------------------------------------------------------------------------
    def _lock(self, thread_id: str) -> asyncio.Lock:
        return self._locks.setdefault(thread_id, asyncio.Lock())

    @staticmethod
    def validate_response(kind: str, response: Any, decided_by: str | None) -> dict[str, Any]:
        if not isinstance(response, dict):
            raise DispatchError("invalid_response", "response must be a JSON object", 422)
        data = dict(response)
        if decided_by:
            data.setdefault("decided_by", decided_by)
        model = RESPONSE_SCHEMAS.get(kind)
        if model is None:
            return data
        try:
            return model.model_validate(data).model_dump(mode="json")
        except ValidationError as exc:
            errs = [
                {"loc": ".".join(str(x) for x in e["loc"]), "msg": str(e["msg"])}
                for e in exc.errors(include_url=False, include_context=False, include_input=False)
            ]
            raise DispatchError(
                "invalid_response", "; ".join(f"{e['loc']}: {e['msg']}" for e in errs)[:400], 422, {"errors": errs}
            ) from exc

    async def _live_pending(self, thread_id: str) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        try:
            thread = dict(await self.client.threads.get(thread_id))
        except Exception as exc:
            raise DispatchError("not_found", f"thread {thread_id!r} not found: {exc!r}"[:300], 404) from exc
        return thread, {i["id"]: i for i in flatten_interrupts(thread)}

    async def resume(
        self, approval_id: str, *, interrupt_id: str | None, response: Any, nonce: str | None, decided_by: str | None
    ) -> dict[str, Any]:
        row = await self.get(approval_id)
        if row is None:
            raise DispatchError("not_found", f"approval {approval_id!r} not found", 404)
        if nonce and row["status"] == "decided" and await self.db.run(self.db.get_approval_nonce, approval_id) == nonce:
            return {
                "status": "duplicate",
                "approval_id": approval_id,
                "thread_id": row["thread_id"],
                "interrupt_id": row["interrupt_id"],
            }
        thread_id = row.get("thread_id")
        if not thread_id:
            raise DispatchError("conflict", "approval has no thread to resume", 409)
        iid = interrupt_id or row.get("interrupt_id")
        out = await self._resume_many(
            thread_id, {str(iid): response}, nonce=nonce, decided_by=decided_by, approval_hint={str(iid): approval_id}
        )
        first = out["decisions"][0]
        return {
            "status": "resumed",
            "approval_id": first["approval_id"],
            "thread_id": thread_id,
            "interrupt_id": first["interrupt_id"],
            "run_id": out["run_id"],
            "decision_event_id": first["event_id"],
            "remaining_interrupts": out["remaining"],
        }

    async def resume_batch(
        self, thread_id: str, responses: dict[str, Any], *, nonce: str | None, decided_by: str | None
    ) -> dict[str, Any]:
        out = await self._resume_many(thread_id, responses, nonce=nonce, decided_by=decided_by, approval_hint={})
        return {
            "status": "resumed",
            "thread_id": thread_id,
            "run_id": out["run_id"],
            "interrupt_ids": [d["interrupt_id"] for d in out["decisions"]],
            "decision_event_ids": [d["event_id"] for d in out["decisions"]],
        }

    async def resume_from_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        """Dispatcher hook for the ``r-approval-decided`` rule (a decision delivered as an event)."""
        if not plan.get("approval_id"):
            raise DispatchError("bad_request", "approval.decided event without payload.approval_id", 400)
        out = await self.resume(
            str(plan["approval_id"]),
            interrupt_id=plan.get("interrupt_id"),
            response=plan.get("response") or {},
            nonce=plan.get("nonce"),
            decided_by=plan.get("decided_by"),
        )
        return out

    async def _resume_many(
        self,
        thread_id: str,
        responses: dict[str, Any],
        *,
        nonce: str | None,
        decided_by: str | None,
        approval_hint: dict[str, str],
    ) -> dict[str, Any]:
        if not responses:
            raise DispatchError("bad_request", "no responses given", 400)
        async with self._lock(thread_id):
            thread, pending = await self._live_pending(thread_id)
            unknown = [i for i in responses if i not in pending]
            if unknown:
                raise DispatchError(
                    "stale_interrupt",
                    "interrupt is not pending on this thread (already decided, or unknown)",
                    409,
                    {"unknown": unknown, "pending": sorted(pending)},
                )
            resume_map: dict[str, Any] = {}
            decisions: list[dict[str, Any]] = []
            for iid, response in responses.items():
                value = pending[iid].get("value")
                kind = str((value or {}).get("kind") or "approval") if isinstance(value, dict) else "approval"
                clean = self.validate_response(kind, response, decided_by)
                resume_map[iid] = clean
                decisions.append(
                    {
                        "interrupt_id": iid,
                        "approval_id": approval_hint.get(iid) or approval_id_for(value, iid),
                        "kind": kind,
                        "response": clean,
                    }
                )
            md = thread.get("metadata") or {}
            graph_id = str(md.get(M_GRAPH) or md.get("graph_id") or "")
            if not graph_id:
                raise DispatchError("conflict", "cannot determine which graph owns this thread", 409)
            ctx = await self._original_context(thread_id)
            kwargs: dict[str, Any] = {
                "command": {"resume": resume_map},
                "metadata": {"foghorn_resume": ",".join(resume_map)},
                "multitask_strategy": "enqueue",
                "stream_mode": list(self.es.run_stream_modes),
                "stream_subgraphs": self.es.run_stream_subgraphs,
                "stream_resumable": self.es.run_stream_resumable,
            }
            if ctx:
                kwargs["context" if self.es.run_config_mode == "context" else "config"] = (
                    ctx if self.es.run_config_mode == "context" else {"configurable": ctx}
                )
            try:
                run = await self.client.runs.create(thread_id, graph_id, **kwargs)
            except Exception as exc:
                raise DispatchError("not_ready", f"could not resume the thread: {exc!r}"[:300], 503) from exc
            run_id = str(run["run_id"])
            incident_id = md.get(M_INCIDENT_ID)
            for dec in decisions:
                key = nonce or dec["interrupt_id"]
                res = await self.bus.emit(
                    "approval.decided",
                    idempotency_key=f"approval.decided:{dec['approval_id']}:{key}",
                    payload={
                        "approval_id": dec["approval_id"],
                        "kind": dec["kind"],
                        "decision": dec["response"].get("decision", "answered"),
                        "decided_by": dec["response"].get("decided_by", decided_by or "operator"),
                        "note": dec["response"].get("note", ""),
                        "thread_id": thread_id,
                        "interrupt_id": dec["interrupt_id"],
                        "run_id": run_id,
                        "incident_id": incident_id,
                    },
                    severity="info",
                    scenario_id=md.get("foghorn_scenario_id"),
                    correlation_id=md.get("foghorn_correlation_id"),
                )
                dec["event_id"] = res.event_id
                if nonce:
                    await self.db.run(self.db.set_approval_nonce, dec["approval_id"], nonce)
                self._confirmed.discard(dec["approval_id"])
            await self.dispatcher.track_external_run(
                thread_id=thread_id,
                run_id=run_id,
                graph_id=graph_id,
                event_id=decisions[0]["event_id"],
                route="approval-resume",
                incident_id=incident_id,
            )
            return {"run_id": run_id, "decisions": decisions, "remaining": len(pending) - len(decisions)}

    async def _original_context(self, thread_id: str) -> dict[str, Any]:
        rows, _ = await self.db.run(self.db.list_dispatches, thread_id=thread_id, limit=20)
        for d in rows:  # newest first
            ctx = (d.get("plan") or {}).get("context")
            if ctx:
                return dict(ctx)
        return {}
