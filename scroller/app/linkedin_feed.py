from __future__ import annotations

import asyncio
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from playwright.sync_api import BrowserContext, Page, sync_playwright

from .models import FeedPost, ScanResponse


def _dismiss_noise(page: Page) -> None:
    for label in ("Dismiss", "Not now", "No thanks", "Skip", "Close"):
        try:
            btn = page.get_by_role("button", name=re.compile(label, re.I)).first
            if btn.is_visible(timeout=500):
                btn.click(timeout=1000)
        except Exception:
            continue


def _looks_logged_in(page: Page) -> bool:
    """Positive signals that the home feed / nav is available (prefer over text heuristics)."""
    url = page.url.lower()
    if "/feed" in url or "/mynetwork" in url or "/messaging" in url:
        try:
            if page.locator('[data-testid="mainFeed"]').first.is_visible(timeout=800):
                return True
        except Exception:
            pass
        try:
            if page.locator('[data-testid="expandable-text-box"]').count() > 0:
                return True
        except Exception:
            pass
        try:
            # Logged-in global nav (Me / profile menu) — not present on auth walls
            if page.locator('img.global-nav__me-photo, [data-control-name="identity_welcome_message"]').count() > 0:
                return True
        except Exception:
            pass
        try:
            if page.get_by_role("navigation").first.is_visible(timeout=500):
                # Feed URL + nav is usually enough once past login
                if "/feed" in url and "login" not in url and "checkpoint" not in url:
                    return True
        except Exception:
            pass
    return False


def _is_login_wall(page: Page) -> bool:
    # If feed/nav already loaded, never treat as login wall (LinkedIn body text often
    # contains "Sign in" / "email" and used to trap the wait loop for loginWaitSeconds).
    if _looks_logged_in(page) and not _is_security_checkpoint(page):
        return False

    url = page.url.lower()
    if any(x in url for x in ("/login", "/uas/login", "/checkpoint", "authwall", "/signup")):
        return True

    # Dedicated auth form only — not generic page text
    try:
        email = page.get_by_role("textbox", name=re.compile(r"^(email|phone|email or phone)", re.I)).first
        password = page.get_by_role("textbox", name=re.compile(r"password", re.I)).first
        if email.is_visible(timeout=600) and password.is_visible(timeout=600):
            return True
    except Exception:
        pass
    try:
        if page.locator('form.login__form, #username, input[name="session_key"]').first.is_visible(timeout=600):
            return True
    except Exception:
        pass
    return _is_security_checkpoint(page)


def _is_security_checkpoint(page: Page) -> bool:
    """CAPTCHA / 'Let's do a quick security check' — feed scrape must pause."""
    url = (page.url or "").lower()
    if "/checkpoint" in url or "/challenge" in url:
        return True
    try:
        if page.locator(
            "text=/Let's do a quick security check|Security Verification|I'm not a robot/i"
        ).first.is_visible(timeout=400):
            return True
    except Exception:
        pass
    try:
        if page.locator("iframe[src*='recaptcha'], .g-recaptcha, #captcha-internal").first.is_visible(timeout=400):
            return True
    except Exception:
        pass
    return False


def _wait_out_checkpoint(
    page: Page,
    *,
    seconds: int,
    on_progress: Any = None,
) -> bool:
    """Wait for the user to solve LinkedIn CAPTCHA in the remote browser. Returns True if cleared."""
    if not _is_security_checkpoint(page):
        return True
    print(
        f"[scan] LinkedIn security checkpoint / CAPTCHA — solve it in the remote browser "
        f"(waiting up to {seconds}s)… url={page.url}",
        flush=True,
    )
    if callable(on_progress):
        try:
            on_progress(
                {
                    "phase": "checkpoint",
                    "status": "awaiting_user",
                    "message": "LinkedIn security check — complete CAPTCHA in the remote browser.",
                }
            )
        except Exception:
            pass
    deadline = time.time() + max(30, int(seconds))
    last_log = 0.0
    while time.time() < deadline:
        if not _is_security_checkpoint(page) and (_looks_logged_in(page) or "/feed" in (page.url or "").lower()):
            print(f"[scan] checkpoint cleared — url={page.url}", flush=True)
            try:
                page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=45000)
            except Exception:
                pass
            page.wait_for_timeout(2000)
            return True
        now = time.time()
        if now - last_log > 20:
            remaining = int(deadline - now)
            print(f"[scan] still on checkpoint… {remaining}s left  url={page.url}", flush=True)
            last_log = now
        page.wait_for_timeout(2000)
    return not _is_security_checkpoint(page)


