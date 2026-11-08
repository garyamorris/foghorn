"""Where the pipeline reads the commander's thread from. An injectable seam: production uses the in-process SDK client,
tests install a fake with ``set_source_factory``."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from foghorn.logging import get_logger

log = get_logger("postmortem.ports")


class IncidentSource(Protocol):
    async def commander_state(self, thread_id: str) -> dict[str, Any] | None:
        """``values`` of the commander thread (``values["report"]`` is the ``CommanderReport``), or None if unknown."""
        ...


class SdkIncidentSource:
    """Reads ``threads.get_state`` through the in-process SDK client (no network hop, same process as the server)."""

    def __init__(self, client_factory: Callable[[], Any] | None = None) -> None:
        self._factory = client_factory

    def _client(self) -> Any:
        if self._factory is not None:
            return self._factory()
        from langgraph_sdk import get_client

        return get_client(url=None)

    async def commander_state(self, thread_id: str) -> dict[str, Any] | None:
        state = await self._client().threads.get_state(thread_id)
        values = state.get("values") if isinstance(state, dict) else None
        return dict(values) if isinstance(values, dict) and values else None


SourceFactory = Callable[[], IncidentSource]
_factory: SourceFactory = SdkIncidentSource


def set_source_factory(factory: SourceFactory | None) -> None:
    """Tests inject a fake; ``None`` restores the SDK-backed default."""
    global _factory
    _factory = factory or SdkIncidentSource


def get_source() -> IncidentSource:
    return _factory()
