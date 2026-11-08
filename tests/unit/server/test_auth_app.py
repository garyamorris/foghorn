"""Auth matrix, app wiring (redirect, static mount, route precedence), CORS/PNA preflight, error shapes, lifespan tolerance."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import httpx
import pytest

from foghorn.server.app import create_app
from foghorn.settings import get_settings
from tests.unit.events.fakes import make_runtime

API_ROUTES = [
    ("GET", "/api/health"),
    ("GET", "/api/config"),
    ("GET", "/api/summary"),
    ("GET", "/api/clock"),
    ("GET", "/api/queues"),
    ("GET", "/api/events"),
    ("GET", "/api/events/presets"),
    ("GET", "/api/events/evt_1"),
    ("GET", "/api/trace/evt_1"),
    ("GET", "/api/links/event/evt_1"),
    ("GET", "/api/dispatches"),
    ("GET", "/api/dlq"),
    ("GET", "/api/replay"),
    ("GET", "/api/streams"),
    ("GET", "/api/scenarios"),
    ("GET", "/api/scenarios/S01"),
    ("GET", "/api/faults"),
    ("GET", "/api/services"),
    ("GET", "/api/incidents"),
    ("GET", "/api/incidents/INC-1"),
    ("GET", "/api/approvals"),
    ("GET", "/api/approvals/apr_1"),
    ("GET", "/api/evals"),
    ("GET", "/api/evals/evr_1"),
    ("GET", "/api/memory/search"),
    ("GET", "/api/memory/namespaces"),
    ("GET", "/api/datasets"),
    ("GET", "/api/graphs"),
    ("GET", "/api/assistants"),
    ("GET", "/api/studio/guide"),
    ("GET", "/api/stream"),
    ("POST", "/api/clock"),
    ("POST", "/api/events/inject"),
    ("POST", "/api/replay"),
    ("POST", "/api/replay/pause"),
    ("POST", "/api/scenarios/S01/launch"),
    ("POST", "/api/faults"),
    ("POST", "/api/approvals/apr_1/resume"),
    ("POST", "/api/approvals/resume-batch"),
    ("POST", "/api/evals"),
    ("POST", "/api/dlq/dlq_1/retry"),
    ("POST", "/api/dlq/dlq_1/discard"),
    ("POST", "/api/admin/reset"),
    ("POST", "/api/admin/ambient"),
]


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv("FOGHORN_DASHBOARD_API_TOKEN", "s3cret")
    get_settings.cache_clear()
    return "s3cret"


@pytest.mark.parametrize(("method", "path"), API_ROUTES)
async def test_every_api_route_requires_the_bearer_token_when_one_is_configured(api, token, method, path):
    r = await api.http.request(method, path)
    assert r.status_code == 401, (method, path, r.status_code)
    assert r.json()["error"]["code"] == "unauthorized" and r.headers["www-authenticate"] == "Bearer"
    bad = await api.http.request(method, path, headers={"Authorization": "Bearer wrong"})
    assert bad.status_code == 401
    scheme = await api.http.request(method, path, headers={"Authorization": "Basic s3cret"})
    assert scheme.status_code == 401


async def test_valid_bearer_is_accepted_and_dev_default_is_open(api, monkeypatch):
    assert (await api.get("/api/health")).status_code == 200  # no token configured -> open
    monkeypatch.setenv("FOGHORN_DASHBOARD_API_TOKEN", "s3cret")
    get_settings.cache_clear()
    assert (await api.get("/api/health", headers={"Authorization": "Bearer s3cret"})).status_code == 200
    assert (
        await api.get("/api/health", headers={"authorization": "bearer s3cret"})
    ).status_code == 200  # scheme is case-insensitive
    for h in ({"Authorization": "Bearer "}, {"Authorization": "s3cret"}, {}):
        assert (await api.get("/api/health", headers=h)).status_code == 401


async def test_public_routes_stay_public_with_a_token(api, token):
    assert (await api.get("/healthz")).status_code == 200
    r = await api.http.get("/", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/app/"
    assert (await api.get("/app/")).status_code in {
        200,
        404,
    }  # static assets are public (the SPA itself holds the token)


async def test_sse_accepts_an_access_token_query_param_for_eventsource(api, token):
    assert (await api.get("/api/stream?access_token=wrong")).status_code == 401
    assert (
        await api.get("/api/events?access_token=s3cret")
    ).status_code == 401  # the query-param escape hatch is for /api/stream only


async def test_webhooks_accept_hmac_instead_of_a_bearer_token_when_a_token_is_configured(api, token):
    from foghorn.server.deps import sign

    body = b'{"type":"customer.report","payload":{}}'
    secret = api.rt.settings.webhook_secret.get_secret_value()
    assert (await api.http.post("/api/events", content=body)).status_code == 401
    assert (
        await api.http.post("/api/events", content=body, headers={"X-Foghorn-Signature": sign(body, secret)})
    ).status_code == 202
    assert (
        await api.http.post("/api/events", content=body, headers={"Authorization": "Bearer s3cret"})
    ).status_code == 202


async def test_error_shapes_never_use_fastapi_detail(api):
    for r in (
        await api.get("/api/events/evt_nope"),
        await api.get("/api/nope"),
        await api.http.put("/api/events"),
        await api.post("/api/clock", json={"speed": "fast"}),
        await api.post("/api/admin/reset", json={}),
    ):
        body = r.json()
        assert "detail" not in body and set(body["error"]) == {"code", "message", "details"}, (r.url, body)
    assert (await api.get("/api/nope")).status_code == 404 and (await api.http.put("/api/events")).status_code == 405


async def test_root_redirect_static_mount_and_no_shadowed_builtin_routes(api):
    r = await api.http.get("/", follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (307, "/app/")
    paths = set()
    for route in api.app.routes:
        paths.add(getattr(route, "path", ""))
    sub = [getattr(x, "path", "") for rt in api.app.routes for x in getattr(rt, "routes", [])] + list(paths)
    for forbidden in (
        "/info",
        "/ok",
        "/docs",
        "/openapi.json",
        "/metrics",
        "/assistants",
        "/threads",
        "/runs",
        "/store",
    ):
        assert forbidden not in paths and forbidden not in sub, forbidden
    mounts = [route for route in api.app.routes if type(route).__name__ == "Mount"]
    assert [m.path for m in mounts] == ["/app"]  # mounted under /app only, never at "/"


async def test_cors_private_network_preflight_header_is_stripped_before_the_servers_cors_layer(tmp_path):
    """Starlette 1.x answers a PNA preflight with 400 before langgraph-api's own PNA middleware can approve it. The
    middleware removes the request header from OPTIONS only, so the server's layer sees an ordinary preflight."""
    from foghorn.server.app import StripPrivateNetworkRequest

    seen: list[list[tuple[bytes, bytes]]] = []

    async def inner(scope, receive, send):
        seen.append(scope["headers"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    mw = StripPrivateNetworkRequest(inner)
    hdrs = [
        (b"origin", b"https://smith.langchain.com"),
        (b"access-control-request-private-network", b"true"),
        (b"access-control-request-method", b"POST"),
    ]

    async def run(method):
        sent = []

        async def send(m):
            sent.append(m)

        await mw({"type": "http", "method": method, "headers": list(hdrs)}, None, send)
        return sent

    await run("OPTIONS")
    await run("POST")
    assert all(k != b"access-control-request-private-network" for k, _ in seen[0])  # stripped on the preflight
    assert any(k == b"access-control-request-private-network" for k, _ in seen[1])  # untouched elsewhere
    assert (b"access-control-request-method", b"POST") in seen[0]  # the rest of the preflight is intact


# ------------------------------------------------------------------------------------------------------ lifespan
async def test_lifespan_boots_without_a_database_and_serves_503_instead_of_crashing(tmp_path):
    def broken():
        raise RuntimeError("cannot open state db")

    app = create_app(runtime_factory=broken, start_background=False)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c,
    ):
        h = (await c.get("/healthz")).json()
        assert h["status"] == "degraded" and "cannot open state db" in h["error"]
        r = await c.get("/api/events")
        assert r.status_code == 503 and r.json()["error"]["code"] == "not_ready"
        assert (await c.get("/", follow_redirects=False)).status_code == 307


async def test_lifespan_with_background_tasks_survives_seeding_failure(tmp_path):
    rt, client, data, wall = make_runtime(tmp_path, start_background=True)
    client.fail_assistants = client.fail_store = True
    data.ok = False
    app = create_app(runtime_factory=lambda: rt)
    async with app.router.lifespan_context(app):
        for _ in range(200):
            if rt.seed_state != "pending":
                break
            await asyncio.sleep(0.01)
        assert rt.seed_state == "done"  # failures were reported, not raised
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            assert (await c.get("/api/health")).json()["components"]["seeding"] == "done"
            assert (await c.get("/api/scenarios")).status_code == 503  # data layer not ready -> graceful, app still up
    assert not rt.started


async def test_lifespan_when_the_agent_server_never_comes_up(tmp_path):
    rt, client, data, wall = make_runtime(tmp_path, start_background=True)
    client.fail_search = True
    app = create_app(runtime_factory=lambda: rt)
    async with app.router.lifespan_context(app):
        for _ in range(200):
            if rt.agent_state != "waiting":
                break
            await asyncio.sleep(0.01)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            h = (await c.get("/api/health")).json()
            assert h["status"] == "degraded" and h["ready"] is False
            assert (await c.get("/api/events")).status_code == 200  # ingest/read keep working without the Agent Server


# ----------------------------------------------------------------------------------------- no blocking IO on the loop
def _async_function_bodies(path: Path):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            yield node


def _loop_nodes(fn: ast.AsyncFunctionDef):
    """Nodes that execute ON the event loop: everything in the async def except bodies of nested sync defs/lambdas
    (those are handed to `asyncio.to_thread` / `db.run`)."""
    stack: list[ast.AST] = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.Lambda)):
            continue
        stack.extend(ast.iter_child_nodes(node))