def _dedupe_key(post: FeedPost) -> str:
    """Stable key: prefer activity URN/URL, else normalized author+text."""
    url = (post.url or "").strip().split("?")[0].rstrip("/").lower()
    if url:
        m = re.search(r"urn:li:activity:\d+", url)
        if m:
            return f"url:{m.group(0)}"
        m = re.search(r"activity[:\-](\d+)", url)
        if m:
            return f"url:urn:li:activity:{m.group(1)}"
        return f"url:{url}"
    author = re.sub(r"\s+", " ", (post.author or "").strip().lower())
    text = re.sub(r"\s+", " ", (post.text or "").strip().lower())[:240]
    return f"body:{author}|{text}"


def _expand_see_more(page: Page) -> int:
    """Click LinkedIn '…more' / 'see more' controls so full post text is in the DOM."""
    clicked = page.evaluate(
        """() => {
          let n = 0;
          const root = document.querySelector('[data-testid="mainFeed"]') || document.querySelector('main') || document.body;
          const nodes = Array.from(root.querySelectorAll('button, span[role="button"], a'));
          for (const el of nodes) {
            const t = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
            const aria = (el.getAttribute('aria-label') || '').trim();
            if (!/^(see more|…more|\\.\\.\\.more|show more|see translation)$/i.test(t)
                && !/see more|show more/i.test(aria)) continue;
            try { el.click(); n += 1; } catch (e) {}
          }
          return n;
        }"""
    )
    if clicked:
        page.wait_for_timeout(600)
        print(f"[scan] expanded see-more x{clicked}", flush=True)
    return int(clicked or 0)


