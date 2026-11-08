"""Custom FastAPI app mounted by `langgraph dev` / the Agent Server via langgraph.json `http.app`.

Rules proven by probes (docs/design/01):
1. async only - blockbuster flags blocking calls on the server loop (SQLite goes through ``asyncio.to_thread``);
2. custom routes are NOT covered by @auth.authenticate (we do not enable ``http.enable_custom_route_auth``), so every
   ``/api`` route authenticates itself (``FOGHORN_DASHBOARD_API_TOKEN`` -> bearer; optional HMAC on webhooks);
3. ``get_client(url=None)`` talks to the same server in-process (used by the dispatcher, approvals and seeding);
4. route precedence: ``/ok /metrics /docs /openapi.json`` always belong to the server; our routes win over the protected
   built-ins; never ``mount("/")`` (it swallows ``/assistants`` ``/threads`` ...) and never define ``/info``.
5. CORS: the server's default policy is kept. The ONE gap is Chrome's Private-Network-Access preflight
   (``Access-Control-Request-Private-Network: true``), which starlette 1.x answers with ``400 Disallowed CORS
   private-network`` before langgraph-api's own PNA middleware can approve it (reproduced live, see docs/API.md and the
   server tests). :class:`StripPrivateNetworkRequest` removes that request header from OPTIONS preflights so the
   server's middleware answers ``Access-Control-Allow-Private-Network: true``. ``http.cors`` must stay unset.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Receive, Scope, Send

from foghorn.events.config import get_events_settings
from foghorn.events.dispatcher import DispatchError
from foghorn.events.service import EventsRuntime
from foghorn.logging import configure_logging, get_logger
from foghorn.server import routes_events, routes_graphs, routes_ops, routes_scenarios, routes_work
from foghorn.server.deps import ApiError, RateLimiter, api_error_handler, validation_error_handler
from foghorn.settings import get_settings

configure_logging()
log = get_logger("web")
_STATIC_DIR = Path(__file__).parent / "static"

RuntimeFactory = Callable[[], EventsRuntime]


class StripPrivateNetworkRequest:
    """Pure-ASGI middleware: drop ``Access-Control-Request-Private-Network`` from OPTIONS so preflights are not 400'd."""

    _HEADER = b"access-control-request-private-network"

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("method") == "OPTIONS":
            headers = [(k, v) for k, v in scope["headers"] if k.lower() != self._HEADER]
            if len(headers) != len(scope["headers"]):
                scope = {**scope, "headers": headers}
        await self.app(scope, receive, send)


def default_runtime() -> EventsRuntime:
    return EventsRuntime(settings=get_settings(), es=get_events_settings())


def create_app(*, runtime_factory: RuntimeFactory | None = None, start_background: bool | None = None) -> FastAPI:
    """Build the app. Tests pass ``runtime_factory`` (fakes, tmp DB) and ``start_background=False``."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.rt = None
        app.state.startup_error = None
        rt: EventsRuntime | None = None
        try:
            rt = (runtime_factory or default_runtime)()
            await rt.start(background=start_background)
            app.state.rt = rt
        except Exception as exc:  # the Agent Server must still boot; /api answers 503 and /healthz says why
            app.state.startup_error = repr(exc)
            log.error("events_runtime_start_failed", error=repr(exc))
        try:
            yield
        finally:
            if rt is not None and rt.started:
                try:
                    await rt.stop()
                except Exception as exc:
                    log.warning("events_runtime_stop_failed", error=repr(exc))
            app.state.rt = None

    app = FastAPI(title="foghorn custom routes", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.rt = None
    app.state.startup_error = None
    app.state.limiter = RateLimiter()
    app.add_middleware(StripPrivateNetworkRequest)
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(DispatchError, api_error_handler)
    app.add_exception_handler(StarletteHTTPException, api_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(Exception, api_error_handler)

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/app/", status_code=307)

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        out: dict[str, Any] = {"status": "ok", "provider": get_settings().resolved_provider()}
        if app.state.startup_error:
            out.update(status="degraded", error=app.state.startup_error)
        return out

    app.include_router(routes_ops.router)
    app.include_router(routes_events.router)
    app.include_router(routes_events.ingest_router)
    app.include_router(routes_scenarios.router)
    app.include_router(routes_work.router)
    app.include_router(routes_graphs.router)

    # StaticFiles does its file IO in a worker thread (anyio), so it is safe under blockbuster, unlike open() in async def.
    # Mount under /app, NOT /ui (/ui belongs to LangGraph's generative-UI routes) and NEVER at "/".
    app.mount("/app", StaticFiles(directory=_STATIC_DIR, html=True, check_dir=False), name="dashboard")
    return app


app = create_app()
