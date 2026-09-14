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
    if _looks_logged_in(page):
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
    return False


def _dedupe_key(post: FeedPost) -> str:
    if post.url:
        return post.url.rstrip("/").lower()
    blob = f"{post.author or ''}|{(post.text or '')[:180]}".lower()
    return re.sub(r"\s+", " ", blob)


def _extract_posts_via_js(page: Page, max_posts: int) -> list[FeedPost]:
    """LinkedIn 2026 UI: hashed CSS classes; stable data-testid / aria-labels."""
    raw = page.evaluate(
        """(maxPosts) => {
          const posts = [];
          const seen = new Set();
          const root = document.querySelector('[data-testid="mainFeed"]') || document.body;
          const boxes = Array.from(root.querySelectorAll('[data-testid="expandable-text-box"]'));

          for (const box of boxes) {
            if (posts.length >= maxPosts) break;
            const text = (box.innerText || '').trim();
            if (!text || text.length < 20) continue;

            let card = box;
            for (let i = 0; i < 14 && card.parentElement; i++) {
              card = card.parentElement;
              if (card.querySelector('[data-view-name="feed-control-menu"]')) break;
            }

            let author = null;
            const menu = card.querySelector('[data-view-name="feed-control-menu"]');
            if (menu) {
              const m = (menu.getAttribute('aria-label') || '').match(/post by (.+)$/i);
              if (m) author = m[1].trim();
            }
            if (!author) {
              const hide = card.querySelector('[data-view-name="feed-hide-post-action"]');
              if (hide) {
                const m = (hide.getAttribute('aria-label') || '').match(/Hide post by (.+)$/i);
                if (m) author = m[1].trim();
              }
            }
            if (!author) {
              const named = card.querySelector('[aria-label*="Verified Profile"], [aria-label*="1st"], [aria-label*="2nd"]');
              if (named) {
                author = (named.getAttribute('aria-label') || '')
                  .replace(/Verified Profile.*$/i, '')
                  .replace(/\\b\\d+(st|nd|rd|th)\\b.*$/i, '')
                  .trim();
              }
            }

            let url = null;
            const link = card.querySelector('a[href*="/feed/update/"], a[href*="/posts/"]');
            if (link && link.href) url = link.href.split('?')[0];

            let socialProof = null;
            const socialBtns = card.querySelectorAll('[aria-label]');
            for (const el of socialBtns) {
              const label = el.getAttribute('aria-label') || '';
              if (/reaction|comment|like/i.test(label) && label.length < 180) {
                socialProof = label;
                break;
              }
            }

            const key = (url || '') + '|' + (author || '') + '|' + text.slice(0, 160);
            if (seen.has(key)) continue;
            seen.add(key);

            posts.push({
              author: author || null,
              headline: null,
              text: text.slice(0, 4000),
              url,
              socialProof,
            });
          }
          return posts;
        }""",
        max_posts,
    )
    out: list[FeedPost] = []
    for i, item in enumerate(raw or [], start=1):
        out.append(
            FeedPost(
                author=item.get("author"),
                headline=item.get("headline"),
                text=item.get("text"),
                url=item.get("url"),
                socialProof=item.get("socialProof"),
                rank=i,
            )
        )
    return out


