"""
MCP server for LinkedIn feed scroller (Streamable HTTP).

Cloud remote login: when LinkedIn requires auth, the job enters status
`awaiting_login` and exposes `loginUrl` (noVNC). User opens that link from
nebulaONE chat, signs in, then the scan continues — no local MCP required.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import websockets
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocket

from app.linkedin_feed import scan_feed
from app.remote_display import (
    build_login_url,
    display_ready,
    get_active_display,
    novnc_static_root,
    remote_login_available,
    session_summary,
    start_remote_display,
    stop_remote_display,
)

load_dotenv()

ROOT = Path(__file__).resolve().parent
PROFILES_DIR = Path(os.getenv("PROFILES_DIR", "/data/profiles"))
if not PROFILES_DIR.is_absolute():
    PROFILES_DIR = ROOT / PROFILES_DIR
PROFILES_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_MAX_POSTS = int(os.getenv("DEFAULT_MAX_POSTS", "60"))
DEFAULT_MAX_SCROLLS = int(os.getenv("DEFAULT_MAX_SCROLLS", "20"))
DEFAULT_HEADED = os.getenv("DEFAULT_HEADED", "false").lower() in {"1", "true", "yes"}
DEFAULT_LOGIN_WAIT = int(os.getenv("DEFAULT_LOGIN_WAIT_SECONDS", "600"))
MCP_BEARER_TOKEN = os.getenv("MCP_BEARER_TOKEN", "").strip()
API_KEY = os.getenv("API_KEY", "").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
REMOTE_LOGIN_ENABLED = os.getenv("REMOTE_LOGIN_ENABLED", "true").lower() in {"1", "true", "yes"}

_JOBS: dict[str, dict[str, Any]] = {}
_JOBS_LOCK = asyncio.Lock()
_LOGIN_TOKENS: dict[str, str] = {}  # token -> job_id
_MAIN_LOOP: asyncio.AbstractEventLoop | None = None

INSTRUCTIONS = """
LinkedIn home-feed scroller (read-only) for cloud agents (e.g. nebulaONE).

Preferred workflow:
1) start_linkedin_feed_scan with the user's stable userId
2) get_linkedin_feed_scan with jobId until status is completed or failed
3) Use posts JSON only as evidence — do not invent posts

If status is awaiting_login:
- Give the user the full loginUrl exactly (must include /login/vnc.html — do not rewrite or shorten it).
- Tell them to open it and sign into LinkedIn (and 2FA).
- Keep polling get_linkedin_feed_scan. Do NOT say a browser will open on their PC —
  the loginUrl is a remote Chromium session in the cloud.
- After they finish login, the job continues scrolling automatically.

