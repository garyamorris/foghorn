"""Public data API (docs/ARCHITECTURE.md section 4.1). Synchronous, thread-safe, JSON-returning.

Truth (root cause, red herrings, forbidden actions, expected interrupts...) is never part of a public view: it is
returned only by ``get_truth`` / ``get_scenario(..., reveal_truth=True)`` / ``read_dataset(..., reveal_truth=True)``.
Extensions beyond ARCHITECTURE.md 4.1 (all optional / additive): ``list_scenarios(include_generated=)``,
``read_dataset(..., reveal_truth=, scenario_id=)``, ``iter_stream_labels``, ``memory_seed_items``,
``build_fault_scenario(..., onset_offset_min=, noise_sigma=)``.
"""

from __future__ import annotations

import copy
import json
import os
import threading
from collections.abc import Iterator
from typing import Any

from foghorn.data import access, paths, store
from foghorn.data import build as build_mod
from foghorn.data import scenarios as scen
from foghorn.data.access import ScenarioNotFound
from foghorn.data.util import canonical_json, to_iso

__all__ = [
    "ScenarioNotFound",
    "build_fault_scenario",
    "datasets_index",
    "default_as_of",
    "fault_categories",
    "get_scenario",
    "get_truth",
    "iter_stream",
    "iter_stream_labels",
    "list_scenarios",
    "list_streams",
    "memory_seed_items",
    "read_dataset",
    "scenario_events",
    "service_catalog",
    "sweep_specs",
]
_GEN_LOCK = threading.Lock()
_HIDDEN_KEYS = ("truth", "fault_args")


_PUBLIC_DIFFICULTY = ("noise_sigma", "evidence_lag_min", "signal_strength")


def _public(rec: dict[str, Any]) -> dict[str, Any]:
    """Public view: no truth, and no difficulty knob that would hint at it (red herrings, lookback, gap components)."""
    out = {k: copy.deepcopy(v) for k, v in rec.items() if k not in _HIDDEN_KEYS}
    diff = out.get("world", {}).get("difficulty")
    if diff is not None:
        out["world"]["difficulty"] = {k: diff[k] for k in _PUBLIC_DIFFICULTY if k in diff}
    return out


def _ids_generated() -> list[str]:
    d = paths.generated_dir()
    return sorted(p.stem for p in d.glob("X-*.json")) if d.exists() else []


# --- scenarios --------------------------------------------------------------------------------------------------------------------
def list_scenarios(tier: int | None = None, *, include_generated: bool = False) -> list[dict[str, Any]]:
    ids = [f"S{i:02d}" for i in range(1, 17)] + (_ids_generated() if include_generated else [])
    out = []
    for sid in ids:
        rec = access.scenario_record(sid)
        if tier is None or rec["tier"] == tier:
            out.append(_public(rec))
    return out


def get_scenario(scenario_id: str, *, reveal_truth: bool = False) -> dict[str, Any]:
    rec = access.scenario_record(scenario_id)
    if reveal_truth:
        return copy.deepcopy(rec)
    return _public(rec)


def get_truth(scenario_id: str) -> dict[str, Any]:
    return copy.deepcopy(access.truth(scenario_id))


def default_as_of(scenario_id: str) -> str:
    """Virtual 'now' at which the scenario starts: first real signal + detection lag (ISO)."""
    return str(access.scenario_record(scenario_id)["trigger"]["as_of"])


def scenario_events(scenario_id: str) -> list[dict[str, Any]]:
    """Every event of the scenario in virtual-time order, as ``EventEnvelope.to_state()`` dicts."""
    access.scenario_record(scenario_id)  # validates the id
    with store.connect_ro(access.db_path(scenario_id)) as c:
        rows = c.execute("SELECT body FROM events WHERE scenario_id = ? ORDER BY seq", (scenario_id,)).fetchall()
    return [json.loads(r["body"]) for r in rows]


# --- streams ------------------------------------------------------------------------------------------------------------------------
def _stream_file(name: str, labels: bool = False) -> Any:
    from foghorn.data.streams import STREAMS

    if name not in STREAMS:
        raise KeyError(f"unknown stream {name!r}; choose from {', '.join(STREAMS)}")
    suffix = ".labels.jsonl" if labels else ".jsonl"
    return access.ensure_datasets() / "streams" / f"{name}{suffix}"


def list_streams() -> list[dict[str, Any]]:
    from foghorn.data.streams import STREAMS

    m = access.manifest()["datasets"]
    out = []
    for name, cfg in STREAMS.items():
        ent = m.get(name, {})
        out.append(
            {
                "name": name,
                "events": ent.get("records", cfg["n"]),
                "start": cfg["start"],
                "end": cfg["end"],
                "scenarios": list(cfg["scenarios"]),
                "storm": cfg["storm"],
                "description": cfg["description"],
                "labels_dataset": f"{name}_labels",
            }
        )
    return out


def iter_stream(name: str) -> Iterator[dict[str, Any]]:
    """Events of a stream in replay order (virtual-time ascending)."""
    for row in access.read_jsonl(_stream_file(name)):
        yield copy.deepcopy(row)


