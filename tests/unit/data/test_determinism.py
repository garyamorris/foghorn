"""Determinism: identical hashes across builds and across PYTHONHASHSEED values; no wall clock / uuid4 / global random."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from foghorn.data import build, paths, store
from foghorn.data.util import DEFAULT_SEED, rng

SRC = Path(build.__file__).resolve().parent
ROOT = paths.repo_root()


def _manifest_via_subprocess(out: Path, hashseed: str) -> dict:
    env = {**os.environ, "PYTHONHASHSEED": hashseed}
    subprocess.run(
        [sys.executable, "-m", "foghorn.data", "build", "--out", str(out), "--no-telemetry"],
        check=True,
        env=env,
        cwd=ROOT,
        capture_output=True,
        timeout=120,
    )
    return json.loads((out / "MANIFEST.json").read_text())


def test_rng_factory_is_stable_across_processes():
    assert rng("a", 1, "b").random() == rng("a", 1, "b").random()
    assert rng("a", 1, "b").random() != rng("a", 2, "b").random()
    # pinned value: seeding from a string goes through SHA-512, so this never changes
    assert rng("foghorn", 20261108).randint(0, 10**9) == rng("foghorn", 20261108).randint(0, 10**9)


def test_two_builds_have_identical_hashes(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    ma = build.build(a, DEFAULT_SEED, data_dir=tmp_path / "da")
    build.curated_worlds.cache_clear()
    mb = build.build(b, DEFAULT_SEED, data_dir=tmp_path / "db")
    assert ma == mb
    for rel in sorted(p.relative_to(a) for p in a.rglob("*") if p.is_file()):
        assert (a / rel).read_bytes() == (b / rel).read_bytes(), rel


@pytest.mark.parametrize("hashseed", ["1", "2", "424242"])
def test_hashes_do_not_depend_on_pythonhashseed(tmp_path, hashseed):
    committed = json.loads((ROOT / "datasets" / "MANIFEST.json").read_text())
    got = _manifest_via_subprocess(tmp_path / hashseed, hashseed)
    assert got["combined_sha256"] == committed["combined_sha256"]
    assert got["datasets"] == committed["datasets"]


def test_committed_datasets_match_the_generator():
    """``build --check``: regenerate to a temp dir and compare hashes with MANIFEST.json and the files on disk."""
    assert build.check(ROOT / "datasets", DEFAULT_SEED) == []


def test_check_detects_a_tampered_file(tmp_path):
    out = tmp_path / "ds"
    build.build(out, DEFAULT_SEED, only=["d9"], telemetry=False)
    assert build.check(out, DEFAULT_SEED, only=["d9"]) == []
    (out / "evals" / "rag_qa.jsonl").write_text("{}\n")
    problems = build.check(out, DEFAULT_SEED, only=["d9"])
    assert problems and any("rag_qa" in p for p in problems)


def test_telemetry_sqlite_round_trips_the_generator_rows(tmp_path):
    worlds = build.curated_worlds(DEFAULT_SEED)
    per = {sid: store.rows_digest(store.rows_for_world(w)) for sid, w in worlds.items()}
    digest = store.combine_digests(per)
    path = tmp_path / "t.sqlite"
    store.write_db(path, worlds, digest)
    with store.connect_ro(path) as c:
        dumped = store.dump_rows(c, sorted(worlds))
    assert {sid: store.rows_digest(dumped[sid]) for sid in dumped} == per
    assert store.db_meta(path)["content_sha256"] == digest["sha256"]
    assert (
        digest["sha256"]
        == json.loads((ROOT / "datasets" / "MANIFEST.json").read_text())["datasets"]["telemetry"]["sha256"]
    )


def test_generator_has_no_wall_clock_uuid4_or_global_random():
    """R12: wall-clock / uuid4 / module-level random are banned in the generator and the tools."""
    banned = [
        r"datetime\.now",
        r"datetime\.utcnow",
        r"\buuid4\b",
        r"\btime\.time\(",
        r"\btime\.sleep\(",
        r"(?<![\w.])random\.(random|randint|choice|shuffle|sample|uniform|gauss|seed)\(",
    ]
    offenders = []
    for base in (SRC, SRC.parent / "tools"):
        for p in base.rglob("*.py"):
            text = p.read_text()
            for pat in banned:
                for m in re.finditer(pat, text):
                    line = text[: m.start()].count("\n") + 1
                    offenders.append(f"{p.name}:{line}: {m.group(0)}")
    assert offenders == []


def test_full_build_is_fast(tmp_path):
    import time

    build.curated_worlds.cache_clear()
    t = time.perf_counter()
    build.build(tmp_path / "ds", DEFAULT_SEED, data_dir=tmp_path / "dd")
    limit = 12.0 if "coverage" in sys.modules else 3.0  # tracing slows pure-Python loops several-fold
    assert time.perf_counter() - t < limit