def _extract_posts_via_js(page: Page, max_posts: int) -> list[FeedPost]:
    """LinkedIn 2026 UI: hashed CSS classes; stable data-testid / aria-labels."""
    raw = page.evaluate(
        """(maxPosts) => {
          const posts = [];
          const seen = new Set();
          const root = document.querySelector('[data-testid="mainFeed"]') || document.querySelector('main') || document.body;

          const cardRoots = [];
          const pushUnique = (el) => {
            if (!el || cardRoots.includes(el)) return;
            cardRoots.push(el);
          };

          root.querySelectorAll('[data-urn*="activity"], [data-id*="urn:li:activity"], .feed-shared-update-v2, [data-view-name="feed-full-update"]').forEach(pushUnique);
          root.querySelectorAll('[data-testid="expandable-text-box"]').forEach((box) => {
            let card = box;
            for (let i = 0; i < 18 && card.parentElement; i++) {
              card = card.parentElement;
              if (card.getAttribute('data-urn') || card.querySelector('[data-view-name="feed-control-menu"]') || (card.className && String(card.className).includes('feed-shared-update'))) {
                pushUnique(card);
                return;
              }
            }
            pushUnique(box.closest('div') || box);
          });

          const relTime = (card) => {
            const t = card.querySelector('time');
            if (t) {
              const title = (t.getAttribute('datetime') || t.getAttribute('title') || t.innerText || '').trim();
              if (title) return title;
            }
            const labels = Array.from(card.querySelectorAll('span, a')).map(el => (el.innerText || '').trim());
            for (const s of labels) {
              if (/^(just now|\\d+\\s*[smhdw]|\\d+\\s*(mo|yr|year|week|day|hour|minute|min|sec)s?)$/i.test(s)) return s;
              if (/^\\d+[smhdw]$/i.test(s)) return s;
            }
            return null;
          };

          const cleanName = (s) => {
            if (!s) return null;
            let a = String(s).replace(/\\s+/g, ' ').trim();
            a = a.replace(/\\s*Premium Profile\\s*/gi, ' ')
              .replace(/Verified Profile.*$/i, '')
              .replace(/,\\s*Open to work.*/i, '')
              .replace(/\\s*•.*$/, '')
              .replace(/\\b\\d+(st|nd|rd|th)\\b.*$/i, '')
              .replace(/^View\\s+/i, '')
              .replace(/['\\u2019]s profile$/i, '')
              .replace(/\\s+profile$/i, '')
              .replace(/['\\u2019]s$/i, '')
              .replace(/\\s+/g, ' ')
              .replace(/^[\\s,|\\-]+|[\\s,|\\-]+$/g, '');
            if (!a || /^(unknown|linkedin member|member|follow|connect|view|more)$/i.test(a)) return null;
            if (a.length < 2 || a.length > 120) return null;
            return a;
          };

          for (const card of cardRoots) {
            if (posts.length >= maxPosts) break;

            let text = '';
            const textBox = card.querySelector('[data-testid="expandable-text-box"]');
            if (textBox) text = (textBox.innerText || '').trim();
            if (!text) {
              const chunks = Array.from(card.querySelectorAll('span, p, div'))
                .map(el => (el.innerText || '').trim())
                .filter(t => t.length > 40 && t.length < 20000 && !/^(Follow|Connect|Like|Comment|Repost|Send)$/i.test(t));
              chunks.sort((a, b) => b.length - a.length);
              text = chunks[0] || '';
            }
            text = text.replace(/\\s*(…|\\.\\.\\.)\\s*more\\s*$/i, '').trim();
            if (!text || text.length < 12) continue;
            if (/^Feed post actions/i.test(text)) continue;
            if (/recommended for you/i.test(text) && text.length < 80) continue;

            // For reposts / "X liked this" engagement-surfaced items, `card` is the
            // outer wrapper containing BOTH the social-context banner (the liker/
            // commenter) and the actual embedded post nested inside. Scope author
            // lookups to the real inner update when present, so the banner's actor
            // is never mistaken for the post's real author. For a normal post,
            // `card` already IS the update, so this nested search is a safe no-op.
            const innerUpdate = card.querySelector('[data-view-name="feed-full-update"]') || card;

            let author = null;
            const menu = innerUpdate.querySelector('[data-view-name="feed-control-menu"]');
            if (menu) {
              const m = (menu.getAttribute('aria-label') || '').match(/post by (.+)$/i);
              if (m) author = cleanName(m[1]);
            }
            if (!author) {
              const hide = innerUpdate.querySelector('[data-view-name="feed-hide-post-action"]');
              if (hide) {
                const m = (hide.getAttribute('aria-label') || '').match(/Hide post by (.+)$/i);
                if (m) author = cleanName(m[1]);
              }
            }
            if (!author) {
              for (const el of innerUpdate.querySelectorAll('[aria-label]')) {
                const label = el.getAttribute('aria-label') || '';
                let m = label.match(/^(?:View )?(.+?)(?:['\\u2019]s profile|\\s+profile)$/i);
                if (m) { author = cleanName(m[1]); if (author) break; }
                if (/Verified Profile|Premium Profile|\\b\\d+(st|nd|rd|th)\\b/i.test(label)) {
                  author = cleanName(label);
                  if (author) break;
                }
              }
            }
            if (!author) {
              const links = Array.from(innerUpdate.querySelectorAll('a[href*="/in/"], a[href*="/company/"], a[href*="/school/"]'));
              for (const a of links) {
                const t = cleanName((a.innerText || '').trim().split('\\n')[0]);
                if (!t) continue;
                if (/^(follow|connect|message|view|more|see all)$/i.test(t)) continue;
                if (/followers|connections|premium|degree/i.test(t) && t.length < 24) continue;
                author = t;
                break;
              }
            }
            if (!author) {
              const actor = innerUpdate.querySelector('[data-view-name*="actor"], [data-control-name*="actor"], .update-components-actor__name, .feed-shared-actor__name');
              if (actor) author = cleanName((actor.innerText || '').split('\\n')[0]);
            }

            let url = null;
            const link = card.querySelector('a[href*="/feed/update/"], a[href*="/posts/"], a[href*="activity:"]');
            if (link && link.href) url = link.href.split('?')[0];

            const images = [];
            const imgSeen = new Set();
            for (const img of card.querySelectorAll('img')) {
              let src = img.currentSrc || img.src || img.getAttribute('data-delayed-url') || '';
              if (!src || src.startsWith('data:')) continue;
              // Skip avatars, emoji, tiny logos — keep feed photos / video thumbs
              if (/emoji|ghost|presence|profile-displayphoto|sprite|company-logo_100_100|shrink_100_100/i.test(src)) continue;
              const w = img.naturalWidth || img.width || 0;
              const h = img.naturalHeight || img.height || 0;
              // Allow lazy-loaded images with unknown size if URL looks like feed media
              const looksFeed = /feedshare|image-shrink|dms.image|thumbnail-shrink|videocover/i.test(src);
              if (!looksFeed && ((w && w < 120) || (h && h < 120))) continue;
              // Dedupe on the bare URL, but keep the query string on the stored value —
              // LinkedIn's CDN often requires a signed-access token there; stripping it
              // produced URLs that looked captured but 403'd/expired when later rendered.
              const bare = src.split('?')[0];
              if (imgSeen.has(bare)) continue;
              imgSeen.add(bare);
              images.push(src);
              if (images.length >= 6) break;
            }
            // LinkedIn native video posts render their preview via <video poster="...">,
            // not <img> — the loop above never captured these, so video posts had zero
            // images unconditionally.
            for (const video of card.querySelectorAll('video')) {
              if (images.length >= 6) break;
              let src = video.poster || '';
              if (!src || src.startsWith('data:')) continue;
              const bare = src.split('?')[0];
              if (imgSeen.has(bare)) continue;
              imgSeen.add(bare);
              images.push(src);
            }

            let socialProof = null;
            for (const el of card.querySelectorAll('[aria-label]')) {
              const label = el.getAttribute('aria-label') || '';
              if (/reaction|comment|like/i.test(label) && label.length < 180) {
                socialProof = label;
                break;
              }
            }

            const postedAt = relTime(card);
            const normUrl = (() => {
              if (!url) return '';
              let u = String(url).split('?')[0].replace(/\\/+$/, '').toLowerCase();
              let m = u.match(/urn:li:activity:\\d+/);
              if (m) return m[0];
              m = u.match(/activity[:\\-]?(\\d+)/);
              if (m) return 'urn:li:activity:' + m[1];
              return u;
            })();
            const bodyKey = ((author || '') + '|' + text.replace(/\\s+/g, ' ').trim().toLowerCase().slice(0, 240));
            const key = normUrl ? ('url:' + normUrl) : ('body:' + bodyKey);
            if (seen.has(key)) continue;
            seen.add(key);

            posts.push({
              author: author || null,
              headline: null,
              text: text.slice(0, 20000),
              url,
              socialProof,
              postedAt,
              images,
            });
          }
          return posts;
        }""",
        max_posts,
    )
    out: list[FeedPost] = []
    for i, item in enumerate(raw or [], start=1):
        imgs = item.get("images") or []
        if not isinstance(imgs, list):
            imgs = []
        out.append(
            FeedPost(
                author=item.get("author"),
                headline=item.get("headline"),
                text=item.get("text"),
                url=item.get("url"),
                socialProof=item.get("socialProof"),
                postedAt=item.get("postedAt"),
                images=[str(u) for u in imgs if u][:6],
                rank=i,
            )
        )
    return out

