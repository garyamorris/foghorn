"""Graph catalog + Studio guide aggregation (tolerant of missing / malformed GUIDE.md files)."""

from __future__ import annotations

import pytest

from foghorn.contracts.meta import GRAPH_IDS
from foghorn.server import catalog

GUIDE = """---
graph_id: incident_commander
title: Incident commander guide
summary: Walk the supervisor.
features: [interrupt, Command.goto]
studio_steps:
  - Open the graph selector and pick incident_commander
  - Press Submit
sample_inputs:
  - name: S10
    input: {scenario_id: S10}
  - not-a-sample
---
# Details

Body text with a --- horizontal rule inside.

---

after
"""


def test_parse_guide_splits_front_matter_and_body():
    meta, body = catalog.parse_guide(GUIDE)
    assert meta["graph_id"] == "incident_commander" and meta["studio_steps"][0].startswith("Open the graph")
    assert body.startswith("# Details") and "after" in body
    for bad in ("no front matter", "---\nunterminated: yes\n", "---\n- a list\n---\nbody", "---\n: : :\n---\n"):
        with pytest.raises((ValueError, Exception)):
            catalog.parse_guide(bad)


def test_guide_entry_falls_back_to_the_catalog_when_files_are_missing_or_broken(tmp_path):
    entry, warn = catalog.guide_entry("rca_tournament", tmp_path)
    assert (
        warn is None
        and entry["has_guide"] is False
        and entry["title"] == "RCA tournament"
        and "Send map-reduce" in entry["features"]
        and entry["studio_steps"] == []
    )
    (tmp_path / "rca_tournament").mkdir()
    (tmp_path / "rca_tournament" / "GUIDE.md").write_text("not yaml front matter at all")
    entry, warn = catalog.guide_entry("rca_tournament", tmp_path)
    assert entry["has_guide"] is False and "front matter" in warn and warn.startswith("flows/rca_tournament/GUIDE.md")


def test_build_guide_covers_every_graph_and_reports_warnings(tmp_path):
    (tmp_path / "incident_commander").mkdir()
    (tmp_path / "incident_commander" / "GUIDE.md").write_text(GUIDE)
    (tmp_path / "storm_correlator").mkdir()
    (tmp_path / "storm_correlator" / "GUIDE.md").write_text("---\n[broken\n---\n")
    entries, warnings = catalog.build_guide(tmp_path)
    assert [e["graph_id"] for e in entries] == list(GRAPH_IDS)
    ic = next(e for e in entries if e["graph_id"] == "incident_commander")
    assert (
        ic["has_guide"]
        and ic["title"] == "Incident commander guide"
        and ic["features"] == ["interrupt", "Command.goto"]
    )
    assert ic["sample_inputs"] == [{"name": "S10", "input": {"scenario_id": "S10"}}]  # the malformed sample was dropped
    assert len(warnings) == 1 and "storm_correlator" in warnings[0]


async def test_studio_guide_endpoint_merges_real_files_with_the_catalog(api, tmp_path, monkeypatch):
    flows = tmp_path / "flows"
    (flows / "incident_commander").mkdir(parents=True)
    (flows / "incident_commander" / "GUIDE.md").write_text(GUIDE)
    import foghorn.server.routes_graphs as rg

    real = catalog.build_guide
    monkeypatch.setattr(rg, "build_guide", lambda: real(flows))
    g = (await api.get("/api/studio/guide")).json()
    assert g["studio"]["base_url"].startswith("https://smith.langchain.com/studio/") and g["setup"]["steps"][
        0
    ].startswith("Run `make dev`")
    assert [x["graph_id"] for x in g["graphs"]] == list(GRAPH_IDS)
    ic = next(x for x in g["graphs"] if x["graph_id"] == "incident_commander")
    assert ic["has_guide"] is True and ic["studio_steps"][1] == "Press Submit" and ic["body"].startswith("# Details")
    assert next(x for x in g["graphs"] if x["graph_id"] == "event_router")["has_guide"] is False and g["warnings"] == []


async def test_graph_catalog_lists_assistants_per_graph(api):
    await api.rt._bootstrap()  # noqa: SLF001 - seeds the named assistants into the fake server
    g = (await api.get("/api/graphs")).json()
    assert g["assistants_scan_ok"] is True and [i["graph_id"] for i in g["items"]] == list(GRAPH_IDS)
    ic = next(i for i in g["items"] if i["graph_id"] == "incident_commander")
    assert (
        ic["title"] == "Incident Commander"
        and any("Command" in f for f in ic["features"])
        and len(ic["assistants"]) == 8  # the system assistant + 7 named ones
        and sum(a["seeded"] for a in ic["assistants"]) == 7
    )
    assert ic["studio"]["base_url"].startswith("https://smith.langchain.com/studio/") and "role" in ic
    a = (await api.get("/api/assistants")).json()
    assert (
        a["scan_ok"]
        and len(a["items"]) >= 8
        and {"assistant_id", "graph_id", "name", "context", "seeded"} <= set(a["items"][0])
    )
    await api.rt.dispatcher.stop()
    await api.rt.scheduler.stop()
    api.client.fail_search = True
    degraded = (await api.get("/api/graphs")).json()
    assert degraded["assistants_scan_ok"] is False and all(i["assistants"] == [] for i in degraded["items"])
    assert (await api.get("/api/assistants")).json() == {"items": [], "scan_ok": False}


async def test_the_real_flows_directory_is_tolerated_whatever_the_graph_owners_have_written_so_far():
    entries, warnings = catalog.build_guide()
    assert len(entries) == len(GRAPH_IDS) and all(e["title"] for e in entries)
    assert isinstance(warnings, list)
