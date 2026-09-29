"""Copy SQLite between fast local disk and Azure Files mount (SMB locks break live SQLite)."""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

log = logging.getLogger("market_pulse")

PERSIST_DIR = Path(os.getenv("PERSIST_DIR", "/persist"))
LOCAL_DIR = Path(os.getenv("DATA_DIR", "./data"))
DB_NAME = "market_pulse.db"
# Fresh path under /persist — root-level market_pulse.db got stuck on Azure Files
# (rename/replace left a cursed name that open/unlink cannot use).
DB_REL = Path("sqlite") / "mpulse.sqlite"
SETTINGS_NAME = "local_settings.json"
SETTINGS_REL = Path("sqlite") / SETTINGS_NAME
BACKUP_KEEP = 20
# Never delete snapshots that still hold briefs/scans.
CONTENT_SNAP_KEEP = 30
# After a successful restore, refuse content regressions for this long (deploy races).
WIPE_GUARD_SEC = 300

# Boot outcome: "none" | "restored" | "fresh"
_boot_mode = "none"
_boot_at = 0.0
_boot_content = 0
_last_status: dict[str, object] = {
    "persistReady": False,
    "restored": False,
    "localBytes": 0,
    "persistBytes": 0,
    "briefs": 0,
    "scans": 0,
    "message": "not started",
}


def persist_status() -> dict[str, object]:
    return dict(_last_status)


def _paths() -> tuple[Path, Path, Path, Path]:
    local_db = LOCAL_DIR / DB_NAME
    persist_db = PERSIST_DIR / DB_REL
    local_settings = LOCAL_DIR / SETTINGS_NAME
    persist_settings = PERSIST_DIR / SETTINGS_REL
    return local_db, persist_db, local_settings, persist_settings


def _wait_for_persist(timeout_sec: float = 45.0) -> bool:
    """Container Apps may attach Azure Files slightly after process start."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if PERSIST_DIR.is_dir():
            try:
                probe_dir = PERSIST_DIR / "sqlite"
                probe_dir.mkdir(parents=True, exist_ok=True)
                probe = probe_dir / ".mpulse-ready"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink(missing_ok=True)
                return True
            except OSError:
                pass
        time.sleep(1.0)
    return PERSIST_DIR.is_dir()


def _db_stats(path: Path) -> dict[str, int]:
    """Lightweight integrity/size signal for wipe protection."""
    out = {"bytes": 0, "briefs": 0, "scans": 0, "users": 0, "posts": 0, "ok": 0}
    try:
        if not path.is_file():
            return out
        out["bytes"] = int(path.stat().st_size)
        if out["bytes"] < 1024:
            return out
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=15)
        try:
            cur = conn.cursor()
            cur.execute("PRAGMA quick_check")
            row = cur.fetchone()
            if not row or str(row[0]).lower() != "ok":
                return out
            out["ok"] = 1
            for table, key in (
                ("briefs", "briefs"),
                ("scans", "scans"),
                ("users", "users"),
                ("posts", "posts"),
            ):
                try:
                    cur.execute(f"SELECT COUNT(*) FROM {table}")
                    out[key] = int(cur.fetchone()[0] or 0)
                except sqlite3.Error:
                    pass
        finally:
            conn.close()
    except Exception:
        pass
    return out


def _content(stats: dict[str, int]) -> int:
    return int(stats.get("briefs") or 0) + int(stats.get("scans") or 0)


def _score(stats: dict[str, int]) -> tuple[int, int, int, int]:
    """Higher = more valuable DB. Prefer more content over raw bytes alone."""
    return (
        int(stats.get("ok") or 0),
        _content(stats),
        int(stats.get("posts") or 0),
        int(stats.get("bytes") or 0),
    )


def _azure_safe_write(src: Path, dst: Path) -> None:
    """Copy onto Azure Files without rename/replace/unlink of the destination."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time() * 1000)
    remote_tmp = dst.parent / f".{dst.name}.{stamp}.tmp"
    try:
        shutil.copy2(src, remote_tmp)
        try:
            shutil.copy2(remote_tmp, dst)
        except OSError as exc:
            alt = dst.parent / f"{dst.stem}.{stamp}{dst.suffix}"
            shutil.copy2(remote_tmp, alt)
            log.warning("Primary persist path failed (%s); wrote %s", exc, alt.name)
            return
        size = dst.stat().st_size if dst.is_file() else 0
        log.info("Persisted %s (%s bytes)", dst, size)
    finally:
        try:
            remote_tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _write_content_snapshot(src: Path, stats: dict[str, int]) -> None:
    """Append-only snapshot when the DB still has briefs/scans — never overwritten."""
    if _content(stats) <= 0:
        return
    try:
        snap_dir = PERSIST_DIR / "sqlite" / "snaps"
        snap_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        name = f"mpulse.content.{stamp}.b{stats.get('briefs', 0)}.s{stats.get('scans', 0)}.sqlite"
        dest = snap_dir / name
        shutil.copy2(src, dest)
        snaps = sorted(snap_dir.glob("mpulse.content.*.sqlite"), key=lambda p: p.stat().st_mtime)
        # Keep newest N content snaps; never delete if under keep limit
        for old in snaps[:-CONTENT_SNAP_KEEP]:
            try:
                old.unlink(missing_ok=True)
            except OSError:
                pass
    except OSError as exc:
        log.warning("Could not write content snapshot: %s", exc)


