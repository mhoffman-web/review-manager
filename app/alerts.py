"""Instant alerts, emailed minutes after the sync that noticed them: new negative reviews,
reviews that are no longer on the platform, and listings that keep failing to sync.
Recipients are the "alerts" list on Admin -> Report recipients."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .config import ALERTS_KEY, settings
from .events import record
from .models import ReportRecipient, Review, ReviewSourceLink

log = logging.getLogger(__name__)
_env = Environment(loader=FileSystemLoader(str(Path(__file__).parent / "templates")), autoescape=select_autoescape(["html"]))

NEW_WITHIN = timedelta(hours=36)      # a review first seen longer ago than this is history, not news
POSTED_WITHIN = timedelta(days=14)    # ... and so is anything the customer posted more than two weeks ago


@dataclass
class AlertBatch:
    negatives: List[Review] = field(default_factory=list)
    removed: List[Review] = field(default_factory=list)
    failing: List[ReviewSourceLink] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.negatives) + len(self.removed) + len(self.failing)

    @property
    def empty(self) -> bool:
        return self.count == 0


def alert_recipients(session: Session) -> List[str]:
    rows = session.execute(select(ReportRecipient).where(ReportRecipient.active.is_(True))).scalars().all()
    return [r.email for r in rows if ALERTS_KEY in r.editions]


def collect(session: Session, now: Optional[datetime] = None) -> AlertBatch:
    now = now or datetime.utcnow()
    opts = (selectinload(Review.source_link).selectinload(ReviewSourceLink.location),)
    batch = AlertBatch()
    if settings.alert_negatives:
        batch.negatives = list(session.execute(
            select(Review).join(ReviewSourceLink).where(
                ReviewSourceLink.active.is_(True), Review.is_deleted.is_(False), Review.alerted_at.is_(None),
                Review.rating <= settings.negative_rating_max, Review.first_seen_at >= now - NEW_WITHIN,
                Review.created_at_source >= now - POSTED_WITHIN).options(*opts).order_by(Review.created_at_source)).scalars().all())
    batch.removed = list(session.execute(
        select(Review).join(ReviewSourceLink).where(
            ReviewSourceLink.active.is_(True), Review.is_deleted.is_(True), Review.removed_at.isnot(None),
            Review.removal_notified_at.is_(None)).options(*opts).order_by(Review.removed_at)).scalars().all())
    batch.failing = list(session.execute(
        select(ReviewSourceLink).where(ReviewSourceLink.active.is_(True), ReviewSourceLink.fail_count >= settings.alert_fail_threshold,
                                       ReviewSourceLink.failure_notified_at.is_(None))
        .options(selectinload(ReviewSourceLink.location))).scalars().all())
    return batch


def subject_for(batch: AlertBatch) -> str:
    parts = []
    if batch.negatives:
        parts.append(f"{len(batch.negatives)} new negative review{'s' if len(batch.negatives) != 1 else ''}")
    if batch.removed:
        parts.append(f"{len(batch.removed)} review{'s' if len(batch.removed) != 1 else ''} removed")
    if batch.failing:
        parts.append(f"{len(batch.failing)} listing{'s' if len(batch.failing) != 1 else ''} failing to sync")
    return "Review alert: " + ", ".join(parts)


def render_html(batch: AlertBatch) -> str:
    tz = ZoneInfo(settings.timezone)
    return _env.get_template("alert_email.html").render(
        b=batch, settings=settings, to_local=lambda dt: dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz) if dt else None)


def render_text(batch: AlertBatch) -> str:
    tz = ZoneInfo(settings.timezone)
    def loc(dt):
        return dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz).strftime("%a %b %-d %-I:%M %p") if dt else ""
    lines = [subject_for(batch), ""]
    for r in batch.negatives:
        lines.append(f"NEW {r.rating}* at {r.location.name if r.location else r.source_link.display_name} - {r.author_name or 'Anonymous'} {loc(r.created_at_source)}")
        lines.append(f"    {(r.text or '(rating only)')[:300]}")
        lines.append(f"    {settings.app_base_url}/reviews/{r.id}")
    for r in batch.removed:
        lines.append(f"REMOVED {r.rating or '-'}* at {r.location.name if r.location else r.source_link.display_name} - {r.author_name or 'Anonymous'}, noticed {loc(r.removed_at)}")
        lines.append(f"    {(r.text or '(rating only)')[:300]}")
    for l in batch.failing:
        lines.append(f"SYNC FAILING {l.location.name if l.location else l.display_name}: {l.fail_count} runs in a row - {(l.last_error or '')[:200]}")
    return "\n".join(lines)


def send_alerts(session: Session, dry_run: bool = False, to_override: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    """Email anything new and stamp it so it is never sent twice. Returns a summary or None when
    there was nothing to send or nobody to send it to. Without recipients nothing is stamped, so
    the alerts go out once a list exists."""
    batch = collect(session)
    if batch.empty:
        return None
    to = to_override or alert_recipients(session)
    if not to:
        log.info("%d alert item(s) waiting; no alert recipients configured", batch.count)
        return None
    subject, html, text = subject_for(batch), render_html(batch), render_text(batch)
    if not dry_run:
        from .mailer import send_email
        send_email(to, subject, html, text)
    now = datetime.utcnow()
    for r in batch.negatives:
        r.alerted_at = now
        record(session, r, "alert_sent", at=now, what="new_negative", to=len(to))
    for r in batch.removed:
        r.removal_notified_at = now
        record(session, r, "alert_sent", at=now, what="removed", to=len(to))
    for l in batch.failing:
        l.failure_notified_at = now
    session.flush()
    return {"count": batch.count, "to": to, "subject": subject, "html": html, "dry_run": dry_run}
