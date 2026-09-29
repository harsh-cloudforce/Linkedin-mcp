"""SQLAlchemy models for Market Pulse."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    """Login account (password_hash set) or LinkedIn scan identity (owner_pk set)."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    active: Mapped[int] = mapped_column(Integer, default=1)  # 1/0 for sqlite simplicity
    is_admin: Mapped[int] = mapped_column(Integer, default=0)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Last / preferred LinkedIn email for this login account (Cloudforce login != LinkedIn).
    linkedin_email: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    # For LinkedIn-only rows: which login account owns this scan identity.
    owner_pk: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    scans: Mapped[list[Scan]] = relationship(back_populates="user")
    briefs: Mapped[list[Brief]] = relationship(back_populates="user")


class Source(Base):
    """Pluggable ingest sources — linkedin first; rss/etc later."""

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True)  # linkedin, rss, ...
    name: Mapped[str] = mapped_column(String(128))
    enabled: Mapped[int] = mapped_column(Integer, default=1)


class Scan(Base):
    __tablename__ = "scans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_pk: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    status: Mapped[str] = mapped_column(String(32), default="queued")  # queued|running|awaiting_login|completed|failed
    external_job_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    login_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    post_count: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    user: Mapped[User] = relationship(back_populates="scans")
    source: Mapped[Source] = relationship()
    posts: Mapped[list[Post]] = relationship(back_populates="scan", cascade="all, delete-orphan")
    brief: Mapped[Brief | None] = relationship(back_populates="scan", uselist=False)


class Post(Base):
    __tablename__ = "posts"
    __table_args__ = (UniqueConstraint("scan_id", "rank", name="uq_scan_rank"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scan_id: Mapped[int] = mapped_column(ForeignKey("scans.id"), index=True)
    rank: Mapped[int] = mapped_column(Integer)
    author: Mapped[str | None] = mapped_column(String(512), nullable=True)
    headline: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    social_proof: Mapped[str | None] = mapped_column(Text, nullable=True)
    images_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list of image URLs
    raw_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    scan: Mapped[Scan] = relationship(back_populates="posts")


class Brief(Base):
    """Dated Market Pulse report — never overwrite; new row per day/run."""

    __tablename__ = "briefs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_pk: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    scan_id: Mapped[int | None] = mapped_column(ForeignKey("scans.id"), nullable=True)
    brief_date: Mapped[str] = mapped_column(String(10), index=True)  # YYYY-MM-DD
    title: Mapped[str] = mapped_column(String(512))
    markdown: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="ready")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    user: Mapped[User] = relationship(back_populates="briefs")
    scan: Mapped[Scan | None] = relationship(back_populates="brief")


class ApiKey(Base):
    """Named integration keys for nebulaONE / REST. Full secret shown once at create."""

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    key_prefix: Mapped[str] = mapped_column(String(16))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    active: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
