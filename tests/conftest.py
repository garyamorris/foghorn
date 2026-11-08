"""Global test guards. Order matters: env is fixed BEFORE any foghorn import."""

from __future__ import annotations

import os
import socket
import tempfile

# 1. Force offline + kill every credential, even ones in a developer's .env. EMPTY string (not pop):
#    pydantic-settings lets a real (even empty) env var beat the .env file; pop() would let .env win.
os.environ.update(
    FOGHORN_PROVIDER="offline",
    FOGHORN_ENVIRONMENT="test",
    # generated data + state DBs go to a throwaway dir: tests must never write into the repo (`var/` is watched by
    # `langgraph dev` hot reload and would pollute a developer's runtime state)
    FOGHORN_DATA_DIR=tempfile.mkdtemp(prefix="foghorn-tests-"),
    ANTHROPIC_API_KEY="",
    OPENAI_API_KEY="",
    LANGSMITH_API_KEY="",
    LANGCHAIN_API_KEY="",
    LANGSMITH_TRACING="false",
    LANGCHAIN_TRACING_V2="false",
    LANGSMITH_TEST_TRACKING="false",
    LANGSMITH_ENDPOINT="http://127.0.0.1:9",  # even a stray client cannot reach the real API
)

import datetime as dt  # noqa: E402
import itertools  # noqa: E402

import pytest  # noqa: E402

from foghorn import clock  # noqa: E402
from foghorn.models.offline import SCRIPT_OVERRIDES  # noqa: E402
from foghorn.settings import get_settings  # noqa: E402

_LOOPBACK = {"127.0.0.1", "::1", "localhost", "0.0.0.0"}  # noqa: S104
_real_connect = socket.socket.connect


def _guarded_connect(self: socket.socket, address: object) -> None:
    host = address[0] if isinstance(address, tuple) else address
    if isinstance(host, str) and host not in _LOOPBACK and not host.startswith("/"):
        raise RuntimeError(f"network access blocked in tests: {address!r}")
    return _real_connect(self, address)  # type: ignore[arg-type]


@pytest.fixture(autouse=True, scope="session")
def _no_network():
    socket.socket.connect = _guarded_connect  # type: ignore[method-assign]
    yield
    socket.socket.connect = _real_connect  # type: ignore[method-assign]


@pytest.fixture(autouse=True)
def _isolate():
    get_settings.cache_clear()
    SCRIPT_OVERRIDES.clear()
    yield
    SCRIPT_OVERRIDES.clear()
    get_settings.cache_clear()


@pytest.fixture
def frozen():
    """Deterministic clock + id factory (inject-don't-patch)."""
    counter = itertools.count(1)
    clock.install(now_fn=lambda: dt.datetime(2026, 1, 1, tzinfo=dt.UTC), id_fn=lambda: f"id-{next(counter):04d}")
    yield
    clock.install(now_fn=lambda: dt.datetime.now(dt.UTC), id_fn=lambda: __import__("uuid").uuid4().hex)