def _rotate_backups(persist_db: Path) -> None:
    """Keep dated copies of the current canonical file before overwrite."""
    if not persist_db.is_file():
        return
    try:
        remote_stats = _db_stats(persist_db)
        # Don't waste backup slots on empty shells while richer snaps exist
        if _content(remote_stats) == 0 and persist_db.stat().st_size < 50_000:
            return
        stamp = time.strftime("%Y%m%d-%H%M%S")
        bak = persist_db.parent / f"mpulse.bak.{stamp}.sqlite"
        shutil.copy2(persist_db, bak)
    except OSError as exc:
        log.warning("Could not write persist backup: %s", exc)
        return
    try:
        backups = sorted(
            persist_db.parent.glob("mpulse.bak.*.sqlite"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in backups[BACKUP_KEEP:]:
            # Never delete a bak that still has content if we are low on content snaps
            try:
                if _content(_db_stats(old)) > 0:
                    continue
                old.unlink(missing_ok=True)
            except OSError:
                pass
    except OSError:
        pass


def _copy_sqlite_to_persist(src: Path, dst: Path) -> None:
    """Snapshot SQLite locally, then copy the finished file onto Azure Files."""
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix="mpulse-", suffix=".db", dir=str(LOCAL_DIR))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        src_conn = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True, timeout=30)
        try:
            dst_conn = sqlite3.connect(tmp.as_posix(), timeout=30)
            try:
                src_conn.backup(dst_conn)
                dst_conn.commit()
            finally:
                dst_conn.close()
        finally:
            src_conn.close()
        stats = _db_stats(tmp)
        _write_content_snapshot(tmp, stats)
        _rotate_backups(dst)
        _azure_safe_write(tmp, dst)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _legacy_candidates() -> list[Path]:
    """Older layouts + leftovers from failed rename attempts + backups + snaps."""
    names = [
        PERSIST_DIR / DB_REL,
        PERSIST_DIR / DB_NAME,
        PERSIST_DIR / f"{DB_NAME}.upload",
        PERSIST_DIR / "sqlite" / DB_NAME,
    ]
    out: list[Path] = []
    try:
        out.extend(PERSIST_DIR.glob(f".{DB_NAME}.*.tmp"))
        out.extend(PERSIST_DIR.glob(f"{DB_NAME}.*"))
        sqlite_dir = PERSIST_DIR / "sqlite"
        if sqlite_dir.is_dir():
            out.extend(sqlite_dir.glob("mpulse*.sqlite"))
            out.extend(sqlite_dir.glob("mpulse.bak.*.sqlite"))
            out.extend(sqlite_dir.glob(f".mpulse.sqlite.*.tmp"))
            out.extend(sqlite_dir.glob("market_pulse*"))
            snap_dir = sqlite_dir / "snaps"
            if snap_dir.is_dir():
                out.extend(snap_dir.glob("mpulse.content.*.sqlite"))
    except OSError:
        pass
    for p in names:
        if p not in out:
            out.append(p)
    return out


def _recover_persist_candidate() -> Path | None:
    """Prefer the richest non-empty DB under /persist (not merely newest).

    On a content tie, prefer the canonical mpulse.sqlite so password / settings
    updates are not rolled back by an older backup with the same brief/scan count.
    """
    candidates: list[tuple[tuple[int, int, int, int], int, Path]] = []
    try:
        canonical = (PERSIST_DIR / DB_REL).resolve()
    except OSError:
        canonical = PERSIST_DIR / DB_REL
    for p in _legacy_candidates():
        try:
            if not p.is_file() or p.stat().st_size <= 0:
                continue
            stats = _db_stats(p)
            if not stats.get("ok"):
                continue
            try:
                is_canonical = 0 if p.resolve() == canonical else 1
            except OSError:
                is_canonical = 1
            candidates.append((_score(stats), is_canonical, p))
        except OSError:
            continue
    if not candidates:
        return None
    # Highest content score first; on ties prefer canonical (is_canonical == 0)
    candidates.sort(key=lambda x: (x[0], -x[1]), reverse=True)
    return candidates[0][2]


