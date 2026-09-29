"""Persist scans/posts and generate dated Market Pulse briefs."""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from app.models import Brief, Post, Scan, Source, User


def ensure_user(
    db: Session,
    user_id: str,
    display_name: str | None = None,
    *,
    owner_pk: int | None = None,
) -> User:
    """Ensure a LinkedIn scan identity exists. Does not grant webapp login."""
    uid = user_id.strip().lower()
    user = db.query(User).filter(User.user_id == uid).one_or_none()
    if user:
        if owner_pk is not None and not user.password_hash:
            if user.owner_pk is None or user.owner_pk == owner_pk:
                user.owner_pk = owner_pk
                db.commit()
                db.refresh(user)
        return user
    user = User(
        user_id=uid,
        display_name=display_name or uid,
        active=1,
        is_admin=0,
        password_hash=None,
        owner_pk=owner_pk,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    try:
        from app.services.persist import flush_to_persist_safe

        flush_to_persist_safe()
    except Exception:
        pass
    return user


def ensure_source(db: Session, key: str = "linkedin", name: str = "LinkedIn home feed") -> Source:
    src = db.query(Source).filter(Source.key == key).one_or_none()
    if src:
        return src
    src = Source(key=key, name=name, enabled=1)
    db.add(src)
    db.commit()
    db.refresh(src)
    return src


def create_scan(db: Session, *, user: User, source: Source) -> Scan:
    scan = Scan(
        user_pk=user.id,
        source_id=source.id,
        status="queued",
        started_at=datetime.now(timezone.utc),
    )
    db.add(scan)
    db.commit()
    db.refresh(scan)
    return scan


def clean_author(raw: str | None) -> str | None:
    if not raw:
        return None
    a = re.sub(r"\s+", " ", str(raw)).strip()
    a = re.sub(r"^View\s+", "", a, flags=re.I)
    a = re.sub(r"\s*Premium Profile\s*", " ", a, flags=re.I)
    a = re.sub(r",?\s*Open to work.*$", "", a, flags=re.I)
    a = re.sub(r"\s*Verified Profile.*$", "", a, flags=re.I)
    a = re.sub(r"['\u2019]s\s+profile$", "", a, flags=re.I)
    a = re.sub(r"\s+profile$", "", a, flags=re.I)
    a = re.sub(r"\s*[•·].*$", "", a)
    a = re.sub(r"['\u2019]s$", "", a)  # Madhan Vadlamudi's → Madhan Vadlamudi
    a = a.strip(" ,|-")
    if not a or a.lower() in {"unknown", "linkedin member", "member", "view", "more"}:
        return None
    return a


def _post_images(p: Post | dict) -> list[str]:
    if isinstance(p, dict):
        imgs = p.get("images") or p.get("imageUrls") or []
        if isinstance(imgs, str):
            try:
                imgs = json.loads(imgs)
            except Exception:
                imgs = []
        return [str(u) for u in (imgs or []) if u][:6]
    raw = p.images_json
    if not raw:
        # fallback: raw_json from scroller
        try:
            blob = json.loads(p.raw_json or "{}")
            imgs = blob.get("images") or []
            return [str(u) for u in imgs if u][:6]
        except Exception:
            return []
    try:
        imgs = json.loads(raw)
        return [str(u) for u in (imgs or []) if u][:6]
    except Exception:
        return []


def _post_dedupe_key(p: dict) -> str:
    url = (p.get("url") or "").strip().split("?")[0].rstrip("/").lower()
    if url:
        m = re.search(r"urn:li:activity:\d+", url)
        if m:
            return f"url:{m.group(0)}"
        m = re.search(r"activity[:\-](\d+)", url)
        if m:
            return f"url:urn:li:activity:{m.group(1)}"
        return f"url:{url}"
    author = re.sub(r"\s+", " ", (clean_author(p.get("author")) or "").lower())
    text = re.sub(r"\s+", " ", (p.get("text") or "").strip().lower())[:240]
    return f"body:{author}|{text}"


def save_posts_from_payload(db: Session, scan: Scan, payload: dict) -> int:
    """Persist scraped posts only — never invents content. Dedupes by activity URL/body."""
    posts = payload.get("posts") or []
    db.query(Post).filter(Post.scan_id == scan.id).delete()
    seen: set[str] = set()
    count = 0
    for i, p in enumerate(posts, start=1):
        if not isinstance(p, dict):
            continue
        key = _post_dedupe_key(p)
        if key in seen:
            continue
        seen.add(key)
        text = (p.get("text") or "").strip()
        # Skip empty synthetic-looking shells
        if not text and not p.get("url") and not (p.get("images") or p.get("imageUrls")):
            continue
        imgs = _post_images(p)
        count += 1
        db.add(
            Post(
                scan_id=scan.id,
                rank=count,
                author=clean_author(p.get("author")),
                headline=p.get("headline"),
                text=p.get("text"),
                url=p.get("url"),
                social_proof=p.get("socialProof") or p.get("social_proof"),
                images_json=json.dumps(imgs, ensure_ascii=False) if imgs else None,
                raw_json=json.dumps(p, ensure_ascii=False),
            )
        )
    scan.post_count = count
    db.commit()
    return count


def format_user_time(
    dt: datetime,
    *,
    tz_name: str | None = None,
    utc_offset_minutes: int | None = None,
) -> str:
    """Format UTC datetime in the user's timezone (name preferred, else JS getTimezoneOffset)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)

    if tz_name:
        try:
            local = dt.astimezone(ZoneInfo(tz_name))
            return local.strftime("%Y-%m-%d %H:%M %Z")
        except ZoneInfoNotFoundError:
            pass
        except Exception:
            pass

    if utc_offset_minutes is not None:
        # JS getTimezoneOffset: minutes to add to local to get UTC → local = utc - offset
        local = dt + timedelta(minutes=-int(utc_offset_minutes))
        sign = "-" if utc_offset_minutes > 0 else "+"
        abs_m = abs(int(utc_offset_minutes))
        hh, mm = divmod(abs_m, 60)
        return local.strftime("%Y-%m-%d %H:%M") + f" UTC{sign}{hh:02d}:{mm:02d}"

    return dt.strftime("%Y-%m-%d %H:%M UTC")


def render_brief_html(markdown_text: str) -> str:
    """Turn stored brief markdown into HTML with LinkedIn images that can load."""
    import html as html_lib
    import markdown as md

    raw = markdown_text or ""
    try:
        body = md.markdown(
            raw,
            extensions=["nl2br", "sane_lists", "fenced_code"],
            output_format="html5",
        )
    except Exception:
        return f"<pre class='markdown-fallback'>{html_lib.escape(raw)}</pre>"

    # LinkedIn CDN often blocks hotlinks that send a Referer — strip it.
    def _img_attrs(match: re.Match[str]) -> str:
        tag = match.group(0)
        if "referrerpolicy" not in tag.lower():
            tag = tag[:-1] + ' referrerpolicy="no-referrer" loading="lazy">'
        if "class=" not in tag.lower():
            tag = tag.replace("<img ", '<img class="brief-img" ', 1)
        return tag

    body = re.sub(r"<img\b[^>]*>", _img_attrs, body, flags=re.IGNORECASE)
    return body


def build_brief_markdown(
    *,
    user_id: str,
    brief_when: str,
    posts: list[Post],
    scan_id: int,
    focus_keywords: list[str] | None = None,
) -> str:
    authors = Counter((clean_author(p.author) or "Unknown") for p in posts)
    top_authors = ", ".join(f"{a} ({n})" for a, n in authors.most_common(8)) or "n/a"
    focus = [k.strip() for k in (focus_keywords or []) if k.strip()]
    lines = [
        f"# Market Pulse brief — {brief_when}",
        "",
        f"- **User:** `{user_id}`",
        f"- **Source:** LinkedIn home feed",
        f"- **Scan id:** {scan_id}",
        f"- **Posts captured:** {len(posts)}",
        f"- **Frequent authors:** {top_authors}",
    ]
    if focus:
        lines.append(f"- **Focus:** {', '.join(focus)}")
    lines += [
        "",
        "_Note: scroll count ≠ post count. LinkedIn reuses feed cards; duplicates are removed; "
        "recency settings may drop older posts._",
        "",
        "## Themes",
        "",
    ]
    buckets = {
        "AI / tech": ["ai", "llm", "gpt", "model", "cloud", "data"],
        "Jobs / recruiting": ["hiring", "job", "role", "recruit", "offer", "career"],
        "Marketing / brand": ["brand", "campaign", "marketing", "content", "employer"],
        "Sales / GTM": ["sales", "pipeline", "customer", "deal", "gtm"],
    }
    for kw in focus:
        buckets[f"Focus: {kw}"] = [kw.lower()]
    texts = " ".join((p.text or "").lower() for p in posts)
    hit_any = False
    for name, keys in buckets.items():
        hits = sum(1 for k in keys if k in texts)
        if hits:
            hit_any = True
            lines.append(f"- **{name}:** signal ~{hits}")
    if not hit_any:
        lines.append("- General professional network activity.")

    ordered = list(posts)
    matched_ids: set[int] = set()
    if focus:
        keys = [k.lower() for k in focus]

        def score(p: Post) -> int:
            blob = f"{p.author or ''} {p.headline or ''} {p.text or ''}".lower()
            return sum(1 for k in keys if k in blob)

        ordered = sorted(posts, key=score, reverse=True)
        matched = [p for p in ordered if score(p) > 0]
        matched_ids = {p.id for p in matched if p.id is not None}
        lines += ["", "## Focus matches", ""]
        if matched:
            for p in matched:
                author = clean_author(p.author) or "Unknown"
                preview = re.sub(r"\s+", " ", (p.text or "").strip())[:160]
                url = f" — {p.url}" if p.url else ""
                lines.append(f"- **{author}**{url}: {preview}{'…' if len((p.text or '').strip()) > 160 else ''}")
            lines.append("")
        else:
            lines.append("_No posts matched focus keywords; showing full feed below._")
            lines.append("")

    # Full post bodies listed once (focus matches are short bullets above, not duplicated).
    lines += [
        "",
        f"## Posts ({len(ordered)})",
        "",
        "_All posts below are from the LinkedIn feed scrape; nothing is invented._",
        "",
    ]
    for i, p in enumerate(ordered, start=1):
        author = clean_author(p.author) or "Unknown"
        text = (p.text or "").strip()
        url = f" — {p.url}" if p.url else ""
        focus_tag = " _(focus)_" if p.id in matched_ids else ""
        lines.append(f"{i}. **{author}**{url}{focus_tag}")
        paras = [para.strip() for para in text.splitlines() if para.strip()] or ([text] if text else [])
        for para in paras:
            lines.append(f"   {para}")
        for img in _post_images(p):
            lines.append(f"   ![]({img})")
        lines.append("")
    lines += [
        "## GTM so-what",
        "",
        "- Marketing: themes for posts or campaigns?",
        "- Recruiting / employer brand: hiring or workplace signals?",
        "- Sales: accounts or topics for outreach?",
        "",
    ]
    return "\n".join(lines)


def create_brief_from_scan(
    db: Session,
    scan: Scan,
    focus_keywords: list[str] | None = None,
    *,
    tz_name: str | None = None,
    utc_offset_minutes: int | None = None,
) -> Brief | None:
    """Build a brief only when the scan has posts. Returns None for empty scans."""
    user = scan.user
    posts = (
        db.query(Post).filter(Post.scan_id == scan.id).order_by(Post.rank.asc()).all()
    )
    if not posts:
        return None
    now = datetime.now(timezone.utc)
    when = format_user_time(now, tz_name=tz_name, utc_offset_minutes=utc_offset_minutes)
    brief_date = now.strftime("%Y-%m-%d")
    title = f"Market Pulse — {user.user_id} — {when}"
    md = build_brief_markdown(
        user_id=user.user_id,
        brief_when=when,
        posts=posts,
        scan_id=scan.id,
        focus_keywords=focus_keywords,
    )
    brief = Brief(
        user_pk=user.id,
        scan_id=scan.id,
        brief_date=brief_date,
        title=title,
        markdown=md,
        status="ready",
    )
    db.add(brief)
    db.commit()
    db.refresh(brief)
    try:
        from app.services.persist import flush_to_persist_safe

        flush_to_persist_safe()
    except Exception:
        pass
    return brief
