"""Session helpers for UI login (separate from MCP API key)."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.models import User
from app.security import login_rate_bucket, verify_password


@dataclass
class SessionUser:
    email: str
    is_admin: bool
    user_pk: int

    @property
    def user_id(self) -> str:
        return self.email


PUBLIC_PATH_PREFIXES = (
    "/login",
    "/logout",
    "/healthz",
    "/static",
    "/mcp",
    "/api/integration",
)


def is_public_path(path: str) -> bool:
    if path in {"/login", "/logout", "/healthz", "/favicon.ico"}:
        return True
    return any(path == p or path.startswith(p + "/") for p in PUBLIC_PATH_PREFIXES)


def get_session_user(request: Request, db: Session) -> SessionUser | None:
    email = (request.session.get("email") or "").strip().lower()
    if not email:
        return None
    user = db.query(User).filter(User.user_id == email).one_or_none()
    # Only password-backed login accounts may hold a UI session
    if not user or not user.active or not user.password_hash:
        request.session.clear()
        return None
    return SessionUser(email=user.user_id, is_admin=bool(user.is_admin), user_pk=user.id)


def require_session_user(request: Request, db: Session) -> SessionUser | RedirectResponse:
    user = get_session_user(request, db)
    if user:
        return user
    return RedirectResponse("/login", status_code=303)


def require_admin(user: SessionUser) -> bool:
    return bool(user.is_admin)


def login_allowed(email: str, password: str, db: Session, *, client_ip: str) -> tuple[User | None, str | None]:
    """Returns (user, error_message)."""
    bucket = login_rate_bucket()
    now = time.time()
    hits = [t for t in bucket.get(client_ip, []) if now - t < 300]
    if len(hits) >= 20:
        return None, "Too many sign-in attempts. Try again in a few minutes."
    hits.append(now)
    bucket[client_ip] = hits

    email = email.strip().lower()
    user = db.query(User).filter(User.user_id == email).one_or_none()
    if not user or not user.active:
        return None, "Invalid email or password."
    if not user.password_hash:
        return None, "Account has no password yet. Ask an admin to reset it."
    if not verify_password(password, user.password_hash):
        return None, "Invalid email or password."
    return user, None


def set_login_session(request: Request, user: User) -> None:
    request.session.clear()
    request.session["email"] = user.user_id
    request.session["is_admin"] = bool(user.is_admin)


def template_session(user: SessionUser | None) -> dict[str, Any]:
    if not user:
        return {"email": "", "is_admin": False}
    return {"email": user.email, "is_admin": user.is_admin, "user_id": user.email}
