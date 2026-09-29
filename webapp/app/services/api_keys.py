"""Named integration API keys (admin-managed). Multiple keys stay active until revoked."""
from __future__ import annotations

import hashlib
import hmac
import os
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import ApiKey
from app.security import expected_integration_key, generate_api_key


def hash_api_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_api_key(db: Session, *, name: str, created_by: str | None) -> tuple[ApiKey, str]:
    label = (name or "").strip() or "Untitled key"
    if len(label) > 128:
        label = label[:128]
    raw = generate_api_key()
    row = ApiKey(
        name=label,
        key_prefix=raw[:8],
        key_hash=hash_api_key(raw),
        created_by=(created_by or "").strip().lower() or None,
        active=1,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row, raw


def list_api_keys(db: Session) -> list[ApiKey]:
    return db.query(ApiKey).order_by(ApiKey.created_at.desc()).all()


def revoke_api_key(db: Session, key_id: int) -> bool:
    row = db.query(ApiKey).filter(ApiKey.id == key_id).one_or_none()
    if not row or not row.active:
        return False
    row.active = 0
    row.revoked_at = datetime.now(timezone.utc)
    db.commit()
    return True


def count_active_keys(db: Session) -> int:
    return db.query(ApiKey).filter(ApiKey.active == 1).count()


def provided_key_is_valid(provided: str | None, db: Session | None = None) -> bool:
    """Accept any active named key, or legacy INTEGRATION_API_KEY env (deploy bootstrap)."""
    if not provided:
        return False
    candidate = provided.strip()
    if not candidate:
        return False

    legacy = expected_integration_key()
    if legacy and hmac.compare_digest(candidate, legacy):
        return True

    close = False
    if db is None:
        from app.db import SessionLocal

        db = SessionLocal()
        close = True
    try:
        digest = hash_api_key(candidate)
        row = (
            db.query(ApiKey)
            .filter(ApiKey.key_hash == digest, ApiKey.active == 1)
            .one_or_none()
        )
        return row is not None
    finally:
        if close:
            db.close()


def has_any_configured_key(db: Session | None = None) -> bool:
    if expected_integration_key():
        return True
    close = False
    if db is None:
        from app.db import SessionLocal

        db = SessionLocal()
        close = True
    try:
        return count_active_keys(db) > 0
    finally:
        if close:
            db.close()


def bootstrap_legacy_env_key(db: Session) -> None:
    """If env INTEGRATION_API_KEY is set and no named keys exist, register it once."""
    legacy = expected_integration_key()
    if not legacy:
        return
    if count_active_keys(db) > 0:
        return
    digest = hash_api_key(legacy)
    if db.query(ApiKey).filter(ApiKey.key_hash == digest).one_or_none():
        return
    db.add(
        ApiKey(
            name="Deploy default",
            key_prefix=legacy[:8],
            key_hash=digest,
            created_by="system",
            active=1,
        )
    )
    db.commit()
