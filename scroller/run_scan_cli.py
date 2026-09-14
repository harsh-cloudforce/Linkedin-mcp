"""
Direct CLI LinkedIn feed scan (no HTTP server).

PowerShell:
  cd scroller
  set PLAYWRIGHT_BROWSERS_PATH to .playwright-browsers
  .venv\\Scripts\\python.exe run_scan_cli.py --userId USER --headed
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Ensure package imports work when run as a script
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.linkedin_feed import _scan_feed_sync  # noqa: E402


def default_profiles_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / "LinkedInMarketPulse" / "profiles"


def main() -> int:
    parser = argparse.ArgumentParser(description="Scroll LinkedIn home feed and dump JSON")
    parser.add_argument("--userId", default=os.environ.get("USERNAME", "user"))
    parser.add_argument("--maxPosts", type=int, default=60)
    parser.add_argument("--maxScrolls", type=int, default=20)
    parser.add_argument("--headed", action="store_true", default=True)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--loginWaitSeconds", type=int, default=300)
    parser.add_argument(
        "--profilesDir",
        default=str(default_profiles_dir()),
        help="Browser profile root (keep off OneDrive)",
    )
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    user_id = args.userId.strip().lower().replace(" ", "-")
    headed = not args.headless
    profiles_dir = Path(args.profilesDir)
    profiles_dir.mkdir(parents=True, exist_ok=True)

    browsers = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    print(f"userId={user_id}")
    print(f"profilesDir={profiles_dir}")
    print(f"PLAYWRIGHT_BROWSERS_PATH={browsers}")
    print("If Chromium opens on LinkedIn login, sign in within the wait window.")

    result = _scan_feed_sync(
        user_id=user_id,
        profiles_dir=profiles_dir,
        max_posts=args.maxPosts,
        max_scrolls=args.maxScrolls,
        headed=headed,
        feed_url="https://www.linkedin.com/feed/",
        login_wait_seconds=args.loginWaitSeconds,
    )

    payload = result.model_dump(mode="json")
    out_dir = ROOT / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out) if args.out else out_dir / f"{user_id}-latest-scan.json"
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(f"Saved: {out_path}")
    print(
        f"postCount={result.postCount} loginRequired={result.loginRequired} message={result.message}"
    )
    return 0 if result.postCount > 0 or result.loginRequired else 1


if __name__ == "__main__":
    raise SystemExit(main())
