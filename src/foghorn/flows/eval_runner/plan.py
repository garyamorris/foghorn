"""Suite expansion: ``core16`` (S01-S16), ``custom`` (explicit ids, curated or generated) and ``sweep60`` (the 60 generated
fault specs, expanded through the fault builder into ``X-nnnn`` scenarios). Pure/sync: call it from a sync node or a thread."""

from __future__ import annotations

from typing import Any

from foghorn.data import api as data


def _task(rec: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"scenario_id": rec["scenario_id"], "title": rec.get("title"), "tier": rec.get("tier"), **extra}


def plan_suite(
    suite: str, scenario_ids: list[str] | None = None, max_scenarios: int | None = None
) -> list[dict[str, Any]]:
    """The ordered list of scenario tasks. ``scenario_ids`` narrows ``core16`` / ``sweep60`` and is required for ``custom``."""
    ids = [str(s) for s in scenario_ids or []]
    if suite == "custom":
        if not ids:
            raise ValueError("suite 'custom' needs scenario_ids")
        tasks = [_task(data.get_scenario(sid)) for sid in dict.fromkeys(ids)]
    elif suite == "sweep60":
        tasks = []
        for spec in data.sweep_specs():
            rec = data.build_fault_scenario(**spec["build_args"])
            tasks.append(_task(rec, spec_id=spec.get("spec_id")))
        if ids:
            tasks = [t for t in tasks if t["scenario_id"] in ids or t.get("spec_id") in ids]
    elif suite == "core16":
        tasks = [_task(rec) for rec in data.list_scenarios()]
        if ids:
            tasks = [t for t in tasks if t["scenario_id"] in ids]
    else:
        raise ValueError(f"unknown suite {suite!r}: choose core16, sweep60 or custom")
    if max_scenarios:
        tasks = tasks[: max(int(max_scenarios), 0)]
    return tasks
