"""``ops_copilot``: a ``langchain.agents.create_agent`` ReAct agent with a real middleware stack, tools over the Foghorn data
layer, long-term memory, a human review of side-effecting tool calls and an ops-brief structured output.

Middleware, outermost first (docs/design/02 section 2.3 / 1.9; every one of these really exists in langchain 1.4.9):

1. ``PIIMiddleware`` x2 (email, credit card)   redact before the model ever sees customer PII (POL-004)
2. ``CopilotMiddleware``                       editable system prompt + scenario/time/team-preference facts; brief gate
3. ``SummarizationMiddleware``                 long chats are summarised, the last messages kept
4. ``ContextEditingMiddleware``                old tool results are cleared when the prompt gets big
5. ``ModelCallLimitMiddleware`` / ``ToolCallLimitMiddleware``   a runaway tool loop ends politely
6. ``ModelFallbackMiddleware`` > ``ModelRetryMiddleware``      transient provider errors are retried, then a backup model answers
7. ``AuditMiddleware``                         one log event per tool call
8. ``ToolReviewMiddleware``                    HumanInTheLoop (per call): approve / edit / reject / ask on ``propose_action`` and
                                               ``remember_preference``; payload = ``build_interrupt("tool_review")``
9. ``ToolRetryMiddleware``                     flaky read-only tools are retried (inside the review: a retry never re-asks the human)
"""

from __future__ import annotations

from typing import Any, Literal

from langchain.agents import create_agent
from langchain.agents.middleware import (
    ClearToolUsesEdit,
    ContextEditingMiddleware,
    ModelCallLimitMiddleware,
    ModelFallbackMiddleware,
    ModelRetryMiddleware,
    PIIMiddleware,
    SummarizationMiddleware,
    ToolCallLimitMiddleware,
    ToolRetryMiddleware,
)
from langchain.agents.structured_output import ProviderStrategy, ToolStrategy
from langchain_core.language_models.chat_models import BaseChatModel

from foghorn.flows.ops_copilot.brief import OpsBrief
from foghorn.flows.ops_copilot.context import CopilotContext
from foghorn.flows.ops_copilot.middleware import AuditMiddleware, CopilotMiddleware, ToolReviewMiddleware
from foghorn.flows.ops_copilot.scripts import SUMMARY_SCRIPT, build_script
from foghorn.flows.ops_copilot.state import CopilotState
from foghorn.flows.ops_copilot.tools import OWN_TOOLS, READ_ONLY_TOOLS
from foghorn.models import offline as o
from foghorn.models.factory import get_chat_model
from foghorn.settings import get_settings
from foghorn.tools.lc import tools_for

Strategy = Literal["auto", "tool", "provider"]
TRANSIENT = (TimeoutError, ConnectionError, OSError)


def build_graph(
    checkpointer: Any = None,
    store: Any = None,
    *,
    model: BaseChatModel | None = None,
    response_strategy: Strategy | None = None,
    script: o.Script | None = None,
    summary_model: BaseChatModel | None = None,
    summarize_after: int = 40,
    summarize_keep: int = 12,
    **_deps: Any,
) -> Any:
    """Compile the copilot. No checkpointer unless a test passes one (the server injects its own).

    ``response_strategy``: how the ops brief is produced - ``tool`` (a tool call; the narration still streams as text),
    ``provider`` (native JSON schema), ``auto`` (the model profile decides). Default: ``tool`` offline, ``auto`` with a real
    provider (Claude 5.x rejects the forced tool choice a tool strategy needs).
    """
    offline = get_settings().resolved_provider() == "offline"
    script = script or build_script()
    smart = model or get_chat_model("smart", offline_script=script)
    backup = get_chat_model("fast", offline_script=script)
    summariser = summary_model or get_chat_model("fast", offline_script=SUMMARY_SCRIPT)
    strategy: Strategy = response_strategy or ("tool" if offline else "auto")
    response_format: Any = {
        "tool": ToolStrategy(OpsBrief, tool_message_content="Ops brief filed."),
        "provider": ProviderStrategy(OpsBrief),
        "auto": OpsBrief,
    }[strategy]

    middleware: list[Any] = [
        PIIMiddleware("email", strategy="redact"),
        PIIMiddleware("credit_card", strategy="redact"),
        CopilotMiddleware(),
        SummarizationMiddleware(
            model=summariser, trigger=("messages", summarize_after), keep=("messages", summarize_keep)
        ),
        ContextEditingMiddleware(edits=[ClearToolUsesEdit(trigger=60_000, keep=6, exclude_tools=("ask_user",))]),
        ModelCallLimitMiddleware(run_limit=24, exit_behavior="end"),
        ToolCallLimitMiddleware(run_limit=18),
        ModelFallbackMiddleware(backup),
        ModelRetryMiddleware(max_retries=2, retry_on=TRANSIENT, on_failure="error", initial_delay=0.05, jitter=False),
        AuditMiddleware(),
        ToolReviewMiddleware(),
        ToolRetryMiddleware(
            max_retries=1,
            tools=list(READ_ONLY_TOOLS),
            retry_on=TRANSIENT,
            on_failure="continue",
            initial_delay=0.05,
            jitter=False,
        ),
    ]
    return create_agent(
        smart,
        [*tools_for(READ_ONLY_TOOLS), *OWN_TOOLS],
        middleware=middleware,
        response_format=response_format,
        state_schema=CopilotState,
        context_schema=CopilotContext,
        checkpointer=checkpointer,
        store=store,
        name="ops_copilot",
    )
