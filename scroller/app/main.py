from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .linkedin_feed import scan_feed
from .models import HealthResponse, ScanRequest, ScanResponse

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent


def _default_profiles_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "LinkedInMarketPulse" / "profiles"
    return ROOT / ".profiles"


PROFILES_DIR = Path(os.getenv("PROFILES_DIR", str(_default_profiles_dir())))
if not PROFILES_DIR.is_absolute():
    PROFILES_DIR = ROOT / PROFILES_DIR

DEFAULT_MAX_POSTS = int(os.getenv("DEFAULT_MAX_POSTS", "50"))
DEFAULT_MAX_SCROLLS = int(os.getenv("DEFAULT_MAX_SCROLLS", "15"))
DEFAULT_HEADED = os.getenv("DEFAULT_HEADED", "true").lower() in {"1", "true", "yes"}
API_KEY = os.getenv("API_KEY", "").strip()

app = FastAPI(
    title="LinkedIn Feed Scroller",
    description="Background browser worker for nebulaONE LinkedIn Market Pulse. Scrolls the home feed and returns structured posts.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _check_api_key(x_api_key: str | None) -> None:
    if not API_KEY:
        return
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Api-Key")


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.post("/scan", response_model=ScanResponse)
async def scan(
    body: ScanRequest,
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
) -> ScanResponse:
    _check_api_key(x_api_key)
    user_id = body.userId.strip().lower().replace(" ", "-")
    if not user_id:
        raise HTTPException(status_code=400, detail="userId is required")

    headed = DEFAULT_HEADED if body.headed is None else body.headed
    max_posts = body.maxPosts or DEFAULT_MAX_POSTS
    max_scrolls = body.maxScrolls or DEFAULT_MAX_SCROLLS

    try:
        return await scan_feed(
            user_id=user_id,
            profiles_dir=PROFILES_DIR,
            max_posts=max_posts,
            max_scrolls=max_scrolls,
            headed=headed,
            feed_url=body.feedUrl,
            login_wait_seconds=body.loginWaitSeconds,
        )
    except Exception as exc:
        return ScanResponse(
            userId=user_id,
            scannedAt=datetime.now(timezone.utc),
            postCount=0,
            loginRequired=False,
            message=f"Scan failed: {exc}",
            posts=[],
        )
