"""Market Pulse webapp — dashboard + REST + MCP for nebulaONE."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.applications import Starlette
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import Response
from starlette.routing import Mount
from starlette.types import ASGIApp, Receive, Scope, Send

from app.auth import require_api_key
from app.db import get_db, init_db
from app.mcp_tools import mcp
from app.models import Brief, Scan, User
from app.security import hash_password, public_base_url, secret_key
from app.services.api_keys import create_api_key, list_api_keys, revoke_api_key
from app.services.briefs import ensure_user, format_user_time, render_brief_html
from app.services.pipeline import run_linkedin_pipeline
from app.services.scheduler import start_scheduler, stop_scheduler
from app.session_auth import (
    get_session_user,
    is_public_path,
    login_allowed,
    set_login_session,
    template_session,
)

load_dotenv()
from app.services.settings_store import (  # noqa: E402
    apply_to_environ,
    get_public_status,
    save_settings,
)

apply_to_environ()
logging.basicConfig(level=logging.INFO)

ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(ROOT / "templates"))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    from app.services.persist import flush_to_persist_safe, restore_from_persist, start_persist_loop, stop_persist_loop

    restore_from_persist()
    apply_to_environ()
    init_db()
    from app.db import SessionLocal
    from app.services.briefs import ensure_source

    db = SessionLocal()
    try:
        ensure_source(db)
    finally:
        db.close()
    start_scheduler()
    start_persist_loop()
    flush_to_persist_safe()
    yield
    stop_persist_loop()
    stop_scheduler()


api = FastAPI(title="Market Pulse", lifespan=lifespan)

static_dir = ROOT / "static"
static_dir.mkdir(exist_ok=True)
api.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


def _client_ip(request: Request) -> str:
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    return forwarded or (request.client.host if request.client else "unknown")


def _tz_from_request(request: Request) -> tuple[str | None, int | None]:
    tz_name = (request.cookies.get("mp_tz") or "").strip() or None
    offset = None
    try:
        raw = (request.cookies.get("mp_utc_offset") or "").strip()
        if raw != "":
            offset = int(raw)
    except ValueError:
        offset = None
    return tz_name, offset


@api.middleware("http")
async def ui_login_gate(request: Request, call_next):
    path = request.url.path
    if is_public_path(path):
        return await call_next(request)
    # SessionMiddleware runs outside FastAPI middleware stack when we wrap later;
    # session may be empty here if only FastAPI middleware — gate also in routes.
    email = ""
    try:
        email = (request.session.get("email") or "").strip()
    except AssertionError:
        # SessionMiddleware not yet attached in some test paths
        pass
    if not email and not path.startswith("/api/integration") and not path.startswith("/mcp"):
        if path.startswith("/api/ui/"):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return RedirectResponse("/login", status_code=303)
    return await call_next(request)


@api.get("/healthz")
@api.get("/api/integration/v1/health")
def health():
    from app.services.persist import list_persist_candidates, persist_status

    persist = persist_status()
    try:
        cands = list_persist_candidates()[:5]
        persist["candidates"] = [
            {
                "name": c["name"],
                "briefs": c["briefs"],
                "scans": c["scans"],
                "posts": c["posts"],
                "bytes": c["bytes"],
            }
            for c in cands
        ]
    except Exception:
        persist["candidates"] = []
    return {
        "status": "ok",
        "service": "market-pulse-webapp",
        "dailyScanEnabled": os.getenv("DAILY_SCAN_ENABLED", "true").lower() in {"1", "true", "yes"},
        "persist": persist,
    }


@api.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    if get_session_user(request, db):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "flash": request.query_params.get("flash"),
            "flash_error": request.query_params.get("err") == "1",
        },
    )


@api.post("/login")
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user, err = login_allowed(email, password, db, client_ip=_client_ip(request))
    if err or not user:
        return RedirectResponse(
            f"/login?flash={quote(err or 'Sign-in failed')}&err=1",
            status_code=303,
        )
    set_login_session(request, user)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("mp_user", user.user_id, max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax")
    return resp


@api.get("/logout")
def logout(request: Request):
    request.session.clear()
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("mp_user")
    return resp


@api.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    session_user = get_session_user(request, db)
    if not session_user:
        return RedirectResponse("/login", status_code=303)

    from app.services.ownership import (
        identities_with_counts,
        resolve_active_linkedin_email,
    )

    identity_rows = identities_with_counts(db, session_user)
    users = [r["user"] for r in identity_rows]
    active = resolve_active_linkedin_email(
        db,
        session_user,
        query_user=request.query_params.get("user"),
        cookie_user=request.cookies.get("mp_user"),
    )

    active_user = db.query(User).filter(User.user_id == active).one_or_none()
    tz_name, offset = _tz_from_request(request)

    if active_user:
        briefs = (
            db.query(Brief)
            .filter(Brief.user_pk == active_user.id)
            .order_by(Brief.created_at.desc())
            .limit(40)
            .all()
        )
        scans = (
            db.query(Scan)
            .filter(Scan.user_pk == active_user.id)
            .order_by(Scan.created_at.desc())
            .limit(40)
            .all()
        )
    else:
        briefs, scans = [], []

    # Newest-first list ordinal for this LinkedIn identity (1 = latest)
    scan_rows = []
    for i, s in enumerate(scans, start=1):
        scan_rows.append({"scan": s, "n": i, "total": len(scans)})

    brief_rows = [
        {
            "id": b.id,
            "time_label": format_user_time(b.created_at, tz_name=tz_name, utc_offset_minutes=offset)
            if b.created_at
            else b.brief_date,
        }
        for b in briefs
    ]

    other_with_data = [
        r
        for r in identity_rows
        if r["user_id"] != active and (int(r["briefs"]) + int(r["scans"])) > 0
    ]
    empty_hint = None
    if not briefs and not scans and other_with_data:
        top = other_with_data[0]
        empty_hint = (
            f"No data for {active}. Try {top['user_id']} "
            f"({top['briefs']} briefs, {top['scans']} scans) from the email menu."
        )

    status = get_public_status()
    try:
        status["dailyScanMinuteUtc"] = f"{int(status['dailyScanMinuteUtc']):02d}"
    except Exception:
        status["dailyScanMinuteUtc"] = str(status.get("dailyScanMinuteUtc") or "00")

    resp = templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "briefs": briefs,
            "brief_rows": brief_rows,
            "users": users,
            "identity_rows": identity_rows,
            "scans": scans,
            "scan_rows": scan_rows,
            "active_user": active,
            "status": status,
            "flash": request.query_params.get("flash") or empty_hint,
            "flash_error": request.query_params.get("err") == "1",
            "session_user": template_session(session_user),
        },
    )
    if active:
        resp.set_cookie("mp_user", active, max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax")
    return resp


@api.get("/api/ui/activity")
async def ui_activity(request: Request, userId: str = "", db: Session = Depends(get_db)):
    session_user = get_session_user(request, db)
    if not session_user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    uid = (userId or "").strip().lower()
    if not uid:
        return {"userId": "", "briefs": [], "scans": [], "live": False, "linkedinSession": None}
    from app.services.ownership import can_access_linkedin_user
    from app.services.scroller_client import get_linkedin_session_status

    user = db.query(User).filter(User.user_id == uid).one_or_none()
    if not user or not can_access_linkedin_user(db, session_user, user):
        return {
            "userId": uid,
            "briefs": [],
            "scans": [],
            "live": False,
            "error": "forbidden",
            "linkedinSession": None,
        }
    briefs = (
        db.query(Brief)
        .filter(Brief.user_pk == user.id)
        .order_by(Brief.created_at.desc())
        .limit(40)
        .all()
    )
    scans = (
        db.query(Scan)
        .filter(Scan.user_pk == user.id)
        .order_by(Scan.created_at.desc())
        .limit(40)
        .all()
    )
    live = any(s.status in {"running", "queued", "awaiting_login"} for s in scans)
    tz_name, offset = _tz_from_request(request)
    linkedin_session = None
    try:
        linkedin_session = await get_linkedin_session_status(uid)
    except Exception:
        linkedin_session = {"ok": False, "canSkipVnc": False, "message": "Could not reach scroller"}
    return {
        "userId": uid,
        "live": live,
        "linkedinSession": linkedin_session,
        "briefs": [
            {
                "id": b.id,
                "briefDate": b.brief_date,
                "createdAt": b.created_at.isoformat() if b.created_at else None,
                "title": b.title,
                "timeLabel": format_user_time(b.created_at, tz_name=tz_name, utc_offset_minutes=offset)
                if b.created_at
                else b.brief_date,
            }
            for b in briefs
        ],
        "scans": [
            {
                "id": s.id,
                "n": i,
                "total": len(scans),
                "status": s.status,
                "postCount": s.post_count,
                "loginUrl": s.login_url if s.status == "awaiting_login" else None,
                "message": s.message,
                "error": (s.error or "")[:120] or None,
            }
            for i, s in enumerate(scans, start=1)
        ],
    }


@api.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    session_user = get_session_user(request, db)
    if not session_user:
        return RedirectResponse("/login", status_code=303)
    users = []
    api_keys = []
    if session_user.is_admin:
        # Webapp login accounts only (not LinkedIn-only scan identities)
        users = (
            db.query(User)
            .filter((User.password_hash.isnot(None)) | (User.is_admin == 1))
            .order_by(User.user_id.asc())
            .all()
        )
        api_keys = list_api_keys(db)
    base = public_base_url(str(request.base_url).rstrip("/"))
    new_api_key = request.session.pop("flash_api_key", None)
    new_api_key_name = request.session.pop("flash_api_key_name", None)
    from app.services.ownership import default_linkedin_email

    preferred_li = default_linkedin_email(db, session_user)
    persist_candidates = []
    if session_user.is_admin:
        try:
            from app.services.persist import list_persist_candidates

            persist_candidates = list_persist_candidates()[:8]
        except Exception:
            persist_candidates = []
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "status": get_public_status(),
            "flash": request.query_params.get("flash"),
            "flash_error": request.query_params.get("err") == "1",
            "new_api_key": new_api_key,
            "new_api_key_name": new_api_key_name,
            "users": users,
            "api_keys": api_keys,
            "session_user": template_session(session_user),
            "base_url": base,
            "persist_candidates": persist_candidates,
            "preferred_linkedin": preferred_li,
            "preferred_linkedin_email": preferred_li,
        },
    )


@api.post("/settings")
async def settings_save(
    request: Request,
    daily_scan_enabled: str = Form("true"),
    daily_scan_hour_utc: str = Form("13"),
    daily_scan_minute_utc: str = Form("0"),
    scan_max_posts: str = Form("40"),
    scan_max_scrolls: str = Form("80"),
    scan_recency: str = Form("today"),
    focus_keywords: str = Form(""),
    db: Session = Depends(get_db),
):
    session_user = get_session_user(request, db)
    if not session_user:
        return RedirectResponse("/login", status_code=303)
    kwargs = {
        "scan_max_posts": scan_max_posts,
        "scan_max_scrolls": scan_max_scrolls,
        "scan_recency": scan_recency,
        "focus_keywords": focus_keywords,
    }
    if session_user.is_admin:
        kwargs.update(
            daily_scan_enabled=daily_scan_enabled,
            daily_scan_hour_utc=daily_scan_hour_utc,
            daily_scan_minute_utc=daily_scan_minute_utc,
        )
    save_settings(**kwargs)
    if session_user.is_admin:
        stop_scheduler()
        start_scheduler()
    return RedirectResponse("/settings?flash=Saved", status_code=303)


def _admin_count(db: Session) -> int:
    return db.query(User).filter(User.is_admin == 1, User.active == 1).count()


@api.post("/ui/recover-persist")
def ui_recover_persist(request: Request, db: Session = Depends(get_db)):
    """Admin: re-hydrate working DB from the richest Azure Files backup/snapshot."""
    session_user = get_session_user(request, db)
    if not session_user or not session_user.is_admin:
        return RedirectResponse("/login", status_code=303)
    from app.services.persist import flush_to_persist_safe, list_persist_candidates, restore_from_persist

    before = list_persist_candidates()[:3]
    restore_from_persist()
    flush_to_persist_safe()
    after = list_persist_candidates()[:1]
    top = after[0] if after else None
    if top and (int(top.get("briefs") or 0) + int(top.get("scans") or 0)) > 0:
        msg = (
            f"Recovered from storage — {top.get('briefs')} briefs, {top.get('scans')} scans "
            f"({top.get('name')}). Refresh Briefs."
        )
        return RedirectResponse(f"/settings?flash={quote(msg)}", status_code=303)
    detail = ", ".join(
        f"{c.get('name')}: b{c.get('briefs')}/s{c.get('scans')}" for c in before
    ) or "no backups found"
    msg = (
        "No briefs/scans found in Azure Files backups. "
        f"Checked: {detail}. Run a new Scan feed."
    )
    return RedirectResponse(f"/settings?flash={quote(msg)}&err=1", status_code=303)


@api.post("/ui/api-keys/create")
def ui_create_api_key(
    request: Request,
    name: str = Form(...),
    db: Session = Depends(get_db),
):
    session_user = get_session_user(request, db)
    if not session_user or not session_user.is_admin:
        return RedirectResponse("/settings?flash=Admin+only&err=1", status_code=303)
    row, raw = create_api_key(db, name=name, created_by=session_user.email)
    request.session["flash_api_key"] = raw
    request.session["flash_api_key_name"] = row.name
    return RedirectResponse("/settings?flash=API+key+created", status_code=303)


@api.post("/ui/api-keys/{key_id}/revoke")
def ui_revoke_api_key(key_id: int, request: Request, db: Session = Depends(get_db)):
    session_user = get_session_user(request, db)
    if not session_user or not session_user.is_admin:
        return RedirectResponse("/settings?flash=Admin+only&err=1", status_code=303)
    if revoke_api_key(db, key_id):
        return RedirectResponse("/settings?flash=API+key+revoked", status_code=303)
    return RedirectResponse("/settings?flash=Key+not+found&err=1", status_code=303)


@api.post("/ui/users/add")
def ui_add_user(
    request: Request,
    email: str = Form(...),
    display_name: str = Form(""),
    password: str = Form(...),
    role: str = Form("member"),
    db: Session = Depends(get_db),
):
    session_user = get_session_user(request, db)
    if not session_user or not session_user.is_admin:
        return RedirectResponse("/settings?flash=Admin+only&err=1", status_code=303)
    uid = email.strip().lower()
    if "@" not in uid:
        return RedirectResponse("/settings?flash=Invalid+email&err=1", status_code=303)
    pw = password.strip()
    if len(pw) < 8:
        return RedirectResponse("/settings?flash=Password+must+be+8%2B+chars&err=1", status_code=303)
    make_admin = role.strip().lower() == "admin"
    existing = db.query(User).filter(User.user_id == uid).one_or_none()
    if existing:
        existing.active = 1
        existing.password_hash = hash_password(pw)
        existing.is_admin = 1 if make_admin else 0
        if display_name.strip():
            existing.display_name = display_name.strip()
        db.commit()
        try:
            from app.services.persist import flush_to_persist_safe

            flush_to_persist_safe()
        except Exception:
            pass
        return RedirectResponse("/settings?flash=User+updated", status_code=303)
    db.add(
        User(
            user_id=uid,
            display_name=display_name.strip() or uid,
            active=1,
            is_admin=1 if make_admin else 0,
            password_hash=hash_password(pw),
        )
    )
    db.commit()
    try:
        from app.services.persist import flush_to_persist_safe

        flush_to_persist_safe()
    except Exception:
        pass
    return RedirectResponse("/settings?flash=User+added", status_code=303)


@api.post("/ui/users/{user_pk}/deactivate")
def ui_deactivate_user(user_pk: int, request: Request, db: Session = Depends(get_db)):
    session_user = get_session_user(request, db)
    if not session_user or not session_user.is_admin:
        return RedirectResponse("/settings?flash=Admin+only&err=1", status_code=303)
    user = db.query(User).filter(User.id == user_pk).one_or_none()
    if not user:
        return RedirectResponse("/settings?flash=User+not+found&err=1", status_code=303)
    if user.user_id == session_user.email:
        return RedirectResponse("/settings?flash=Cannot+deactivate+yourself&err=1", status_code=303)
    if user.is_admin and _admin_count(db) <= 1:
        return RedirectResponse("/settings?flash=Cannot+deactivate+last+admin&err=1", status_code=303)
    user.active = 0
    db.commit()
    return RedirectResponse("/settings?flash=User+deactivated", status_code=303)


@api.post("/ui/users/{user_pk}/activate")
def ui_activate_user(user_pk: int, request: Request, db: Session = Depends(get_db)):
    session_user = get_session_user(request, db)
    if not session_user or not session_user.is_admin:
        return RedirectResponse("/settings?flash=Admin+only&err=1", status_code=303)
    user = db.query(User).filter(User.id == user_pk).one_or_none()
    if not user:
        return RedirectResponse("/settings?flash=User+not+found&err=1", status_code=303)
    user.active = 1
    db.commit()
    return RedirectResponse("/settings?flash=User+activated", status_code=303)


@api.post("/ui/users/{user_pk}/role")
def ui_set_role(
    user_pk: int,
    request: Request,
    role: str = Form(...),
    db: Session = Depends(get_db),
):
    session_user = get_session_user(request, db)
    if not session_user or not session_user.is_admin:
        return RedirectResponse("/settings?flash=Admin+only&err=1", status_code=303)
    user = db.query(User).filter(User.id == user_pk).one_or_none()
    if not user:
        return RedirectResponse("/settings?flash=User+not+found&err=1", status_code=303)
    if user.user_id == session_user.email:
        return RedirectResponse("/settings?flash=Cannot+change+your+own+role&err=1", status_code=303)
    want_admin = role.strip().lower() == "admin"
    if user.is_admin and not want_admin and _admin_count(db) <= 1:
        return RedirectResponse("/settings?flash=Cannot+remove+last+admin&err=1", status_code=303)
    user.is_admin = 1 if want_admin else 0
    db.commit()
    return RedirectResponse(
        f"/settings?flash={'Made+admin' if want_admin else 'Made+member'}",
        status_code=303,
    )


@api.post("/ui/users/{user_pk}/set-password")
def ui_set_password(
    user_pk: int,
    request: Request,
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    session_user = get_session_user(request, db)
    if not session_user or not session_user.is_admin:
        return RedirectResponse("/settings?flash=Admin+only&err=1", status_code=303)
    user = db.query(User).filter(User.id == user_pk).one_or_none()
    if not user:
        return RedirectResponse("/settings?flash=User+not+found&err=1", status_code=303)
    pw = password.strip()
    if len(pw) < 8:
        return RedirectResponse("/settings?flash=Password+must+be+8%2B+chars&err=1", status_code=303)
    user.password_hash = hash_password(pw)
    db.commit()
    try:
        from app.services.persist import flush_to_persist_safe

        flush_to_persist_safe()
    except Exception:
        pass
    return RedirectResponse("/settings?flash=Password+updated", status_code=303)


@api.post("/ui/change-my-password")
def ui_change_my_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
):
    """Any signed-in user can change their own password (persisted to Azure Files)."""
    from app.security import verify_password

    session_user = get_session_user(request, db)
    if not session_user:
        return RedirectResponse("/login", status_code=303)
    user = db.query(User).filter(User.id == session_user.user_pk).one_or_none()
    if not user or not user.password_hash:
        return RedirectResponse("/settings?flash=Account+not+found&err=1", status_code=303)
    if not verify_password(current_password, user.password_hash):
        return RedirectResponse("/settings?flash=Current+password+is+wrong&err=1", status_code=303)
    pw = (new_password or "").strip()
    if len(pw) < 8:
        return RedirectResponse("/settings?flash=New+password+must+be+8%2B+chars&err=1", status_code=303)
    if pw != (confirm_password or "").strip():
        return RedirectResponse("/settings?flash=New+passwords+do+not+match&err=1", status_code=303)
    user.password_hash = hash_password(pw)
    db.commit()
    try:
        from app.services.persist import flush_to_persist_safe

        flush_to_persist_safe()
    except Exception:
        pass
    return RedirectResponse("/settings?flash=Your+password+was+updated", status_code=303)


def _can_access_brief(session_user, brief: Brief) -> bool:
    from app.services.ownership import can_access_brief

    return can_access_brief(session_user, brief)


@api.get("/briefs/{brief_id}", response_class=HTMLResponse)
def brief_page(brief_id: int, request: Request, db: Session = Depends(get_db)):
    session_user = get_session_user(request, db)
    if not session_user:
        return RedirectResponse("/login", status_code=303)
    brief = db.query(Brief).filter(Brief.id == brief_id).one_or_none()
    if not brief or not _can_access_brief(session_user, brief):
        return HTMLResponse("Brief not found", status_code=404)
    return templates.TemplateResponse(
        "brief.html",
        {
            "request": request,
            "brief": brief,
            "brief_html": render_brief_html(brief.markdown or ""),
            "session_user": template_session(session_user),
        },
    )


@api.get("/briefs/{brief_id}/download")
def brief_download(brief_id: int, request: Request, db: Session = Depends(get_db)):
    import re

    session_user = get_session_user(request, db)
    if not session_user:
        return RedirectResponse("/login", status_code=303)
    brief = db.query(Brief).filter(Brief.id == brief_id).one_or_none()
    if not brief or not _can_access_brief(session_user, brief):
        return HTMLResponse("Brief not found", status_code=404)
    owner = brief.user.user_id
    stamp = brief.created_at.strftime("%Y%m%d-%H%M") if brief.created_at else brief.brief_date
    safe_user = re.sub(r"[^a-zA-Z0-9._-]+", "_", owner)[:60]
    filename = f"market-pulse-{safe_user}-{stamp}.md"
    return Response(
        content=brief.markdown.encode("utf-8"),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@api.post("/ui/briefs/{brief_id}/rebuild")
def ui_rebuild_brief(brief_id: int, request: Request, db: Session = Depends(get_db)):
    from app.services.briefs import clean_author, create_brief_from_scan

    session_user = get_session_user(request, db)
    if not session_user:
        return RedirectResponse("/login", status_code=303)
    brief = db.query(Brief).filter(Brief.id == brief_id).one_or_none()
    if not brief or not brief.scan_id or not _can_access_brief(session_user, brief):
        return RedirectResponse("/?flash=Brief+not+found&err=1", status_code=303)
    scan = db.query(Scan).filter(Scan.id == brief.scan_id).one_or_none()
    if not scan:
        return RedirectResponse("/?flash=Scan+missing&err=1", status_code=303)
    for p in scan.posts:
        p.author = clean_author(p.author)
    db.commit()
    tz_name, offset = _tz_from_request(request)
    db.delete(brief)
    db.commit()
    new_brief = create_brief_from_scan(db, scan, tz_name=tz_name, utc_offset_minutes=offset)
    if new_brief is None:
        return RedirectResponse("/?flash=Cannot+rebuild+-+scan+has+0+posts&err=1", status_code=303)
    return RedirectResponse(f"/briefs/{new_brief.id}", status_code=303)


@api.post("/ui/briefs/{brief_id}/delete")
def ui_delete_brief(
    brief_id: int,
    request: Request,
    confirm: str = Form(""),
    db: Session = Depends(get_db),
):
    session_user = get_session_user(request, db)
    if not session_user:
        return RedirectResponse("/login", status_code=303)
    if confirm != "DELETE":
        return RedirectResponse("/?flash=Delete+cancelled&err=1", status_code=303)
    brief = db.query(Brief).filter(Brief.id == brief_id).one_or_none()
    if not brief or not _can_access_brief(session_user, brief):
        return RedirectResponse("/?flash=Brief+not+found&err=1", status_code=303)
    owner = brief.user.user_id
    db.delete(brief)
    db.commit()
    return RedirectResponse(f"/?user={quote(owner)}&flash=Brief+deleted", status_code=303)


async def _pipeline_background(
    user_id: str,
    tz_name: str | None = None,
    utc_offset_minutes: int | None = None,
) -> None:
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        await run_linkedin_pipeline(
            db,
            user_id=user_id,
            tz_name=tz_name,
            utc_offset_minutes=utc_offset_minutes,
        )
    except Exception:
        logging.getLogger("market_pulse").exception("background scan failed for %s", user_id)
    finally:
        db.close()


@api.post("/ui/connect-linkedin")
async def ui_connect_linkedin(
    request: Request,
    user_id: str = Form(...),
    db: Session = Depends(get_db),
):
    """One-click LinkedIn connect: opens remote login and auto-saves cookies."""
    import asyncio
    from datetime import datetime, timezone

    session_user = get_session_user(request, db)
    if not session_user:
        return RedirectResponse("/login", status_code=303)
    uid = user_id.strip().lower()
    if "@" not in uid:
        return RedirectResponse("/?flash=Enter+a+LinkedIn+email&err=1", status_code=303)
    from app.services.ownership import claim_or_reject_linkedin_email
    from app.services.scroller_client import get_linkedin_scan, start_linkedin_login
    from app.services.briefs import ensure_user, ensure_source, create_scan

    _li, err = claim_or_reject_linkedin_email(db, session_user, uid)
    if err:
        return RedirectResponse(f"/?flash={quote(err)}&err=1", status_code=303)

    try:
        started = await start_linkedin_login(user_id=uid, login_wait_seconds=600)
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(
            f"/?flash={quote(f'Connect failed: {exc}')}&err=1",
            status_code=303,
        )

    if started.get("status") == "completed" and not started.get("jobId"):
        return RedirectResponse(
            f"/?user={quote(uid)}&flash={quote('LinkedIn already connected')}",
            status_code=303,
        )

    job_id = started.get("jobId")
    # Wait briefly for remote login URL so the UI can show Sign in immediately
    if job_id and not started.get("loginUrl"):
        for _ in range(8):
            await asyncio.sleep(1.5)
            try:
                st = await get_linkedin_scan(job_id)
            except Exception:
                continue
            if st.get("loginUrl"):
                started["loginUrl"] = st["loginUrl"]
                started["status"] = st.get("status") or "awaiting_login"
                started["message"] = st.get("message") or started.get("message")
                break
            if (st.get("status") or "").lower() in {"completed", "failed"}:
                started.update(st)
                break

    user = ensure_user(db, uid, owner_pk=session_user.user_pk)
    source = ensure_source(db, "linkedin")
    scan = create_scan(db, user=user, source=source)
    scan.status = "awaiting_login" if started.get("loginUrl") or started.get("jobId") else "running"
    scan.external_job_id = started.get("jobId")
    scan.login_url = started.get("loginUrl")
    scan.message = started.get("message") or "Connect LinkedIn — open the sign-in link"
    db.commit()

    job_id = started.get("jobId")
    if job_id:

        async def _watch() -> None:
            from app.db import SessionLocal

            elapsed = 0
            while elapsed < 700:
                await asyncio.sleep(5)
                elapsed += 5
                try:
                    st = await get_linkedin_scan(job_id)
                except Exception:
                    continue
                status = (st.get("status") or "").lower()
                db2 = SessionLocal()
                try:
                    row = db2.query(Scan).filter(Scan.id == scan.id).one_or_none()
                    if not row:
                        return
                    if st.get("loginUrl"):
                        row.login_url = st["loginUrl"]
                        row.status = "awaiting_login"
                    row.message = st.get("message") or row.message
                    if status == "completed":
                        row.status = "completed"
                        row.login_url = None
                        row.message = st.get("message") or "LinkedIn session saved"
                        row.completed_at = datetime.now(timezone.utc)
                        db2.commit()
                        return
                    if status == "failed":
                        row.status = "failed"
                        row.error = st.get("error") or "Connect failed"
                        row.completed_at = datetime.now(timezone.utc)
                        db2.commit()
                        return
                    db2.commit()
                finally:
                    db2.close()

        asyncio.create_task(_watch())

    resp = RedirectResponse(
        f"/?user={quote(uid)}&flash={quote('Open the Sign in link below — session saves automatically')}",
        status_code=303,
    )
    resp.set_cookie("mp_user", uid, max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax")
    return resp


@api.post("/ui/run-scan")
async def ui_run_scan(
    request: Request,
    user_id: str = Form(...),
    tz_name: str = Form(""),
    utc_offset_minutes: str = Form(""),
    db: Session = Depends(get_db),
):
    import asyncio

    session_user = get_session_user(request, db)
    if not session_user:
        return RedirectResponse("/login", status_code=303)
    status = get_public_status()
    if not status["scrollerTokenSet"]:
        return RedirectResponse(
            "/?flash=Scroller+token+missing+on+server&err=1",
            status_code=303,
        )
    uid = user_id.strip().lower()
    if "@" not in uid:
        return RedirectResponse("/?flash=Enter+a+LinkedIn+email&err=1", status_code=303)
    from app.services.ownership import claim_or_reject_linkedin_email

    _li, err = claim_or_reject_linkedin_email(db, session_user, uid)
    if err:
        return RedirectResponse(f"/?flash={quote(err)}&err=1", status_code=303)
    offset: int | None = None
    try:
        if utc_offset_minutes.strip() != "":
            offset = int(utc_offset_minutes)
    except ValueError:
        offset = None
    tz = tz_name.strip() or None
    asyncio.create_task(_pipeline_background(uid, tz, offset))
    resp = RedirectResponse(f"/?user={quote(uid)}&flash=Scan+started", status_code=303)
    resp.set_cookie("mp_user", uid, max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax")
    if tz:
        resp.set_cookie("mp_tz", tz, max_age=60 * 60 * 24 * 365, httponly=False, samesite="lax")
    if offset is not None:
        resp.set_cookie("mp_utc_offset", str(offset), max_age=60 * 60 * 24 * 365, httponly=False, samesite="lax")
    return resp


@api.post("/ui/switch-user")
def ui_switch_user(request: Request, user_id: str = Form(...), db: Session = Depends(get_db)):
    session_user = get_session_user(request, db)
    if not session_user:
        return RedirectResponse("/login", status_code=303)
    uid = user_id.strip().lower()
    if "@" not in uid:
        return RedirectResponse("/?flash=Enter+a+LinkedIn+email&err=1", status_code=303)
    from app.services.ownership import claim_or_reject_linkedin_email, can_access_linkedin_user

    target = db.query(User).filter(User.user_id == uid).one_or_none()
    if target and not can_access_linkedin_user(db, session_user, target):
        return RedirectResponse("/?flash=That+LinkedIn+identity+belongs+to+another+user&err=1", status_code=303)
    if not target:
        _li, err = claim_or_reject_linkedin_email(db, session_user, uid)
        if err:
            return RedirectResponse(f"/?flash={quote(err)}&err=1", status_code=303)
    resp = RedirectResponse(f"/?user={quote(uid)}", status_code=303)
    resp.set_cookie("mp_user", uid, max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax")
    return resp


@api.post("/ui/save-linkedin-session")
async def ui_save_linkedin_session(
    request: Request,
    linkedin_email: str = Form(...),
    li_at: str = Form(...),
    li_a: str = Form(""),
    db: Session = Depends(get_db),
):
    """Paste li_at cookie so cloud scans skip VNC sign-in."""
    session_user = get_session_user(request, db)
    if not session_user:
        return RedirectResponse("/login", status_code=303)
    uid = linkedin_email.strip().lower()
    if "@" not in uid:
        return RedirectResponse("/settings?flash=Enter+a+LinkedIn+email&err=1", status_code=303)
    from app.services.ownership import claim_or_reject_linkedin_email
    from app.services.scroller_client import save_linkedin_session

    _li, err = claim_or_reject_linkedin_email(db, session_user, uid)
    if err:
        return RedirectResponse(f"/settings?flash={quote(err)}&err=1", status_code=303)
    cookie = (li_at or "").strip()
    if len(cookie) < 20:
        return RedirectResponse(
            "/settings?flash=Paste+the+full+li_at+cookie+value&err=1",
            status_code=303,
        )
    try:
        result = await save_linkedin_session(
            user_id=uid,
            li_at=cookie,
            li_a=(li_a or "").strip() or None,
        )
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(
            f"/settings?flash={quote(f'Session save failed: {exc}')}&err=1",
            status_code=303,
        )
    if not result.get("ok"):
        return RedirectResponse(
            f"/settings?flash={quote(result.get('error') or 'Save failed')}&err=1",
            status_code=303,
        )
    return RedirectResponse(
        f"/settings?flash={quote('LinkedIn session saved — Scan should skip VNC')}",
        status_code=303,
    )


@api.get("/api/integration/v1/briefs")
def api_list_briefs(userId: str | None = None, limit: int = 20, db: Session = Depends(get_db)):
    q = db.query(Brief).order_by(Brief.created_at.desc())
    if userId:
        u = db.query(User).filter(User.user_id == userId.strip().lower()).one_or_none()
        if not u:
            return {"count": 0, "briefs": []}
        q = q.filter(Brief.user_pk == u.id)
    rows = q.limit(max(1, min(limit, 100))).all()
    return {
        "count": len(rows),
        "briefs": [
            {
                "id": b.id,
                "userId": b.user.user_id,
                "briefDate": b.brief_date,
                "title": b.title,
                "createdAt": b.created_at.isoformat() if b.created_at else None,
            }
            for b in rows
        ],
    }


@api.get("/api/integration/v1/briefs/{brief_id}")
def api_get_brief(brief_id: int, db: Session = Depends(get_db)):
    b = db.query(Brief).filter(Brief.id == brief_id).one_or_none()
    if not b:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {
        "id": b.id,
        "userId": b.user.user_id,
        "briefDate": b.brief_date,
        "title": b.title,
        "markdown": b.markdown,
        "scanId": b.scan_id,
        "createdAt": b.created_at.isoformat() if b.created_at else None,
    }


@api.post("/api/integration/v1/users")
def api_register_user(body: dict, db: Session = Depends(get_db)):
    """MCP/API can register LinkedIn identity rows but cannot grant UI login without password."""
    uid = (body.get("userId") or "").strip().lower()
    if not uid:
        return JSONResponse({"error": "userId required"}, status_code=400)
    u = ensure_user(db, uid, body.get("displayName"))
    return {"ok": True, "userId": u.user_id, "id": u.id, "note": "UI login still requires admin invite"}


@api.post("/api/integration/v1/scans")
async def api_start_scan(body: dict, db: Session = Depends(get_db)):
    uid = (body.get("userId") or "").strip()
    if not uid:
        return JSONResponse({"error": "userId required"}, status_code=400)
    return await run_linkedin_pipeline(
        db,
        user_id=uid,
        max_posts=int(body.get("maxPosts") or 60),
        max_scrolls=int(body.get("maxScrolls") or 20),
    )


@api.get("/api/integration/v1/scans/{scan_id}")
def api_get_scan(scan_id: int, db: Session = Depends(get_db)):
    s = db.query(Scan).filter(Scan.id == scan_id).one_or_none()
    if not s:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {
        "id": s.id,
        "userId": s.user.user_id,
        "status": s.status,
        "postCount": s.post_count,
        "loginUrl": s.login_url,
        "error": s.error,
    }


class IntegrationAuthMiddleware:
    """Require API key on /api/integration/* and /mcp."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        path = scope.get("path") or ""
        if path.startswith("/api/integration") or path.startswith("/mcp"):
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            try:
                require_api_key(
                    authorization=headers.get("authorization"),
                    x_integration_key=headers.get("x-integration-key"),
                    x_api_key=headers.get("x-api-key"),
                )
            except Exception as exc:
                from fastapi import HTTPException

                if isinstance(exc, HTTPException):
                    resp = Response(exc.detail, status_code=exc.status_code)
                    await resp(scope, receive, send)
                    return
                raise
        await self.app(scope, receive, send)


def build_app() -> ASGIApp:
    mcp_asgi = mcp.streamable_http_app()
    combined = Starlette(
        routes=[
            Mount("/mcp", app=mcp_asgi),
            Mount("/", app=api),
        ],
        lifespan=api.router.lifespan_context,
    )
    # Session cookie for UI login (httponly). HTTPS on Azure → secure cookies.
    secure = os.getenv("SESSION_SECURE", "true").lower() in {"1", "true", "yes"}
    sessioned = SessionMiddleware(
        combined,
        secret_key=secret_key(),
        session_cookie="mp_session",
        same_site="lax",
        https_only=secure,
        max_age=60 * 60 * 24 * 90,
    )
    return IntegrationAuthMiddleware(sessioned)


app = build_app()
