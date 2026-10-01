"""Named integration API keys (admin-managed). Multiple keys stay active until revoked."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import ApiKey
from app.security import generate_api_key


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


def list_api_keys(db: Session, *, owner_email: str) -> list[ApiKey]:
    owner = owner_email.strip().lower()
    if not owner:
        return []
    return (
        db.query(ApiKey)
        .filter(ApiKey.created_by == owner)
        .order_by(ApiKey.created_at.desc())
        .all()
    )


def delete_api_key(db: Session, key_id: int, *, owner_email: str) -> bool:
    row = (
        db.query(ApiKey)
        .filter(ApiKey.id == key_id, ApiKey.created_by == owner_email.strip().lower())
        .one_or_none()
    )
    if not row:
        return False
    db.delete(row)
    db.commit()
    return True


def delete_user_api_keys(db: Session, *, owner_email: str) -> int:
    owner = owner_email.strip().lower()
    if not owner:
        return 0
    count = db.query(ApiKey).filter(ApiKey.created_by == owner).delete(synchronize_session=False)
    db.commit()
    return count


def delete_all_api_keys(db: Session) -> int:
    count = db.query(ApiKey).delete(synchronize_session=False)
    db.commit()
    return count


def revoke_api_key(db: Session, key_id: int, *, owner_email: str) -> bool:
    row = (
        db.query(ApiKey)
        .filter(ApiKey.id == key_id, ApiKey.created_by == owner_email.strip().lower())
        .one_or_none()
    )
    if not row or not row.active:
        return False
    row.active = 0
    row.revoked_at = datetime.now(timezone.utc)
    db.commit()
    return True


def regenerate_api_key(db: Session, key_id: int, *, owner_email: str) -> tuple[ApiKey, str] | None:
    """Issue a new secret for an existing key row, reactivating it if it was revoked."""
    row = (
        db.query(ApiKey)
        .filter(ApiKey.id == key_id, ApiKey.created_by == owner_email.strip().lower())
        .one_or_none()
    )
    if not row:
        return None
    raw = generate_api_key()
    row.key_prefix = raw[:8]
    row.key_hash = hash_api_key(raw)
    row.active = 1
    row.revoked_at = None
    db.commit()
    db.refresh(row)
    return row, raw


def count_active_keys(db: Session, *, owner_email: str | None = None) -> int:
    query = db.query(ApiKey).filter(ApiKey.active == 1)
    if owner_email is not None:
        owner = owner_email.strip().lower()
        if not owner:
            return 0
        query = query.filter(ApiKey.created_by == owner)
    return query.count()


def provided_key_is_valid(provided: str | None, db: Session | None = None) -> bool:
    """Accept only an active named key; legacy keys are bootstrapped into this table."""
    if not provided:
        return False
    candidate = provided.strip()
    if not candidate:
        return False

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


