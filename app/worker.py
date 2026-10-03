"""Single always-on process: sync every N minutes (one full re-pull a day so removed
reviews are noticed), send the morning report once a day, email instant alerts after each sync."""
from __future__ import annotations

import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from .config import settings
from .db import session_scope
from .models import SyncRun
from .reports import missed_report_date, pending_editions, send_morning_report
from .sync import sync_all

log = logging.getLogger(__name__)


def needs_full_pull(session, now_local: datetime) -> bool:
    """True once per local day, at or after FULL_SYNC_HOUR_LOCAL."""
    if now_local.hour < settings.full_sync_hour_local:
        return False
    last_full = session.execute(select(func.max(SyncRun.started_at)).where(SyncRun.full.is_(True))).scalar()
    if last_full is None:
        return True
    last_local = last_full.replace(tzinfo=ZoneInfo("UTC")).astimezone(now_local.tzinfo)
    return last_local.date() < now_local.date()


def tick() -> None:
    now_local = datetime.now(ZoneInfo(settings.timezone))
    with session_scope() as s:
        full = needs_full_pull(s, now_local)
        totals = sync_all(s, full=full)
        log.info("sync tick (%s): %s", "full" if full else "incremental", totals)
    if now_local.hour >= settings.report_hour_local:
        with session_scope() as s:
            missed = missed_report_date(s, now_local.date())
            if missed:
                log.warning("the report for %s never went out (worker down past midnight); sending it late", missed)
                send_morning_report(s, report_date=missed)
            todo = pending_editions(s, now_local.date())
            if todo:
                log.info("sending morning report: %s", ", ".join(todo))
                send_morning_report(s, editions=todo, report_date=now_local.date())


def run_forever() -> None:
    interval = max(1, settings.sync_interval_minutes) * 60
    log.info("worker started: sync every %d min, full pull after %02d:00, report at %02d:00 %s",
             settings.sync_interval_minutes, settings.full_sync_hour_local, settings.report_hour_local, settings.timezone)
    while True:
        started = time.time()
        try:
            tick()
        except Exception:
            log.exception("worker tick failed")
        time.sleep(max(30, interval - (time.time() - started)))