def _scroll_feed(page: Page) -> None:
    """Scroll LinkedIn's feed container (page mouse.wheel often does nothing on their layout)."""
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
            return (oy === 'auto' || oy === 'scroll' || oy === 'overlay') && el.scrollHeight > el.clientHeight + 40;
          };

          // Walk up from mainFeed to find the real scroll parent
          let el = document.querySelector('[data-testid="mainFeed"]') || document.querySelector('main');
          while (el && el !== document.body) {
            if (isScrollable(el)) {
              const before = el.scrollTop;
              el.scrollBy(0, Math.max(900, Math.floor(el.clientHeight * 0.9)));
              return { target: 'parent', before, after: el.scrollTop, tag: el.tagName };
            }
            el = el.parentElement;
          }

          for (const c of candidates) {
            const before = c.scrollTop || window.scrollY;
            if (typeof c.scrollBy === 'function') c.scrollBy(0, 1400);
            else window.scrollBy(0, 1400);
            const after = c.scrollTop || window.scrollY;
            if (after > before + 10) return { target: 'candidate', before, after };
          }

          window.scrollBy(0, 1400);
          return { target: 'window', before: 0, after: window.scrollY };
        }"""
    )
    print(f"[scan] scroll move={moved}", flush=True)
    try:
        page.keyboard.press("PageDown")
    except Exception:
        pass
    page.wait_for_timeout(2200)


def _collect_posts(page: Page, max_posts: int) -> list[FeedPost]:
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
) -> ScanResponse:
    profile_path = profiles_dir / user_id
    profile_path.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        try:
            launch_args = ["--disable-blink-features=AutomationControlled"]
            if os.environ.get("DISPLAY"):
                # Required for Chromium under Xvfb in containers
                launch_args.extend(["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
            context: BrowserContext = p.chromium.launch_persistent_context(
                user_data_dir=str(profile_path),
                headless=not headed,
                viewport={"width": 1400, "height": 900},
                args=launch_args,
                ignore_default_args=["--enable-automation"],
            )
        except Exception as exc:
            return ScanResponse(
                userId=user_id,
                scannedAt=datetime.now(timezone.utc),
                postCount=0,
                loginRequired=False,
                message=(
                    f"Browser failed to start ({exc}). "
                    "Keep PROFILES_DIR under %LOCALAPPDATA%\\LinkedInMarketPulse\\profiles (not OneDrive)."
                ),
                posts=[],
            )
        page = context.pages[0] if context.pages else context.new_page()

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
            page.wait_for_timeout(2000)

            if _is_login_wall(page):
                if not headed:
                    context.close()
                    return ScanResponse(
                        userId=user_id,
                        scannedAt=datetime.now(timezone.utc),
                        postCount=0,
                        loginRequired=True,
                        message="LinkedIn login required. Re-run with headed=true and sign in once.",
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
                    page.wait_for_timeout(3000)
                    if _looks_logged_in(page) or not _is_login_wall(page):
                        print(f"[scan] login complete — url={page.url}", flush=True)
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
                    context.close()
                    return ScanResponse(
                        userId=user_id,
                        scannedAt=datetime.now(timezone.utc),
                        postCount=0,
                        loginRequired=True,
                        message="Still on LinkedIn login after wait. Open the loginUrl, sign in, then retry.",
                        posts=[],
                    )

            print("[scan] scrolling feed…", flush=True)
            posts: list[FeedPost] = []
            seen: set[str] = set()
            for i in range(max_scrolls):
                batch = _collect_posts(page, max_posts)
                for p in batch:
                    key = _dedupe_key(p)
                    if key in seen:
                        continue
                    seen.add(key)
                    posts.append(p)
                    if len(posts) >= max_posts:
                        break
                # re-rank
                for idx, p in enumerate(posts, start=1):
                    p.rank = idx
                print(f"[scan] scroll {i + 1}/{max_scrolls} — posts so far: {len(posts)}", flush=True)
                if len(posts) >= max_posts:
                    break
                _scroll_feed(page)
                _dismiss_noise(page)

            posts = posts[:max_posts]
            message = None
            if not posts:
                debug = _save_debug(page, user_id)
                message = (
                    "No posts extracted. LinkedIn DOM may have changed, feed empty, "
                    f"or session not fully logged in. Debug: {debug}"
                )
            context.close()
            return ScanResponse(
                userId=user_id,
                scannedAt=datetime.now(timezone.utc),
                postCount=len(posts),
                loginRequired=False,
                message=message,
                posts=posts,
            )
        except Exception as exc:
            try:
                context.close()
            except Exception:
                pass
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
    )
