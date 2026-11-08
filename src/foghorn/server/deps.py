"""Shared FastAPI plumbing: error shape, auth dependencies, capped body reads, rate limiting, runtime access.

Auth model (ARCHITECTURE.md 4.2): custom routes are NOT covered by the Agent Server's ``@auth.authenticate`` (we do not
set ``http.enable_custom_route_auth``), so every ``/api`` route authenticates itself. With no
``FOGHORN_DASHBOARD_API_TOKEN`` configured everything is open (dev default).
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections import defaultdict, deque
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from foghorn.events.dispatcher import DispatchError
from foghorn.events.service import EventsRuntime
from foghorn.logging import get_logger
from foghorn.settings import get_settings

log = get_logger("web")


class ApiError(Exception):
    def __init__(
        self, status: int, code: str, message: str, details: Any = None, headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.details, self.headers = status, code, message, details, headers or {}


def error_body(code: str, message: str, details: Any = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details}}


def error_response(
    status: int, code: str, message: str, details: Any = None, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(error_body(code, message, details), status_code=status, headers=headers)


async def api_error_handler(_: Request, exc: Exception) -> JSONResponse:
    if isinstance(exc, ApiError):
        return error_response(exc.status, exc.code, exc.message, exc.details, exc.headers)
    if isinstance(exc, DispatchError):
        return error_response(exc.status, exc.code, exc.message, exc.details)
    if isinstance(exc, HTTPException):
        code = {
            400: "bad_request",
            401: "unauthorized",
            404: "not_found",
            405: "method_not_allowed",
            413: "payload_too_large",
            422: "schema_invalid",
            429: "rate_limited",
        }.get(exc.status_code, "error")
        return error_response(exc.status_code, code, str(exc.detail), None, dict(exc.headers or {}))
    log.error("unhandled_api_error", error=repr(exc))
    return error_response(500, "internal_error", "internal server error")


async def validation_error_handler(_: Request, exc: Exception) -> JSONResponse:
    errors: list[dict[str, Any]] = getattr(exc, "errors", lambda: [])()
    details = [
        {"loc": ".".join(str(x) for x in e.get("loc", ()) if x != "body"), "msg": str(e.get("msg"))} for e in errors
    ]
    msg = "; ".join(f"{d['loc']}: {d['msg']}" if d["loc"] else d["msg"] for d in details) or "invalid request"
    return error_response(422, "schema_invalid", msg[:500], {"errors": details})


def get_rt(request: Request) -> EventsRuntime:
    rt: EventsRuntime | None = getattr(request.app.state, "rt", None)
    if rt is None or not rt.started:
        raise ApiError(503, "not_ready", "events runtime is not started yet")
    return rt


def _bearer(request: Request) -> str | None:
    scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
    return supplied if scheme.lower() == "bearer" and supplied else None


def bearer_ok(request: Request) -> bool:
    token = get_settings().dashboard_api_token
    if token is None:
        return True
    supplied = _bearer(request)
    return supplied is not None and hmac.compare_digest(supplied, token.get_secret_value())


async def require_auth(request: Request) -> None:
    """Dependency for every ``/api`` route (except the webhook routes, which use :func:`authorize_ingest`)."""
    if get_settings().dashboard_api_token is None or bearer_ok(request):
        return
    if request.url.path == "/api/stream":  # EventSource cannot set headers
        token = get_settings().dashboard_api_token
        q = request.query_params.get("access_token", "")
        if token is not None and q and hmac.compare_digest(q, token.get_secret_value()):
            return
    raise ApiError(401, "unauthorized", "missing or invalid bearer token", headers={"WWW-Authenticate": "Bearer"})


class RateLimiter:
    """Sliding-window per-key limiter (single process; the webhook limit in settings is per client per minute)."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def limited(self, key: str, limit: int, window_s: float = 60.0) -> bool:
        now = time.monotonic()
        q = self._hits[key]
        while q and now - q[0] > window_s:
            q.popleft()
        if len(q) >= limit:
            return True
        q.append(now)
        return False

    def reset(self) -> None:
        self._hits.clear()


async def read_capped(request: Request, cap: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > cap:
        raise ApiError(413, "payload_too_large", f"body larger than {cap} bytes")
    body = bytearray()
    async for chunk in request.stream():  # chunked uploads have no content-length: cap while reading
        body.extend(chunk)
        if len(body) > cap:
            raise ApiError(413, "payload_too_large", f"body larger than {cap} bytes")
    return bytes(body)


def sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def authorize_ingest(request: Request, rt: EventsRuntime) -> bytes:
    """Read the (size-capped) body of an ingest request and apply the auth + rate-limit matrix.

    * a signature header that is present but wrong -> 401 ``bad_signature`` (always)
    * valid signature OR valid bearer -> allowed
    * neither: allowed only when no dashboard token is configured AND signatures are not required
    """
    s = get_settings()
    body = await read_capped(request, s.webhook_max_body_bytes)
    sig = request.headers.get("x-foghorn-signature")
    sig_ok = False
    if sig is not None:
        sig_ok = hmac.compare_digest(sign(body, s.webhook_secret.get_secret_value()), sig)
        if not sig_ok:
            log.warning("webhook_bad_signature", client=request.client.host if request.client else None)
            raise ApiError(401, "bad_signature", "invalid X-Foghorn-Signature")
    if not (sig_ok or (s.dashboard_api_token is not None and bearer_ok(request))) and (
        rt.es.webhook_require_signature or s.dashboard_api_token is not None
    ):
        raise ApiError(
            401, "unauthorized", "a valid signature or bearer token is required", headers={"WWW-Authenticate": "Bearer"}
        )
    limiter: RateLimiter = request.app.state.limiter
    if limiter.limited(request.client.host if request.client else "?", s.webhook_rate_per_min):
        raise ApiError(429, "rate_limited", "too many requests", {"retry_after": 30}, headers={"Retry-After": "30"})
    return body
