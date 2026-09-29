"""Orchestrate LinkedIn scan → DB → brief; used by API, MCP, and daily scheduler."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import User
from app.services.briefs import (
    create_brief_from_scan,
    create_scan,
    ensure_source,
    ensure_user,
    save_posts_from_payload,
)
from app.services.scroller_client import get_linkedin_scan, start_linkedin_scan
from app.services.settings_store import get_scan_options

log = logging.getLogger("market_pulse.pipeline")


def _rank_by_focus(posts: list, keywords: list[str]) -> list:
    """Surface keyword matches first; keep the rest below (no hard drop)."""
    if not keywords:
        return posts
    keys = [k.lower() for k in keywords]

    def score(p: dict) -> int:
        blob = f"{p.get('author') or ''} {p.get('headline') or ''} {p.get('text') or ''}".lower()
        return sum(1 for k in keys if k in blob)

    ranked = sorted(posts, key=score, reverse=True)
    for i, p in enumerate(ranked, start=1):
        if isinstance(p, dict):
            p["rank"] = i
    return ranked


def _filter_recent(posts: list, recency: str) -> list:
    if recency in {"all", "", "any"} or not posts:
        return posts
    import re
    from datetime import datetime, timezone

    def keep(p: dict) -> bool:
        posted = (p.get("postedAt") or p.get("posted_at") or "").strip().lower()
        if not posted:
            return True
        if "just now" in posted:
            return True
        if "ago" in posted:
            m = re.search(r"(\d+)\s*(minute|hour|day|week|month|year)s?", posted)
            if not m:
                return True
            n, unit = int(m.group(1)), m.group(2)
            if unit.startswith(("minute", "hour")):
                return True
            if unit.startswith("day"):
                return n <= 2  # allow yesterday-ish
            return False
        m = re.match(r"^(\d+)\s*([smhdw])\b", posted)
        if m:
            n, u = int(m.group(1)), m.group(2)
            if u in {"s", "m", "h"}:
                return True
            if u == "d":
                return n <= 2
            return False
        try:
            if "t" in posted and "-" in posted:
                dt = datetime.fromisoformat(posted.replace("z", "+00:00"))
                age = datetime.now(timezone.utc) - (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc))
                return age.total_seconds() <= 48 * 3600
        except Exception:
            pass
        return True

    filtered = [p for p in posts if keep(p)]
    # Soft floor: don't leave the brief nearly empty because of timestamps
    if filtered and len(filtered) < max(12, len(posts) // 3):
        return posts
    return filtered or posts


async def run_linkedin_pipeline(
    db: Session,
    *,
    user_id: str,
    max_posts: int | None = None,
    max_scrolls: int | None = None,
    poll_seconds: int = 10,
    max_wait_seconds: int = 900,
    tz_name: str | None = None,
    utc_offset_minutes: int | None = None,
) -> dict:
    opts = get_scan_options()
    max_posts = max_posts if max_posts is not None else opts["maxPosts"]
    max_scrolls = max_scrolls if max_scrolls is not None else opts["maxScrolls"]
    recent_only = opts["recency"]
    focus = opts["focusKeywords"]

    user = ensure_user(db, user_id)
    source = ensure_source(db)
    scan = create_scan(db, user=user, source=source)
    scan.status = "running"
    db.commit()

    try:
        started = await start_linkedin_scan(
            user_id=user.user_id,
            max_posts=max_posts,
            max_scrolls=max_scrolls,
            recent_only=recent_only,
        )
    except Exception as exc:
        scan.status = "failed"
        scan.error = str(exc)
        scan.completed_at = datetime.now(timezone.utc)
        db.commit()
        return {"scanId": scan.id, "status": "failed", "error": str(exc)}

    job_id = started.get("jobId") or started.get("job_id")
    scan.external_job_id = job_id
    if started.get("loginUrl"):
        scan.login_url = started["loginUrl"]
        scan.status = "awaiting_login"
    db.commit()

    if not job_id:
        # sync-style payload already complete?
        if started.get("posts") is not None:
            posts = _filter_recent(started.get("posts") or [], recent_only)
            posts = _rank_by_focus(posts, focus)
            if started.get("loginRequired") or not posts:
                scan.status = "failed"
                scan.error = started.get("message") or (
                    "LinkedIn login required" if started.get("loginRequired") else "No posts captured — sign-in may have failed or feed was empty"
                )
                scan.login_url = started.get("loginUrl")
                scan.completed_at = datetime.now(timezone.utc)
                scan.post_count = 0
                db.commit()
                return {
                    "scanId": scan.id,
                    "status": "failed",
                    "error": scan.error,
                    "loginUrl": scan.login_url,
                }
            save_posts_from_payload(db, scan, {**started, "posts": posts})
            scan.status = "completed"
            scan.completed_at = datetime.now(timezone.utc)
            db.commit()
            brief = create_brief_from_scan(
                db, scan, focus_keywords=focus, tz_name=tz_name, utc_offset_minutes=utc_offset_minutes
            )
            return {
                "scanId": scan.id,
                "status": "completed",
                "briefId": brief.id,
                "postCount": scan.post_count,
            }
        scan.status = "failed"
        scan.error = f"No jobId from scroller: {started}"
        db.commit()
        return {"scanId": scan.id, "status": "failed", "error": scan.error}

    elapsed = 0
    first_poll = True
    while elapsed < max_wait_seconds:
        if not first_poll:
            await asyncio.sleep(poll_seconds)
            elapsed += poll_seconds
        first_poll = False
        try:
            status = await get_linkedin_scan(job_id)
        except Exception as exc:
            log.warning("poll failed: %s", exc)
            continue

        st = (status.get("status") or "").lower()
        if st == "awaiting_login" or (status.get("loginUrl") and st not in {"running", "completed", "failed"}):
            if status.get("loginUrl"):
                scan.login_url = status["loginUrl"]
            scan.status = "awaiting_login"
            scan.message = status.get("message")
            db.commit()
        elif st == "running":
            scan.status = "running"
            scan.login_url = None
            scan.message = status.get("message")
            if status.get("postCount") is not None:
                try:
                    scan.post_count = int(status["postCount"])
                except (TypeError, ValueError):
                    pass
            db.commit()
        elif st in {"queued"} and scan.status != "awaiting_login":
            scan.status = "queued"
            db.commit()

        if st == "completed":
            result = status.get("result") or status
            payload = result if isinstance(result, dict) and "posts" in result else status
            if not isinstance(payload, dict):
                payload = {}
            if payload.get("loginRequired"):
                scan.status = "failed"
                scan.error = payload.get("message") or "LinkedIn login required"
                scan.login_url = status.get("loginUrl") or scan.login_url
                scan.completed_at = datetime.now(timezone.utc)
                scan.post_count = 0
                db.commit()
                return {
                    "scanId": scan.id,
                    "status": "failed",
                    "error": scan.error,
                    "loginUrl": scan.login_url,
                }
            posts = payload.get("posts") or []
            if posts:
                posts = _filter_recent(posts, recent_only)
                posts = _rank_by_focus(posts, focus)
                payload = {**payload, "posts": posts}
            save_posts_from_payload(db, scan, payload)
            msg = payload.get("message") or (result.get("message") if isinstance(result, dict) else None)
            scan.message = msg
            # Zero posts is never a success — usually login/session/DOM failure
            if scan.post_count <= 0:
                scan.status = "failed"
                scan.error = msg or (
                    "No posts captured — LinkedIn sign-in may have failed, session expired, or feed was empty. "
                    "Open Sign in if shown, then retry Scan."
                )
                if status.get("loginUrl"):
                    scan.login_url = status["loginUrl"]
                scan.completed_at = datetime.now(timezone.utc)
                db.commit()
                return {
                    "scanId": scan.id,
                    "status": "failed",
                    "error": scan.error,
                    "loginUrl": scan.login_url,
                    "postCount": 0,
                }
            scan.status = "completed"
            scan.login_url = None
            scan.completed_at = datetime.now(timezone.utc)
            db.commit()
            brief = create_brief_from_scan(
                db, scan, focus_keywords=focus, tz_name=tz_name, utc_offset_minutes=utc_offset_minutes
            )
            if brief is None:
                scan.status = "failed"
                scan.error = "No posts to build a brief"
                db.commit()
                return {"scanId": scan.id, "status": "failed", "error": scan.error, "postCount": 0}
            return {
                "scanId": scan.id,
                "status": "completed",
                "briefId": brief.id,
                "postCount": scan.post_count,
                "briefDate": brief.brief_date,
            }
        if st == "failed":
            scan.status = "failed"
            scan.error = status.get("error") or status.get("message") or "scan failed"
            scan.completed_at = datetime.now(timezone.utc)
            db.commit()
            return {"scanId": scan.id, "status": "failed", "error": scan.error}

        # Poll faster while waiting for login / active scrape
        poll_seconds = 5 if st in {"awaiting_login", "running", "queued"} else 10

    scan.status = "failed"
    scan.error = f"Timed out after {max_wait_seconds}s (last status may be awaiting_login)"
    scan.completed_at = datetime.now(timezone.utc)
    db.commit()
    return {
        "scanId": scan.id,
        "status": "failed",
        "error": scan.error,
        "loginUrl": scan.login_url,
    }


async def run_daily_for_all_active_users() -> list[dict]:
    db = SessionLocal()
    try:
        # Only password-backed login accounts; scan their preferred LinkedIn email
        users = db.query(User).filter(User.active == 1, User.password_hash.isnot(None)).all()
        if not users:
            log.info("daily job: no active login users")
            return []
        results = []
        for u in users:
            scan_as = (u.linkedin_email or u.user_id).strip().lower()
            if "@" not in scan_as:
                continue
            log.info("daily job: scanning %s (login %s)", scan_as, u.user_id)
            results.append(await run_linkedin_pipeline(db, user_id=scan_as))
        return results
    finally:
        db.close()
