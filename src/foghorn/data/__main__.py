"""``python -m foghorn.data build [--seed N] [--check] [--only d1,d5] [--out DIR]`` - deterministic dataset generator."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from foghorn.data import build as build_mod
from foghorn.data import paths
from foghorn.data.util import DEFAULT_SEED


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m foghorn.data", description="Deterministic, seeded, stdlib-only Foghorn data generator."
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="regenerate datasets/ (+ MANIFEST.json) and the telemetry SQLite")
    b.add_argument("--seed", type=int, default=DEFAULT_SEED)
    b.add_argument(
        "--check",
        action="store_true",
        help="regenerate to a temp dir and compare hashes with the committed MANIFEST; exit 1 on drift",
    )
    b.add_argument("--only", default="", help="comma separated: d1..d12 or dataset names (e.g. d5,d6 or scenarios)")
    b.add_argument("--out", type=Path, default=None, help="datasets directory (default: <repo>/datasets)")
    b.add_argument(
        "--data-dir", type=Path, default=None, help="runtime data dir for telemetry.sqlite (default: settings.data_dir)"
    )
    b.add_argument("--no-telemetry", action="store_true", help="do not write the telemetry SQLite")
    sub.add_parser("list", help="list datasets and their MANIFEST hashes")
    args = ap.parse_args(argv)

    out = args.out if getattr(args, "out", None) else paths.repo_root() / "datasets"
    only = [t for t in args.only.split(",") if t.strip()] if getattr(args, "only", "") else None
    t0 = time.perf_counter()
    if args.cmd == "list":
        import json

        m = json.loads((out / "MANIFEST.json").read_text())
        for name, d in m["datasets"].items():
            print(f"{d['id']:>3}  {name:<28} {d['role']:<3} {d.get('records', ''):>6}  {d['sha256'][:12]}")
        print(f"combined {m['combined_sha256']}  seed {m['seed']}")
        return 0
    if args.check:
        problems = build_mod.check(out, args.seed, only)
        dt = time.perf_counter() - t0
        if problems:
            print(f"datasets are out of date ({len(problems)} problem(s), {dt:.1f}s):", file=sys.stderr)
            for p in problems:
                print(f"  - {p}", file=sys.stderr)
            return 1
        print(f"ok: regenerated hashes match {out / 'MANIFEST.json'} ({dt:.1f}s)")
        return 0
    data_dir = args.data_dir or paths.data_dir()
    manifest = build_mod.build(out, args.seed, only, data_dir=data_dir, telemetry=not args.no_telemetry)
    dt = time.perf_counter() - t0
    print(
        f"built {len(manifest['datasets'])} datasets into {out} in {dt:.2f}s; combined sha256 {manifest['combined_sha256'][:16]}"
    )
    if not args.no_telemetry:
        print(f"telemetry: {data_dir / 'telemetry.sqlite'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