def is_recent_posted_at(posted_at: str | None, *, mode: str = "today") -> bool:
    """Keep posts that look like today / last ~36h. Unknown timestamps are kept."""
    if mode in {"all", "", "any"}:
        return True
    if not posted_at:
        return True
    s = posted_at.strip().lower()
    if "just now" in s or s in {"now"}:
        return True
    try:
        if "t" in s and "-" in s:
            dt = datetime.fromisoformat(s.replace("z", "+00:00"))
            age = datetime.now(timezone.utc) - (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc))
            return age.total_seconds() <= 36 * 3600
    except Exception:
        pass
    if "ago" in s:
        m2 = re.search(r"(\d+)\s*(minute|hour|day|week|month|year)s?", s)
        if m2:
            n = int(m2.group(1))
            unit = m2.group(2)
            if unit.startswith("minute") or unit.startswith("hour"):
                return True
            if unit.startswith("day"):
                return n <= 1
            return False
    m = re.match(
        r"^(\d+)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days|w|wk|week|weeks|mo|month|months|y|yr|year|years)?\b",
        s,
    )
    if not m:
        return True
    n = int(m.group(1))
    unit = (m.group(2) or "h")[0]
    if unit in {"s", "m", "h"}:
        return True
    if unit == "d":
        return n <= 1
    return False


