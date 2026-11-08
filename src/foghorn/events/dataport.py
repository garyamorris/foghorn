"""The events/server side of the DATA engineer's ``foghorn.data.api`` (ARCHITECTURE.md section 4.1).

Everything is imported lazily and every failure maps to :class:`DataUnavailable`, so the app boots (and its tests run
against a small fake implementing :class:`DataPort`) even while ``foghorn.data`` is incomplete. All calls are
SYNCHRONOUS and may touch SQLite/files (the telemetry DB is built lazily): callers run them with ``asyncio.to_thread``.
"""

from __future__ import annotations

import contextlib
import importlib
from collections.abc import Iterator
from typing import Any, Protocol


class DataUnavailable(Exception):
    """``foghorn.data.api`` (or one function of it) is not importable / not implemented yet."""


class UnknownScenario(KeyError):
    pass


class DataPort(Protocol):
    def available(self) -> bool: ...
    def list_scenarios(self, tier: int | None = None, *, include_generated: bool = False) -> list[dict[str, Any]]: ...
    def get_scenario(self, scenario_id: str, *, reveal_truth: bool = False) -> dict[str, Any]: ...
    def default_as_of(self, scenario_id: str) -> str: ...
    def scenario_events(self, scenario_id: str) -> list[dict[str, Any]]: ...
    def list_streams(self) -> list[dict[str, Any]]: ...
    def iter_stream(self, name: str) -> Iterator[dict[str, Any]]: ...
    def service_catalog(self) -> list[dict[str, Any]]: ...
    def datasets_index(self) -> list[dict[str, Any]]: ...
    def read_dataset(
        self, name: str, offset: int = 0, limit: int = 50, *, reveal_truth: bool = False, scenario_id: str | None = None
    ) -> dict[str, Any]: ...
    def build_fault_scenario(
        self, category: str, component: str, magnitude: float, red_herrings: int, seed: int
    ) -> dict[str, Any]: ...
    def fault_categories(self) -> list[str]: ...
    def commander_input(self, scenario_id: str, incident: dict[str, Any]) -> dict[str, Any]: ...
    def seed_memories(self) -> list[dict[str, Any]]: ...


#: used only when the data layer exposes neither ``fault_categories`` nor ``faults.CATEGORIES``
DEFAULT_FAULT_CATEGORIES = [
    "bad_deploy",
    "cert_expiry",
    "disk_full",
    "planned_maintenance",
    "db_pool_exhaustion",
    "cache_stampede",
    "queue_poison_message",
    "dependency_outage",
    "memory_leak",
    "config_change",
    "slow_query_migration",
    "network_partition",
    "telemetry_gap",
    "runaway_query",
]


_INTERRUPT_CHIPS = {
    "approval": "approval",
    "question": "question",
    "break_glass": "break-glass",
    "edit": "edit",
    "memory_write": "memory write",
    "four_eyes": "four-eyes",
    "tool_review": "tool review",
}
STORM_ALERTS = 40  # a scenario with at least this many alerts is a storm


def derive_features(truth: dict[str, Any], public: dict[str, Any]) -> list[str]:
    """Human-readable chips for the launcher: WHAT a scenario exercises (interrupt styles, memory, injection, storm),
    never what the answer is. Only generic labels are emitted - no component names, change ids or remediation."""
    out: list[str] = []
    interrupts = [i for i in truth.get("expected_interrupts") or [] if isinstance(i, dict)]
    for i in interrupts:
        chip = _INTERRUPT_CHIPS.get(str(i.get("kind")), str(i.get("kind")).replace("_", " "))
        if chip not in out:
            out.append(chip)
        if int(i.get("approvers") or 0) >= 2 and "two-person approval" not in out:
            out.append("two-person approval")
    if len(interrupts) > 1:
        out.append("multiple interrupts")
    if (truth.get("expected_memory") or {}).get("recall"):
        out.append("memory recall")
    for flag in truth.get("flags") or []:
        out.append("injection" if "injection" in str(flag) else str(flag).replace("_", " "))
    if truth.get("is_noise"):
        out.append("no-op")
    if int((public.get("stats") or {}).get("alerts") or 0) >= STORM_ALERTS:
        out.append("alert storm")
    return out


def derive_category(truth: dict[str, Any]) -> str | None:
    cat = (truth.get("root_cause") or {}).get("category")
    if cat:
        return str(cat)
    return "planned_maintenance" if truth.get("is_noise") else None


