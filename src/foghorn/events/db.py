# ruff: noqa: S608  - dynamic SQL below only interpolates internal column/table names (never user input); values are bound
"""SQLite state DB: events, dispatches, incidents, approvals, dead letters, eval runs, generated scenarios.

* Plain synchronous helpers on purpose. ``langgraph dev`` raises ``BlockingError`` for ``sqlite3`` calls made on the
  event loop, so async callers go through :func:`StateDB.run` (``asyncio.to_thread``); nothing here is ever awaited.
* One connection (``check_same_thread=False``) guarded by a re-entrant lock; WAL journal; autocommit with explicit
  ``BEGIN IMMEDIATE`` transactions via :meth:`StateDB.transaction`.
* The incident/approval tables are PROJECTIONS (the LangGraph thread state is the source of truth for interrupts).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar

T = TypeVar("T")

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, n INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, type TEXT NOT NULL, source TEXT NOT NULL,
  severity TEXT NOT NULL, occurred_at TEXT NOT NULL, received_at TEXT NOT NULL, component TEXT, environment TEXT,
  subject TEXT NOT NULL, payload TEXT NOT NULL, dedupe_key TEXT, idempotency_key TEXT UNIQUE,
  correlation_id TEXT, causation_id TEXT, trace TEXT NOT NULL, attributes TEXT NOT NULL,
  status TEXT NOT NULL, status_reason TEXT, occurrences INTEGER NOT NULL DEFAULT 1, last_seen_at TEXT,
  duplicate_of TEXT, scenario_id TEXT, dedupe_scope TEXT NOT NULL DEFAULT '', window_closed INTEGER NOT NULL DEFAULT 0,
  launch_id TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_type ON events(type);
CREATE INDEX IF NOT EXISTS ix_events_component ON events(component);
CREATE INDEX IF NOT EXISTS ix_events_corr ON events(correlation_id);
CREATE INDEX IF NOT EXISTS ix_events_cause ON events(causation_id);
CREATE INDEX IF NOT EXISTS ix_events_dedupe ON events(dedupe_key, type, dedupe_scope);
CREATE INDEX IF NOT EXISTS ix_events_status ON events(status);
CREATE TABLE IF NOT EXISTS dispatches (
  n INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, event_id TEXT NOT NULL, route TEXT NOT NULL,
  generation INTEGER NOT NULL DEFAULT 0, kind TEXT NOT NULL DEFAULT 'run', priority TEXT NOT NULL,
  status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, graph_id TEXT, assistant_id TEXT, thread_id TEXT,
  run_id TEXT, incident_id TEXT, parent_dispatch_id TEXT, plan TEXT NOT NULL, mode TEXT NOT NULL DEFAULT 'start',
  enqueued_at TEXT, started_at TEXT, finished_at TEXT, next_attempt_at TEXT, error TEXT,
  UNIQUE(event_id, route, generation)
);
CREATE INDEX IF NOT EXISTS ix_disp_status ON dispatches(status);
CREATE INDEX IF NOT EXISTS ix_disp_event ON dispatches(event_id);
CREATE INDEX IF NOT EXISTS ix_disp_thread ON dispatches(thread_id);
CREATE TABLE IF NOT EXISTS incidents (
  incident_id TEXT PRIMARY KEY, correlation_id TEXT, thread_id TEXT, title TEXT, severity TEXT,
  status TEXT NOT NULL, scenario_id TEXT, component TEXT, opened_at TEXT, updated_at TEXT, resolved_at TEXT,
  root_cause TEXT, graph_id TEXT
);
CREATE TABLE IF NOT EXISTS approvals (
  approval_id TEXT PRIMARY KEY, thread_id TEXT, run_id TEXT, interrupt_id TEXT, kind TEXT, status TEXT NOT NULL,
  title TEXT, summary TEXT, risk TEXT, reversible INTEGER, incident_id TEXT, graph_id TEXT, requested_at TEXT,
  decided_at TEXT, decided_by TEXT, decision TEXT, payload TEXT, response_schema TEXT, options TEXT, nonce TEXT
);
CREATE INDEX IF NOT EXISTS ix_appr_status ON approvals(status);
CREATE INDEX IF NOT EXISTS ix_appr_thread ON approvals(thread_id);
CREATE TABLE IF NOT EXISTS dead_letters (
  id TEXT PRIMARY KEY, reason TEXT NOT NULL, status TEXT NOT NULL, poison INTEGER NOT NULL DEFAULT 0,
  attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, event_id TEXT, dispatch_id TEXT, thread_id TEXT,
  run_id TEXT, checkpoint_id TEXT, envelope TEXT, raw_body TEXT, created_at TEXT, resolved_at TEXT,
  resolved_by TEXT, resolution TEXT
);
CREATE TABLE IF NOT EXISTS eval_runs (
  eval_run_id TEXT PRIMARY KEY, status TEXT NOT NULL, suite TEXT, profile TEXT, label TEXT, scenario_ids TEXT,
  concurrency INTEGER, baseline_id TEXT, created_at TEXT, finished_at TEXT, done INTEGER NOT NULL DEFAULT 0,
  total INTEGER NOT NULL DEFAULT 0, cases TEXT NOT NULL DEFAULT '[]', summary TEXT, thread_id TEXT, run_id TEXT,
  event_id TEXT
);
CREATE TABLE IF NOT EXISTS generated_scenarios (
  scenario_id TEXT PRIMARY KEY, category TEXT, component TEXT, magnitude REAL, red_herrings INTEGER, seed INTEGER,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS suppressions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, component TEXT, started_at TEXT NOT NULL, ended_at TEXT, event_id TEXT
);
CREATE TABLE IF NOT EXISTS launches (
  id TEXT PRIMARY KEY, scenario_id TEXT, mode TEXT, speed REAL, context TEXT NOT NULL, created_at TEXT,
  events_total INTEGER, correlation_id TEXT, incident_id TEXT, replay_id TEXT
);
"""