def _scroll_feed(page: Page) -> None:
    """Scroll LinkedIn's feed hard enough to replace the virtualized card window."""
    # Bring the bottom-most activity card into view first (triggers infinite scroll).
    try:
        page.evaluate(
            """() => {
              const cards = document.querySelectorAll(
                '[data-urn*="activity"], [data-id*="urn:li:activity"], .feed-shared-update-v2'
              );
              if (cards.length) {
                cards[cards.length - 1].scrollIntoView({ block: 'end', behavior: 'instant' });
              }
              const sentinel = document.querySelector(
                '.scaffold-finite-scroll__loader, [data-testid="lazy-load"], .artdeco-loader'
              );
              if (sentinel) sentinel.scrollIntoView({ block: 'end', behavior: 'instant' });
            }"""
        )
    except Exception:
        pass

    for _step in range(3):
        moved = page.evaluate(
            """() => {
              const candidates = [
                document.querySelector('[data-testid="mainFeed"]'),
                document.querySelector('main'),
                document.querySelector('.scaffold-finite-scroll__content'),
                document.scrollingElement,
                document.documentElement,
                document.body,
              ].filter(Boolean);

              const isScrollable = (el) => {
                const style = window.getComputedStyle(el);
                const oy = style.overflowY;
                return (oy === 'auto' || oy === 'scroll' || oy === 'overlay')
                  && el.scrollHeight > el.clientHeight + 40;
              };

              let el = document.querySelector('[data-testid="mainFeed"]') || document.querySelector('main');
              while (el && el !== document.body) {
                if (isScrollable(el)) {
                  const before = el.scrollTop;
                  el.scrollBy(0, Math.max(1200, Math.floor(el.clientHeight * 1.1)));
                  return { target: 'parent', before, after: el.scrollTop, tag: el.tagName };
                }
                el = el.parentElement;
              }

              for (const c of candidates) {
                const before = c.scrollTop || window.scrollY;
                if (typeof c.scrollBy === 'function') c.scrollBy(0, 1800);
                else window.scrollBy(0, 1800);
                const after = c.scrollTop || window.scrollY;
                if (after > before + 10) return { target: 'candidate', before, after };
              }

              window.scrollBy(0, 1800);
              return { target: 'window', before: 0, after: window.scrollY };
            }"""
        )
        print(f"[scan] scroll move={moved}", flush=True)
        try:
            page.keyboard.press("PageDown")
        except Exception:
            pass
        try:
            page.mouse.wheel(0, 2200)
        except Exception:
            pass
        page.wait_for_timeout(900)

    try:
        page.keyboard.press("End")
    except Exception:
        pass
    # Give LinkedIn time to fetch the next feed chunk
    page.wait_for_timeout(2800)
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass
    page.wait_for_timeout(1800)
    _expand_see_more(page)


def _wait_for_new_feed_cards(page: Page, known_urns: set[str], timeout_ms: int = 9000) -> int:
    """After a scroll, wait until LinkedIn injects at least one unseen activity card."""
    known = list(known_urns)[:400]
    try:
        page.wait_for_function(
            """(known) => {
              const set = new Set(known || []);
              const nodes = document.querySelectorAll(
                '[data-urn*="activity"], [data-id*="urn:li:activity"]'
              );
              for (const n of nodes) {
                const u = n.getAttribute('data-urn') || n.getAttribute('data-id') || '';
                if (u && !set.has(u)) return true;
              }
              return false;
            }""",
            arg=known,
            timeout=timeout_ms,
        )
    except Exception:
        pass
    # Count how many urns are new right now
    try:
        found = page.evaluate(
            """(known) => {
              const set = new Set(known || []);
              const out = [];
              document.querySelectorAll('[data-urn*="activity"], [data-id*="urn:li:activity"]').forEach((n) => {
                const u = n.getAttribute('data-urn') || n.getAttribute('data-id') || '';
                if (u && !set.has(u)) out.push(u);
              });
              return out.length;
            }""",
            known,
        )
        return int(found or 0)
    except Exception:
        return 0


def _collect_posts(page: Page, max_posts: int) -> list[FeedPost]:
    _expand_see_more(page)
    return _extract_posts_via_js(page, max_posts)


def _save_debug(page: Page, user_id: str) -> str | None:
    try:
        out = Path(__file__).resolve().parent.parent / "out"
        out.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        shot = out / f"{user_id}-{stamp}-debug.png"
        html = out / f"{user_id}-{stamp}-debug.html"
        page.screenshot(path=str(shot), full_page=False)
        html.write_text(page.content(), encoding="utf-8", errors="ignore")
        return f"url={page.url} title={page.title()} screenshot={shot.name} html={html.name}"
    except Exception as exc:
        return f"debug-save-failed: {exc}"


