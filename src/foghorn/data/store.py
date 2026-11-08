"""Telemetry SQLite (D6): schema, row canonicalisation, content digests, lazy hash-checked build, read-only access.

``rows_for_world`` is the single canonicalisation of a world into table rows: the same rows are inserted, hashed (the
MANIFEST digest) and re-read (``dump_rows``), so "the DB equals the generator output" is a testable equality.
Only the curated scenarios live in ``telemetry.sqlite``; each generated ``X-nnnn`` has its own small database.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from foghorn.data.util import GENERATOR_VERSION, canonical_json, content_hash
from foghorn.data.world import World

SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE series(scenario_id TEXT NOT NULL, service TEXT NOT NULL, metric TEXT NOT NULL, t0 INTEGER NOT NULL, step_s INTEGER NOT NULL,
                    n INTEGER NOT NULL, vals TEXT NOT NULL, base TEXT, PRIMARY KEY(scenario_id, service, metric)) WITHOUT ROWID;
CREATE TABLE logs(scenario_id TEXT NOT NULL, seq INTEGER NOT NULL, ts INTEGER NOT NULL, service TEXT NOT NULL, level TEXT NOT NULL,
                  trace_id TEXT NOT NULL, message TEXT NOT NULL, kind TEXT NOT NULL, PRIMARY KEY(scenario_id, seq)) WITHOUT ROWID;
CREATE INDEX logs_by_service ON logs(scenario_id, service, ts);
CREATE TABLE changes(scenario_id TEXT NOT NULL, change_id TEXT NOT NULL, ts INTEGER NOT NULL, kind TEXT NOT NULL, component TEXT NOT NULL,
                     actor TEXT NOT NULL, summary TEXT NOT NULL, details TEXT NOT NULL, role TEXT NOT NULL, PRIMARY KEY(scenario_id, change_id)) WITHOUT ROWID;
CREATE INDEX changes_by_ts ON changes(scenario_id, ts);
CREATE TABLE alerts(scenario_id TEXT NOT NULL, alert_id TEXT NOT NULL, rule TEXT NOT NULL, metric TEXT NOT NULL, component TEXT NOT NULL,
                    fired_at INTEGER NOT NULL, resolved_at INTEGER, severity TEXT NOT NULL, value, threshold, for_s INTEGER NOT NULL,
                    label TEXT NOT NULL, event_type TEXT NOT NULL, PRIMARY KEY(scenario_id, alert_id)) WITHOUT ROWID;
CREATE INDEX alerts_by_time ON alerts(scenario_id, fired_at);
CREATE TABLE reports(scenario_id TEXT NOT NULL, ticket_id TEXT NOT NULL, ts INTEGER NOT NULL, channel TEXT NOT NULL, text TEXT NOT NULL,
                     customer_tier TEXT NOT NULL, order_ref TEXT, severity TEXT NOT NULL, component TEXT, PRIMARY KEY(scenario_id, ticket_id)) WITHOUT ROWID;
CREATE TABLE events(scenario_id TEXT NOT NULL, seq INTEGER NOT NULL, id TEXT NOT NULL, occurred_at TEXT NOT NULL, body TEXT NOT NULL,
                    PRIMARY KEY(scenario_id, seq)) WITHOUT ROWID;
"""
TABLES = ("series", "logs", "changes", "alerts", "reports", "events")
_ORDER = {
    "series": "scenario_id, service, metric",
    "logs": "scenario_id, seq",
    "changes": "scenario_id, change_id",
    "alerts": "scenario_id, alert_id",
    "reports": "scenario_id, ticket_id",
    "events": "scenario_id, seq",
}
_BUILD_LOCK = threading.Lock()


def _js(v: Any) -> str:
    return json.dumps(v, separators=(",", ":"))


