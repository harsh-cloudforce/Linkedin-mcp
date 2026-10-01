"""Database engine and session helpers."""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

ROOT = Path(__file__).resolve().parent.parent
# Local fallback when DATABASE_URL is unset (dev / SQLite).
DATA_DIR = Path(os.getenv("DATA_DIR", str(ROOT / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "market_pulse.db"
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DB_PATH.as_posix()}").strip()

_connect_args: dict = {}
if DATABASE_URL.startswith("sqlite"):
    _connect_args = {"check_same_thread": False, "timeout": 60}

engine = create_engine(
    DATABASE_URL,
    connect_args=_connect_args,
    pool_pre_ping=True,
)


@event.listens_for(engine, "connect")
def _on_connect(dbapi_conn, _) -> None:  # noqa: ANN001
    if not DATABASE_URL.startswith("sqlite"):
        return
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=60000")
    cur.execute("PRAGMA journal_mode=DELETE")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from app import models  # noqa: F401

    log = logging.getLogger("market_pulse")
    last_err: Exception | None = None
    for attempt in range(1, 8):
        try:
            Base.metadata.create_all(bind=engine)
            _migrate_schema()
            last_err = None
            break
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            log.warning("init_db attempt %s failed: %s", attempt, exc)
            time.sleep(min(2 * attempt, 10))
    if last_err is not None:
        raise last_err

    _ensure_admin()


def _migrate_schema() -> None:
    with engine.begin() as conn:
        if DATABASE_URL.startswith("sqlite"):
            cols = {row[1] for row in conn.execute(text("PRAGMA table_info(users)")).fetchall()}
            if "is_admin" not in cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0"))
            if "password_hash" not in cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN password_hash VARCHAR(255)"))
            if "linkedin_email" not in cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN linkedin_email VARCHAR(255)"))
            if "owner_pk" not in cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN owner_pk INTEGER"))
            post_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(posts)")).fetchall()}
            if "images_json" not in post_cols:
                conn.execute(text("ALTER TABLE posts ADD COLUMN images_json TEXT"))
            return

        # Postgres
        cols = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'users'"
                )
            ).fetchall()
        }
        if cols:
            if "is_admin" not in cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0"))
            if "password_hash" not in cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN password_hash VARCHAR(255)"))
            if "linkedin_email" not in cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN linkedin_email VARCHAR(255)"))
            if "owner_pk" not in cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN owner_pk INTEGER"))
        post_cols = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'posts'"
                )
            ).fetchall()
        }
        if post_cols and "images_json" not in post_cols:
            conn.execute(text("ALTER TABLE posts ADD COLUMN images_json TEXT"))


def _ensure_admin() -> None:
    """Bootstrap admin from env (ADMIN_EMAIL + ADMIN_PASSWORD).

    Password rules:
    - New admin: created with ADMIN_PASSWORD.
    - Existing admin with no hash: set from ADMIN_PASSWORD.
    - ADMIN_PASSWORD_FORCE=1: overwrite DB hash from env (recovery).
    In-app password changes are authoritative until FORCE is used.
    """
    from app.models import User
    from app.security import hash_password

    email = (os.getenv("ADMIN_EMAIL") or "").strip().lower()
    password = (os.getenv("ADMIN_PASSWORD") or "").strip()
    if not email:
        return
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.user_id == email).one_or_none()
        if user is None:
            if not password:
                logging.getLogger("market_pulse").warning(
                    "ADMIN_EMAIL set but ADMIN_PASSWORD missing — admin not created"
                )
                return
            user = User(
                user_id=email,
                display_name=os.getenv("ADMIN_DISPLAY_NAME") or email,
                active=1,
                is_admin=1,
                password_hash=hash_password(password),
            )
            db.add(user)
            db.commit()
            logging.getLogger("market_pulse").info("Admin user created: %s", email)
            return
        user.is_admin = 1
        user.active = 1
        changed = False
        if password:
            force = os.getenv("ADMIN_PASSWORD_FORCE", "").lower() in {"1", "true", "yes"}
            if force or not user.password_hash:
                user.password_hash = hash_password(password)
                changed = True
                logging.getLogger("market_pulse").info(
                    "Admin password %s for %s",
                    "forced from ADMIN_PASSWORD" if force else "set (was empty)",
                    email,
                )
        db.commit()
        if changed:
            try:
                # Keep heal-from-backup from rolling back this password for a few minutes
                os.environ["_MPULSE_PASSWORD_FORCED_AT"] = str(time.time())
                from app.services.persist import flush_to_persist_safe

                flush_to_persist_safe()
            except Exception:
                logging.getLogger("market_pulse").exception("persist after admin password update failed")
    finally:
        db.close()
