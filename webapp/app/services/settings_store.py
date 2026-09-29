"""Local settings overlay (UI-editable). Secrets stay env-only except INTEGRATION_API_KEY rotate."""
from __future__ import annotations

import json
import os
from typing import Any

from app.db import DATA_DIR

SETTINGS_PATH = DATA_DIR / "local_settings.json"

# Scroller bearer is NEVER written from the UI — Azure env / deploy only.
KEYS = (
    "INTEGRATION_API_KEY",
    "DAILY_SCAN_ENABLED",
    "DAILY_SCAN_HOUR_UTC",
    "DAILY_SCAN_MINUTE_UTC",
    "SCAN_MAX_POSTS",
    "SCAN_MAX_SCROLLS",
    "SCAN_RECENCY",
    "FOCUS_KEYWORDS",
)


def _read_file() -> dict[str, Any]:
    if not SETTINGS_PATH.is_file():
        return {}
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def apply_to_environ() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    data = _read_file()
    for k in KEYS:
        if k in data and data[k] is not None and str(data[k]).strip() != "":
            os.environ[k] = str(data[k]).strip()


def get_scan_options() -> dict[str, Any]:
    apply_to_environ()
    try:
        max_posts = int(os.getenv("SCAN_MAX_POSTS", "40"))
    except ValueError:
        max_posts = 40
    try:
        max_scrolls = int(os.getenv("SCAN_MAX_SCROLLS", "80"))
    except ValueError:
        max_scrolls = 80
    recency = (os.getenv("SCAN_RECENCY", "all") or "all").strip().lower()
    if recency not in {"today", "all"}:
        recency = "all"
    raw_kw = os.getenv("FOCUS_KEYWORDS", "") or ""
    keywords = [k.strip() for k in raw_kw.replace(";", ",").split(",") if k.strip()]
    return {
        "maxPosts": max(5, min(max_posts, 200)),
        "maxScrolls": max(1, min(max_scrolls, 120)),
        "recency": recency,
        "focusKeywords": keywords,
    }


def get_public_status() -> dict[str, Any]:
    """Status safe for templates — never expose raw secrets."""
    apply_to_environ()
    scroll = os.getenv("SCROLLER_BEARER_TOKEN", "").strip()
    opts = get_scan_options()
    from app.db import SessionLocal
    from app.services.api_keys import count_active_keys, has_any_configured_key

    db = SessionLocal()
    try:
        active_keys = count_active_keys(db)
        keys_ok = has_any_configured_key(db)
    finally:
        db.close()
    try:
        from app.services.persist import persist_status

        persist = persist_status()
    except Exception:
        persist = {"persistReady": False, "message": "unavailable"}
    return {
        "integrationKeySet": keys_ok,
        "activeApiKeyCount": active_keys,
        "scrollerTokenSet": bool(scroll),
        "scrollerConfigured": bool(scroll),
        "dailyScanEnabled": os.getenv("DAILY_SCAN_ENABLED", "true").lower() in {"1", "true", "yes"},
        "dailyScanHourUtc": os.getenv("DAILY_SCAN_HOUR_UTC", "13"),
        "dailyScanMinuteUtc": os.getenv("DAILY_SCAN_MINUTE_UTC", "0"),
        "scanMaxPosts": str(opts["maxPosts"]),
        "scanMaxScrolls": str(opts["maxScrolls"]),
        "scanRecency": opts["recency"],
        "focusKeywords": ", ".join(opts["focusKeywords"]),
        "database": "SQLite + Azure Files",
        "persistReady": bool(persist.get("persistReady")),
        "persistRestored": bool(persist.get("restored")),
        "persistBriefs": persist.get("briefs", 0),
        "persistScans": persist.get("scans", 0),
        "persistMessage": persist.get("message", ""),
    }


def set_integration_api_key(key: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    data = _read_file()
    data["INTEGRATION_API_KEY"] = key.strip()
    os.environ["INTEGRATION_API_KEY"] = key.strip()
    SETTINGS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def save_settings(
    *,
    daily_scan_enabled: str | None = None,
    daily_scan_hour_utc: str | None = None,
    daily_scan_minute_utc: str | None = None,
    scan_max_posts: str | None = None,
    scan_max_scrolls: str | None = None,
    scan_recency: str | None = None,
    focus_keywords: str | None = None,
) -> dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    data = _read_file()

    def _set(key: str, value: str | None, *, allow_blank_clear: bool = False) -> None:
        if value is None:
            return
        v = value.strip()
        if v == "" and not allow_blank_clear:
            return
        if v == "" and allow_blank_clear:
            data.pop(key, None)
            os.environ.pop(key, None)
            return
        data[key] = v
        os.environ[key] = v

    if daily_scan_enabled is not None:
        data["DAILY_SCAN_ENABLED"] = "true" if daily_scan_enabled in {"1", "true", "yes", "on"} else "false"
        os.environ["DAILY_SCAN_ENABLED"] = data["DAILY_SCAN_ENABLED"]
    _set("DAILY_SCAN_HOUR_UTC", daily_scan_hour_utc, allow_blank_clear=False)
    _set("DAILY_SCAN_MINUTE_UTC", daily_scan_minute_utc, allow_blank_clear=False)
    _set("SCAN_MAX_POSTS", scan_max_posts, allow_blank_clear=False)
    _set("SCAN_MAX_SCROLLS", scan_max_scrolls, allow_blank_clear=False)
    if scan_recency is not None:
        v = scan_recency.strip().lower()
        if v in {"today", "all"}:
            data["SCAN_RECENCY"] = v
            os.environ["SCAN_RECENCY"] = v
    _set("FOCUS_KEYWORDS", focus_keywords, allow_blank_clear=True)

    SETTINGS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return get_public_status()
