"""Fixtures for the custom-app tests: an in-process ASGI client over ``create_app`` with a fake Agent Server client,
a fake data layer and a tmp SQLite DB. The lifespan is entered explicitly (httpx's ASGITransport does not run it)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from foghorn.events.service import EventsRuntime
from foghorn.server.app import create_app
from tests.unit.events.conftest import _events_env  # noqa: F401  (autouse env isolation shared with the events tests)
from tests.unit.events.fakes import FakeClient, FakeData, FakeWall, make_runtime


@dataclass
class Api:
    http: httpx.AsyncClient
    app: FastAPI
    rt: EventsRuntime
    client: FakeClient
    data: FakeData
    wall: FakeWall

    async def get(self, url: str, **kw: Any) -> httpx.Response:
        return await self.http.get(url, **kw)

    async def post(self, url: str, json: Any = None, **kw: Any) -> httpx.Response:
        return await self.http.post(url, json=json, **kw)

    async def event(self, **ev: Any) -> httpx.Response:
        ev.setdefault("source", "test")
        return await self.http.post("/api/events", json=ev)

    async def idle(self) -> None:
        await self.rt.dispatcher.run_until_idle()


@pytest.fixture
async def api(tmp_path: Path) -> AsyncIterator[Api]:
    holder: dict[str, Any] = {}

    def factory() -> EventsRuntime:
        rt, client, data, wall = make_runtime(tmp_path)
        holder.update(rt=rt, client=client, data=data, wall=wall)
        return rt

    app = create_app(runtime_factory=factory, start_background=False)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield Api(http, app, holder["rt"], holder["client"], holder["data"], holder["wall"])
