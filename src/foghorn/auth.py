"""Auth for the Agent Server routes (/threads, /runs, /assistants, /store).

* Studio / SDK send `Authorization: Bearer <token>` (SDK: headers=...). Studio requests carry
  `x-auth-scheme: langsmith`; unless auth.disable_studio_auth=true the server routes them past this handler.
* @auth.on.* handlers stamp ownership metadata and return a filter => per-user isolation of threads.
"""

from __future__ import annotations

import hmac
from typing import Any

from langgraph_sdk import Auth

from foghorn.settings import get_settings

auth = Auth()


@auth.authenticate
async def authenticate(authorization: str | None) -> Auth.types.MinimalUserDict:
    token = get_settings().dashboard_api_token
    if token is None:  # dev convenience; settings validator should forbid in prod
        return {"identity": "dev-user", "is_authenticated": True}
    scheme, _, supplied = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(supplied, token.get_secret_value()):
        raise Auth.exceptions.HTTPException(status_code=401, detail="invalid or missing bearer token")
    return {"identity": "dashboard", "is_authenticated": True}


@auth.on
async def owner_only(ctx: Auth.types.AuthContext, value: dict[str, Any]) -> dict[str, Any]:
    owner = ctx.user.identity
    md = value.setdefault("metadata", {})
    md["owner"] = owner
    return {"owner": owner}
