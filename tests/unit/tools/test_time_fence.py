"""THE TIME FENCE: no tool ever returns data after ``as_of`` (acceptance A4) - for every tool, scenario and as_of."""

from __future__ import annotations

import re
from typing import Any

import pytest

from foghorn.data import access, catalog, paths, store
from foghorn.data import api as data
from foghorn.data.util import to_epoch, to_iso
from foghorn.tools import api

SIDS = [f"S{i:02d}" for i in range(1, 17)]
ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
#: announced-in-advance facts are allowed to lie after as_of (a maintenance window's planned end)
ALLOWED_FUTURE_KEYS = {"ends_at"}


def stamps(obj: Any, key: str = "") -> list[tuple[str, str]]:
    """Every ISO timestamp inside a JSON-like result, with the key it sits under (free text included)."""
    out: list[tuple[str, str]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out += stamps(v, k)
    elif isinstance(obj, list):
        for v in obj:
            out += stamps(v, key)
    elif isinstance(obj, str):
        out += [(key, m) for m in ISO.findall(obj)]
    return out


def assert_fenced(result: Any, as_of: str, what: str) -> None:
    for key, ts in stamps(result):
        if key in ALLOWED_FUTURE_KEYS:
            continue
        assert ts <= as_of, f"{what}: {key}={ts} is after as_of={as_of}"


def as_of_grid(sid: str) -> list[str]:
    rec = data.get_scenario(sid)
    t0, onset = to_epoch(rec["world"]["window"][0]), to_epoch(rec["world"]["onset"])
    end = t0 + 134 * 60
    default = to_epoch(rec["trigger"]["as_of"])
    pts = [t0 - 600, t0, t0 + 17 * 60, onset - 60, onset, default, onset + 20 * 60, end - 60, end, end + 1800]
    return [to_iso(p) for p in pts]


def call_everything(sid: str, as_of: str, actions: list[dict[str, Any]]) -> dict[str, Any]:
    ctx = {"scenario_id": sid, "as_of": as_of, "applied_actions": actions}
    root = (data.get_truth(sid)["root_cause"] or {"component": "orders-db"})["component"]
    out: dict[str, Any] = {}
    for comp in (root, "checkout-service", "orders-db", "edge-gateway"):
        for metric in catalog.COMPONENTS[comp].metrics:
            out[f"query:{comp}:{metric}"] = api.query_metric(ctx, comp, metric, 135)
        out[f"compare:{comp}"] = api.compare_baseline(ctx, comp, "error_rate")
        out[f"logs:{comp}"] = api.search_logs(ctx, comp, "", None, 135, 200)
        out[f"verify:{comp}"] = api.verify_recovery(ctx, comp)
    out["changes"] = api.list_changes(ctx, 240)
    out["changes_week"] = api.list_changes(ctx, 240, lookback_days=7)
    out["alerts"] = api.list_alerts(ctx, window_minutes=135)
    out["oncall"] = api.get_oncall(ctx, root)
    out["topology"] = api.get_topology(ctx, root)
    out["blast"] = api.blast_radius(ctx, root)
    out["action"] = api.execute_action(ctx, "open_ticket", root, {})
    return out


@pytest.mark.parametrize("sid", SIDS)
def test_no_tool_returns_data_after_as_of(sid):
    for as_of in as_of_grid(sid):
        for name, res in call_everything(sid, as_of, []).items():
            assert_fenced(res, as_of, f"{sid}@{as_of} {name}")


@pytest.mark.parametrize("sid", ["S01", "S10", "S12"])
def test_fence_holds_with_applied_actions_too(sid):
    t = data.get_truth(sid)["remediation"]["preferred"]
    a, tgt = t.split(":")
    default = data.default_as_of(sid)
    rec = api.execute_action({"scenario_id": sid, "as_of": default}, a, tgt, {})
    for as_of in as_of_grid(sid):
        for name, res in call_everything(sid, as_of, [rec]).items():
            assert_fenced(res, as_of, f"{sid}@{as_of} {name}")


def test_the_fence_is_not_vacuous_future_data_exists():
    sid = "S10"
    early, late = "2026-11-05T10:30:00Z", "2026-11-05T11:29:00Z"
    n_early = api.search_logs({"scenario_id": sid, "as_of": early}, "pricing-service", "", None, 135, 200)[
        "total_matches"
    ]
    n_late = api.search_logs({"scenario_id": sid, "as_of": late}, "pricing-service", "", None, 135, 200)[
        "total_matches"
    ]
    assert n_late > n_early
    assert (
        api.query_metric({"scenario_id": sid, "as_of": late}, "pricing-service", "p95_ms", 135)["n"]
        > api.query_metric({"scenario_id": sid, "as_of": early}, "pricing-service", "p95_ms", 135)["n"]
    )
    assert len(api.list_changes({"scenario_id": sid, "as_of": late})["changes"]) > len(
        api.list_changes({"scenario_id": sid, "as_of": "2026-11-05T09:40:00Z"})["changes"]
    )


def test_logs_match_a_direct_sql_cut_at_as_of():
    sid, as_of = "S12", "2026-11-06T11:46:00Z"
    with store.connect_ro(access.db_path(sid)) as c:
        n = c.execute(
            "SELECT COUNT(*) FROM logs WHERE scenario_id=? AND service=? AND ts<=? AND ts>?",
            (sid, "checkout-service", to_epoch(as_of), to_epoch(as_of) - 30 * 60),
        ).fetchone()[0]
        newest = c.execute(
            "SELECT MAX(ts) FROM logs WHERE scenario_id=? AND service=? AND ts<=?",
            (sid, "checkout-service", to_epoch(as_of)),
        ).fetchone()[0]
    res = api.search_logs({"scenario_id": sid, "as_of": as_of}, "checkout-service", "", None, 30, 200)
    assert res["total_matches"] == n and res["lines"][-1]["ts"] == to_iso(newest)


def test_editing_as_of_backwards_hides_the_incident():
    """Recipe F4: fork a thread, edit ``as_of`` to before the onset -> the tools show a healthy world."""
    sid = "S10"
    ctx = {"scenario_id": sid, "as_of": "2026-11-05T10:30:00Z"}
    q = api.query_metric(ctx, "pricing-service", "p95_ms", 60)
    assert q["trend"] in ("flat", "insufficient") and q["max_z"] < 6 and q["onset_estimate"] is None
    assert api.list_alerts(ctx, window_minutes=135)["count"] <= 1  # only the benign probe flap, if any
    assert all(c["component"] != "pricing-service" for c in api.list_changes(ctx)["changes"])
    before = {"scenario_id": sid, "as_of": "2026-11-05T08:00:00Z"}  # before the window even starts
    assert api.query_metric(before, "pricing-service", "p95_ms", 30)["n"] == 0
    assert api.search_logs(before, "pricing-service")["total_matches"] == 0


def test_alert_resolution_is_not_leaked_before_it_happens():
    sid = "S04"
    full = api.list_alerts({"scenario_id": sid, "as_of": "2026-11-02T04:30:00Z"}, window_minutes=135)["alerts"]
    resolved = [a for a in full if a["resolved_at"]]
    assert resolved
    cut = to_iso(to_epoch(resolved[0]["resolved_at"]) - 60)
    at = api.list_alerts({"scenario_id": sid, "as_of": cut}, window_minutes=135)["alerts"]
    mine = next(a for a in at if a["alert_id"] == resolved[0]["alert_id"])
    assert mine["resolved_at"] is None
    assert paths.generated_dir() != paths.repo_root() / "var" / "generated"  # tests never touch the repo's var/
