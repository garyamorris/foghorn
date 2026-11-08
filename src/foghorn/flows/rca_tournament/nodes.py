"""Nodes of the RCA tournament: gather -> plan (Send x N) -> score_hypothesis (parallel) -> rank (defer) -> report."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from langgraph.runtime import Runtime
from langgraph.types import Command, Send

from foghorn.common.stream import emit
from foghorn.contracts.inputs import GraphResult
from foghorn.flows.incident_commander.context import ctx_get
from foghorn.flows.incident_commander.investigation import focus_components
from foghorn.flows.rca_tournament import scoring as sc
from foghorn.flows.rca_tournament.facts import Facts, hhmm
from foghorn.flows.rca_tournament.gather import gather_evidence
from foghorn.flows.rca_tournament.state import RcaState, WorkerArg

SEND_TIMEOUT_S = 10.0


def profile_of(runtime: Runtime[Any]) -> str:
    return str(ctx_get(runtime.context, "model_profile", "competent") or "competent")


def compact(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What a worker needs: ids, kinds and data (summaries stay in the parent state)."""
    return [
        {"id": e["id"], "kind": e["kind"], "data": e.get("data", {})}
        for e in evidence
        if e.get("kind") not in ("oncall",)
    ]


def gather(state: RcaState, runtime: Runtime[Any]) -> dict[str, Any]:
    """Standalone entry: with no evidence yet, investigate through the pure tool API (the commander passes its own)."""
    if state.get("evidence"):
        return {}
    emit("progress", "rca:gather", agent="rca_tournament", label="gathering evidence", pct=0.1)
    ev, as_of, calls = gather_evidence(
        state["scenario_id"], state.get("as_of"), profile=profile_of(runtime), candidates=state.get("candidates")
    )
    emit("note", "rca:gathered", agent="rca_tournament", text=f"{len(ev)} evidence items from {calls} tool calls")
    return {"evidence": ev, "as_of": as_of}


def plan(state: RcaState, runtime: Runtime[Any]) -> Command[Literal["score_hypothesis", "rank"]]:
    """One worker per candidate hypothesis (component, category)."""
    f = Facts.from_evidence(state.get("evidence"), state.get("as_of"))
    prof = profile_of(runtime)
    ev = compact(state.get("evidence") or [])
    if prof == "naive":
        first = next(iter(focus_components(f) or state.get("candidates") or []), None)
        picks = sc.naive_rank(f, first)
        args = [
            WorkerArg(
                component=s.component,
                category=s.category,
                evidence=ev,
                profile=prof,
                fixed_score=s.score,
                fixed_for=[t for _w, t, _r in s.for_],
            )
            for s in picks
        ]
    else:
        pairs = sc.candidate_pairs(f, list(state.get("candidates") or []))
        args = [
            WorkerArg(component=c, category=cat, evidence=ev, profile=prof, fixed_score=None, fixed_for=[])
            for c, cat in pairs
        ]
    if not args:
        return Command(goto="rank", update={"pairs": []})
    emit("progress", "rca:plan", agent="rca_tournament", label=f"{len(args)} hypotheses go to the tournament", pct=0.3)
    return Command(
        goto=[Send("score_hypothesis", a, timeout=SEND_TIMEOUT_S) for a in args],
        update={"pairs": [[a["component"], a["category"]] for a in args]},
    )


async def score_hypothesis(arg: WorkerArg) -> dict[str, Any]:
    """Score ONE hypothesis against the evidence (deterministic, no I/O). Emits the live ``hypothesis.update``."""
    f = Facts.from_evidence([{"id": e["id"], "kind": e["kind"], "data": e["data"]} for e in arg["evidence"]])
    if arg["fixed_score"] is not None:
        scored = sc.Scored(
            arg["component"],
            arg["category"],
            arg["fixed_score"],
            [(arg["fixed_score"], t, None) for t in arg["fixed_for"]],
            [],
        )
    else:
        scored = await asyncio.to_thread(sc.score_hypothesis, f, arg["component"], arg["category"])
    hyp = scored.as_hypothesis()
    emit(
        "hypothesis.update",
        f"hypothesis:{scored.id}",
        agent="rca_tournament",
        id=scored.id,
        label=hyp["label"],
        score=hyp["score"],
        delta=hyp["score"],
        **{"for": hyp["for"], "against": hyp["against"]},
    )
    return {"scores": [{"id": scored.id, **hyp}]}


def rank(state: RcaState, runtime: Runtime[Any]) -> dict[str, Any]:
    """Deferred reduce: runs once every worker has answered (or timed out)."""
    f = Facts.from_evidence(state.get("evidence"), state.get("as_of"))
    previous = {h["id"]: h["score"] for h in state.get("hypotheses") or []}
    asked = set(state.get("candidates") or [])  # a component the caller named wins a tie
    ranked = sorted(
        state.get("scores") or [],
        key=lambda h: (-h["score"], h["component"] not in asked, h["component"], h["category"]),
    )
    out: list[dict[str, Any]] = []
    for i, h in enumerate(ranked):
        delta = round(h["score"] - previous.get(h["id"], 0.0), 3)
        out.append({**h, "rank": i + 1, "delta": delta})
    rh: list[dict[str, Any]] = []
    if out and profile_of(runtime) != "naive":
        scored = [sc.Scored(h["component"], h["category"], h["score"], [], []) for h in out]
        rh = sc.red_herrings(f, scored)
    if out:
        top = out[0]
        margin = top["score"] - (out[1]["score"] if len(out) > 1 else 0.0)
        emit(
            "decision",
            "rca:leading",
            agent="rca_tournament",
            what=f"leading hypothesis: {top['label']} ({top['score']:.0%})",
            why=f"margin over the runner-up {margin:.0%}; {len(rh)} red herring(s) set aside"
            + (f"; since {hhmm(state.get('as_of'))}" if state.get("as_of") else ""),
        )
    return {"hypotheses": out, "red_herrings": rh}


def report(state: RcaState) -> dict[str, Any]:
    hyps = state.get("hypotheses") or []
    top = hyps[0] if hyps else None
    result: GraphResult = {
        "graph_id": "rca_tournament",
        "status": "ok",
        "event_id": None,
        "correlation_id": None,
        "summary": (f"{top['label']} ({top['score']:.0%})" if top else "no hypothesis could be formed"),
        "artifacts": [
            {
                "kind": "table",
                "name": "hypotheses",
                "ref": None,
                "data": [{k: h[k] for k in ("id", "score", "label")} for h in hyps[:8]],
            }
        ],
        "metrics": {"hypotheses": float(len(hyps)), "top_score": float(top["score"]) if top else 0.0},
        "child_threads": [],
    }
    return {"result": result}
