"""Small shared helpers of the memory keeper (pure, or thin around the memory API)."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from langchain_core.runnables import RunnableConfig

WRITER = "memory_keeper"
GENERAL = "general"  # namespace component for lessons with no component


def thread_id(config: RunnableConfig) -> str | None:
    tid = config.get("configurable", {}).get("thread_id")
    return str(tid) if tid else None


def normalise(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", text.lower())).strip()


def lesson_key(component: str | None, text: str) -> str:
    """Business key of a NEW lesson: the same lesson text for the same component always lands on the same item, so
    re-running a learn pass (or a resumed one) upserts instead of duplicating."""
    digest = hashlib.sha256(f"{component or GENERAL}|{normalise(text)}".encode()).hexdigest()[:10]
    return f"les-{digest}"


def provenance(config: RunnableConfig, incident_id: str | None) -> dict[str, Any]:
    """Every runtime write carries ``provenance`` (docs/design/03 2.7)."""
    meta = config.get("metadata") or {}
    return {
        "thread_id": thread_id(config),
        "event_id": meta.get("foghorn_event_id"),
        "incident_id": incident_id,
        "written_by": WRITER,
    }


def summarise_text(text: str, n: int = 140) -> str:
    return text if len(text) <= n else text[: n - 1] + "..."