class RealDataPort:
    """Adapter over ``foghorn.data.api``; optional functions fall back gracefully."""

    module_name = "foghorn.data.api"

    def __init__(self) -> None:
        self._enrichment: dict[str, tuple[list[str], str | None]] = {}

    def _enrich(self, rec: dict[str, Any]) -> dict[str, Any]:
        """Add ``features[]`` and ``category`` (derived from the scenario spec) to a scenario view, without ever copying
        any other truth field into it. Cached: curated scenarios are static and generated ids are content hashes."""
        sid = rec.get("scenario_id")
        if not sid or ("features" in rec and "category" in rec):
            return rec
        if sid not in self._enrichment:
            try:
                truth = self._fn("get_truth")(sid)
            except Exception:  # no truth available -> no chips, never an error
                truth = {}
            self._enrichment[sid] = (derive_features(truth, rec), derive_category(truth))
        features, category = self._enrichment[sid]
        return {"features": list(features), "category": category, **rec}  # keys the data layer already set win

    def _api(self) -> Any:
        try:
            return importlib.import_module(self.module_name)
        except Exception as exc:  # ImportError, but also any error raised by an unfinished package
            raise DataUnavailable(f"{self.module_name} is not importable: {exc!r}") from exc

    def _fn(self, name: str) -> Any:
        fn = getattr(self._api(), name, None)
        if fn is None:
            raise DataUnavailable(f"{self.module_name}.{name} is not implemented yet")
        return fn

    def available(self) -> bool:
        try:
            self._fn("list_scenarios")
        except DataUnavailable:
            return False
        return True

    def _scenario_call(self, name: str, scenario_id: str, **kw: Any) -> Any:
        try:
            return self._fn(name)(scenario_id, **kw)
        except KeyError as exc:
            raise UnknownScenario(scenario_id) from exc

    def list_scenarios(self, tier: int | None = None, *, include_generated: bool = False) -> list[dict[str, Any]]:
        fn = self._fn("list_scenarios")
        try:
            rows = list(fn(tier, include_generated=include_generated))
        except TypeError:  # an older data layer without the flag
            rows = list(fn(tier))
        return [self._enrich(r) for r in rows]

    def get_scenario(self, scenario_id: str, *, reveal_truth: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = self._enrich(self._scenario_call("get_scenario", scenario_id, reveal_truth=reveal_truth))
        return out

    def default_as_of(self, scenario_id: str) -> str:
        return str(self._scenario_call("default_as_of", scenario_id))

    def scenario_events(self, scenario_id: str) -> list[dict[str, Any]]:
        return list(self._scenario_call("scenario_events", scenario_id))

    def list_streams(self) -> list[dict[str, Any]]:
        return list(self._fn("list_streams")())

    def iter_stream(self, name: str) -> Iterator[dict[str, Any]]:
        try:
            return iter(self._fn("iter_stream")(name))
        except KeyError as exc:
            raise UnknownScenario(name) from exc

    def service_catalog(self) -> list[dict[str, Any]]:
        return list(self._fn("service_catalog")())

    def datasets_index(self) -> list[dict[str, Any]]:
        return list(self._fn("datasets_index")())

    def read_dataset(
        self, name: str, offset: int = 0, limit: int = 50, *, reveal_truth: bool = False, scenario_id: str | None = None
    ) -> dict[str, Any]:
        fn = self._fn("read_dataset")
        try:
            try:
                out: dict[str, Any] = fn(name, offset, limit, reveal_truth=reveal_truth, scenario_id=scenario_id)
            except TypeError:  # an older data layer without the truth switch
                out = fn(name, offset, limit)
        except KeyError as exc:
            raise UnknownScenario(name) from exc
        return out

    def build_fault_scenario(
        self, category: str, component: str, magnitude: float, red_herrings: int, seed: int
    ) -> dict[str, Any]:
        out: dict[str, Any] = self._fn("build_fault_scenario")(category, component, magnitude, red_herrings, seed)
        return out

    def fault_categories(self) -> list[str]:
        try:
            fn = getattr(self._api(), "fault_categories", None)
        except DataUnavailable:
            return list(DEFAULT_FAULT_CATEGORIES)
        if fn is not None:
            return [str(c) for c in fn()]
        with contextlib.suppress(Exception):  # the data layer's own category list
            faults = importlib.import_module(self.module_name.rsplit(".", 1)[0] + ".faults")
            return [str(c) for c in faults.CATEGORIES]
        try:
            cats = sorted({str(s.get("category")) for s in self.list_scenarios() if s.get("category")})
        except DataUnavailable:
            cats = []
        return cats or list(DEFAULT_FAULT_CATEGORIES)

    def commander_input(self, scenario_id: str, incident: dict[str, Any]) -> dict[str, Any]:
        """A ``CommanderInput`` for a direct launch: the scenario, its default ``as_of`` and the given incident ref."""
        as_of = None
        with contextlib.suppress(DataUnavailable):
            as_of = self.default_as_of(scenario_id)
        out: dict[str, Any] = {"scenario_id": scenario_id, "incident": incident}
        if as_of:
            out["as_of"] = as_of
        return out

    def seed_memories(self) -> list[dict[str, Any]]:
        """Seed items ``{namespace: [..], key, value}``: ``api.memory_seed_items()`` (the DATA engineer's name) or
        ``api.seed_memories()``; as a last resort a seed dataset."""
        api = self._api()
        for name in ("memory_seed_items", "seed_memories"):
            fn = getattr(api, name, None)
            if fn is not None:
                return [dict(i) for i in fn()]
        out: list[dict[str, Any]] = []
        for name in ("memories_seed", "seed_memories", "memories"):
            try:
                offset = 0
                while True:
                    page = self.read_dataset(name, offset, 200)
                    items = page.get("items") or page.get("rows") or page.get("records") or []
                    out.extend(
                        i for i in items if isinstance(i, dict) and "namespace" in i and "key" in i and "value" in i
                    )
                    offset += len(items)
                    if not items or offset >= int(page.get("total", 0)):
                        break
            except (DataUnavailable, UnknownScenario):
                continue
            if out:
                break
        return out
