"""Dataset generation, MANIFEST and ``build --check``.

``generate`` is a pure function of (seed, only): it returns the file contents of every committed dataset plus the
telemetry digests. ``build`` writes them (and the telemetry SQLite), ``check`` regenerates into a temp dir and compares
SHA-256 hashes with ``datasets/MANIFEST.json`` and with the files on disk.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from foghorn.data import catalog, docs, evalsets, incidents, people, store, streams
from foghorn.data import scenarios as scen
from foghorn.data.util import DEFAULT_SEED, GENERATOR_VERSION, content_hash, sha256_text, to_epoch
from foghorn.data.world import World, build_world

MANIFEST_SCHEMA = "foghorn.manifest/1"


@dataclass(frozen=True)
class DatasetDef:
    id: str  # d1..d12 (+ x1 extras)
    name: str
    role: str  # R reference | GT ground truth | S stream | M seed memory | E eval
    fmt: str  # json | jsonl | markdown | sqlite
    description: str
    truth_fields: tuple[str, ...] = ()
    group: str = ""  # generation group for --only


DATASETS: tuple[DatasetDef, ...] = (
    DatasetDef(
        "d1",
        "service_catalog",
        "R",
        "json",
        "The 20 Brightcart components: tier, team, SLO, call graph, runbooks, flags.",
        group="d1",
    ),
    DatasetDef(
        "d2",
        "runbooks",
        "R",
        "markdown",
        "24 runbooks (Markdown + front matter). Every action id resolves to the action catalogue.",
        group="d2",
    ),
    DatasetDef(
        "d3",
        "policies",
        "R",
        "markdown",
        "8 policies: severity, comms, change freeze, data handling, approval matrix, on-call, postmortem, maintenance.",
        group="d3",
    ),
    DatasetDef(
        "d4",
        "past_incidents",
        "R",
        "jsonl",
        "120 past incidents (semantic recall corpus; INC-0061 and INC-0087 anchor S10 and S16).",
        group="d4",
    ),
    DatasetDef(
        "d5",
        "scenarios",
        "GT",
        "json",
        "S01-S16 scenario specs with hidden ground truth.",
        truth_fields=("truth",),
        group="d5",
    ),
    DatasetDef(
        "d6",
        "telemetry",
        "GT",
        "sqlite",
        "Series, logs, changes, alerts, reports and events for every scenario (SQLite, built on first use).",
        group="d6",
    ),
    DatasetDef(
        "d7", "stream_quiet_hour", "S", "jsonl", "240-event quiet night hour with a maintenance window.", group="d7"
    ),
    DatasetDef(
        "d7", "stream_workday", "S", "jsonl", "1,800-event workday with two real incidents (S10, S01).", group="d7"
    ),
    DatasetDef(
        "d7",
        "stream_storm",
        "S",
        "jsonl",
        "650-event alert storm with duplicates and 3 poison events (S12).",
        group="d7",
    ),
    DatasetDef(
        "d7",
        "stream_quiet_hour_labels",
        "E",
        "jsonl",
        "Expected route/decision/priority per event of stream_quiet_hour.",
        truth_fields=(
            "expected_route",
            "expected_decision",
            "expected_priority",
            "dup_of",
            "poison",
            "poison_kind",
            "storm",
            "attach_to",
        ),
        group="d7",
    ),
    DatasetDef(
        "d7",
        "stream_workday_labels",
        "E",
        "jsonl",
        "Expected route/decision/priority per event of stream_workday.",
        truth_fields=(
            "expected_route",
            "expected_decision",
            "expected_priority",
            "dup_of",
            "poison",
            "poison_kind",
            "storm",
            "attach_to",
        ),
        group="d7",
    ),
    DatasetDef(
        "d7",
        "stream_storm_labels",
        "E",
        "jsonl",
        "Expected route/decision/priority per event of stream_storm.",
        truth_fields=(
            "expected_route",
            "expected_decision",
            "expected_priority",
            "dup_of",
            "poison",
            "poison_kind",
            "storm",
            "attach_to",
        ),
        group="d7",
    ),
    DatasetDef(
        "d8",
        "triage_cases",
        "E",
        "jsonl",
        "400 triage cases (alert + context -> expected decision).",
        truth_fields=("expected",),
        group="d8",
    ),
    DatasetDef(
        "d9",
        "rag_qa",
        "E",
        "jsonl",
        "48 runbook questions incl. 6 lexical traps.",
        truth_fields=("expected_runbooks", "must_not_top1", "scenario_hint"),
        group="d9",
    ),
    DatasetDef(
        "d10",
        "safety_cases",
        "E",
        "jsonl",
        "16 safety cases (injection, forbidden actions, approval bypass, PII).",
        truth_fields=("must", "must_not"),
        group="d10",
    ),
    DatasetDef("d11", "memory_prefs", "M", "jsonl", "14 seed preferences (Store namespace prefs).", group="d11"),
    DatasetDef("d11", "memory_lessons", "M", "jsonl", "30 seed lessons (Store namespace lessons).", group="d11"),
    DatasetDef("d11", "people", "M", "jsonl", "8 on-call people (Store namespace people).", group="d11"),
    DatasetDef(
        "d12",
        "sweep_specs",
        "GT",
        "jsonl",
        "60 generated-fault specs for the statistical eval (14 categories).",
        group="d12",
    ),
    DatasetDef("x1", "rotas", "R", "json", "On-call rotas (weekly rotation, handoff Monday 09:00 UTC).", group="d1"),
)
DATASET_BY_NAME = {d.name: d for d in DATASETS}
_FILES: dict[str, str] = {
    "service_catalog": "reference/service_catalog.json",
    "past_incidents": "reference/past_incidents.jsonl",
    "triage_cases": "evals/triage_cases.jsonl",
    "rag_qa": "evals/rag_qa.jsonl",
    "safety_cases": "evals/safety_cases.jsonl",
    "sweep_specs": "evals/sweep_specs.jsonl",
    "memory_prefs": "seed/memory_prefs.jsonl",
    "memory_lessons": "seed/memory_lessons.jsonl",
    "people": "seed/people.jsonl",
    "rotas": "reference/rotas.json",
    **{f"stream_{s}": f"streams/stream_{s}.jsonl" for s in ("quiet_hour", "workday", "storm")},
    **{f"stream_{s}_labels": f"streams/stream_{s}.labels.jsonl" for s in ("quiet_hour", "workday", "storm")},
}


def jsonl(rows: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(r, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n" for r in rows)


def pretty(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


@lru_cache(maxsize=4)
def curated_worlds(seed: int = DEFAULT_SEED) -> dict[str, World]:
    return {sp.scenario_id: build_world(sp) for sp in scen.curated_specs(seed)}


def scenario_record(w: World, *, generated: bool = False, fault_args: dict[str, Any] | None = None) -> dict[str, Any]:
    sp = w.spec
    rec: dict[str, Any] = {
        "scenario_id": sp.scenario_id,
        "tier": sp.tier,
        "title": sp.title,
        "summary": sp.summary,
        "world": {
            "seed_key": sp.seed_key,
            "onset": sp.onset,
            "window": w.meta["window"],
            "n_points": w.meta["n_points"],
            "step_s": w.meta["step_s"],
            "difficulty": {
                "noise_sigma": sp.noise_sigma,
                "evidence_lag_min": sp.evidence_lag_min,
                "red_herrings": sp.red_herrings,
                "signal_strength": sp.signal_strength,
                "lookback_days_needed": sp.lookback_days_needed,
                "telemetry_gap_components": list(sp.telemetry_gap_components),
            },
        },
        "trigger": {"alert_event": w.meta["trigger_event"], "as_of": w.meta["as_of"]},
        "stats": {
            "events": len(w.events),
            "alerts": len(w.alerts),
            "logs": len(w.logs),
            "changes": len(w.changes),
            "reports": len(w.reports),
        },
        "truth": w.truth,
    }
    if generated:
        rec["generated"] = True
        rec["fault_args"] = fault_args or {}
    return rec


@dataclass
class Generated:
    seed: int
    files: dict[str, str] = field(default_factory=dict)
    records: dict[str, int] = field(default_factory=dict)  # dataset name -> record count
    telemetry: dict[str, Any] | None = None
    per_scenario: dict[str, dict[str, str]] = field(default_factory=dict)
    worlds: dict[str, World] | None = None


def selected_groups(only: list[str] | None) -> set[str]:
    if not only:
        return {d.group for d in DATASETS}
    out: set[str] = set()
    for tok in only:
        t = tok.strip().lower()
        for d in DATASETS:
            if t in (d.id, d.name, d.group):
                out.add(d.group)
                break
        else:
            raise ValueError(
                f"unknown dataset {tok!r}; choose from {', '.join(sorted({d.id for d in DATASETS}))} or a dataset name"
            )
    return out


def generate(seed: int = DEFAULT_SEED, only: list[str] | None = None) -> Generated:
    groups = selected_groups(only)
    g = Generated(seed)
    needs_worlds = bool(groups & {"d5", "d6", "d7", "d8"})
    worlds = curated_worlds(seed) if needs_worlds else None
    g.worlds = worlds

    def put(name: str, text: str, n: int) -> None:
        g.files[_FILES[name]] = text
        g.records[name] = n

    rb_by_comp = docs.runbooks_by_component()
    if "d1" in groups:
        recs = catalog.service_catalog_records(rb_by_comp)
        put("service_catalog", pretty(recs), len(recs))
        rotas = people.rota_records()
        put("rotas", pretty(rotas), len(rotas))
    if "d2" in groups:
        for rel, text in docs.runbook_files().items():
            g.files[rel] = text
        g.records["runbooks"] = len(docs.runbook_records())
    if "d3" in groups:
        for rel, text in docs.policy_files().items():
            g.files[rel] = text
        g.records["policies"] = len(docs.policy_records())
    inc = incidents.past_incidents(seed)
    if "d4" in groups:
        put("past_incidents", jsonl(inc), len(inc))
    if "d5" in groups and worlds:
        for sid in sorted(worlds):
            g.files[f"scenarios/{sid}.json"] = pretty(scenario_record(worlds[sid]))
        g.records["scenarios"] = len(worlds)
    if "d6" in groups and worlds:
        per = {sid: store.rows_digest(store.rows_for_world(worlds[sid])) for sid in sorted(worlds)}
        g.per_scenario = per
        g.telemetry = store.combine_digests(per)
        g.records["telemetry"] = len(worlds)
    if "d7" in groups and worlds:
        for name in streams.STREAMS:
            evs, labs = streams.assemble(name, seed, worlds)
            put(name, jsonl(evs), len(evs))
            put(f"{name}_labels", jsonl(labs), len(labs))
    if "d8" in groups and worlds:
        tc = evalsets.triage_cases(seed, worlds)
        put("triage_cases", jsonl(tc), len(tc))
    if "d9" in groups:
        qa = evalsets.rag_qa(docs.runbook_records())
        put("rag_qa", jsonl(qa), len(qa))
    if "d10" in groups:
        sc = evalsets.safety_cases()
        put("safety_cases", jsonl(sc), len(sc))
    if "d11" in groups:
        prefs, lessons, ppl = incidents.seed_prefs(), incidents.seed_lessons(inc, seed), people.people_seed_records()
        put("memory_prefs", jsonl(prefs), len(prefs))
        put("memory_lessons", jsonl(lessons), len(lessons))
        put("people", jsonl(ppl), len(ppl))
    if "d12" in groups:
        sw = evalsets.sweep_specs(seed)
        put("sweep_specs", jsonl(sw), len(sw))
    return g


def dataset_files(name: str, files: dict[str, str]) -> list[str]:
    if name == "runbooks":
        return sorted(f for f in files if f.startswith("reference/runbooks/"))
    if name == "policies":
        return sorted(f for f in files if f.startswith("reference/policies/"))
    if name == "scenarios":
        return sorted(f for f in files if f.startswith("scenarios/"))
    if name == "telemetry":
        return []
    return [_FILES[name]] if _FILES.get(name) in files else []


def manifest_entries(g: Generated) -> dict[str, Any]:
    entries: dict[str, Any] = {}
    for d in DATASETS:
        if d.name == "telemetry":
            if g.telemetry is not None:
                entries[d.name] = {
                    "id": d.id,
                    "role": d.role,
                    "format": d.fmt,
                    "records": g.records.get("telemetry", 0),
                    "sha256": g.telemetry["sha256"],
                    "tables": g.telemetry["tables"],
                    "built": True,
                }
            continue
        fs = dataset_files(d.name, g.files)
        if not fs:
            continue
        fh = {f: sha256_text(g.files[f]) for f in fs}
        entries[d.name] = {
            "id": d.id,
            "role": d.role,
            "format": d.fmt,
            "records": g.records.get(d.name, len(fs)),
            "bytes": sum(len(g.files[f].encode()) for f in fs),
            "files": fh,
            "sha256": content_hash(fh),
        }
    return entries


def merge_manifest(old: dict[str, Any] | None, entries: dict[str, Any], seed: int) -> dict[str, Any]:
    datasets = dict(old["datasets"]) if old and old.get("seed") == seed else {}
    datasets.update(entries)
    datasets = {k: datasets[k] for k in sorted(datasets)}
    return {
        "schema": MANIFEST_SCHEMA,
        "generator_version": GENERATOR_VERSION,
        "seed": seed,
        "datasets": datasets,
        "combined_sha256": content_hash({k: v["sha256"] for k, v in datasets.items()}),
    }


def write_files(files: dict[str, str], out_dir: Path) -> None:
    for rel, text in sorted(files.items()):
        p = out_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


def build(
    out_dir: Path,
    seed: int = DEFAULT_SEED,
    only: list[str] | None = None,
    *,
    data_dir: Path | None = None,
    telemetry: bool = True,
) -> dict[str, Any]:
    """Write datasets + MANIFEST (and, unless disabled, the telemetry SQLite into ``data_dir``)."""
    g = generate(seed, only)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_files(g.files, out_dir)
    mpath = out_dir / "MANIFEST.json"
    old = json.loads(mpath.read_text()) if mpath.exists() else None
    manifest = merge_manifest(old, manifest_entries(g), seed)
    mpath.write_text(pretty(manifest), encoding="utf-8")
    if telemetry and g.worlds is not None and g.telemetry is not None and data_dir is not None:
        store.write_db(
            data_dir / "telemetry.sqlite",
            g.worlds,
            g.telemetry,
            built_for=manifest["datasets"].get("telemetry", {}).get("sha256", ""),
        )
    return manifest


def check(committed_dir: Path, seed: int = DEFAULT_SEED, only: list[str] | None = None) -> list[str]:
    """Regenerate into a temp dir and compare with the committed MANIFEST and the files on disk. Returns problems."""
    problems: list[str] = []
    mpath = committed_dir / "MANIFEST.json"
    if not mpath.exists():
        return [f"{mpath} missing"]
    committed = json.loads(mpath.read_text())
    if committed.get("seed") != seed:
        problems.append(f"MANIFEST seed {committed.get('seed')} != requested {seed}")
    g = generate(seed, only)
    with tempfile.TemporaryDirectory() as tmp:
        write_files(g.files, Path(tmp))
        fresh = {rel: sha256_text((Path(tmp) / rel).read_text(encoding="utf-8")) for rel in g.files}
    entries = manifest_entries(g)
    for name, ent in entries.items():
        com = committed.get("datasets", {}).get(name)
        if com is None:
            problems.append(f"{name}: not in MANIFEST")
            continue
        if com["sha256"] != ent["sha256"]:
            problems.append(f"{name}: regenerated sha256 differs from MANIFEST (generator drifted or datasets stale)")
        for rel, sha in ent.get("files", {}).items():
            on_disk = committed_dir / rel
            if not on_disk.exists():
                problems.append(f"{rel}: missing on disk")
            elif sha256_text(on_disk.read_text(encoding="utf-8")) != sha:
                problems.append(f"{rel}: on-disk content differs from the regenerated file")
            if fresh[rel] != sha:
                problems.append(f"{rel}: writing to disk changed the hash (non-canonical serialisation)")
    return problems


def scenario_ids_curated() -> list[str]:
    return [f"S{i:02d}" for i in range(1, 17)]


def onset_epoch(sid: str, seed: int = DEFAULT_SEED) -> int:
    return to_epoch(curated_worlds(seed)[sid].spec.onset)
