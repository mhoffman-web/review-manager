"""Single always-on process: sync every N minutes, send the morning report once a day."""
from __future__ import annotations

import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import settings
from .db import session_scope
from .reports import already_sent_today, send_morning_report
from .sync import sync_all

log = logging.getLogger(__name__)


def tick() -> None:
    with session_scope() as s:
        totals = sync_all(s)
        log.info("sync tick: %s", totals)
    now_local = datetime.now(ZoneInfo(settings.timezone))
    if now_local.hour >= settings.report_hour_local:
        with session_scope() as s:
            if not already_sent_today(s):
                log.info("sending morning report")
                send_morning_report(s)


def run_forever() -> None:
    interval = max(1, settings.sync_interval_minutes) * 60
    log.info("worker started: sync every %d min, report at %02d:00 %s",
             settings.sync_interval_minutes, settings.report_hour_local, settings.timezone)
    while True:
        started = time.time()
        try:
            tick()
        except Exception:
            log.exception("worker tick failed")
        time.sleep(max(30, interval - (time.time() - started)))