_TABLES = (
    "events",
    "dispatches",
    "incidents",
    "approvals",
    "dead_letters",
    "eval_runs",
    "generated_scenarios",
    "suppressions",
    "launches",
)


def dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)


def loads(text: str | None, default: Any = None) -> Any:
    if text is None or text == "":
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default


def like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def glob_to_like(pattern: str) -> str:
    """``deploy.*`` -> ``deploy.%`` (only ``*`` is a wildcard)."""
    return like_escape(pattern).replace("*", "%")


class StateDB:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path) if str(path) != ":memory:" else None
        self._target = str(path)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    # --- lifecycle ---------------------------------------------------------------------------------------------
    def open(self) -> None:
        with self._lock:
            if self._conn is not None:
                return
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self._target, check_same_thread=False, isolation_level=None, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(SCHEMA)
            self._conn = conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    @property
    def is_open(self) -> bool:
        return self._conn is not None

    async def run(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Run a sync helper (this class' methods or any function that uses them) off the event loop."""
        return await asyncio.to_thread(fn, *args, **kwargs)

    # --- primitives --------------------------------------------------------------------------------------------
    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("state DB is not open")
        return self._conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self.conn
            if conn.in_transaction:  # nested use inside an outer transaction
                yield conn
                return
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, params)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(sql, params).fetchone()
            return dict(row) if row is not None else None

    def scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        with self._lock:
            row = self.conn.execute(sql, params).fetchone()
            return row[0] if row is not None else None

    def next_n(self, name: str) -> int:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO counters(name, n) VALUES(?, 1) ON CONFLICT(name) DO UPDATE SET n = n + 1", (name,)
            )
            n = conn.execute("SELECT n FROM counters WHERE name = ?", (name,)).fetchone()[0]
            return int(n)

    # --- kv ----------------------------------------------------------------------------------------------------
    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self.query_one("SELECT v FROM kv WHERE k = ?", (key,))
        return loads(row["v"], default) if row else default

    def kv_set(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v", (key, dumps(value))
        )

    # --- events ------------------------------------------------------------------------------------------------
    @staticmethod
    def event_from_row(r: dict[str, Any]) -> dict[str, Any]:
        return {
            "seq": r["seq"],
            "id": r["id"],
            "schema_version": "foghorn.event/1",
            "type": r["type"],
            "source": r["source"],
            "severity": r["severity"],
            "occurred_at": r["occurred_at"],
            "received_at": r["received_at"],
            "subject": loads(r["subject"], {}),
            "component": r["component"],
            "payload": loads(r["payload"], {}),
            "dedupe_key": r["dedupe_key"],
            "idempotency_key": r["idempotency_key"],
            "correlation_id": r["correlation_id"],
            "causation_id": r["causation_id"],
            "trace": loads(r["trace"], {}),
            "attributes": loads(r["attributes"], {}),
            "status": r["status"],
            "status_reason": r["status_reason"],
            "occurrences": r["occurrences"],
            "last_seen_at": r["last_seen_at"],
            "duplicate_of": r["duplicate_of"],
            "scenario_id": r["scenario_id"],
        }

    def insert_event(self, ev: dict[str, Any]) -> dict[str, Any]:
        """Insert an event (assigning ``seq`` and, when absent, ``id``); returns the stored row as an event dict."""
        with self.transaction() as conn:
            if ev.get("seq"):
                seq = int(ev["seq"])
                # never move the counter backwards (a lower explicit seq would make next_n hand out a used number)
                conn.execute(
                    "INSERT INTO counters(name, n) VALUES('event_seq', ?) ON CONFLICT(name) DO UPDATE SET n = MAX(n, ?)",
                    (seq, seq),
                )
            else:
                seq = self.next_n("event_seq")
            eid = ev.get("id") or f"evt_{seq:07d}"
            subject = ev.get("subject") or {}
            conn.execute(
                "INSERT INTO events(seq,id,type,source,severity,occurred_at,received_at,component,environment,subject,"
                "payload,dedupe_key,idempotency_key,correlation_id,causation_id,trace,attributes,status,status_reason,"
                "occurrences,last_seen_at,duplicate_of,scenario_id,dedupe_scope,launch_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    seq,
                    eid,
                    ev["type"],
                    ev["source"],
                    ev["severity"],
                    ev["occurred_at"],
                    ev["received_at"],
                    subject.get("component"),
                    subject.get("environment"),
                    dumps(subject),
                    dumps(ev.get("payload") or {}),
                    ev.get("dedupe_key"),
                    ev.get("idempotency_key"),
                    ev.get("correlation_id"),
                    ev.get("causation_id"),
                    dumps(ev.get("trace") or {}),
                    dumps(ev.get("attributes") or {}),
                    ev.get("status", "accepted"),
                    ev.get("status_reason"),
                    ev.get("occurrences", 1),
                    ev.get("last_seen_at") or ev["occurred_at"],
                    ev.get("duplicate_of"),
                    ev.get("scenario_id"),
                    ev.get("dedupe_scope", ""),
                    ev.get("launch_id"),
                ),
            )
            row = self.get_event(eid)
        assert row is not None
        return row

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        r = self.query_one("SELECT * FROM events WHERE id = ?", (event_id,))
        return self.event_from_row(r) if r else None

    def get_event_by_idempotency(self, key: str) -> dict[str, Any] | None:
        r = self.query_one("SELECT * FROM events WHERE idempotency_key = ?", (key,))
        return self.event_from_row(r) if r else None

    def peek_next_seq(self) -> int:
        return int(self.scalar("SELECT COALESCE((SELECT n FROM counters WHERE name = 'event_seq'), 0)") or 0) + 1

    def latest_seq(self) -> int:
        return int(self.scalar("SELECT COALESCE(MAX(seq), 0) FROM events") or 0)

    def set_event_status(self, event_id: str, status: str, reason: str | None = None) -> None:
        self.execute("UPDATE events SET status = ?, status_reason = ? WHERE id = ?", (status, reason, event_id))

    def set_event_correlation(self, event_id: str, correlation_id: str) -> None:
        self.execute("UPDATE events SET correlation_id = ? WHERE id = ?", (correlation_id, event_id))

    def find_dedupe_candidate(self, dedupe_key: str, etype: str, scope: str) -> dict[str, Any] | None:
        r = self.query_one(
            "SELECT * FROM events WHERE dedupe_key = ? AND type = ? AND dedupe_scope = ? AND window_closed = 0 "
            "AND status != 'suppressed' ORDER BY seq DESC LIMIT 1",
            (dedupe_key, etype, scope),
        )
        return self.event_from_row(r) if r else None

    def bump_occurrences(self, event_id: str, last_seen: str) -> int:
        self.execute(
            "UPDATE events SET occurrences = occurrences + 1, last_seen_at = ? WHERE id = ?", (last_seen, event_id)
        )
        return int(self.scalar("SELECT occurrences FROM events WHERE id = ?", (event_id,)))

    def close_dedupe_window(self, dedupe_key: str, etype: str, scope: str) -> None:
        self.execute(
            "UPDATE events SET window_closed = 1 WHERE dedupe_key = ? AND type = ? AND dedupe_scope = ?",
            (dedupe_key, etype, scope),
        )

    def list_events(
        self,
        *,
        types: Sequence[str] = (),
        component: str | None = None,
        severities: Sequence[str] = (),
        statuses: Sequence[str] = (),
        source: str | None = None,
        scenario_id: str | None = None,
        correlation_id: str | None = None,
        q: str | None = None,
        since: int | None = None,
        before: int | None = None,
        limit: int = 100,
        order: str = "desc",
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if types:
            where.append("(" + " OR ".join("type LIKE ? ESCAPE '\\'" for _ in types) + ")")
            params.extend(glob_to_like(t) for t in types)
        if component:
            where.append("component = ?")
            params.append(component)
        if severities:
            where.append("severity IN (" + ",".join("?" * len(severities)) + ")")
            params.extend(severities)
        if statuses:
            where.append("status IN (" + ",".join("?" * len(statuses)) + ")")
            params.extend(statuses)
        if source:
            where.append("source = ?")
            params.append(source)
        if scenario_id:
            where.append("scenario_id = ?")
            params.append(scenario_id)
        if correlation_id:
            where.append("correlation_id = ?")
            params.append(correlation_id)
        if q:
            where.append("(type || ' ' || COALESCE(component,'') || ' ' || payload || ' ' || id) LIKE ? ESCAPE '\\'")
            params.append(f"%{like_escape(q)}%")
        if since is not None:
            where.append("seq > ?")
            params.append(since)
        if before is not None:
            where.append("seq < ?")
            params.append(before)
        sql = "SELECT * FROM events"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY seq {'ASC' if order == 'asc' else 'DESC'} LIMIT ?"
        params.append(max(1, min(limit, 5000)))
        return [self.event_from_row(r) for r in self.query(sql, params)]

    def events_by_causation(self, parent_ids: Sequence[str]) -> list[dict[str, Any]]:
        if not parent_ids:
            return []
        rows = self.query(
            f"SELECT * FROM events WHERE causation_id IN ({','.join('?' * len(parent_ids))}) ORDER BY seq",
            list(parent_ids),
        )
        return [self.event_from_row(r) for r in rows]

    def causal_chain(self, event_id: str, *, max_nodes: int = 500) -> tuple[str, list[tuple[dict[str, Any], int]]]:
        """Root id and every event reachable from the root through ``causation_id`` (with depth), root first."""
        seen: set[str] = set()
        cur = self.get_event(event_id)
        if cur is None:
            return event_id, []
        root = cur
        while root.get("causation_id") and root["causation_id"] not in seen:
            seen.add(root["id"])
            parent = self.get_event(root["causation_id"])
            if parent is None:
                break
            root = parent
        out: list[tuple[dict[str, Any], int]] = [(root, 0)]
        frontier = [root["id"]]
        visited = {root["id"]}
        depth = 0
        while frontier and len(out) < max_nodes:
            depth += 1
            children = [c for c in self.events_by_causation(frontier) if c["id"] not in visited]
            for c in children:
                visited.add(c["id"])
                out.append((c, depth))
            frontier = [c["id"] for c in children]
        return root["id"], out[:max_nodes]

    def count_events(self) -> int:
        return int(self.scalar("SELECT COUNT(*) FROM events") or 0)

    def events_since_wall(self, received_after: str) -> int:
        return int(self.scalar("SELECT COUNT(*) FROM events WHERE received_at > ?", (received_after,)) or 0)

    # --- dispatches --------------------------------------------------------------------------------------------
    @staticmethod
    def dispatch_from_row(r: dict[str, Any]) -> dict[str, Any]:
        out = {k: r[k] for k in r if k not in {"n", "plan"}}
        out["plan"] = loads(r["plan"], {})
        return out

    def insert_dispatch(
        self,
        *,
        event_id: str,
        route: str,
        priority: str,
        plan: dict[str, Any],
        generation: int = 0,
        kind: str = "run",
        mode: str = "start",
        status: str = "queued",
        enqueued_at: str | None = None,
        parent_dispatch_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        with self.transaction() as conn:
            n = self.next_n("dispatch")
            did = f"dsp_{n:07d}"
            conn.execute(
                "INSERT INTO dispatches(n,id,event_id,route,generation,kind,priority,status,attempts,graph_id,"
                "assistant_id,thread_id,run_id,incident_id,parent_dispatch_id,plan,mode,enqueued_at) "
                "VALUES(?,?,?,?,?,?,?,?,0,?,?,?,?,?,?,?,?,?)",
                (
                    n,
                    did,
                    event_id,
                    route,
                    generation,
                    kind,
                    priority,
                    status,
                    plan.get("graph_id"),
                    plan.get("assistant_id"),
                    plan.get("thread_id"),
                    run_id,
                    plan.get("incident_id"),
                    parent_dispatch_id,
                    dumps(plan),
                    mode,
                    enqueued_at,
                ),
            )
        row = self.get_dispatch(did)
        assert row is not None
        return row

    def get_dispatch(self, dispatch_id: str) -> dict[str, Any] | None:
        r = self.query_one("SELECT * FROM dispatches WHERE id = ?", (dispatch_id,))
        return self.dispatch_from_row(r) if r else None

    def update_dispatch(self, dispatch_id: str, **fields: Any) -> dict[str, Any]:
        allowed = {
            "status",
            "attempts",
            "run_id",
            "thread_id",
            "incident_id",
            "started_at",
            "finished_at",
            "next_attempt_at",
            "error",
            "mode",
            "plan",
            "graph_id",
            "assistant_id",
        }
        sets, params = [], []
        for k, v in fields.items():
            if k not in allowed:
                raise KeyError(k)
            sets.append(f"{k} = ?")
            params.append(dumps(v) if k == "plan" else v)
        if sets:
            self.execute(f"UPDATE dispatches SET {', '.join(sets)} WHERE id = ?", [*params, dispatch_id])
        row = self.get_dispatch(dispatch_id)
        assert row is not None
        return row

    def list_dispatches(
        self,
        *,
        statuses: Sequence[str] = (),
        event_id: str | None = None,
        graph_id: str | None = None,
        incident_id: str | None = None,
        thread_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        where: list[str] = []
        params: list[Any] = []
        if statuses:
            where.append("status IN (" + ",".join("?" * len(statuses)) + ")")
            params.extend(statuses)
        for col, val in (
            ("event_id", event_id),
            ("graph_id", graph_id),
            ("incident_id", incident_id),
            ("thread_id", thread_id),
        ):
            if val:
                where.append(f"{col} = ?")
                params.append(val)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        total = int(self.scalar(f"SELECT COUNT(*) FROM dispatches{clause}", params) or 0)
        rows = self.query(
            f"SELECT * FROM dispatches{clause} ORDER BY n DESC LIMIT ? OFFSET ?",
            [*params, max(1, min(limit, 500)), offset],
        )
        return [self.dispatch_from_row(r) for r in rows], total

    def dispatches_for_events(self, event_ids: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {e: [] for e in event_ids}
        if not event_ids:
            return out
        for r in self.query(
            f"SELECT * FROM dispatches WHERE event_id IN ({','.join('?' * len(event_ids))}) ORDER BY n", list(event_ids)
        ):
            out.setdefault(r["event_id"], []).append(self.dispatch_from_row(r))
        return out

    def dispatch_count_by_status(self) -> dict[str, int]:
        return {r["status"]: r["c"] for r in self.query("SELECT status, COUNT(*) AS c FROM dispatches GROUP BY status")}

    def next_generation(self, event_id: str, route: str) -> int:
        v = self.scalar("SELECT MAX(generation) FROM dispatches WHERE event_id = ? AND route = ?", (event_id, route))
        return 0 if v is None else int(v) + 1

    def dispatch_exists(self, event_id: str, route: str, generation: int) -> bool:
        return bool(
            self.scalar(
                "SELECT 1 FROM dispatches WHERE event_id=? AND route=? AND generation=?", (event_id, route, generation)
            )
        )

    # --- incidents ---------------------------------------------------------------------------------------------
    @staticmethod
    def incident_from_row(r: dict[str, Any]) -> dict[str, Any]:
        out = dict(r)
        out["root_cause"] = loads(r.get("root_cause"))
        return out

    def get_incident(self, incident_id: str) -> dict[str, Any] | None:
        r = self.query_one("SELECT * FROM incidents WHERE incident_id = ?", (incident_id,))
        return self.incident_from_row(r) if r else None

    def upsert_incident(self, incident_id: str, **fields: Any) -> dict[str, Any]:
        """Insert or update; ``None`` values never overwrite; a ``resolved`` incident never regresses."""
        cur = self.get_incident(incident_id)
        if cur is None:
            cols = {"incident_id": incident_id, "status": fields.get("status") or "open"}
            cols.update({k: v for k, v in fields.items() if v is not None and k != "status"})
            if "root_cause" in cols:
                cols["root_cause"] = dumps(cols["root_cause"])
            names = ",".join(cols)
            self.execute(f"INSERT INTO incidents({names}) VALUES({','.join('?' * len(cols))})", list(cols.values()))
        else:
            upd = {k: v for k, v in fields.items() if v is not None}
            if cur["status"] == "resolved" and upd.get("status") != "resolved":
                upd.pop("status", None)
                upd.pop("resolved_at", None)
            if "root_cause" in upd:
                upd["root_cause"] = dumps(upd["root_cause"])
            if upd:
                self.execute(
                    f"UPDATE incidents SET {', '.join(f'{k} = ?' for k in upd)} WHERE incident_id = ?",
                    [*upd.values(), incident_id],
                )
        row = self.get_incident(incident_id)
        assert row is not None
        return row

    def list_incidents(
        self,
        *,
        statuses: Sequence[str] = (),
        severities: Sequence[str] = (),
        scenario_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        where: list[str] = []
        params: list[Any] = []
        if statuses:
            where.append("status IN (" + ",".join("?" * len(statuses)) + ")")
            params.extend(statuses)
        if severities:
            where.append("severity IN (" + ",".join("?" * len(severities)) + ")")
            params.extend(severities)
        if scenario_id:
            where.append("scenario_id = ?")
            params.append(scenario_id)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        total = int(self.scalar(f"SELECT COUNT(*) FROM incidents{clause}", params) or 0)
        rows = self.query(
            f"SELECT * FROM incidents{clause} ORDER BY COALESCE(opened_at, '') DESC, rowid DESC LIMIT ? OFFSET ?",
            [*params, max(1, min(limit, 500)), offset],
        )
        return [self.incident_from_row(r) for r in rows], total

    def open_incident_for_component(self, component: str | None) -> dict[str, Any] | None:
        if not component:
            return None
        r = self.query_one(
            "SELECT * FROM incidents WHERE component = ? AND status != 'resolved' ORDER BY rowid DESC LIMIT 1",
            (component,),
        )
        return self.incident_from_row(r) if r else None

    def count_open_incidents(self) -> int:
        return int(self.scalar("SELECT COUNT(*) FROM incidents WHERE status != 'resolved'") or 0)

    # --- approvals ---------------------------------------------------------------------------------------------
    @staticmethod
    def approval_from_row(r: dict[str, Any]) -> dict[str, Any]:
        out = dict(r)
        out["reversible"] = None if r.get("reversible") is None else bool(r["reversible"])
        for k in ("decision", "payload", "response_schema", "options"):
            out[k] = loads(r.get(k))
        out.pop("nonce", None)
        return out

    def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        r = self.query_one("SELECT * FROM approvals WHERE approval_id = ?", (approval_id,))
        return self.approval_from_row(r) if r else None

    def get_approval_nonce(self, approval_id: str) -> str | None:
        v = self.scalar("SELECT nonce FROM approvals WHERE approval_id = ?", (approval_id,))
        return None if v is None else str(v)

    def upsert_approval(self, approval_id: str, **fields: Any) -> dict[str, Any]:
        cols = dict(fields)
        for k in ("decision", "payload", "response_schema", "options"):
            if k in cols and cols[k] is not None:
                cols[k] = dumps(cols[k])
        if "reversible" in cols and cols["reversible"] is not None:
            cols["reversible"] = 1 if cols["reversible"] else 0
        cols = {k: v for k, v in cols.items() if v is not None}
        if self.query_one("SELECT 1 FROM approvals WHERE approval_id = ?", (approval_id,)) is None:
            cols.setdefault("status", "pending")
            cols["approval_id"] = approval_id
            self.execute(
                f"INSERT INTO approvals({','.join(cols)}) VALUES({','.join('?' * len(cols))})", list(cols.values())
            )
        elif cols:
            self.execute(
                f"UPDATE approvals SET {', '.join(f'{k} = ?' for k in cols)} WHERE approval_id = ?",
                [*cols.values(), approval_id],
            )
        row = self.get_approval(approval_id)
        assert row is not None
        return row

    def list_approvals(
        self,
        *,
        statuses: Sequence[str] = (),
        incident_id: str | None = None,
        kind: str | None = None,
        thread_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if statuses:
            where.append("status IN (" + ",".join("?" * len(statuses)) + ")")
            params.extend(statuses)
        for col, val in (("incident_id", incident_id), ("kind", kind), ("thread_id", thread_id)):
            if val:
                where.append(f"{col} = ?")
                params.append(val)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows = self.query(
            f"SELECT * FROM approvals{clause} ORDER BY COALESCE(requested_at,'') DESC, rowid DESC LIMIT ?",
            [*params, max(1, min(limit, 1000))],
        )
        return [self.approval_from_row(r) for r in rows]

    def count_pending_approvals(self) -> int:
        return int(self.scalar("SELECT COUNT(*) FROM approvals WHERE status = 'pending'") or 0)

    def set_approval_nonce(self, approval_id: str, nonce: str | None) -> None:
        self.execute("UPDATE approvals SET nonce = ? WHERE approval_id = ?", (nonce, approval_id))

    # --- dead letters ------------------------------------------------------------------------------------------
    @staticmethod
    def dead_letter_from_row(r: dict[str, Any]) -> dict[str, Any]:
        out = dict(r)
        out["poison"] = bool(r["poison"])
        out["envelope"] = loads(r.get("envelope"))
        return out

    def insert_dead_letter(
        self,
        *,
        reason: str,
        created_at: str,
        attempts: int = 0,
        last_error: str | None = None,
        event_id: str | None = None,
        dispatch_id: str | None = None,
        thread_id: str | None = None,
        run_id: str | None = None,
        checkpoint_id: str | None = None,
        envelope: dict[str, Any] | None = None,
        raw_body: str | None = None,
        poison: bool = False,
    ) -> dict[str, Any]:
        n = self.next_n("dead_letter")
        did = f"dlq_{n:07d}"
        self.execute(
            "INSERT INTO dead_letters(id,reason,status,poison,attempts,last_error,event_id,dispatch_id,thread_id,run_id,"
            "checkpoint_id,envelope,raw_body,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                did,
                reason,
                "open",
                int(poison),
                attempts,
                last_error,
                event_id,
                dispatch_id,
                thread_id,
                run_id,
                checkpoint_id,
                dumps(envelope) if envelope is not None else None,
                raw_body,
                created_at,
            ),
        )
        row = self.get_dead_letter(did)
        assert row is not None
        return row

    def get_dead_letter(self, dlq_id: str) -> dict[str, Any] | None:
        r = self.query_one("SELECT * FROM dead_letters WHERE id = ?", (dlq_id,))
        return self.dead_letter_from_row(r) if r else None

    def update_dead_letter(self, dlq_id: str, **fields: Any) -> dict[str, Any]:
        if fields:
            self.execute(
                f"UPDATE dead_letters SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                [*fields.values(), dlq_id],
            )
        row = self.get_dead_letter(dlq_id)
        assert row is not None
        return row

    def list_dead_letters(
        self, *, status: str | None = "open", reason: str | None = None, limit: int = 100, offset: int = 0
    ) -> tuple[list[dict[str, Any]], int]:
        where: list[str] = []
        params: list[Any] = []
        if status and status != "all":
            where.append("status = ?")
            params.append(status)
        if reason:
            where.append("reason = ?")
            params.append(reason)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        total = int(self.scalar(f"SELECT COUNT(*) FROM dead_letters{clause}", params) or 0)
        rows = self.query(
            f"SELECT * FROM dead_letters{clause} ORDER BY rowid DESC LIMIT ? OFFSET ?",
            [*params, max(1, min(limit, 500)), offset],
        )
        return [self.dead_letter_from_row(r) for r in rows], total

    def count_open_dead_letters(self) -> int:
        return int(self.scalar("SELECT COUNT(*) FROM dead_letters WHERE status = 'open'") or 0)

    # --- eval runs ---------------------------------------------------------------------------------------------
    @staticmethod
    def eval_from_row(r: dict[str, Any]) -> dict[str, Any]:
        cases = loads(r["cases"], [])
        evaluators: list[str] = []
        for c in cases:
            for name in (c.get("scores") or {}) if isinstance(c, dict) else ():
                if name not in evaluators:
                    evaluators.append(name)
        return {
            "eval_run_id": r["eval_run_id"],
            "status": r["status"],
            "suite": r["suite"],
            "profile": r["profile"],
            "label": r["label"],
            "scenario_ids": loads(r["scenario_ids"], []),
            "concurrency": r["concurrency"],
            "baseline_id": r["baseline_id"],
            "created_at": r["created_at"],
            "finished_at": r["finished_at"],
            "progress": {"done": r["done"], "total": r["total"]},
            "evaluators": evaluators,
            "cases": cases,
            "summary": loads(r["summary"]),
            "thread_id": r["thread_id"],
            "run_id": r["run_id"],
            "event_id": r["event_id"],
        }

    def create_eval_run(
        self,
        *,
        created_at: str,
        suite: str,
        profile: str,
        label: str | None,
        scenario_ids: list[str],
        concurrency: int | None,
        baseline_id: str | None,
    ) -> dict[str, Any]:
        n = self.next_n("eval_run")
        eid = f"evr_{n:04d}"
        self.execute(
            "INSERT INTO eval_runs(eval_run_id,status,suite,profile,label,scenario_ids,concurrency,baseline_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (eid, "requested", suite, profile, label, dumps(scenario_ids), concurrency, baseline_id, created_at),
        )
        row = self.get_eval_run(eid)
        assert row is not None
        return row

    def get_eval_run(self, eval_run_id: str) -> dict[str, Any] | None:
        r = self.query_one("SELECT * FROM eval_runs WHERE eval_run_id = ?", (eval_run_id,))
        return self.eval_from_row(r) if r else None

    def update_eval_run(self, eval_run_id: str, **fields: Any) -> dict[str, Any] | None:
        cols = {k: (dumps(v) if k in {"cases", "summary", "scenario_ids"} else v) for k, v in fields.items()}
        if cols:
            self.execute(
                f"UPDATE eval_runs SET {', '.join(f'{k} = ?' for k in cols)} WHERE eval_run_id = ?",
                [*cols.values(), eval_run_id],
            )
        return self.get_eval_run(eval_run_id)

    def list_eval_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        return [
            self.eval_from_row(r)
            for r in self.query("SELECT * FROM eval_runs ORDER BY rowid DESC LIMIT ?", (max(1, min(limit, 500)),))
        ]

    # --- generated scenarios / suppressions / launches -------------------------------------------------------------------
    def record_generated(
        self,
        scenario_id: str,
        *,
        category: str,
        component: str,
        magnitude: float,
        red_herrings: int,
        seed: int,
        created_at: str,
    ) -> None:
        self.execute(
            "INSERT OR REPLACE INTO generated_scenarios(scenario_id,category,component,magnitude,red_herrings,seed,created_at) VALUES(?,?,?,?,?,?,?)",
            (scenario_id, category, component, magnitude, red_herrings, seed, created_at),
        )

    def list_generated(self) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM generated_scenarios ORDER BY rowid DESC")

    def open_suppression(self, component: str | None, started_at: str, event_id: str) -> None:
        self.execute(
            "INSERT INTO suppressions(component, started_at, event_id) VALUES(?,?,?)",
            (component or "*", started_at, event_id),
        )

    def close_suppression(self, component: str | None, ended_at: str) -> int:
        cur = self.execute(
            "UPDATE suppressions SET ended_at = ? WHERE ended_at IS NULL AND component = ?",
            (ended_at, component or "*"),
        )
        return cur.rowcount

    def suppressions_for(self, component: str | None) -> list[dict[str, Any]]:
        return self.query(
            "SELECT * FROM suppressions WHERE component = ? OR component = '*' ORDER BY id DESC", (component or "*",)
        )

    def insert_launch(
        self,
        *,
        scenario_id: str,
        mode: str,
        speed: float,
        context: dict[str, Any],
        created_at: str,
        events_total: int,
        correlation_id: str | None,
    ) -> dict[str, Any]:
        n = self.next_n("launch")
        lid = f"lau_{n:04d}"
        self.execute(
            "INSERT INTO launches(id,scenario_id,mode,speed,context,created_at,events_total,correlation_id) VALUES(?,?,?,?,?,?,?,?)",
            (lid, scenario_id, mode, speed, dumps(context), created_at, events_total, correlation_id),
        )
        return self.get_launch(lid) or {}

    def get_launch(self, launch_id: str) -> dict[str, Any] | None:
        r = self.query_one("SELECT * FROM launches WHERE id = ?", (launch_id,))
        if r is None:
            return None
        r["context"] = loads(r["context"], {})
        return r

    def update_launch(self, launch_id: str, **fields: Any) -> None:
        if fields:
            self.execute(
                f"UPDATE launches SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                [*fields.values(), launch_id],
            )

    # --- admin ---------------------------------------------------------------------------------------------------
    def reset(self) -> dict[str, int]:
        deleted: dict[str, int] = {}
        with self.transaction() as conn:
            for t in _TABLES:
                deleted[t] = int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
                conn.execute(f"DELETE FROM {t}")
            conn.execute("DELETE FROM counters")
            conn.execute("DELETE FROM kv WHERE k LIKE 'replay_runs:%' OR k = 'clock'")
        return deleted
