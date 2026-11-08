"""Run context of the handoff (no prompts: this graph has no model)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class HandoffContext:
    # run-context keys the dispatcher always sends (contracts/meta.py); a dataclass rejects unknown keys
    model_profile: str = "competent"
    approval_policy: str = "standard"
    token_delay_ms: int = 0
    breakpoints: list[str] | None = None
    memory_enabled: bool = True
    chaos: str | None = None
    as_of: str | None = None
    scenario_id: str | None = None
    # handoff settings
    default_window_hours: int = 8
    stale_sev1_minutes: int = 60
