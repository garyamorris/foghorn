"""Determinism primitives for the data generator: the ONE rng factory, canonical JSON, hashing, virtual time.

Rules (each one is enforced by a test):

* every random draw comes from ``rng(*keys)`` - never the module-level ``random`` functions;
* there is no wall clock and no random UUID anywhere in ``foghorn.data``; time is an integer count of epoch seconds
  derived from scenario onsets;
* JSON is canonical (sorted keys, compact separators) wherever a hash is taken.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import random
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any

DEFAULT_SEED = 20261108
GENERATOR_VERSION = "1.0.0"


def rng(*keys: object) -> random.Random:
    """The only RNG factory. String seeds go through SHA-512, so streams are stable across processes,
    platforms and ``PYTHONHASHSEED`` values."""
    return random.Random("|".join(map(str, keys)))


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_hash(obj: Any) -> str:
    return sha256_text(canonical_json(obj))


def stable_int(*keys: object, mod: int = 1 << 32) -> int:
    """Deterministic integer from keys (for ids / tie-breaks), independent of ``PYTHONHASHSEED``."""
    return int(hashlib.sha256("|".join(map(str, keys)).encode()).hexdigest()[:12], 16) % mod


def hex_id(*keys: object, n: int = 6) -> str:
    return hashlib.sha256("|".join(map(str, keys)).encode()).hexdigest()[:n]


# --- virtual time -----------------------------------------------------------------------------------------------------
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@lru_cache(maxsize=65536)
def to_iso(epoch_s: int) -> str:
    return (_EPOCH + timedelta(seconds=epoch_s)).strftime("%Y-%m-%dT%H:%M:%SZ")


@lru_cache(maxsize=4096)
def to_epoch(iso: str) -> int:
    s = iso.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return calendar.timegm(dt.utctimetuple())


def minute_of_day(epoch_s: int) -> int:
    return (epoch_s // 60) % 1440


def weekday(epoch_s: int) -> int:
    """Monday=0 .. Sunday=6 (1970-01-01 was a Thursday)."""
    return (epoch_s // 86400 + 3) % 7


def add_minutes(iso: str, minutes: float) -> str:
    return to_iso(to_epoch(iso) + round(minutes * 60))


def in_freeze(epoch_s: int) -> bool:
    """POL-003 'Peak trading': Mon-Fri 09:00-18:00 UTC."""
    return weekday(epoch_s) < 5 and 9 * 60 <= minute_of_day(epoch_s) < 18 * 60


def clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x