def test_no_blocking_calls_inside_async_defs():
    """Lint-style guard (docs/design/02 gotcha 4): `time.sleep`, sqlite3 and sync HTTP raise BlockingError under langgraph dev."""
    root = Path(__file__).resolve().parents[3] / "src" / "foghorn"
    bad: list[str] = []
    for pkg in ("events", "server"):
        for py in (root / pkg).rglob("*.py"):
            if "static" in py.parts:
                continue
            for fn in _async_function_bodies(py):
                for node in _loop_nodes(fn):
                    if not isinstance(node, ast.Call):
                        continue
                    f = node.func
                    name = (
                        f"{getattr(f.value, 'id', '')}.{f.attr}"
                        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
                        else getattr(f, "id", "")
                    )
                    if name in {
                        "time.sleep",
                        "sqlite3.connect",
                        "requests.get",
                        "requests.post",
                        "urllib.request.urlopen",
                        "os.scandir",
                        "subprocess.run",
                    }:
                        bad.append(f"{py.name}:{node.lineno} {fn.name} calls {name}")
                    if (
                        isinstance(f, ast.Attribute)
                        and f.attr in {"execute", "executescript", "fetchall", "fetchone"}
                        and "db" not in py.name
                    ):
                        bad.append(f"{py.name}:{node.lineno} {fn.name} runs raw SQL `{f.attr}` on the loop")
    assert not bad, bad


def test_state_db_is_only_touched_through_to_thread_helpers():
    """Async code must reach the sync StateDB methods only via `db.run(...)` / `asyncio.to_thread`."""
    root = Path(__file__).resolve().parents[3] / "src" / "foghorn"
    offenders: list[str] = []
    for pkg in ("events", "server"):
        for py in (root / pkg).rglob("*.py"):
            if py.name == "db.py" or "static" in py.parts:
                continue
            for fn in _async_function_bodies(py):
                for node in _loop_nodes(fn):
                    # a direct call like `self.db.get_event(...)` / `rt.db.get_event(...)` (not passed as an argument)
                    if (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Attribute)
                        and node.func.value.attr == "db"
                        and node.func.attr not in {"run"}
                    ):
                        offenders.append(f"{py.name}:{node.lineno} {fn.name} -> db.{node.func.attr}()")
    assert not offenders, offenders