def iter_stream_labels(name: str) -> Iterator[dict[str, Any]]:
    """The label sidecar (expected route / triage decision / priority / duplicate-of / poison), one record per event."""
    for row in access.read_jsonl(_stream_file(name, labels=True)):
        yield copy.deepcopy(row)


# --- reference data -------------------------------------------------------------------------------------------------------------------
def service_catalog() -> list[dict[str, Any]]:
    return copy.deepcopy(list(access.read_json(access.ensure_datasets() / "reference" / "service_catalog.json")))


def sweep_specs() -> list[dict[str, Any]]:
    return [copy.deepcopy(dict(r)) for r in access.read_jsonl(access.ensure_datasets() / "evals" / "sweep_specs.jsonl")]


def datasets_index() -> list[dict[str, Any]]:
    m = access.manifest()["datasets"]
    out = []
    for d in build_mod.DATASETS:
        ent = m.get(d.name)
        if ent is None:
            continue
        out.append(
            {
                "id": d.id,
                "name": d.name,
                "role": d.role,
                "format": d.fmt,
                "description": d.description,
                "records": ent.get("records"),
                "bytes": ent.get("bytes"),
                "sha256": ent["sha256"],
                "has_truth": bool(d.truth_fields) or d.name == "telemetry",
                "truth_fields": list(d.truth_fields),
                "files": sorted(ent.get("files", {})),
            }
        )
    return out


def _strip(rows: list[dict[str, Any]], fields: tuple[str, ...], reveal: bool) -> list[dict[str, Any]]:
    if reveal or not fields:
        return rows
    return [{k: v for k, v in r.items() if k not in fields} for r in rows]


_TELEMETRY_HIDDEN = {
    "series": ("base",),
    "logs": ("kind",),
    "changes": ("role",),
    "alerts": ("label",),
    "reports": (),
    "events": (),
}


def _telemetry_rows(
    sub: str, offset: int, limit: int, reveal: bool, scenario_id: str | None
) -> tuple[int, list[dict[str, Any]]]:
    if sub not in store.TABLES:
        raise KeyError(f"unknown telemetry table {sub!r}; choose from {', '.join(store.TABLES)}")
    ids = [scenario_id] if scenario_id else [f"S{i:02d}" for i in range(1, 17)]
    where = f"scenario_id IN ({','.join('?' * len(ids))})"
    hidden = () if reveal else _TELEMETRY_HIDDEN[sub]
    with store.connect_ro(access.ensure_telemetry()) as c:
        total = c.execute(f"SELECT COUNT(*) FROM {sub} WHERE {where}", ids).fetchone()[0]  # noqa: S608
        cur = c.execute(
            f"SELECT * FROM {sub} WHERE {where} ORDER BY {store._ORDER[sub]} LIMIT ? OFFSET ?",  # noqa: S608
            [*ids, limit, offset],
        )
        rows = [{k: r[k] for k in r.keys() if k not in hidden} for r in cur.fetchall()]  # noqa: SIM118 - sqlite3.Row
    for r in rows:
        for k in ("ts", "fired_at", "resolved_at", "t0"):
            if r.get(k) is not None:
                r[k] = to_iso(r[k])
        if sub == "series":
            r["vals"] = json.loads(r["vals"])
    return total, rows


def read_dataset(
    name: str, offset: int = 0, limit: int = 50, *, reveal_truth: bool = False, scenario_id: str | None = None
) -> dict[str, Any]:
    """Page through any dataset (D1-D12). Ground-truth fields are removed unless ``reveal_truth=True``."""
    base = name.split(".")[0]
    d = build_mod.DATASET_BY_NAME.get(base)
    if d is None:
        raise KeyError(f"unknown dataset {name!r}; see datasets_index()")
    offset, limit = max(int(offset), 0), max(min(int(limit), 500), 1)
    root = access.ensure_datasets()
    hidden_fields = d.truth_fields
    rows: list[dict[str, Any]]
    if base == "telemetry":
        sub = name.split(".", 1)[1] if "." in name else "alerts"
        total, rows = _telemetry_rows(sub, offset, limit, reveal_truth, scenario_id)
        return {
            "name": f"telemetry.{sub}",
            "format": "sqlite",
            "total": total,
            "offset": offset,
            "limit": limit,
            "truth_hidden": not reveal_truth,
            "records": rows,
        }
    if base in ("runbooks", "policies"):
        sub = "runbooks" if base == "runbooks" else "policies"
        all_docs = access.markdown_docs(sub)
        rows = [{**meta, "text": body} for meta, body in all_docs]
        hidden_fields = ()
    elif base == "scenarios":
        recs = [access.scenario_record(f"S{i:02d}") for i in range(1, 17)]
        rows = [copy.deepcopy(r) if reveal_truth else _public(r) for r in recs]
    else:
        rel = build_mod._FILES[base]
        p = root / rel
        rows = [dict(r) for r in access.read_jsonl(p)] if p.suffix == ".jsonl" else _json_rows(access.read_json(p))
    total = len(rows)
    page = _strip(copy.deepcopy(rows[offset : offset + limit]), hidden_fields, reveal_truth)
    return {
        "name": name,
        "format": d.fmt,
        "total": total,
        "offset": offset,
        "limit": limit,
        "truth_hidden": bool(hidden_fields) and not reveal_truth,
        "records": page,
    }


