"""Password hashing, API key helpers, constant-time compares."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from typing import Any


def generate_api_key() -> str:
    return secrets.token_hex(24)


def hash_password(password: str, *, iterations: int = 390_000) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        iterations,
    ).hex()
    return f"pbkdf2_sha256${iterations}${salt}${digest}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored or not password:
        return False
    try:
        algo, iters_s, salt, digest = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        iterations = int(iters_s)
    except Exception:
        return False
    check = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        iterations,
    ).hex()
    return hmac.compare_digest(check, digest)


def secret_key() -> str:
    key = (os.getenv("SESSION_SECRET") or os.getenv("SECRET_KEY") or "").strip()
    if key:
        return key
    # Stable fallback for local only — production must set SESSION_SECRET
    return "dev-only-change-me-market-pulse"


def expected_integration_key() -> str:
    return (
        os.getenv("INTEGRATION_API_KEY", "").strip()
        or os.getenv("MCP_BEARER_TOKEN", "").strip()
        or os.getenv("API_KEY", "").strip()
    )


def api_keys_match(provided: str | None, expected: str | None) -> bool:
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided.strip(), expected.strip())


def require_configured_api_key() -> bool:
    """In production, refuse open MCP when no key is set."""
    return os.getenv("REQUIRE_API_KEY", "true").lower() in {"1", "true", "yes"}


def public_base_url(request_base: str | None = None) -> str:
    env = (os.getenv("PUBLIC_BASE_URL") or os.getenv("WEBAPP_URL") or "").strip().rstrip("/")
    if env:
        return env
    return (request_base or "").rstrip("/")


def mask_secret(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "••••••••"
    return f"{value[:4]}…{value[-4:]}"


def login_rate_bucket() -> dict[str, Any]:
    """Simple process-local rate limit state (enough for single-replica Container App)."""
    global _LOGIN_HITS  # noqa: PLW0603
    return _LOGIN_HITS


_LOGIN_HITS: dict[str, list[float]] = {}