def rows_for_world(w: World) -> dict[str, list[tuple[Any, ...]]]:
    sid = w.spec.scenario_id
    rows: dict[str, list[tuple[Any, ...]]] = {t: [] for t in TABLES}
    for (svc, metric), vals in sorted(w.series.items()):
        base = w.base[(svc, metric)]
        rows["series"].append(
            (sid, svc, metric, w.t0, 60, len(vals), _js(vals), None if base is vals or base == vals else _js(base))
        )
    for seq, ln in enumerate(w.logs, 1):
        rows["logs"].append((sid, seq, ln.ts, ln.service, ln.level, ln.trace_id, ln.message, ln.kind))
    for c in sorted(w.changes, key=lambda x: x["change_id"]):
        rows["changes"].append(
            (
                sid,
                c["change_id"],
                c["ts"],
                c["kind"],
                c["component"],
                c["actor"],
                c["summary"],
                _js(c["details"]),
                c["role"],
            )
        )
    for a in sorted(w.alerts, key=lambda x: x["alert_id"]):
        rows["alerts"].append(
            (
                sid,
                a["alert_id"],
                a["rule"],
                a["metric"],
                a["component"],
                a["fired_at_s"],
                a["resolved_at_s"],
                a["severity"],
                a["value"],
                a["threshold"],
                a["for_s"],
                a["label"],
                a["event_type"],
            )
        )
    for rp in sorted(w.reports, key=lambda x: x["ticket_id"]):
        rows["reports"].append(
            (
                sid,
                rp["ticket_id"],
                rp["ts"],
                rp["channel"],
                rp["text"],
                rp["customer_tier"],
                rp["order_ref"],
                rp["severity"],
                rp.get("component"),
            )
        )
    for seq, e in enumerate(w.events, 1):
        rows["events"].append((sid, seq, e["id"], e["occurred_at"], canonical_json(e)))
    return rows


def rows_digest(rows: dict[str, list[tuple[Any, ...]]]) -> dict[str, str]:
    return {t: content_hash([list(r) for r in rows[t]]) for t in TABLES}


def combine_digests(per_scenario: dict[str, dict[str, str]]) -> dict[str, Any]:
    """MANIFEST telemetry entry: per-table digests over all scenarios + a combined hash."""
    tables = {t: content_hash({sid: per_scenario[sid][t] for sid in sorted(per_scenario)}) for t in TABLES}
    return {"tables": tables, "sha256": content_hash(tables), "scenarios": sorted(per_scenario)}


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def write_rows(conn: sqlite3.Connection, rows: dict[str, list[tuple[Any, ...]]]) -> None:
    for t in TABLES:
        if not rows[t]:
            continue
        marks = ",".join("?" * len(rows[t][0]))
        conn.executemany(f"INSERT INTO {t} VALUES ({marks})", rows[t])  # noqa: S608 - table names are constants


def dump_rows(conn: sqlite3.Connection, scenario_ids: list[str]) -> dict[str, dict[str, list[tuple[Any, ...]]]]:
    out: dict[str, dict[str, list[tuple[Any, ...]]]] = {}
    for sid in scenario_ids:
        out[sid] = {}
        for t in TABLES:
            cur = conn.execute(f"SELECT * FROM {t} WHERE scenario_id = ? ORDER BY {_ORDER[t]}", (sid,))  # noqa: S608
            out[sid][t] = [tuple(r) for r in cur.fetchall()]
    return out


def write_db(path: Path, worlds: dict[str, World], digest: dict[str, Any], built_for: str = "") -> None:
    """Atomically (re)create a telemetry DB with ``worlds``. Safe against concurrent builders (same bytes either way)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{threading.get_ident()}")
    if tmp.exists():
        tmp.unlink()
    conn = sqlite3.connect(tmp)
    try:
        create_schema(conn)
        for sid in sorted(worlds):
            write_rows(conn, rows_for_world(worlds[sid]))
        conn.executemany(
            "INSERT INTO meta VALUES (?, ?)",
            [
                ("generator_version", GENERATOR_VERSION),
                ("content_sha256", digest["sha256"]),
                ("built_for", built_for),
                ("scenarios", _js(sorted(worlds))),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    os.replace(tmp, path)


@contextmanager
def connect_ro(path: Path) -> Iterator[sqlite3.Connection]:
    """A fresh read-only connection per use (cheap, thread-safe, no shared state)."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def db_meta(path: Path) -> dict[str, str]:
    try:
        with connect_ro(path) as c:
            return {r["key"]: r["value"] for r in c.execute("SELECT key, value FROM meta")}
    except sqlite3.Error:
        return {}
