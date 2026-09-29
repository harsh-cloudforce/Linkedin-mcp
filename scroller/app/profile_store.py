"""Durable LinkedIn Chromium profiles + cookie storage_state on Azure Files.

Chromium cannot reliably use Azure Files SMB as user_data_dir (crashes in ~1s).
We keep the lasting copy on PROFILES_DIR and run scans from WORK_PROFILES_DIR (/tmp).

storage_state.json (cookies) is the primary session — small, durable, and enough to
skip VNC on later scans. After one successful login we keep re-saving cookies so the
session lasts as long as LinkedIn accepts it.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

DURABLE_DIR = Path(os.getenv("PROFILES_DIR", "/data/profiles"))
WORK_DIR = Path(os.getenv("WORK_PROFILES_DIR", "/tmp/li-profiles"))
STATE_NAME = "storage_state.json"
# Client-side cookie lifetime hint (LinkedIn may still expire server-side)
COOKIE_EXPIRES_YEARS = 10


def _safe_id(user_id: str) -> str:
    return user_id.strip().lower().replace(" ", "-")


def durable_user_dir(user_id: str) -> Path:
    return DURABLE_DIR / _safe_id(user_id)


def storage_state_path(user_id: str) -> Path:
    return durable_user_dir(user_id) / STATE_NAME


def work_storage_state_path(user_id: str) -> Path:
    return WORK_DIR / _safe_id(user_id) / STATE_NAME


def _azure_safe_write_bytes(dst: Path, data: bytes) -> None:
    """Write file on Azure Files without rename/replace of the final name."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time() * 1000)
    tmp = dst.parent / f".{dst.name}.{stamp}.tmp"
    try:
        tmp.write_bytes(data)
        try:
            shutil.copy2(tmp, dst)
        except OSError:
            # Fallback: write via open truncate (no unlink of dst first)
            with open(dst, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _normalize_cookies(cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep LinkedIn auth cookies and stretch expires so Playwright reuses them."""
    far = int(time.time()) + COOKIE_EXPIRES_YEARS * 365 * 24 * 3600
    out: list[dict[str, Any]] = []
    for c in cookies:
        if not isinstance(c, dict) or not c.get("name") or c.get("value") is None:
            continue
        name = str(c["name"])
        domain = str(c.get("domain") or ".linkedin.com")
        if "linkedin" not in domain.lower() and name not in {"li_at", "li_a", "JSESSIONID"}:
            continue
        item = dict(c)
        item["domain"] = domain if domain.startswith(".") or "linkedin" in domain else ".linkedin.com"
        item["path"] = item.get("path") or "/"
        # Session cookies (expires -1 / missing) → long-lived so they survive restarts
        exp = item.get("expires")
        if exp is None or exp == -1 or (isinstance(exp, (int, float)) and exp < time.time()):
            item["expires"] = far
        item.setdefault("httpOnly", True)
        item.setdefault("secure", True)
        if "sameSite" not in item or item["sameSite"] not in ("Strict", "Lax", "None"):
            item["sameSite"] = "None"
        out.append(item)
    return out


def has_storage_state(user_id: str) -> bool:
    path = storage_state_path(user_id)
    try:
        return path.is_file() and path.stat().st_size > 40
    except OSError:
        return False


def prepare_work_profile(user_id: str) -> Path:
    """Restore durable profile into a local work directory for Chromium."""
    uid = _safe_id(user_id)
    durable = DURABLE_DIR / uid
    work = WORK_DIR / uid
    WORK_DIR.mkdir(parents=True, exist_ok=True)

    if work.exists():
        shutil.rmtree(work, ignore_errors=True)

    if durable.is_dir():
        try:
            has_files = any(durable.iterdir())
        except OSError:
            has_files = False
        if has_files:
            try:
                shutil.copytree(durable, work)
                print(f"[profiles] restored {uid} from {durable}", flush=True)
                return work
            except Exception as exc:  # noqa: BLE001
                print(f"[profiles] restore failed ({exc}); cookies-only fallback", flush=True)
                work.mkdir(parents=True, exist_ok=True)
                state = durable / STATE_NAME
                if state.is_file():
                    try:
                        shutil.copy2(state, work / STATE_NAME)
                        print(f"[profiles] restored cookies-only for {uid}", flush=True)
                    except Exception as exc2:  # noqa: BLE001
                        print(f"[profiles] cookie restore failed: {exc2}", flush=True)
                return work

    work.mkdir(parents=True, exist_ok=True)
    print(f"[profiles] new local work profile {work}", flush=True)
    return work


def persist_work_profile(user_id: str, work: Path | None = None) -> None:
    """Copy local work profile back to durable Azure Files store."""
    uid = _safe_id(user_id)
    work = work or (WORK_DIR / uid)
    if not work.is_dir():
        return
    try:
        DURABLE_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[profiles] durable dir missing ({exc})", flush=True)
        return

    work_state = work / STATE_NAME
    if work_state.is_file():
        try:
            raw = json.loads(work_state.read_text(encoding="utf-8"))
            cookies = _normalize_cookies(raw.get("cookies") or [])
            payload = json.dumps(
                {"cookies": cookies, "origins": raw.get("origins") or []},
                indent=2,
            ).encode("utf-8")
            save_storage_state_bytes(user_id, payload)
        except Exception as exc:  # noqa: BLE001
            print(f"[profiles] cookie persist failed: {exc}", flush=True)

    durable = DURABLE_DIR / uid
    staging = DURABLE_DIR / f".{uid}.staging-{int(time.time())}"
    old = DURABLE_DIR / f".{uid}.old-{int(time.time())}"
    try:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        shutil.copytree(work, staging)
        try:
            if durable.exists():
                try:
                    durable.rename(old)
                except OSError:
                    shutil.rmtree(durable, ignore_errors=True)
        except OSError:
            shutil.rmtree(durable, ignore_errors=True)
        try:
            staging.rename(durable)
        except OSError:
            shutil.copytree(staging, durable, dirs_exist_ok=True)
            shutil.rmtree(staging, ignore_errors=True)
        if old.exists():
            shutil.rmtree(old, ignore_errors=True)
        print(f"[profiles] persisted {uid} -> {durable}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[profiles] persist failed for {uid}: {exc}", flush=True)
        shutil.rmtree(staging, ignore_errors=True)


def save_storage_state_bytes(user_id: str, data: bytes) -> Path:
    path = storage_state_path(user_id)
    _azure_safe_write_bytes(path, data)
    # Timestamped backup (never lose last good session)
    try:
        bak = path.with_name(f"storage_state.{int(time.time())}.json")
        _azure_safe_write_bytes(bak, data)
        # Keep only the 5 newest backups
        backups = sorted(
            path.parent.glob("storage_state.*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in backups[5:]:
            try:
                old.unlink()
            except OSError:
                pass
    except Exception as exc:  # noqa: BLE001
        print(f"[profiles] backup write skipped: {exc}", flush=True)
    print(f"[profiles] saved storage_state for {_safe_id(user_id)} ({len(data)} bytes)", flush=True)
    return path


def save_storage_state_from_context(user_id: str, context: Any) -> Path | None:
    """Export Playwright context cookies to durable storage_state.json."""
    uid = _safe_id(user_id)
    work = WORK_DIR / uid
    work.mkdir(parents=True, exist_ok=True)
    local = work / STATE_NAME
    try:
        context.storage_state(path=str(local))
        raw = json.loads(local.read_text(encoding="utf-8"))
        cookies = _normalize_cookies(raw.get("cookies") or [])
        if not cookies:
            print("[profiles] storage_state export had no LinkedIn cookies", flush=True)
            return None
        payload = json.dumps(
            {"cookies": cookies, "origins": raw.get("origins") or []},
            indent=2,
        ).encode("utf-8")
        local.write_bytes(payload)
        return save_storage_state_bytes(user_id, payload)
    except Exception as exc:  # noqa: BLE001
        print(f"[profiles] storage_state export failed: {exc}", flush=True)
        return None


def load_storage_state_file(user_id: str) -> Path | None:
    """Return path to a usable storage_state.json (work copy preferred)."""
    work = work_storage_state_path(user_id)
    durable = storage_state_path(user_id)
    for candidate in (work, durable):
        try:
            if candidate.is_file() and candidate.stat().st_size > 40:
                # Ensure work has a copy for Playwright
                if candidate != work:
                    work.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(candidate, work)
                return work if work.is_file() else candidate
        except OSError:
            continue
    # Try newest backup
    try:
        backups = sorted(
            durable_user_dir(user_id).glob("storage_state.*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for bak in backups:
            if bak.stat().st_size > 40:
                work.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(bak, work)
                print(f"[profiles] restored session from backup {bak.name}", flush=True)
                return work
    except OSError:
        pass
    return None


def load_cookies_for_context(user_id: str) -> list[dict[str, Any]]:
    """Return cookie dicts suitable for context.add_cookies()."""
    path = load_storage_state_file(user_id)
    if not path:
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        cookies = _normalize_cookies(data.get("cookies") or [])
        if cookies:
            print(f"[profiles] loaded {len(cookies)} cookies from {path}", flush=True)
        return cookies
    except Exception as exc:  # noqa: BLE001
        print(f"[profiles] cookie load failed ({path}): {exc}", flush=True)
        return []


def save_li_at_cookie(user_id: str, li_at: str, *, li_a: str | None = None) -> Path:
    """Build a Playwright storage_state from a pasted LinkedIn li_at cookie (no VNC)."""
    value = (li_at or "").strip()
    if value.lower().startswith("li_at="):
        value = value.split("=", 1)[1].strip()
    if ";" in value:
        for part in value.split(";"):
            part = part.strip()
            if part.lower().startswith("li_at="):
                value = part.split("=", 1)[1].strip()
                break
    if not value or len(value) < 20:
        raise ValueError("li_at cookie looks too short — paste the full value from DevTools")

    far = int(time.time()) + COOKIE_EXPIRES_YEARS * 365 * 24 * 3600
    cookies: list[dict[str, Any]] = [
        {
            "name": "li_at",
            "value": value,
            "domain": ".linkedin.com",
            "path": "/",
            "expires": far,
            "httpOnly": True,
            "secure": True,
            "sameSite": "None",
        }
    ]
    extra = (li_a or "").strip()
    if extra.lower().startswith("li_a="):
        extra = extra.split("=", 1)[1].strip()
    if extra:
        cookies.append(
            {
                "name": "li_a",
                "value": extra,
                "domain": ".linkedin.com",
                "path": "/",
                "expires": far,
                "httpOnly": True,
                "secure": True,
                "sameSite": "None",
            }
        )

    state = {"cookies": cookies, "origins": []}
    data = json.dumps(state, indent=2).encode("utf-8")
    path = save_storage_state_bytes(user_id, data)
    work = WORK_DIR / _safe_id(user_id)
    work.mkdir(parents=True, exist_ok=True)
    (work / STATE_NAME).write_bytes(data)
    return path


def session_status(user_id: str) -> dict[str, Any]:
    path = storage_state_path(user_id)
    durable = durable_user_dir(user_id)
    has_profile = False
    try:
        has_profile = durable.is_dir() and any(p.name != STATE_NAME for p in durable.iterdir())
    except OSError:
        has_profile = False
    size = 0
    mtime = None
    try:
        if path.is_file():
            st = path.stat()
            size = st.st_size
            mtime = datetime_utc_iso(st.st_mtime)
    except OSError:
        pass
    saved = has_storage_state(user_id) or has_profile
    return {
        "userId": _safe_id(user_id),
        "hasStorageState": has_storage_state(user_id),
        "storageStateBytes": size,
        "storageStateUpdatedAt": mtime,
        "hasChromiumProfile": has_profile,
        "canSkipVnc": saved,
        "message": (
            "LinkedIn session saved — scans should not ask you to sign in"
            if saved
            else "No saved LinkedIn session — sign in once (VNC or paste li_at)"
        ),
    }


def datetime_utc_iso(ts: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
