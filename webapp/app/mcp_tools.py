"""MCP tools for nebulaONE (Streamable HTTP) — same API key as REST."""
from __future__ import annotations

import os
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from app.db import SessionLocal, init_db
from app.models import Brief, Scan, User
from app.services.briefs import ensure_user
from app.services.pipeline import run_linkedin_pipeline

mcp = FastMCP(
    "market-pulse",
    instructions=(
        "Market Pulse webapp: daily social/market feed intelligence. "
        "Prefer list_briefs / get_brief for reports. "
        "Use start_market_pulse_scan to trigger a LinkedIn feed scrape+brief for a userId. "
        "register_user adds someone to the daily schedule."
    ),
    host="0.0.0.0",
    port=int(os.getenv("WEBAPP_PORT", "8790")),
    streamable_http_path="/",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@mcp.tool()
async def register_user(userId: str, displayName: str | None = None) -> dict[str, Any]:
    """Register a user for daily Market Pulse scans (per LinkedIn identity)."""
    init_db()
    db = SessionLocal()
    try:
        u = ensure_user(db, userId, displayName)
        return {"ok": True, "userId": u.user_id, "id": u.id}
    finally:
        db.close()


@mcp.tool()
async def start_market_pulse_scan(
    userId: str,
    maxPosts: int = 60,
    maxScrolls: int = 20,
) -> dict[str, Any]:
    """Scrape LinkedIn home feed for userId, store posts in DB, generate a dated brief."""
    init_db()
    db = SessionLocal()
    try:
        return await run_linkedin_pipeline(
            db,
            user_id=userId,
            max_posts=maxPosts,
            max_scrolls=maxScrolls,
        )
    finally:
        db.close()


@mcp.tool()
async def list_briefs(userId: str | None = None, limit: int = 20) -> dict[str, Any]:
    """List recent Market Pulse briefs (dated reports). Optionally filter by userId."""
    init_db()
    db = SessionLocal()
    try:
        q = db.query(Brief).order_by(Brief.created_at.desc())
        if userId:
            u = db.query(User).filter(User.user_id == userId.strip().lower()).one_or_none()
            if not u:
                return {"briefs": [], "count": 0}
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
                    "status": b.status,
                    "createdAt": b.created_at.isoformat() if b.created_at else None,
                }
                for b in rows
            ],
        }
    finally:
        db.close()


@mcp.tool()
async def get_brief(briefId: int) -> dict[str, Any]:
    """Fetch one brief including full markdown report body."""
    init_db()
    db = SessionLocal()
    try:
        b = db.query(Brief).filter(Brief.id == briefId).one_or_none()
        if not b:
            return {"error": "brief not found"}
        return {
            "id": b.id,
            "userId": b.user.user_id,
            "briefDate": b.brief_date,
            "title": b.title,
            "status": b.status,
            "markdown": b.markdown,
            "scanId": b.scan_id,
            "createdAt": b.created_at.isoformat() if b.created_at else None,
        }
    finally:
        db.close()


@mcp.tool()
async def get_scan(scanId: int) -> dict[str, Any]:
    """Get scan status / loginUrl / post counts."""
    init_db()
    db = SessionLocal()
    try:
        s = db.query(Scan).filter(Scan.id == scanId).one_or_none()
        if not s:
            return {"error": "scan not found"}
        return {
            "id": s.id,
            "userId": s.user.user_id,
            "status": s.status,
            "postCount": s.post_count,
            "loginUrl": s.login_url,
            "error": s.error,
            "externalJobId": s.external_job_id,
            "message": s.message,
        }
    finally:
        db.close()
