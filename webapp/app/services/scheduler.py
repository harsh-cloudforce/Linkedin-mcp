"""APScheduler daily Market Pulse runs."""
from __future__ import annotations

import logging
import os

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.services.pipeline import run_daily_for_all_active_users

log = logging.getLogger("market_pulse.scheduler")
_scheduler: AsyncIOScheduler | None = None


def start_scheduler() -> AsyncIOScheduler | None:
    global _scheduler
    if os.getenv("DAILY_SCAN_ENABLED", "true").lower() not in {"1", "true", "yes"}:
        log.info("Daily scanner disabled (DAILY_SCAN_ENABLED=false)")
        return None

    hour = int(os.getenv("DAILY_SCAN_HOUR_UTC", "13"))  # ~9am US Eastern-ish
    minute = int(os.getenv("DAILY_SCAN_MINUTE_UTC", "0"))
    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(
        run_daily_for_all_active_users,
        CronTrigger(hour=hour, minute=minute),
        id="daily_market_pulse",
        replace_existing=True,
    )
    _scheduler.start()
    log.info("Daily scanner scheduled at %02d:%02d UTC", hour, minute)
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