def list_persist_candidates() -> list[dict[str, object]]:
    """Admin/debug: every recoverable DB with counts."""
    rows: list[dict[str, object]] = []
    for p in _legacy_candidates():
        try:
            if not p.is_file() or p.stat().st_size <= 0:
                continue
            stats = _db_stats(p)
            rows.append(
                {
                    "name": p.name,
                    "path": str(p),
                    "bytes": stats.get("bytes", 0),
                    "briefs": stats.get("briefs", 0),
                    "scans": stats.get("scans", 0),
                    "posts": stats.get("posts", 0),
                    "ok": bool(stats.get("ok")),
                    "score": _score(stats),
                }
            )
        except OSError:
            continue
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows


def restore_from_persist() -> None:
    """On boot: hydrate local working copy from Azure Files if present."""
    global _boot_mode, _boot_at, _boot_content, _last_status
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    local_db, persist_db, local_settings, persist_settings = _paths()
    _boot_at = time.time()
    if not _wait_for_persist():
        log.warning(
            "PERSIST_DIR %s not ready - running local-only (data may not survive restart)",
            PERSIST_DIR,
        )
        _boot_mode = "none"
        _boot_content = 0
        _last_status = {
            "persistReady": False,
            "restored": False,
            "localBytes": 0,
            "persistBytes": 0,
            "briefs": 0,
            "scans": 0,
            "message": "persist mount not ready",
        }
        return
    try:
        source = None
        for _attempt in range(1, 8):
            source = _recover_persist_candidate()
            if source is not None and _content(_db_stats(source)) > 0:
                break
            if source is not None and _attempt >= 3:
                break
            time.sleep(2.0)
            source = _recover_persist_candidate()
        if source is not None:
            shutil.copy2(source, local_db)
            stats = _db_stats(local_db)
            log.info(
                "Restored DB from %s (%s bytes, briefs=%s scans=%s posts=%s)",
                source,
                stats.get("bytes"),
                stats.get("briefs"),
                stats.get("scans"),
                stats.get("posts"),
            )
            if source.resolve() != persist_db.resolve() and _content(stats) > 0:
                try:
                    _azure_safe_write(local_db, persist_db)
                except Exception:
                    log.exception("Could not normalize recovered DB to %s", persist_db)
            _boot_mode = "restored"
            _boot_content = _content(stats)
            _last_status = {
                "persistReady": True,
                "restored": True,
                "localBytes": stats.get("bytes", 0),
                "persistBytes": stats.get("bytes", 0),
                "briefs": stats.get("briefs", 0),
                "scans": stats.get("scans", 0),
                "message": f"restored from {source.name}",
            }
        else:
            log.warning(
                "No persisted DB under %s (true fresh start — no prior briefs)",
                PERSIST_DIR,
            )
            _boot_mode = "fresh"
            _boot_content = 0
            _last_status = {
                "persistReady": True,
                "restored": False,
                "localBytes": 0,
                "persistBytes": 0,
                "briefs": 0,
                "scans": 0,
                "message": "fresh start (empty persist)",
            }

        settings_src = None
        if persist_settings.is_file():
            settings_src = persist_settings
        else:
            for alt in (
                PERSIST_DIR / SETTINGS_NAME,
                PERSIST_DIR / f"{SETTINGS_NAME}.upload",
                PERSIST_DIR / "sqlite" / f"{SETTINGS_NAME}.upload",
            ):
                if alt.is_file():
                    settings_src = alt
                    break
        if settings_src is not None:
            shutil.copy2(settings_src, local_settings)
            log.info("Restored settings from %s", settings_src)
    except Exception:
        _boot_mode = "none"
        _boot_content = 0
        _last_status = {
            "persistReady": True,
            "restored": False,
            "localBytes": 0,
            "persistBytes": 0,
            "briefs": 0,
            "scans": 0,
            "message": "restore failed",
        }
        log.exception("restore_from_persist failed")


