"""API key auth (PVT-style Bearer / X-Integration-Key)."""
from __future__ import annotations

from typing import Annotated

from fastapi import Header, HTTPException, status

from app.security import require_configured_api_key
from app.services.api_keys import has_any_configured_key, provided_key_is_valid


def require_api_key(
    authorization: Annotated[str | None, Header()] = None,
    x_integration_key: Annotated[str | None, Header(alias="X-Integration-Key")] = None,
    x_api_key: Annotated[str | None, Header(alias="X-Api-Key")] = None,
) -> None:
    provided = None
    if authorization and authorization.lower().startswith("bearer "):
        provided = authorization[7:].strip()
    provided = provided or (x_integration_key or "").strip() or (x_api_key or "").strip()

    if not has_any_configured_key():
        if require_configured_api_key():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Integration API key not configured",
            )
        return

    if not provided_key_is_valid(provided):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
