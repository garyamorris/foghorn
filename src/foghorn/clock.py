"""Indirection for time and ids so tests can freeze them without monkeypatching the stdlib."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

_now: Callable[[], datetime] = lambda: datetime.now(UTC)  # noqa: E731
_new_id: Callable[[], str] = lambda: uuid.uuid4().hex  # noqa: E731


def now() -> datetime:
    return _now()


def new_id() -> str:
    return _new_id()


def install(*, now_fn: Callable[[], datetime] | None = None, id_fn: Callable[[], str] | None = None) -> None:
    global _now, _new_id
    if now_fn:
        _now = now_fn
    if id_fn:
        _new_id = id_fn
