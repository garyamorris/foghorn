"""Custom middleware of ``ops_copilot`` (the rest of the stack is stock ``langchain.agents.middleware``).

* :class:`ToolReviewMiddleware`  a ``HumanInTheLoopMiddleware`` (per-call mode) whose interrupts are Foghorn's
                                 ``build_interrupt("tool_review", ...)`` payloads resumed with a ``Decision``
                                 (approve / edit / reject / ask), instead of langchain's generic request shape
* :class:`CopilotMiddleware`     a dynamic system prompt (editable prompt + scenario + time + team preferences from the Store) and
                                 the brief gate (structured output only when the operator asks for a brief)
* :class:`AuditMiddleware`       a ``wrap_tool_call`` hook that streams one deterministic ``log`` event per tool call

Order matters (docs/design/02 section 2.3): hooks run outermost-first; the review sits inside the audit hook so a resumed
call re-emits an already-seen (idempotent) audit event, and outside tool retry so a retry never re-asks the human.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, HumanInTheLoopMiddleware, ModelRequest, ModelResponse
from langchain.agents.middleware.human_in_the_loop import Action
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langgraph.config import get_config
from langgraph.types import Command, interrupt

from foghorn.common.lifecycle import emit_event
from foghorn.common.stream import emit
from foghorn.contracts.interrupts import Decision, PolicyCheck, build_interrupt
from foghorn.data import actions as act
from foghorn.data.access import ScenarioNotFound
from foghorn.data.util import in_freeze
from foghorn.flows.ops_copilot.brief import wants_brief
from foghorn.flows.ops_copilot.context import DEFAULT_SYSTEM_PROMPT, ctx_get
from foghorn.flows.ops_copilot.tools import REVIEWED_TOOLS, needs_review
from foghorn.memory import api as mem
from foghorn.tools.context import ToolError, resolve
from foghorn.tools.lc import tool_context

ToolResult = ToolMessage | Command[Any]


def _ctx(request: ModelRequest[Any] | ToolCallRequest) -> Any:
    runtime = request.runtime
    return getattr(runtime, "context", None)


def _policy(ctx: Any) -> str:
    return str(ctx_get(ctx, "approval_policy", "standard"))


def _thread() -> str:
    try:
        return str((get_config().get("configurable") or {}).get("thread_id") or "")
    except RuntimeError:
        return ""


# -------------------------------------------------------------------------------------------- tool review (HITL)
class ToolReviewMiddleware(HumanInTheLoopMiddleware):
    """Per-call human review of the side-effecting tools, speaking Foghorn's interrupt vocabulary.

    The interrupt value is ``build_interrupt("tool_review", ...)`` and the resume value is a ``Decision``:
    ``approve`` runs the call as proposed, ``edit`` merges ``Decision.edits`` into the arguments first (the model is told its
    call was replaced), ``reject`` skips the tool and tells the model why, ``ask`` skips it and relays the reviewer's question.
    """

    def __init__(self, tools: tuple[str, ...] = REVIEWED_TOOLS) -> None:
        super().__init__(
            {t: {"allowed_decisions": ["approve", "edit", "reject"]} for t in tools},
            interrupt_mode="per_call",
            description_prefix="The copilot wants to run a tool that changes something",
        )

    # --- the payload -------------------------------------------------------------------------------------
    def build_payload(self, request: ToolCallRequest) -> dict[str, Any]:
        call = request.tool_call
        name, args = call["name"], dict(call["args"])
        approval_id = f"tr_{call['id']}"
        if name == "propose_action":
            action, target = str(args.get("action", "")), str(args.get("target", ""))
            spec = act.ACTIONS.get(action)
            try:
                as_of_s: int | None = resolve(
                    tool_context(request.runtime)
                ).as_of_s  # the scenario default when state has none
            except (ToolError, ScenarioNotFound):
                as_of_s = None
            checks = [
                PolicyCheck(
                    id="POL-005",
                    status="pass",
                    text=f"{action} needs {spec.approvers if spec else '?'} approver(s): within the copilot's remit",
                )
            ]
            if (
                spec is not None
                and as_of_s is not None
                and act.break_glass_needed(action, target, frozen=in_freeze(as_of_s))
            ):
                checks.append(
                    PolicyCheck(
                        id="POL-003",
                        status="warn",
                        text="change freeze: break-glass (reason + second approver) is required",
                    )
                )
            return build_interrupt(
                "tool_review",
                approval_id=approval_id,
                title=f"Review: {action} on {target}",
                summary=str(args.get("rationale") or f"The copilot proposes {action} on {target}."),
                risk=spec.risk if spec else "medium",  # type: ignore[arg-type]
                reversible=bool(spec.reversible) if spec else False,
                blast_radius=[target],
                policy_checks=checks,
                roles=list(spec.extra_roles) if spec else [],
                proposed={
                    "tool": name,
                    "action": action,
                    "target": target,
                    "params": args.get("params") or {},
                    "args": args,
                },
            )
        return build_interrupt(
            "tool_review",
            approval_id=approval_id,
            title=f"Review: {name}",
            summary=f"The copilot wants to save a preference: {args.get('text', '')}",
            risk="low",
            policy_checks=[
                PolicyCheck(
                    id="POL-004", status="pass", text="stored in the team's namespace, never in a free-text namespace"
                )
            ],
            proposed={"tool": name, "args": args},
        )

    # --- the review --------------------------------------------------------------------------------------
    def _resolve_per_call(self, request: ToolCallRequest) -> tuple[ToolCallRequest, Action | None] | ToolMessage:
        call = request.tool_call
        name, args = call["name"], dict(call["args"])
        if name not in self.interrupt_on or not needs_review(name, args, _policy(_ctx(request))):
            return request, None
        payload = self.build_payload(request)
        approval_id = payload["approval_id"]
        emit_event(
            "approval.requested",
            idempotency_key=f"approval.requested:{approval_id}",
            payload={
                "approval_id": approval_id,
                "kind": "tool_review",
                "thread_id": _thread(),
                "title": payload["title"],
                "summary": payload["summary"],
                "risk": payload["risk"],
            },
        )
        raw = interrupt(payload, response_schema=Decision)  # everything below runs once per resume
        decision = raw if isinstance(raw, Decision) else Decision.model_validate(raw)
        emit_event(
            "approval.decided",
            idempotency_key=f"approval.decided:{approval_id}",
            payload={
                "approval_id": approval_id,
                "decision": decision.decision,
                "decided_by": decision.decided_by,
                "thread_id": _thread(),
            },
        )
        emit(
            "decision",
            f"review:{approval_id}",
            agent="tool_review",
            what=f"{name}: {decision.decision}",
            why=decision.note or "reviewer",
        )
        if decision.decision == "approve":
            return request, None
        if decision.decision == "edit":
            nested = decision.edits.get("args")
            edits: dict[str, Any] = nested if isinstance(nested, dict) else decision.edits
            executed: Action = {"name": name, "args": {**args, **edits}}
            return self._apply_edit(request, executed), executed
        if decision.decision == "ask":
            content = (
                f"The reviewer did not decide; they asked: {decision.question or decision.note!r}. "
                f"`{name}` was NOT executed. Answer the question, then propose again if it is still wanted."
            )
            return ToolMessage(content=content, name=name, tool_call_id=call["id"], status="success")
        reason = f" with reason: {decision.note}" if decision.note else ""
        return ToolMessage(
            content=f"User rejected the tool call for `{name}`{reason}. The tool was not executed. Do not retry it unless asked.",
            name=name,
            tool_call_id=call["id"],
            status="error",
        )


# ------------------------------------------------------------------------------------------ prompt + brief gate
class CopilotMiddleware(AgentMiddleware):
    """System prompt = the editable prompt + facts about this conversation; structured output only for brief requests."""

    @staticmethod
    def _facts(request: ModelRequest[Any], prefs: list[str]) -> str:
        state, ctx = request.state, _ctx(request)
        sid = state.get("scenario_id") or ctx_get(ctx, "scenario_id")
        as_of = state.get("as_of") or ctx_get(ctx, "as_of")
        applied: Any = state.get("applied_actions") or []
        n_applied = len(applied)
        lines = [
            "",
            "Conversation facts (maintained by the platform):",
            f"- scenario: {sid or 'none (not selected yet)'}",
            f"- virtual time (as_of): {as_of or 'scenario default'}",
            f"- actions applied so far: {n_applied}",
        ]
        if prefs:
            lines.append("Team preferences on record (respect them):")
            lines += [f"- {p}" for p in prefs]
        return "\n".join(lines)

    @staticmethod
    def _base(request: ModelRequest[Any]) -> str:
        return str(ctx_get(_ctx(request), "system_prompt", DEFAULT_SYSTEM_PROMPT))

    @staticmethod
    def _team(request: ModelRequest[Any]) -> str:
        return str(ctx_get(_ctx(request), "team", "team-sre"))

    @staticmethod
    def _gate(request: ModelRequest[Any]) -> ModelRequest[Any]:
        """Drop the structured-output requirement unless the latest human message asks for a brief (chat stays chat)."""
        last = next((m for m in reversed(request.messages) if isinstance(m, HumanMessage)), None)
        if last is not None and wants_brief(str(last.text)):
            return request
        return request.override(response_format=None)

    def _apply(self, request: ModelRequest[Any], prefs: list[str]) -> ModelRequest[Any]:
        request = request.override(
            system_message=SystemMessage(content=self._base(request) + self._facts(request, prefs))
        )
        return self._gate(request)

    def wrap_model_call(
        self, request: ModelRequest[Any], handler: Callable[[ModelRequest[Any]], ModelResponse[Any]]
    ) -> Any:
        store = getattr(request.runtime, "store", None) if ctx_get(_ctx(request), "memory_enabled", True) else None
        prefs: list[str] = []
        if store is not None:
            items = store.search(("prefs", self._team(request)), limit=8)
            prefs = [str((i.value or {}).get("text", "")) for i in items if (i.value or {}).get("text")]
        return handler(self._apply(request, prefs))

    async def awrap_model_call(
        self, request: ModelRequest[Any], handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]]
    ) -> Any:
        store = getattr(request.runtime, "store", None) if ctx_get(_ctx(request), "memory_enabled", True) else None
        hits = await mem.get_prefs(store, self._team(request))
        return await handler(self._apply(request, [h["text"] for h in hits[:8] if h["text"]]))


# ------------------------------------------------------------------------------------------------------ audit
class AuditMiddleware(AgentMiddleware):
    """One deterministic ``log`` stream event per tool call (key = the tool call id): the dashboard shows what ran."""

    @staticmethod
    def _announce(request: ToolCallRequest) -> None:
        call = request.tool_call
        text = f"{call['name']}({json.dumps(call['args'], default=str)[:160]})"
        emit("log", f"tool:{call['id']}", agent="tools", level="info", text=text)

    @staticmethod
    def _result(request: ToolCallRequest, res: ToolResult) -> None:
        call = request.tool_call
        status = getattr(res, "status", None) if isinstance(res, ToolMessage) else "ok"
        emit(
            "log",
            f"tool:{call['id']}:done",
            agent="tools",
            level="warn" if status == "error" else "info",
            text=f"{call['name']} -> {status or 'ok'}",
        )

    def wrap_tool_call(self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], ToolResult]) -> ToolResult:
        self._announce(request)
        res = handler(request)
        self._result(request, res)
        return res

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[ToolResult]]
    ) -> ToolResult:
        self._announce(request)
        res = await handler(request)
        self._result(request, res)
        return res