def _json_rows(obj: Any) -> list[dict[str, Any]]:
    return [dict(x) for x in obj] if isinstance(obj, list) else [dict(obj)]


def fault_categories() -> list[str]:
    """The 14 fault categories accepted by ``build_fault_scenario``."""
    from foghorn.data.faults import CATEGORIES

    return list(CATEGORIES)


# --- fault injector -------------------------------------------------------------------------------------------------------------------
def build_fault_scenario(
    category: str,
    component: str,
    magnitude: float = 0.7,
    red_herrings: int = 0,
    seed: int = 1,
    *,
    onset_offset_min: int = 0,
    noise_sigma: float | None = None,
) -> dict[str, Any]:
    """Create (or return the existing) fully labelled scenario ``X-nnnn`` from the injector arguments.

    Deterministic: the same arguments always give the same id and the same world. The id is a 4-digit hash of the
    arguments (linear probing on the rare collision with a different fault). Persisted under ``data_dir/generated``.
    """
    from foghorn.data.world import build_world

    args: dict[str, Any] = {
        "category": category,
        "component": component,
        "magnitude": round(float(magnitude), 3),
        "red_herrings": int(red_herrings),
        "seed": int(seed),
        "onset_offset_min": int(onset_offset_min),
        "noise_sigma": noise_sigma,
    }
    scen.generated_spec(
        "X-0000",
        category,
        component,
        args["magnitude"],
        args["red_herrings"],
        args["seed"],
        onset_offset_min=args["onset_offset_min"],
        noise_sigma=noise_sigma,
    )  # validates
    gdir = paths.generated_dir()
    with _GEN_LOCK:
        n = int(
            scen.generated_id(
                category,
                component,
                args["magnitude"],
                args["red_herrings"],
                args["seed"],
                f"{onset_offset_min}|{noise_sigma}",
            )[2:]
        )
        for _ in range(10000):
            sid = f"X-{n:04d}"
            jp = gdir / f"{sid}.json"
            if jp.exists():
                rec = json.loads(jp.read_text(encoding="utf-8"))
                if rec.get("fault_args") == args:
                    return _public(rec)
                n = (n + 1) % 10000
                continue
            break
        spec = scen.generated_spec(
            sid,
            category,
            component,
            args["magnitude"],
            args["red_herrings"],
            args["seed"],
            onset_offset_min=args["onset_offset_min"],
            noise_sigma=noise_sigma,
        )
        w = build_world(spec)
        rec = build_mod.scenario_record(w, generated=True, fault_args=args)
        gdir.mkdir(parents=True, exist_ok=True)
        digest = store.combine_digests({sid: store.rows_digest(store.rows_for_world(w))})
        store.write_db(gdir / f"{sid}.sqlite", {sid: w}, digest)
        tmp = jp.with_name(f"{jp.name}.tmp.{os.getpid()}.{threading.get_ident()}")
        tmp.write_text(canonical_json(rec) + "\n", encoding="utf-8")
        os.replace(tmp, jp)
    return _public(rec)


# --- memory seeds (extension) -----------------------------------------------------------------------------------------------------------
def memory_seed_items(kind: str | None = None) -> list[dict[str, Any]]:
    """Store seed items ``{namespace, key, value}`` (``value.text`` is the indexed field): incidents (D4), runbooks (D2),
    prefs, lessons, people (D11). ``kind`` filters to one of ``incidents|runbooks|prefs|lessons|people``."""
    root = access.ensure_datasets()
    out: list[dict[str, Any]] = []
    if kind in (None, "incidents"):
        for r in access.read_jsonl(root / "reference" / "past_incidents.jsonl"):
            text = f"{r['title']}. {r['symptoms']} Resolution: {', '.join(r['resolution'])}. Lessons: {' '.join(r['lessons'])}"
            out.append(
                {"namespace": ["incidents", r["root_cause_component"]], "key": r["id"], "value": {"text": text, **r}}
            )
    if kind in (None, "runbooks"):
        for meta, body in access.markdown_docs("runbooks"):
            out.append(
                {
                    "namespace": ["runbooks"],
                    "key": meta["id"],
                    "value": {
                        "text": f"{meta['title']}. {' '.join(meta['symptoms'])}. {body}",
                        "id": meta["id"],
                        "title": meta["title"],
                        "applies_to": meta["applies_to"],
                    },
                }
            )
    for k, fname in (("prefs", "memory_prefs"), ("lessons", "memory_lessons"), ("people", "people")):
        if kind in (None, k):
            out.extend(copy.deepcopy(dict(r)) for r in access.read_jsonl(root / "seed" / f"{fname}.jsonl"))
    return out
