"""Events-subsystem configuration (kept out of the shared ``settings.py``; ARCHITECTURE.md rule 8).

Every knob is an env var with the ``FOGHORN_`` prefix, e.g. ``FOGHORN_MAX_RUNS=2``. Tests build an instance directly
(``EventsSettings(max_runs=1, ...)``) and point ``FOGHORN_DATA_DIR`` at ``tmp_path``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PRIORITIES: tuple[str, ...] = ("P0", "P1", "P2", "P3")
APPROVAL_POLICIES: tuple[str, ...] = ("strict", "standard", "auto_low_risk")
#: Stream modes the dispatcher asks for when it creates a run. The server rejects ``tools`` / ``lifecycle`` (422).
DEFAULT_STREAM_MODES: tuple[str, ...] = ("values", "updates", "messages-tuple", "custom", "tasks", "checkpoints")


class EventsSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="FOGHORN_", extra="ignore", env_file_encoding="utf-8")

    # --- routing -------------------------------------------------------------------------------------------------
    routing_file: Path | None = None  # default: the packaged routing.yaml

    # --- queues / backpressure (docs/design/03 section 3.6) --------------------------------------------------------
    queue_cap_p0: int = Field(500, ge=1)
    queue_cap_p1: int = Field(500, ge=1)
    queue_cap_p2: int = Field(1000, ge=1)
    queue_cap_p3: int = Field(200, ge=1)
    high_water: float = Field(0.8, gt=0, le=1)
    low_water: float = Field(0.5, ge=0, lt=1)
    retry_after_s: int = Field(5, ge=1)
    queue_weights: tuple[int, int, int, int] = (8, 4, 2, 1)
    aging_s: float = 30.0  # anything waiting longer is promoted one class

    # --- dispatcher ----------------------------------------------------------------------------------------------
    max_runs: int = Field(4, ge=1)
    per_graph_max_runs: dict[str, int] = {"incident_commander": 3, "eval_runner": 1}
    max_attempts: int = Field(3, ge=1)
    backoff_s: list[float] = [1.0, 4.0, 16.0]
    run_poll_interval_s: float = Field(0.25, gt=0)
    run_timeout_s: float = 3600.0
    chaos_slow_s: float = 2.0
    run_stream_modes: list[str] = list(DEFAULT_STREAM_MODES)
    run_stream_subgraphs: bool = True
    run_stream_resumable: bool = True
    scan_limit: int = Field(200, ge=1)  # interrupted threads inspected per approvals scan

    # --- dedupe / storms -----------------------------------------------------------------------------------------
    dedupe_window_s: float = 300.0  # virtual seconds
    storm_count: int = Field(8, ge=2)
    storm_within_s: float = 60.0  # virtual seconds
    storm_cooldown_s: float = 120.0  # virtual seconds during which further alerts are storm members
    storm_suppress_members: bool = True

    # --- SSE -----------------------------------------------------------------------------------------------------
    sse_buffer: int = Field(1000, ge=2)
    sse_heartbeat_s: float = Field(15.0, gt=0)
    sse_replay_page: int = Field(500, ge=1)
    queue_tick_s: float = Field(2.0, gt=0)
    clock_tick_s: float = Field(1.0, gt=0)

    # --- virtual clock / scheduler / replay / ambient -----------------------------------------------------------------
    replay_speed: float = Field(60.0, gt=0, le=600)
    replay_idle_gap_s: float = Field(
        120.0, ge=0
    )  # skip quiet stretches longer than this many VIRTUAL seconds (0 = off)
    scheduler_tick_s: float = Field(1.0, gt=0)
    cron_jobs: dict[str, str] = {"shift_handoff": "0 */8 * * *"}  # virtual-time cron expressions (UTC)
    native_cron_schedule: str = "0 */8 * * *"  # the DISABLED server-side cron for shift_handoff
    ambient_enabled: bool = False
    ambient_stream: str = "stream_quiet_hour"
    ambient_speed: float = Field(60.0, gt=0, le=600)

    # --- run defaults ---------------------------------------------------------------------------------------------
    default_model_profile: Literal["competent", "naive", "cautious", "gullible"] = "competent"
    default_approval_policy: Literal["strict", "standard", "auto_low_risk"] = "standard"
    default_token_delay_ms: int = 0
    scenario_category_public: bool = (
        True  # False hides the fault category (a hint at the root cause) from the public view
    )
    chaos_mode: bool = (
        False  # runtime-switchable: new runs get context `chaos: "tools"` (seeded transient tool failures)
    )
    run_config_mode: Literal["context", "configurable"] = "context"  # how the dispatcher passes per-run settings

    # --- webhooks / links -----------------------------------------------------------------------------------------
    webhook_require_signature: bool = False
    studio_thread_url: str | None = None  # e.g. "{studio}/thread/{thread_id}?baseUrl={base}" - UNVERIFIED format

    # --- lifespan ------------------------------------------------------------------------------------------------
    start_background: bool = True
    seed_on_start: bool = True
    ready_timeout_s: float = 90.0
    ready_poll_s: float = 0.5

    @property
    def queue_caps(self) -> dict[str, int]:
        return {"P0": self.queue_cap_p0, "P1": self.queue_cap_p1, "P2": self.queue_cap_p2, "P3": self.queue_cap_p3}


@lru_cache
def get_events_settings() -> EventsSettings:
    return EventsSettings()


def state_db_path(data_dir: Path) -> Path:
    """The SQLite state DB lives next to the generated datasets: ``<data_dir>/state.sqlite``."""
    return data_dir / "state.sqlite"
