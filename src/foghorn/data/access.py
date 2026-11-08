"""Read-side plumbing shared by ``foghorn.data.api`` and ``foghorn.tools``: cached reads of committed datasets,
the lazy hash-checked telemetry build, and per-scenario database resolution.

No global mutable state beyond ``lru_cache``s keyed by (path, mtime, size) of immutable file contents.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any

from foghorn.data import build as build_mod
from foghorn.data import docs, paths, store
from foghorn.data.util import DEFAULT_SEED, GENERATOR_VERSION

_LOCK = threading.Lock()
_log = logging.getLogger(__name__)
_CURATED = re.compile(r"^S\d{2}$")
_GENERATED = re.compile(r"^X-\d{4}$")


class ScenarioNotFound(KeyError):
    """Unknown scenario id."""


def is_generated(sid: str) -> bool:
    return bool(_GENERATED.match(sid))


# --- cached file reads ----------------------------------------------------------------------------------------------------------
def _sig(p: Path) -> tuple[str, int, int]:
    st = p.stat()
    return (str(p), st.st_mtime_ns, st.st_size)


@lru_cache(maxsize=256)
def _read_text(path: str, _mtime: int, _size: int) -> str:
    return Path(path).read_text(encoding="utf-8")


def read_text(p: Path) -> str:
    return _read_text(*_sig(p))


@lru_cache(maxsize=256)
def _json(path: str, mtime: int, size: int) -> Any:
    return json.loads(_read_text(path, mtime, size))


def read_json(p: Path) -> Any:
    return _json(*_sig(p))


@lru_cache(maxsize=64)
def _jsonl(path: str, mtime: int, size: int) -> tuple[dict[str, Any], ...]:
    return tuple(json.loads(line) for line in _read_text(path, mtime, size).splitlines() if line.strip())


def read_jsonl(p: Path) -> tuple[dict[str, Any], ...]:
    return _jsonl(*_sig(p))


# --- datasets directory -----------------------------------------------------------------------------------------------------
def ensure_datasets() -> Path:
    """The directory holding the committed datasets; builds them under data_dir when the repo copy is absent."""
    d = paths.datasets_dir()
    if (d / "MANIFEST.json").exists():
        return d
    with _LOCK:
        if not (d / "MANIFEST.json").exists():
            build_mod.build(d, DEFAULT_SEED, None, data_dir=None, telemetry=False)
    return d


def manifest() -> dict[str, Any]:
    out: dict[str, Any] = read_json(ensure_datasets() / "MANIFEST.json")
    return out


def markdown_docs(sub: str) -> list[tuple[dict[str, Any], str]]:
    d = ensure_datasets() / "reference" / sub
    return [docs.parse_markdown(read_text(p)) for p in sorted(d.glob("*.md"))]


# --- telemetry ------------------------------------------------------------------------------------------------------------------
@lru_cache(maxsize=64)
def _db_verified(path: str, _mtime: int, _size: int, expected: str | None) -> bool:
    meta = store.db_meta(Path(path))
    if not meta or meta.get("generator_version") != GENERATOR_VERSION:
        return False
    return meta.get("built_for", "") == (expected or "")


def expected_telemetry_sha() -> str | None:
    try:
        ent = manifest().get("datasets", {}).get("telemetry")
    except OSError:
        return None
    return ent["sha256"] if ent else None


def ensure_telemetry() -> Path:
    """Path of the curated telemetry DB, (re)built lazily if missing or if its content hash differs from MANIFEST."""
    path = paths.telemetry_path()
    expected = expected_telemetry_sha()
    if path.exists() and _db_verified(*_sig(path), expected):
        return path
    with _LOCK:
        if path.exists() and _db_verified(*_sig(path), expected):
            return path
        worlds = build_mod.curated_worlds(DEFAULT_SEED)
        per = {sid: store.rows_digest(store.rows_for_world(w)) for sid, w in worlds.items()}
        digest = store.combine_digests(per)
        if expected is not None and digest["sha256"] != expected:
            _log.warning(
                "telemetry differs from datasets/MANIFEST.json; run `python -m foghorn.data build` and review the diff"
            )
        store.write_db(path, worlds, digest, built_for=expected or "")
        build_mod.curated_worlds.cache_clear()  # do not keep ~30 MB of generated worlds alive in a server process
    return path


def db_path(sid: str) -> Path:
    if _CURATED.match(sid):
        return ensure_telemetry()
    if is_generated(sid):
        p = paths.generated_dir() / f"{sid}.sqlite"
        if not p.exists():
            raise ScenarioNotFound(sid)
        return p
    raise ScenarioNotFound(sid)


def scenario_record(sid: str) -> dict[str, Any]:
    """The full (truth-bearing) scenario record. Internal: public views are built in ``api``."""
    if _CURATED.match(sid):
        p = ensure_datasets() / "scenarios" / f"{sid}.json"
        if not p.exists():
            raise ScenarioNotFound(sid)
        rec: dict[str, Any] = read_json(p)
        return rec
    if is_generated(sid):
        p = paths.generated_dir() / f"{sid}.json"
        if not p.exists():
            raise ScenarioNotFound(sid)
        rec = read_json(p)
        return rec
    raise ScenarioNotFound(sid)


def truth(sid: str) -> dict[str, Any]:
    t: dict[str, Any] = scenario_record(sid)["truth"]
    return t
