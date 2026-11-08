"""Run context of ``storm_correlator`` (every ``CTX_*`` key the dispatcher can send, plus the correlator's own knobs)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class StormContext:
    model_profile: str = "competent"
    approval_policy: str = "standard"
    scenario_id: str | None = None
    as_of: str | None = None
    token_delay_ms: int | None = None
    breakpoints: list[str] | None = None
    memory_enabled: bool | None = None
    chaos: str | None = field(
        default=None,
        metadata={
            "description": (
                "Failure injection on ONE deterministically chosen analysis branch: 'branch' (or 'tools') = it raises, "
                "'slow' = it exceeds the per-branch timeout, 'transient' = its first attempt fails and the retry heals."
            )
        },
    )
    branch_timeout_s: float = field(
        default=20.0, metadata={"description": "Run timeout of each analyze_candidate branch (Send timeout)."}
    )
    max_merge_levels: int = field(
        default=3,
        metadata={"description": "Bound of the map-reduce-reduce merge loop (a counter, not recursion_limit)."},
    )
    dispatch_commander: bool = field(
        default=True, metadata={"description": "False = cluster only; open no incident and start no commander."}
    )