def _should_block_flush(local_stats: dict[str, int], remote_stats: dict[str, int], remote_name: str) -> bool:
    """Block any flush that would replace richer remote data with a poorer local copy."""
    local_c = _content(local_stats)
    remote_c = _content(remote_stats)
    if remote_c <= local_c:
        return False
    age = time.time() - _boot_at if _boot_at else 0
    # Always block during the post-boot guard window (covers dual-revision deploy races).
    if age < WIPE_GUARD_SEC:
        log.error(
            "BLOCKED persist wipe (boot guard %.0fs): local briefs+scans=%s < remote %s (%s). "
            "Refusing flush.",
            age,
            local_c,
            remote_c,
            remote_name,
        )
        return True
    # After the guard window, still block catastrophic wipe (remote has data, local empty).
    if local_c == 0 and remote_c > 0:
        log.error(
            "BLOCKED persist wipe (empty local): remote %s has briefs+scans=%s",
            remote_name,
            remote_c,
        )
        return True
    # Large regressions (>50% loss) also blocked — likely a bad replica, not user deletes.
    if remote_c >= 4 and local_c < remote_c // 2:
        log.error(
            "BLOCKED persist wipe (large regression): local=%s remote=%s (%s)",
            local_c,
            remote_c,
            remote_name,
        )
        return True
    return False


def flush_to_persist() -> None:
    """Copy local DB/settings to Azure Files — never wipe a richer remote on bad boot."""
    global _boot_mode, _last_status
    if not PERSIST_DIR.is_dir():
        return
    if _boot_mode == "none":
        log.warning("Skip persist flush — restore did not succeed (refusing to wipe remote DB)")
        return
    local_db, persist_db, local_settings, persist_settings = _paths()
    try:
        (PERSIST_DIR / "sqlite").mkdir(parents=True, exist_ok=True)
        if local_db.is_file():
            local_stats = _db_stats(local_db)
            remote = _recover_persist_candidate()
            if remote is not None:
                remote_stats = _db_stats(remote)
                if _should_block_flush(local_stats, remote_stats, remote.name):
                    # Heal: if remote is richer, re-hydrate local instead of flushing.
                    # Skip heal shortly after an admin password force-reset so an older
                    # backup cannot roll back the new hash.
                    forced_at = float(os.getenv("_MPULSE_PASSWORD_FORCED_AT") or 0)
                    if forced_at and (time.time() - forced_at) < 600:
                        log.warning(
                            "Skip heal-from-remote during password-force window "
                            "(remote=%s); keeping local password update",
                            remote.name,
                        )
                        _last_status.update(
                            {
                                "message": "flush blocked during password-force window",
                                "briefs": local_stats.get("briefs", 0),
                                "scans": local_stats.get("scans", 0),
                                "localBytes": local_stats.get("bytes", 0),
                            }
                        )
                        return
                    if _content(remote_stats) > _content(local_stats):
                        try:
                            shutil.copy2(remote, local_db)
                            _boot_mode = "restored"
                            _last_status.update(
                                {
                                    "restored": True,
                                    "message": f"healed from {remote.name}",
                                    "briefs": remote_stats.get("briefs", 0),
                                    "scans": remote_stats.get("scans", 0),
                                    "persistBytes": remote_stats.get("bytes", 0),
                                    "localBytes": remote_stats.get("bytes", 0),
                                }
                            )
                            log.warning("Re-hydrated local DB from richer remote %s", remote.name)
                        except Exception:
                            log.exception("heal-from-remote failed")
                    else:
                        _last_status.update(
                            {
                                "message": "flush blocked (would wipe richer remote DB)",
                                "briefs": remote_stats.get("briefs", 0),
                                "scans": remote_stats.get("scans", 0),
                                "persistBytes": remote_stats.get("bytes", 0),
                                "localBytes": local_stats.get("bytes", 0),
                            }
                        )
                    return
            _copy_sqlite_to_persist(local_db, persist_db)
            stats = _db_stats(persist_db if persist_db.is_file() else local_db)
            _last_status.update(
                {
                    "persistReady": True,
                    "localBytes": local_stats.get("bytes", 0),
                    "persistBytes": stats.get("bytes", 0),
                    "briefs": stats.get("briefs", 0),
                    "scans": stats.get("scans", 0),
                    "message": "flushed ok",
                }
            )
        if local_settings.is_file():
            _azure_safe_write(local_settings, persist_settings)
    except Exception:
        log.exception("flush_to_persist failed")


_stop = threading.Event()
_thread: threading.Thread | None = None
_flush_lock = threading.Lock()


def flush_to_persist_safe() -> None:
    with _flush_lock:
        flush_to_persist()


def start_persist_loop(interval_sec: int = 20) -> None:
    global _thread
    if _thread and _thread.is_alive():
        return

    def _run() -> None:
        while not _stop.wait(interval_sec):
            flush_to_persist_safe()

    _thread = threading.Thread(target=_run, name="persist-flush", daemon=True)
    _thread.start()
    flush_to_persist_safe()


def stop_persist_loop() -> None:
    _stop.set()
    flush_to_persist_safe()
