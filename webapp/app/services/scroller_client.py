"""Call LinkedIn feed scroller MCP (Azure or local)."""
from __future__ import annotations

import json
import os
import uuid
from typing import Any

import httpx

from app.services.settings_store import apply_to_environ

DEFAULT_SCROLLER_MCP_URL = (
    "https://linkedin-feed-scroller.whitesand-f9361ec3.eastus2.azurecontainerapps.io/mcp"
)


def _mcp_url() -> str:
    apply_to_environ()
    return (
        os.getenv("SCROLLER_MCP_URL", DEFAULT_SCROLLER_MCP_URL).strip() or DEFAULT_SCROLLER_MCP_URL
    ).rstrip("/")


def _bearer() -> str:
    apply_to_environ()
    return os.getenv("SCROLLER_BEARER_TOKEN", os.getenv("MCP_BEARER_TOKEN", "")).strip()


def _headers() -> dict[str, str]:
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    token = _bearer()
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


async def _mcp_call(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Minimal Streamable HTTP JSON-RPC tools/call against FastMCP."""
    url = _mcp_url()
    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        init = {
            "jsonrpc": "2.0",
            "id": "init",
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "market-pulse-webapp", "version": "0.1.0"},
            },
        }
        r0 = await client.post(url, headers=_headers(), json=init)
        session = r0.headers.get("mcp-session-id") or r0.headers.get("Mcp-Session-Id")
        headers = _headers()
        if session:
            headers["mcp-session-id"] = session
        await client.post(
            url,
            headers=headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        r = await client.post(url, headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()
    if "error" in data:
        raise RuntimeError(data["error"])
    result = data.get("result") or {}
    content = result.get("content") or []
    for block in content:
        if block.get("type") == "text":
            text = block.get("text") or ""
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"raw": text}
    if isinstance(result, dict) and "structuredContent" in result:
        return result["structuredContent"]
    return result if isinstance(result, dict) else {"result": result}


async def start_linkedin_scan(
    *,
    user_id: str,
    max_posts: int = 40,
    max_scrolls: int = 30,
    login_wait_seconds: int = 600,
    recent_only: str = "today",
) -> dict[str, Any]:
    args: dict[str, Any] = {
        "userId": user_id,
        "maxPosts": max_posts,
        "maxScrolls": max_scrolls,
        "loginWaitSeconds": login_wait_seconds,
        "recentOnly": recent_only,
    }
    return await _mcp_call("start_linkedin_feed_scan", args)


async def get_linkedin_scan(job_id: str) -> dict[str, Any]:
    return await _mcp_call("get_linkedin_feed_scan", {"jobId": job_id})


async def save_linkedin_session(
    *,
    user_id: str,
    li_at: str,
    li_a: str | None = None,
) -> dict[str, Any]:
    args: dict[str, Any] = {"userId": user_id, "liAt": li_at}
    if li_a:
        args["liA"] = li_a
    return await _mcp_call("save_linkedin_session", args)


async def get_linkedin_session_status(user_id: str) -> dict[str, Any]:
    return await _mcp_call("get_linkedin_session_status", {"userId": user_id})


async def start_linkedin_login(
    *,
    user_id: str,
    login_wait_seconds: int = 600,
) -> dict[str, Any]:
    return await _mcp_call(
        "start_linkedin_login",
        {"userId": user_id, "loginWaitSeconds": login_wait_seconds},
    )
