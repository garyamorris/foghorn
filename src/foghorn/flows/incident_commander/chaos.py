"""Chaos mode (run context ``chaos == "tools"``): deterministic, transient tool failures that exercise ``RetryPolicy``.

A (thread, tool, key) triple fails ONCE when its stable hash falls in the unlucky third, then succeeds: retries heal it, a
replay of the same thread sees the same world. ``FAILURES`` lets tests assert that chaos really bit.
"""

from __future__ import annotations

from foghorn.models.offline import stable_int

_SEEN: set[str] = set()
FAILURES: list[str] = []


class ChaosError(ConnectionError):
    """A transient failure injected by chaos mode (retryable: a ConnectionError)."""


def maybe_fail(chaos: str | None, thread_id: str | None, name: str, key: str) -> None:
    if chaos != "tools":
        return
    token = f"{thread_id}|{name}|{key}"
    if stable_int(token) % 3 != 0 or token in _SEEN:
        return
    _SEEN.add(token)
    FAILURES.append(token)
    raise ChaosError(f"chaos: transient failure calling {name}")


def reset() -> None:
    _SEEN.clear()
    FAILURES.clear()
