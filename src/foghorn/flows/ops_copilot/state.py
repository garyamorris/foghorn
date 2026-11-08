"""State of ``ops_copilot``: the agent's ``messages`` (so Studio's chat mode works) plus the three keys every investigation
tool reads from graph state (``scenario_id``, ``as_of``, ``applied_actions``)."""

from __future__ import annotations

from typing import NotRequired

from foghorn.tools.lc import ToolState


class CopilotState(ToolState):
    """``messages`` + ``scenario_id`` / ``as_of`` / ``applied_actions`` (idempotent reducer) + ``structured_response``
    (the OpsBrief, only when a brief was asked for). Editable in Studio like any state."""

    team: NotRequired[str]
