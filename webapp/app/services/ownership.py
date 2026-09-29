"""Per-login ownership of LinkedIn scan identities and briefs."""
from __future__ import annotations

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models import Brief, Scan, User
from app.session_auth import SessionUser


def is_login_account(user: User) -> bool:
    return bool(user.password_hash)


def linkedin_identities_for(db: Session, session_user: SessionUser) -> list[User]:
    """Identities the signed-in user may pick in the Briefs dropdown."""
    if session_user.is_admin:
        # Prefer LinkedIn scan identities (and any account that already has scans/briefs)
        rows = db.query(User).order_by(User.user_id.asc()).limit(200).all()
        with_data = {r[0] for r in db.query(Brief.user_pk).distinct().all()} | {
            r[0] for r in db.query(Scan.user_pk).distinct().all()
        }
        out: list[User] = []
        seen: set[str] = set()
        for u in rows:
            if u.user_id in seen:
                continue
            # Skip other people's login-only accounts that never scanned
            if is_login_account(u) and u.id not in with_data and u.id != session_user.user_pk:
                continue
            seen.add(u.user_id)
            out.append(u)
        return out

    me = db.query(User).filter(User.id == session_user.user_pk).one_or_none()
    preferred = (me.linkedin_email or "").strip().lower() if me else ""
    filters = [
        User.id == session_user.user_pk,
        User.owner_pk == session_user.user_pk,
    ]
    if preferred:
        filters.append(User.user_id == preferred)
    # Orphan LinkedIn identities (no owner yet) with existing data — claimable
    orphans = (
        db.query(User)
        .outerjoin(Brief, Brief.user_pk == User.id)
        .outerjoin(Scan, Scan.user_pk == User.id)
        .filter(
            User.owner_pk.is_(None),
            User.password_hash.is_(None),
            or_(Brief.id.isnot(None), Scan.id.isnot(None)),
        )
        .distinct()
        .all()
    )
    rows = db.query(User).filter(or_(*filters)).order_by(User.user_id.asc()).limit(100).all()
    seen: set[str] = set()
    out: list[User] = []
    for u in list(rows) + list(orphans):
        if u.user_id in seen:
            continue
        seen.add(u.user_id)
        out.append(u)
    return out


def identity_content_counts(db: Session, user_pk: int) -> tuple[int, int]:
    briefs = db.query(func.count(Brief.id)).filter(Brief.user_pk == user_pk).scalar() or 0
    scans = db.query(func.count(Scan.id)).filter(Scan.user_pk == user_pk).scalar() or 0
    return int(briefs), int(scans)


def identities_with_counts(
    db: Session, session_user: SessionUser
) -> list[dict[str, object]]:
    rows = linkedin_identities_for(db, session_user)
    out: list[dict[str, object]] = []
    for u in rows:
        b, s = identity_content_counts(db, u.id)
        out.append({"user_id": u.user_id, "briefs": b, "scans": s, "user": u})
    out.sort(key=lambda r: (int(r["briefs"]) + int(r["scans"]), str(r["user_id"])), reverse=True)
    return out


def default_linkedin_email(db: Session, session_user: SessionUser) -> str:
    me = db.query(User).filter(User.id == session_user.user_pk).one_or_none()
    preferred = (me.linkedin_email or "").strip().lower() if me else ""
    ranked = identities_with_counts(db, session_user)

    if preferred:
        for row in ranked:
            if row["user_id"] == preferred and (int(row["briefs"]) + int(row["scans"])) > 0:
                return preferred

    for row in ranked:
        if int(row["briefs"]) + int(row["scans"]) > 0:
            return str(row["user_id"])

    if preferred:
        return preferred
    return session_user.email


def resolve_active_linkedin_email(
    db: Session,
    session_user: SessionUser,
    *,
    query_user: str | None,
    cookie_user: str | None,
) -> str:
    """Pick which LinkedIn identity the dashboard should show.

    Explicit ?user= wins. Otherwise prefer an identity that actually has
    briefs/scans (avoids sticky cookies on a typo email showing an empty UI).
    """
    explicit = (query_user or "").strip().lower()
    cookie = (cookie_user or "").strip().lower()
    preferred = default_linkedin_email(db, session_user)
    ranked = identities_with_counts(db, session_user)
    allowed = {str(r["user_id"]) for r in ranked}

    if explicit:
        if session_user.is_admin or explicit in allowed:
            return explicit
        return preferred

    candidate = cookie or preferred
    if not session_user.is_admin and candidate not in allowed and preferred:
        candidate = preferred

    # Sticky empty cookie / typo: jump to the identity with data
    counts = {str(r["user_id"]): int(r["briefs"]) + int(r["scans"]) for r in ranked}
    if counts.get(candidate, 0) == 0:
        for row in ranked:
            if int(row["briefs"]) + int(row["scans"]) > 0:
                return str(row["user_id"])
    return candidate or preferred or session_user.email


def can_access_linkedin_user(db: Session, session_user: SessionUser, target: User | None) -> bool:
    if not target:
        return False
    if session_user.is_admin:
        return True
    if target.id == session_user.user_pk:
        return True
    if target.owner_pk == session_user.user_pk:
        return True
    # Orphan scan identity with data — allow view so the user can reclaim via Connect
    if target.owner_pk is None and not is_login_account(target):
        b, s = identity_content_counts(db, target.id)
        if b + s > 0:
            return True
    return False


def claim_or_reject_linkedin_email(
    db: Session, session_user: SessionUser, linkedin_email: str
) -> tuple[User | None, str | None]:
    """
    Attach a LinkedIn email to this login account for scanning.
    Blocks emails owned by another login user.
    """
    uid = linkedin_email.strip().lower()
    if "@" not in uid:
        return None, "Enter a valid LinkedIn email"

    existing = db.query(User).filter(User.user_id == uid).one_or_none()

    # Another person's Market Pulse login — members cannot scan as them
    if (
        existing
        and is_login_account(existing)
        and existing.id != session_user.user_pk
        and not session_user.is_admin
    ):
        return None, "That email is another Market Pulse login account"

    # LinkedIn identity already claimed by someone else
    if (
        existing
        and existing.owner_pk
        and existing.owner_pk != session_user.user_pk
        and not session_user.is_admin
    ):
        return None, "That LinkedIn identity belongs to another user"

    from app.services.briefs import ensure_user

    if existing and is_login_account(existing) and existing.id == session_user.user_pk:
        li_user = existing
    else:
        li_user = ensure_user(db, uid, owner_pk=session_user.user_pk)
        if not is_login_account(li_user) and li_user.owner_pk != session_user.user_pk:
            if li_user.owner_pk is None or session_user.is_admin:
                li_user.owner_pk = session_user.user_pk
                db.commit()

    me = db.query(User).filter(User.id == session_user.user_pk).one_or_none()
    if me:
        me.linkedin_email = uid
        db.commit()

    return li_user, None


def can_access_brief(session_user: SessionUser, brief: Brief) -> bool:
    if session_user.is_admin:
        return True
    owner = brief.user
    if not owner:
        return False
    if owner.id == session_user.user_pk:
        return True
    if owner.owner_pk == session_user.user_pk:
        return True
    return False
