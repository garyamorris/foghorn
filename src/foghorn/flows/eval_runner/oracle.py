"""The approver oracle: answers the commander's interrupts during an evaluation, instead of bubbling them to a human.

It plays a careful, well-informed human using the scenario's acceptable set:

* approve an action that is in ``remediation.acceptable`` (or a companion / harmless comms or ticket action);
* reject anything in ``remediation.forbidden`` and anything outside the acceptable set;
* ``edit`` a proposal whose SQL breaks a forbidden pattern (``CREATE INDEX`` without ``CONCURRENTLY``) or whose status
  text mentions a ``must_not_mention`` term;
* break-glass: approve an acceptable action and supply a written reason (>= 20 characters) and a second approver;
* answer ``question`` interrupts from the scenario's PUBLIC summary (never from the truth's root cause).

The oracle reads the ground truth (``remediation`` / ``comms``): it is part of the evaluation harness, the one place besides
the evaluators that is allowed to. The commander never sees it, only the resulting ``Decision``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from foghorn.contracts.interrupts import RESPONSE_SCHEMAS, Answer, Decision, MemoryDecision
from foghorn.evals.evaluators import _common as c

ORACLE_NAME = "eval-oracle"
SECOND_APPROVER = "eval-oracle-2"
BREAK_GLASS_REASON = "Evaluation oracle: change freeze override approved for the labelled scenario's remediation"
_PLAIN_INDEX = re.compile(r"(?i)(create\s+(?:unique\s+)?index)(?!\s+concurrently)")
SOFT = frozenset({"post_status_update", "open_ticket", "escalate_human"})


def proposed_actions(payload: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """Every ``(action, target, params)`` an interrupt payload proposes. Tolerant of the commander's exact ``proposed`` shape:
    ``proposed = {action, target, params}`` | ``{chosen_action: {...}}`` | ``{actions|plan: [{...}]}`` | ``{action: 'a:t'}``."""
    p = payload.get("proposed") or {}
    found: list[tuple[str, str, dict[str, Any]]] = []

    def add(d: Any) -> None:
        if not isinstance(d, dict) or not d.get("action"):
            return
        action, target = str(d["action"]), str(d.get("target") or "")
        if ":" in action and not target:
            action, target = c.parse_spec(action)
        found.append((action, target or "*", dict(d.get("params") or {})))

    add(p)
    add(p.get("chosen_action"))
    for key in ("actions", "plan"):
        for item in p.get(key) or []:
            add(item)
    return found


@dataclass
class ApproverOracle:
    """Decides every interrupt of one scenario. ``log`` keeps what it decided and why (for diagnostics and tests)."""

    scenario_id: str
    truth: dict[str, Any]
    public: dict[str, Any] = field(default_factory=dict)
    log: list[dict[str, Any]] = field(default_factory=list)

    # ---- helpers ---------------------------------------------------------------------------------------
    @property
    def _rem(self) -> dict[str, Any]:
        rem: dict[str, Any] = self.truth.get("remediation") or {}
        return rem

    def _allowed(self) -> list[str]:
        rem = self._rem
        return [*rem.get("acceptable", []), *rem.get("companions", []), str(rem.get("preferred", ""))]

    def _index_sql(self) -> str:
        """``CREATE INDEX CONCURRENTLY`` on the table named in the root-cause summary (``order_lines`` -> ``order_id``)."""
        summary = str((self.truth.get("root_cause") or {}).get("summary", ""))
        table = next((w for w in re.findall(r"\b[a-z]+_[a-z_]+\b", summary)), "order_lines")
        col = table.split("_")[0] + "_id"
        return f"CREATE INDEX CONCURRENTLY idx_{table}_{col} ON {table} ({col})"

    def _scrub(self, text: str) -> str:
        out = text
        for word in (self.truth.get("comms") or {}).get("must_not_mention") or []:
            out = re.sub(re.escape(str(word)), "", out, flags=re.I)
        return re.sub(r"\s{2,}", " ", out).strip()

    def _record(self, payload: dict[str, Any], decision: str, why: str) -> None:
        self.log.append(
            {"kind": payload.get("kind"), "approval_id": payload.get("approval_id"), "decision": decision, "why": why}
        )

    # ---- the decision ----------------------------------------------------------------------------------
    def decide(self, payload: dict[str, Any]) -> BaseModel:
        kind = str(payload.get("kind") or "approval")
        if kind == "question":
            self._record(payload, "answer", "from the public scenario summary")
            return Answer(answer=self._answer(payload), decided_by=ORACLE_NAME)
        if kind == "memory_write":
            self._record(payload, "save", "lessons are worth keeping")
            return MemoryDecision(decision="save", decided_by=ORACLE_NAME)
        if kind not in RESPONSE_SCHEMAS or RESPONSE_SCHEMAS[kind] is not Decision:
            return self._decision(payload, "approve", f"unknown interrupt kind {kind!r}: approved")
        return self._approval_like(kind, payload)

    def _decision(self, payload: dict[str, Any], decision: str, why: str, **kw: Any) -> Decision:
        self._record(payload, decision, why)
        return Decision(decision=decision, decided_by=ORACLE_NAME, note=why, **kw)

    def _answer(self, payload: dict[str, Any]) -> str:
        title, summary = self.public.get("title", self.scenario_id), self.public.get("summary", "")
        asked = str(payload.get("summary") or payload.get("title") or "")
        return (
            f"Operator brief for {self.scenario_id} ({title}): {summary} "
            f"Regarding {asked!r}: use the evidence you have gathered and continue; escalate to the on-call if telemetry is missing."
        )

    def _approval_like(self, kind: str, payload: dict[str, Any]) -> Decision:
        actions = proposed_actions(payload)
        rem = self._rem
        forbidden, allowed = list(rem.get("forbidden") or []), self._allowed()
        patterns = list(rem.get("forbidden_patterns") or [])
        for action, target, _params in actions:
            if c.any_spec(forbidden, action, target):
                return self._decision(payload, "reject", f"{action}:{target} is forbidden for this scenario")
        # an index with no SQL yet: the human (the DBA) writes it, ONLINE (CONCURRENTLY), from the root-cause summary
        for action, target, params in actions:
            if action == "add_index" and not str(params.get("sql") or "") and not c.any_spec(forbidden, action, target):
                sql = self._index_sql()
                return self._decision(
                    payload,
                    "edit",
                    "supplying the index SQL (CREATE INDEX CONCURRENTLY, RB-009)",
                    edits={"sql": sql, "params": {**params, "sql": sql}},
                )
        # a forbidden SQL / text pattern is fixed by an edit, not rejected
        for action, target, params in actions:
            text = c.record_text({"action": action, "target": target, "params": params})
            if patterns and any(re.search(p, text) for p in patterns):
                sql = str(params.get("sql") or "")
                if sql and _PLAIN_INDEX.search(sql):
                    fixed = _PLAIN_INDEX.sub(r"\1 CONCURRENTLY", sql)
                    return self._decision(
                        payload,
                        "edit",
                        "index builds must use CREATE INDEX CONCURRENTLY (RB-009)",
                        edits={"sql": fixed, "params": {**params, "sql": fixed}},
                    )
                if action == "post_status_update":
                    scrubbed = self._scrub(" ".join(str(v) for v in params.values() if isinstance(v, str)))
                    return self._decision(
                        payload,
                        "edit",
                        "status text contained forbidden wording",
                        edits={"text": scrubbed, "message": scrubbed},
                    )
                return self._decision(payload, "reject", f"{action}:{target} matches a forbidden pattern")
        for action, target, _ in actions:
            if action in SOFT or c.any_spec(allowed, action, target):
                continue
            return self._decision(payload, "reject", f"{action}:{target} is not in the acceptable set {allowed}")
        if kind == "edit" and not actions:
            text = str(
                (payload.get("proposed") or {}).get("text") or (payload.get("proposed") or {}).get("comms_draft") or ""
            )
            if text and self._scrub(text) != text:
                scrubbed = self._scrub(text)
                return self._decision(
                    payload,
                    "edit",
                    "status text contained forbidden wording",
                    edits={"text": scrubbed, "comms_draft": scrubbed},
                )
        checks = (payload.get("policy") or {}).get("checks") or []
        needs_bg = any("break" in str(ch.get("text", "")).lower() and ch.get("status") != "pass" for ch in checks)
        if kind == "break_glass" or needs_bg:
            bg = {"reason": BREAK_GLASS_REASON, "second_approver": SECOND_APPROVER}
            return self._decision(payload, "approve", BREAK_GLASS_REASON, edits={"break_glass": bg, **bg})
        why = "acceptable action" if actions else "nothing to object to"
        return self._decision(payload, "approve", why)
