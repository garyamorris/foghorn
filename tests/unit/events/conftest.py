"""Fixtures for the events subsystem tests. Every file goes under ``tmp_path`` (FOGHORN_DATA_DIR) - never the repo's ``var/``."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from foghorn.events.config import get_events_settings
from foghorn.events.service import EventsRuntime
from foghorn.settings import get_settings
from tests.unit.events.fakes import FakeClient, FakeData, FakeWall, make_runtime


@dataclass
class Env:
    rt: EventsRuntime
    client: FakeClient
    data: FakeData
    wall: FakeWall

    async def ingest(self, **ev: Any) -> Any:
        ev.setdefault("source", "test")
        return await self.rt.bus.ingest(ev, origin=ev.pop("_origin", "api"))

    async def idle(self) -> None:
        await self.rt.dispatcher.run_until_idle()


@pytest.fixture(autouse=True)
def _events_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FOGHORN_DATA_DIR", str(tmp_path / "var"))
    for k in ("FOGHORN_DASHBOARD_API_TOKEN", "FOGHORN_WEBHOOK_REQUIRE_SIGNATURE", "FOGHORN_MAX_RUNS"):
        monkeypatch.delenv(k, raising=False)
    get_settings.cache_clear()
    get_events_settings.cache_clear()
    yield
    get_settings.cache_clear()
    get_events_settings.cache_clear()


@pytest.fixture
async def make_env(tmp_path: Path) -> AsyncIterator[Callable[..., Any]]:
    created: list[EventsRuntime] = []

    async def make(*, start: bool = True, **es: Any) -> Env:
        rt, client, data, wall = make_runtime(tmp_path, **es)
        if start:
            await rt.start(background=False)
        created.append(rt)
        return Env(rt, client, data, wall)

    yield make
    for rt in created:
        await rt.stop()


@pytest.fixture
async def env(make_env: Callable[..., Any]) -> Env:
    return await make_env()