Never post, comment, like, or DM on LinkedIn.
"""

mcp = FastMCP(
    "linkedin-feed-scroller",
    instructions=INSTRUCTIONS.strip(),
    host="0.0.0.0",
    port=8000,
    streamable_http_path="/mcp",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _public_base() -> str:
    return PUBLIC_BASE_URL


async def _run_scan(
    *,
    user_id: str,
    max_posts: int,
    max_scrolls: int,
    headed: bool,
    feed_url: str,
    login_wait_seconds: int,
    on_awaiting_login: Any = None,
) -> dict[str, Any]:
    result = await scan_feed(
        user_id=user_id,
        profiles_dir=PROFILES_DIR,
        max_posts=max_posts,
        max_scrolls=max_scrolls,
        headed=headed,
        feed_url=feed_url,
        login_wait_seconds=login_wait_seconds,
        on_awaiting_login=on_awaiting_login,
    )
    return result.model_dump(mode="json")


async def _set_job(job_id: str, **fields: Any) -> None:
    async with _JOBS_LOCK:
        if job_id in _JOBS:
            _JOBS[job_id].update(fields)


async def _job_worker(job_id: str, kwargs: dict[str, Any]) -> None:
    global _MAIN_LOOP
    _MAIN_LOOP = asyncio.get_running_loop()
    await _set_job(
        job_id,
        status="running",
        startedAt=datetime.now(timezone.utc).isoformat(),
    )
    login_token: str | None = None
    try:
        # Fast path: headless with existing profile
        payload = await _run_scan(**{**kwargs, "headed": False, "login_wait_seconds": 30})
        if not payload.get("loginRequired"):
            await _set_job(
                job_id,
                status="completed",
                result=payload,
                completedAt=datetime.now(timezone.utc).isoformat(),
            )
            return

        # Need interactive LinkedIn login via remote Chromium (noVNC)
        if not REMOTE_LOGIN_ENABLED or not remote_login_available():
            await _set_job(
                job_id,
                status="completed",
                result={
                    **payload,
                    "message": (
                        "LinkedIn login required, but remote login is not available on this host. "
                        "Enable Xvfb/x11vnc/websockify in the container image."
                    ),
                },
                completedAt=datetime.now(timezone.utc).isoformat(),
            )
            return

        base = _public_base()
        if not base:
            await _set_job(
                job_id,
                status="failed",
                error="PUBLIC_BASE_URL is not set; cannot build loginUrl for remote LinkedIn login.",
                completedAt=datetime.now(timezone.utc).isoformat(),
            )
            return

        start_remote_display()
        os.environ["DISPLAY"] = ":99"
        login_token = secrets.token_urlsafe(24)
        _LOGIN_TOKENS[login_token] = job_id
        login_url = build_login_url(base, login_token)

        def _mark_awaiting() -> None:
            loop = _MAIN_LOOP
            if loop is None:
                return

            async def _upd() -> None:
                await _set_job(
                    job_id,
                    status="awaiting_login",
                    loginUrl=login_url,
                    loginRequired=True,
                    message=(
                        "Open loginUrl in your browser, sign into LinkedIn (and 2FA), "
                        "then keep this chat open — the scan continues automatically."
                    ),
                    poll_after_seconds=10,
                )

            asyncio.run_coroutine_threadsafe(_upd(), loop)

        # Pre-set awaiting_login so the agent can show the link immediately
        await _set_job(
            job_id,
            status="awaiting_login",
            loginUrl=login_url,
            loginRequired=True,
            message=(
                "Open loginUrl, sign into LinkedIn (and 2FA). "
                "Keep polling — after login the feed scan continues automatically."
            ),
            poll_after_seconds=10,
        )

        headed_kwargs = {
            **kwargs,
            "headed": True,
            "login_wait_seconds": max(kwargs.get("login_wait_seconds") or DEFAULT_LOGIN_WAIT, 300),
            "on_awaiting_login": _mark_awaiting,
        }
        payload = await _run_scan(**headed_kwargs)
        await _set_job(
            job_id,
            status="completed",
            result=payload,
            loginUrl=None,
            loginRequired=bool(payload.get("loginRequired")),
            completedAt=datetime.now(timezone.utc).isoformat(),
        )
    except Exception as exc:  # noqa: BLE001
        await _set_job(
            job_id,
            status="failed",
            error=str(exc),
            completedAt=datetime.now(timezone.utc).isoformat(),
        )
    finally:
        if login_token:
            _LOGIN_TOKENS.pop(login_token, None)
        stop_remote_display()
        os.environ.pop("DISPLAY", None)


@mcp.tool()
async def start_linkedin_feed_scan(
    userId: str,
    maxPosts: int = 60,
    maxScrolls: int = 20,
    headed: bool | None = None,
    feedUrl: str = "https://www.linkedin.com/feed/",
    loginWaitSeconds: int = 600,
) -> dict:
    """Start a background LinkedIn home-feed scroll. Poll with get_linkedin_feed_scan.

    If LinkedIn login is required, status becomes awaiting_login and loginUrl is set.
    Tell the user to open loginUrl and sign in; keep polling until completed/failed.

    Args:
        userId: Stable per-person id (e.g. email). Each person has their own LinkedIn session.
        maxPosts: Max posts to capture (5-200).
        maxScrolls: Scroll steps.
        headed: Ignored in cloud — remote login is used automatically when needed.
        feedUrl: LinkedIn feed URL.
        loginWaitSeconds: How long to wait for the user to finish remote login.
    """
    uid = userId.strip().lower().replace(" ", "-")
    if not uid:
        return {"status": "failed", "error": "userId is required"}

    job_id = str(uuid.uuid4())
    kwargs = {
        "user_id": uid,
        "max_posts": max(5, min(maxPosts or DEFAULT_MAX_POSTS, 200)),
        "max_scrolls": max(1, min(maxScrolls or DEFAULT_MAX_SCROLLS, 80)),
        "headed": DEFAULT_HEADED if headed is None else headed,
        "feed_url": feedUrl,
        "login_wait_seconds": loginWaitSeconds or DEFAULT_LOGIN_WAIT,
    }
    async with _JOBS_LOCK:
        _JOBS[job_id] = {
            "jobId": job_id,
            "userId": uid,
            "status": "queued",
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "poll_after_seconds": 10,
            "remoteLoginAvailable": remote_login_available() and REMOTE_LOGIN_ENABLED,
        }
    asyncio.create_task(_job_worker(job_id, kwargs))
    return {
        "jobId": job_id,
        "status": "queued",
        "userId": uid,
        "poll_after_seconds": 10,
        "next_tool": "get_linkedin_feed_scan",
        "note": (
            "If status becomes awaiting_login, show the user loginUrl so they can "
            "sign into LinkedIn in the remote browser, then keep polling."
        ),
    }


@mcp.tool()
async def get_linkedin_feed_scan(jobId: str) -> dict:
    """Poll a feed scan. Status may be queued|running|awaiting_login|completed|failed.

    Args:
        jobId: Job id from start_linkedin_feed_scan.
    """
    async with _JOBS_LOCK:
        job = _JOBS.get(jobId)
        if not job:
            return {"status": "failed", "error": f"Unknown jobId: {jobId}"}
        return dict(job)


@mcp.tool()
async def run_linkedin_feed_scan(
    userId: str,
    maxPosts: int = 40,
    maxScrolls: int = 12,
    headed: bool | None = None,
    feedUrl: str = "https://www.linkedin.com/feed/",
    loginWaitSeconds: int = 120,
) -> dict:
    """Synchronous scan — prefer start/get tools. Does not open remote login UI."""
    uid = userId.strip().lower().replace(" ", "-")
    if not uid:
        return {"error": "userId is required", "postCount": 0, "posts": []}
    return await _run_scan(
        user_id=uid,
        max_posts=max(5, min(maxPosts or DEFAULT_MAX_POSTS, 200)),
        max_scrolls=max(1, min(maxScrolls or DEFAULT_MAX_SCROLLS, 80)),
        headed=DEFAULT_HEADED if headed is None else headed,
        feed_url=feedUrl,
        login_wait_seconds=loginWaitSeconds,
    )


def _token_ok(token: str | None) -> bool:
    return bool(token and token in _LOGIN_TOKENS)


def _token_from_request(request: Request) -> str | None:
    return request.query_params.get("token") or request.query_params.get("access_token")


class BearerAuthMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        path = scope.get("path") or ""
        # Public health + remote login UI assets + websocket (token checked in WS handler)
        if (
            path in {"/health", "/healthz", "/", "/vnc.html"}
            or path.startswith("/login")
            or path.startswith("/websockify")
        ):
            await self.app(scope, receive, send)
            return

        if scope["type"] == "websocket":
            # MCP websockets still need auth if configured
            pass

        if not MCP_BEARER_TOKEN and not API_KEY:
            await self.app(scope, receive, send)
            return

        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        auth = headers.get("authorization", "")
        x_key = headers.get("x-api-key", "")
        ok = False
        if MCP_BEARER_TOKEN and auth == f"Bearer {MCP_BEARER_TOKEN}":
            ok = True
        if API_KEY and (x_key == API_KEY or auth == f"Bearer {API_KEY}"):
            ok = True
        if not ok:
            if scope["type"] == "websocket":
                # reject by not accepting — return HTTP-ish close via Response not available;
                # Starlette will just fail; send 403 response for HTTP only
                await self.app(scope, receive, send)
                return
            response = Response("Unauthorized", status_code=401)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


async def healthz(_: Request) -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "service": "linkedin-feed-scroller-mcp",
            "remoteLoginAvailable": remote_login_available() and REMOTE_LOGIN_ENABLED,
            "publicBaseUrlConfigured": bool(PUBLIC_BASE_URL),
            "remoteDisplay": session_summary(),
        }
    )


async def redirect_vnc_html(request: Request) -> RedirectResponse:
    """Agents sometimes strip /login/ — keep query string and send to the real UI."""
    q = request.url.query
    dest = "/login/vnc.html" + (f"?{q}" if q else "?autoconnect=true&resize=scale")
    return RedirectResponse(dest, status_code=302)


async def serve_login_static(request: Request) -> Response:
    """Serve noVNC files from disk. UI is public; VNC websocket is token-gated."""
    root = novnc_static_root()
    if root is None or not root.is_dir():
        return Response("noVNC static files not installed in this image", status_code=500)

    rel = (request.path_params.get("path") or "vnc.html").lstrip("/")
    if not rel or rel.endswith("/"):
        rel = (rel + "vnc.html") if rel else "vnc.html"

    # Prevent path traversal
    target = (root / rel).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        return Response("Not found", status_code=404)
    if not target.is_file():
        return Response("Not found", status_code=404)

    response = FileResponse(target)
    token = _token_from_request(request)
    if token and _token_ok(token) and target.name.endswith(".html"):
        response.set_cookie(
            "linkedin_login_token",
            token,
            max_age=3600,
            httponly=True,
            samesite="lax",
            secure=True,
            path="/",
        )
    return response


async def websockify_proxy(websocket: WebSocket) -> None:
    token = websocket.query_params.get("token") or websocket.query_params.get("access_token")
    if not token:
        raw_q = (websocket.scope.get("query_string") or b"").decode()
        token = parse_qs(raw_q).get("token", [None])[0]
    if not token:
        cookie_header = ""
        for k, v in websocket.scope.get("headers", []):
            if k.decode().lower() == "cookie":
                cookie_header = v.decode()
                break
        for part in cookie_header.split(";"):
            part = part.strip()
            if part.startswith("linkedin_login_token="):
                token = part.split("=", 1)[1]
                break

    if not _token_ok(token):
        print("[vnc-proxy] rejecting: invalid/expired login token", flush=True)
        await websocket.close(code=4401)
        return

    if not display_ready():
        summary = session_summary()
        print(f"[vnc-proxy] rejecting: remote display not ready {summary}", flush=True)
        await websocket.close(code=1013)
        return

    display = get_active_display()
    assert display is not None
    upstream_url = f"ws://127.0.0.1:{display.ws_port}/"

    # Connect upstream first so we don't accept then instantly drop (noVNC: Failed to connect).
    try:
        upstream = await websockets.connect(
            upstream_url,
            max_size=8 * 1024 * 1024,
            open_timeout=10,
            ping_interval=None,
            compression=None,
        )
    except Exception as exc:
        print(f"[vnc-proxy] upstream connect failed url={upstream_url} err={exc!r}", flush=True)
        await websocket.close(code=1011)
        return

    await websocket.accept()
    print(f"[vnc-proxy] client connected; bridging to {upstream_url}", flush=True)

    try:

        async def client_to_upstream() -> None:
            while True:
                msg = await websocket.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if msg.get("bytes") is not None:
                    await upstream.send(msg["bytes"])
                elif msg.get("text") is not None:
                    await upstream.send(msg["text"])

        async def upstream_to_client() -> None:
            async for message in upstream:
                if isinstance(message, bytes):
                    await websocket.send_bytes(message)
                else:
                    await websocket.send_text(message)

        done, pending = await asyncio.wait(
            [
                asyncio.create_task(client_to_upstream()),
                asyncio.create_task(upstream_to_client()),
            ],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        for task in done:
            exc = task.exception()
            if exc:
                print(f"[vnc-proxy] bridge side ended: {exc!r}", flush=True)
    except Exception as exc:
        print(f"[vnc-proxy] bridge error: {exc!r}", flush=True)
    finally:
        try:
            await upstream.close()
        except Exception:
            pass
        try:
            await websocket.close()
        except Exception:
            pass
        print("[vnc-proxy] bridge closed", flush=True)


def build_app() -> ASGIApp:
    mcp_app = mcp.streamable_http_app()
    routes = [
        Route("/health", endpoint=healthz),
        Route("/healthz", endpoint=healthz),
        Route("/vnc.html", endpoint=redirect_vnc_html, methods=["GET"]),
        Route("/login", endpoint=serve_login_static, methods=["GET"]),
        Route("/login/{path:path}", endpoint=serve_login_static, methods=["GET"]),
        WebSocketRoute("/websockify", endpoint=websockify_proxy),
        *list(mcp_app.routes),
    ]
    combined = Starlette(routes=routes, lifespan=mcp_app.router.lifespan_context)
    return BearerAuthMiddleware(combined)


app = build_app()


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("SCROLLER_HOST", "0.0.0.0")
    port = int(os.getenv("MCP_PORT", "8000"))
    print(
        json.dumps(
            {
                "starting": "mcp",
                "host": host,
                "port": port,
                "path": "/mcp",
                "remoteLoginAvailable": remote_login_available(),
                "publicBaseUrl": PUBLIC_BASE_URL or None,
            }
        )
    )
    uvicorn.run("mcp_server:app", host=host, port=port, factory=False)