def _scan_feed_sync(
    *,
    user_id: str,
    profiles_dir: Path,
    max_posts: int,
    max_scrolls: int,
    headed: bool,
    feed_url: str,
    login_wait_seconds: int = 300,
    on_awaiting_login: Any = None,
    on_progress: Any = None,
    recent_only: str = "today",
) -> ScanResponse:
    from app.profile_store import (
        has_storage_state,
        load_cookies_for_context,
        load_storage_state_file,
        persist_work_profile,
        prepare_work_profile,
        save_storage_state_from_context,
    )

    # Playwright needs a local disk profile; Azure Files is durable backup only.
    _ = profiles_dir  # durable root configured via PROFILES_DIR env
    profile_path = prepare_work_profile(user_id)
    state_file = load_storage_state_file(user_id)
    use_cookie_session = bool(state_file) or has_storage_state(user_id)

    with sync_playwright() as p:
        context: BrowserContext | None = None
        browser = None
        try:
            launch_args = ["--disable-blink-features=AutomationControlled"]
            if os.environ.get("DISPLAY") or headed:
                # Required for Chromium under Xvfb in containers
                launch_args.extend(["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])

            if use_cookie_session and state_file:
                # Preferred path: ephemeral browser + durable cookies (skips VNC)
                print(f"[scan] launching with saved storage_state ({state_file})", flush=True)
                browser = p.chromium.launch(
                    headless=not headed,
                    args=launch_args,
                    ignore_default_args=["--enable-automation"],
                )
                context = browser.new_context(
                    storage_state=str(state_file),
                    viewport={"width": 1400, "height": 900},
                )
            else:
                # First-time / no cookies: persistent profile (VNC sign-in once)
                print(f"[scan] launching persistent profile (no saved session yet)", flush=True)
                context = p.chromium.launch_persistent_context(
                    user_data_dir=str(profile_path),
                    headless=not headed,
                    viewport={"width": 1400, "height": 900},
                    args=launch_args,
                    ignore_default_args=["--enable-automation"],
                )
        except Exception as exc:
            persist_work_profile(user_id, profile_path)
            return ScanResponse(
                userId=user_id,
                scannedAt=datetime.now(timezone.utc),
                postCount=0,
                loginRequired=True,
                message=(
                    f"Browser failed to start ({exc}). "
                    "Remote login cannot open until Chromium starts — retry the scan."
                ),
                posts=[],
            )
        page = context.pages[0] if context.pages else context.new_page()

        # Re-inject cookies if persistent profile path (or state load was partial)
        cookies = load_cookies_for_context(user_id)
        if cookies and not (use_cookie_session and state_file):
            try:
                context.add_cookies(cookies)
                print(f"[scan] injected {len(cookies)} session cookies", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"[scan] cookie inject failed: {exc}", flush=True)

        def _close_browser() -> None:
            nonlocal context, browser
            try:
                if context is not None:
                    context.close()
            except Exception:
                pass
            context = None
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass
            browser = None

        try:
            print(f"[scan] opening {feed_url}", flush=True)
            page.goto(feed_url, wait_until="domcontentloaded", timeout=90_000)
            page.wait_for_timeout(4500)
            _dismiss_noise(page)
            print(f"[scan] url={page.url}", flush=True)

            try:
                page.wait_for_selector(
                    '[data-testid="mainFeed"], [data-testid="expandable-text-box"], main',
                    timeout=20_000,
                )
            except Exception:
                pass
            # Cookie sessions need extra settle time before posts appear in the DOM
            page.wait_for_timeout(5000 if use_cookie_session else 2000)
            if use_cookie_session and not _looks_logged_in(page) and not _is_login_wall(page):
                page.wait_for_timeout(3000)
                page.reload(wait_until="domcontentloaded", timeout=90_000)
                page.wait_for_timeout(4000)
                _dismiss_noise(page)

            if _is_login_wall(page):
                if not headed:
                    _close_browser()
                    persist_work_profile(user_id, profile_path)
                    return ScanResponse(
                        userId=user_id,
                        scannedAt=datetime.now(timezone.utc),
                        postCount=0,
                        loginRequired=True,
                        message=(
                            "LinkedIn login required once. Sign in via remote browser "
                            "(or paste li_at in Settings) — session is saved for later scans."
                        ),
                        posts=[],
                    )
                print(
                    f"[scan] login wall detected — sign in in the browser "
                    f"(waiting up to {login_wait_seconds}s)…",
                    flush=True,
                )
                if callable(on_awaiting_login):
                    try:
                        on_awaiting_login()
                    except Exception as exc:
                        print(f"[scan] on_awaiting_login error: {exc}", flush=True)
                deadline = time.time() + login_wait_seconds
                last_note = 0.0
                while time.time() < deadline:
                    page.wait_for_timeout(2500)
                    if _looks_logged_in(page) or not _is_login_wall(page):
                        print(f"[scan] login complete — url={page.url}", flush=True)
                        save_storage_state_from_context(user_id, context)
                        if callable(on_progress):
                            try:
                                on_progress({"phase": "logged_in", "status": "running", "postCount": 0})
                            except Exception as exc:
                                print(f"[scan] on_progress error: {exc}", flush=True)
                        break
                    now = time.time()
                    if now - last_note >= 15:
                        remaining = int(deadline - now)
                        print(
                            f"[scan] still waiting for login… {remaining}s left  url={page.url}",
                            flush=True,
                        )
                        last_note = now
                page.goto(feed_url, wait_until="domcontentloaded", timeout=90_000)
                page.wait_for_timeout(3000)
                _dismiss_noise(page)
                if _is_login_wall(page) and not _looks_logged_in(page):
                    _close_browser()
                    persist_work_profile(user_id, profile_path)
                    return ScanResponse(
                        userId=user_id,
                        scannedAt=datetime.now(timezone.utc),
                        postCount=0,
                        loginRequired=True,
                        message="Still on LinkedIn login after wait. Open the loginUrl, sign in once — session will be saved.",
                        posts=[],
                    )

            if callable(on_progress):
                try:
                    on_progress({"phase": "scrolling", "status": "running", "postCount": 0})
                except Exception as exc:
                    print(f"[scan] on_progress error: {exc}", flush=True)

            print("[scan] scrolling feed…", flush=True)
            posts: list[FeedPost] = []
            seen: set[str] = set()
            body_seen: set[str] = set()
            known_urns: set[str] = set()
            stale_scrolls = 0
            scrolls_done = 0
            for i in range(max_scrolls):
                scrolls_done = i + 1
                if _is_security_checkpoint(page):
                    cleared = _wait_out_checkpoint(
                        page,
                        seconds=min(login_wait_seconds, 420),
                        on_progress=on_progress,
                    )
                    if not cleared:
                        print("[scan] checkpoint not cleared — stopping with posts collected so far", flush=True)
                        break
                before_count = len(posts)
                batch = _collect_posts(page, max_posts * 3)
                for p in batch:
                    key = _dedupe_key(p)
                    if key in seen:
                        continue
                    body = re.sub(r"\s+", " ", f"{p.author or ''}|{(p.text or '')[:240]}".lower())
                    if body in body_seen:
                        # Prefer URL'd copy of the same body
                        if p.url:
                            for idx, prev in enumerate(posts):
                                prev_body = re.sub(
                                    r"\s+", " ", f"{prev.author or ''}|{(prev.text or '')[:240]}".lower()
                                )
                                if prev_body == body and not prev.url:
                                    posts[idx] = p
                                    seen.add(key)
                                    break
                        continue
                    seen.add(key)
                    body_seen.add(body)
                    posts.append(p)
                    if p.url:
                        known_urns.add(p.url)
                    # Track activity urns from dedupe keys
                    if key.startswith("url:"):
                        known_urns.add(key[4:])
                added = len(posts) - before_count
                if added == 0:
                    stale_scrolls += 1
                else:
                    stale_scrolls = 0
                for idx, p in enumerate(posts, start=1):
                    p.rank = idx
                print(
                    f"[scan] scroll {scrolls_done}/{max_scrolls} — "
                    f"+{added} new, {len(posts)} unique so far (stale={stale_scrolls})",
                    flush=True,
                )
                if callable(on_progress):
                    try:
                        on_progress(
                            {
                                "phase": "scrolling",
                                "status": "running",
                                "postCount": len(posts),
                                "scroll": scrolls_done,
                                "maxScrolls": max_scrolls,
                            }
                        )
                    except Exception as exc:
                        print(f"[scan] on_progress error: {exc}", flush=True)
                if len(posts) >= max_posts:
                    break
                # LinkedIn virtualizes ~6 cards in the DOM. Keep scrolling until we
                # hit the target or burn most of the scroll budget — don't quit at 6–7.
                target_floor = max(25, int(max_posts * 0.6))
                if len(posts) < target_floor:
                    # Must use a large share of scrolls before early-stop is allowed.
                    # Capped at max_scrolls itself — otherwise this could exceed the
                    # loop's own iteration budget and permanently disable early-stop.
                    min_scrolls_before_stop = min(max_scrolls, max(1, int(max_scrolls * 0.65)))
                    stale_limit = 28
                    if scrolls_done < min_scrolls_before_stop:
                        stale_limit = 999  # effectively disable early stop
                elif len(posts) < max_posts:
                    stale_limit = 14
                else:
                    stale_limit = 8
                if stale_scrolls >= stale_limit:
                    print(
                        f"[scan] early stop after {stale_scrolls} scrolls with no new unique posts "
                        f"(have {len(posts)}, target {max_posts})",
                        flush=True,
                    )
                    break
                _scroll_feed(page)
                new_cards = _wait_for_new_feed_cards(page, known_urns, timeout_ms=8000)
                if new_cards:
                    print(f"[scan] feed injected ~{new_cards} unseen card urn(s)", flush=True)
                _dismiss_noise(page)

            raw_unique = len(posts)
            posts = posts[:max_posts]
            before_recency = len(posts)
            if recent_only and recent_only not in {"all", "any"}:
                filtered = [p for p in posts if is_recent_posted_at(p.postedAt, mode=recent_only)]
                stamped = sum(1 for p in posts if p.postedAt)
                # Soft recency: never collapse a healthy scrape to a handful of posts
                min_keep = max(20, before_recency // 2)
                if stamped >= max(2, len(posts) // 3) and filtered and len(filtered) >= min_keep:
                    posts = filtered
                    for idx, p in enumerate(posts, start=1):
                        p.rank = idx
                    print(
                        f"[scan] recency={recent_only} kept {len(posts)}/{before_recency} "
                        f"(stamped={stamped})",
                        flush=True,
                    )
                elif filtered and len(filtered) < min_keep:
                    print(
                        f"[scan] recency={recent_only} would keep only {len(filtered)}/"
                        f"{before_recency} — keeping all (soft floor {min_keep})",
                        flush=True,
                    )

            message = (
                f"Captured {len(posts)} unique posts after {scrolls_done} scrolls "
                f"({raw_unique} before cap/recency; recency={recent_only or 'all'}). "
                "Scrolls ≠ posts: LinkedIn reuses cards and we dedupe."
            )
            if not posts:
                debug = _save_debug(page, user_id)
                message = (
                    "No posts extracted. LinkedIn DOM may have changed, feed empty, "
                    f"or session not fully logged in. Debug: {debug}"
                )
            # Persist cookies so the next scan can skip VNC
            if _looks_logged_in(page) or not _is_login_wall(page):
                save_storage_state_from_context(user_id, context)
            _close_browser()
            persist_work_profile(user_id, profile_path)
            return ScanResponse(
                userId=user_id,
                scannedAt=datetime.now(timezone.utc),
                postCount=len(posts),
                loginRequired=False,
                message=message,
                posts=posts,
            )
        except Exception as exc:
            _close_browser()
            persist_work_profile(user_id, profile_path)
            return ScanResponse(
                userId=user_id,
                scannedAt=datetime.now(timezone.utc),
                postCount=0,
                loginRequired=False,
                message=f"Scan failed: {exc}",
                posts=[],
            )


async def scan_feed(
    *,
    user_id: str,
    profiles_dir: Path,
    max_posts: int,
    max_scrolls: int,
    headed: bool,
    feed_url: str,
    login_wait_seconds: int = 300,
    on_awaiting_login: Any = None,
    on_progress: Any = None,
    recent_only: str = "today",
) -> ScanResponse:
    return await asyncio.to_thread(
        _scan_feed_sync,
        user_id=user_id,
        profiles_dir=profiles_dir,
        max_posts=max_posts,
        max_scrolls=max_scrolls,
        headed=headed,
        feed_url=feed_url,
        login_wait_seconds=login_wait_seconds,
        on_awaiting_login=on_awaiting_login,
        on_progress=on_progress,
        recent_only=recent_only,
    )
