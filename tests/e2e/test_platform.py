"""Platform e2e: a REAL `langgraph dev` (what Studio talks to) with the custom app, offline, loopback only.

These assert the things that break Studio or the dashboard silently: a graph that fails to load, a schema Studio
cannot render, a custom route that shadows a built-in one, MCP missing a graph, the live event feed not delivering.
Flow-level behaviour (commander, router, ...) lives in test_flows.py.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from langgraph_sdk import get_client

from foghorn.contracts.meta import GRAPH_IDS

pytestmark = [pytest.mark.asyncio, pytest.mark.e2e]


@pytest.fixture
def client(dev_server):
    return get_client(url=dev_server)


async def test_all_ten_graphs_load_and_studio_endpoints_render(dev_server, client):
    assistants = await client.assistants.search(limit=100)
    system = {a["graph_id"]: a for a in assistants if (a.get("metadata") or {}).get("created_by") == "system"}
    assert set(GRAPH_IDS) <= set(system), f"graphs missing from the server: {set(GRAPH_IDS) - set(system)}"
    async with httpx.AsyncClient(base_url=dev_server, timeout=30) as h:
        for gid in GRAPH_IDS:
            aid = system[gid]["assistant_id"]
            schemas = await h.get(f"/assistants/{aid}/schemas")
            assert schemas.status_code == 200, (gid, schemas.text[:300])
            graph = await h.get(f"/assistants/{aid}/graph", params={"xray": "true"})
            assert graph.status_code == 200 and graph.json()["nodes"], gid


async def test_builtin_routes_are_not_shadowed_by_the_custom_app(dev_server):
    async with httpx.AsyncClient(base_url=dev_server, timeout=30) as h:
        info = (await h.get("/info")).json()
        assert "version" in info and info["flags"]["assistants"] is True  # Studio reads these flags
        assert (await h.get("/ok")).status_code == 200
        assert (await h.post("/assistants/search", json={"limit": 1})).status_code == 200  # POST not swallowed
        assert (await h.post("/threads/search", json={"limit": 1})).status_code == 200
        health = (await h.get("/healthz")).json()
        assert health["provider"] == "offline" and health["status"] == "ok"


async def test_dashboard_is_served_under_app_with_assets(dev_server):
    async with httpx.AsyncClient(base_url=dev_server, timeout=30, follow_redirects=True) as h:
        root = await h.get("/")
        assert root.status_code == 200 and "text/html" in root.headers["content-type"]
        page = await h.get("/app/")
        assert page.status_code == 200 and "<script" in page.text
        for asset in ("/app/js/main.js", "/app/js/api.js", "/app/css/tokens.css"):
            r = await h.get(asset)
            assert r.status_code == 200, asset


async def test_mcp_exposes_every_graph_as_a_tool(dev_server):
    async with httpx.AsyncClient(base_url=dev_server, timeout=30) as h:
        r = await h.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={"accept": "application/json, text/event-stream"},
        )
        assert r.status_code == 200, r.text[:300]
        names = {t["name"] for t in r.json()["result"]["tools"]}
        assert len(names) >= len(GRAPH_IDS)


async def test_named_assistants_and_memories_are_seeded_by_the_lifespan(dev_server, client):
    """The custom app seeds named assistants (profiles/policies) and Store memories after the server is ready."""
    for _ in range(40):
        named = [
            a
            for a in await client.assistants.search(limit=100)
            if (a.get("metadata") or {}).get("created_by") != "system"
        ]
        if len(named) >= 4:
            break
        await asyncio.sleep(0.5)
    assert len(named) >= 4, "lifespan seeding did not create the named assistants"
    lessons = await client.store.search_items(["lessons"], query="roll back the component that changed", limit=3)
    assert lessons["items"], "seed memories missing"


async def test_event_ingest_dedupes_and_the_live_feed_delivers(dev_server):
    envelope = {
        "type": "alert.fired",
        "source": "alertmanager",
        "severity": "high",
        "subject": {"component": "checkout-service"},
        "payload": {"rule": "error_rate_high", "value": 0.09},
        "idempotency_key": "e2e-platform-1",
        "attributes": {"ambient": True},
    }
    async with httpx.AsyncClient(base_url=dev_server, timeout=30) as h:
        first = await h.post("/api/events", json=envelope)
        assert first.status_code == 202, first.text
        event_id = first.json()["event_id"]
        again = await h.post("/api/events", json=envelope)
        assert again.status_code == 200 and again.json()["status"] == "duplicate"
        got = await h.get(f"/api/events/{event_id}")
        assert got.status_code == 200 and got.json()["type"] == "alert.fired"

        seen: list[str] = []
        async with h.stream("GET", "/api/stream", headers={"Last-Event-ID": "0"}, timeout=20) as stream:
            async for line in stream.aiter_lines():
                if line.startswith("data:") and event_id in line:
                    seen.append(json.loads(line[5:].strip()).get("id", ""))
                    break
        assert event_id in seen


async def test_semantic_store_search_with_offline_embeddings(client):
    ns = ["memories", "e2e-sem"]
    await client.store.put_item(ns, key="a", value={"text": "customer prefers refunds by bank transfer"})
    await client.store.put_item(ns, key="b", value={"text": "customer is allergic to peanuts"})
    res = await client.store.search_items(ns, query="how does the customer like refunds paid", limit=2)
    assert res["items"][0]["key"] == "a" and res["items"][0]["score"] > res["items"][-1]["score"]


@pytest.mark.parametrize("path", ["/info", "/assistants/search", "/threads/search", "/api/health", "/api/events"])
async def test_studio_cors_preflight_including_private_network_access(dev_server, path):
    """Studio is a public https page calling YOUR loopback server: Chrome sends a preflight carrying
    `Access-Control-Request-Private-Network`. If the server does not answer it with 200 + allow-private-network, Studio
    shows "Failed to fetch". Verified by hand against the real Studio page (the embedded browser then failed for a
    different, client-side reason: ERR_BLOCKED_BY_CLIENT)."""
    async with httpx.AsyncClient(base_url=dev_server, timeout=30) as h:
        r = await h.options(
            path,
            headers={
                "Origin": "https://smith.langchain.com",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,x-api-key",
                "Access-Control-Request-Private-Network": "true",
            },
        )
    assert r.status_code == 200, f"{path}: {r.status_code} {r.text[:200]}"
    assert r.headers.get("access-control-allow-private-network") == "true"
    assert r.headers.get("access-control-allow-origin") in {"*", "https://smith.langchain.com"}
