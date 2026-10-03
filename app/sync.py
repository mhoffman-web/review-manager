"""Pull reviews from every active source link and upsert them."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Dict, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Review, ReviewSourceLink, SyncRun
from .sources import get_adapter
from .sources.base import NormalizedReview, ReviewSourceAdapter
from .text_intel import apply_intel, build_roster

log = logging.getLogger(__name__)

# Incremental syncs re-read this much history so late edits and clock skew
# never cause a miss. Cheap: it is a page or two per location.
INCREMENTAL_OVERLAP = timedelta(days=3)


def _apply(review: Review, nr: NormalizedReview, now: datetime) -> bool:
    """Copy normalized fields onto the row. Returns True if anything changed."""
    changed = False
    fields = {
        "author_name": nr.author_name,
        "author_is_anonymous": nr.author_is_anonymous,
        "rating": nr.rating,
        "text": nr.text,
        "created_at_source": nr.created_at,
        "updated_at_source": nr.updated_at,
        "has_owner_reply": bool(nr.owner_reply_text),
        "owner_reply_text": nr.owner_reply_text,
        "owner_reply_updated_at": nr.owner_reply_updated_at,
        "raw_json": nr.raw_json,
    }
    for k, v in fields.items():
        if getattr(review, k) != v:
            setattr(review, k, v)
            changed = True
    if review.is_deleted:
        review.is_deleted = False
        changed = True
    review.last_seen_at = now
    return changed


def sync_link(
    session: Session,
    link: ReviewSourceLink,
    full: bool = False,
    adapter: Optional[ReviewSourceAdapter] = None,
) -> SyncRun:
    adapter = adapter or get_adapter(link.source)
    run = SyncRun(source_link_id=link.id, full=full)
    session.add(run)
    session.flush()

    since = None
    if not full and link.last_synced_at is not None:
        since = link.last_synced_at - INCREMENTAL_OVERLAP

    now = datetime.utcnow()
    seen_ids = set()
    roster = build_roster(session)
    try:
        for nr, summary in adapter.fetch_reviews(link, since=since):
            if summary is not None:
                link.avg_rating = summary.avg_rating
                link.total_review_count = summary.total_review_count
            if nr.external_id == "__none__":
                continue
            run.reviews_seen += 1
            seen_ids.add(nr.external_id)
            review = session.execute(
                select(Review).where(Review.source == link.source, Review.external_id == nr.external_id)
            ).scalar_one_or_none()
            if review is None:
                review = Review(source_link_id=link.id, source=link.source, external_id=nr.external_id,
                                created_at_source=nr.created_at, updated_at_source=nr.updated_at,
                                first_seen_at=now)
                _apply(review, nr, now)
                session.add(review)
                session.flush()
                apply_intel(session, review, roster)
                run.reviews_new += 1
            else:
                if review.source_link_id != link.id:
                    review.source_link_id = link.id  # listing moved accounts; follow it
                old_text = review.text
                if _apply(review, nr, now):
                    run.reviews_updated += 1
                    if review.text != old_text:
                        apply_intel(session, review, roster)

        if full:
            # Anything we did not see on a full pull is gone from the platform.
            stale = session.execute(
                select(Review).where(Review.source_link_id == link.id, Review.is_deleted.is_(False))
            ).scalars().all()
            for r in stale:
                if r.external_id not in seen_ids:
                    r.is_deleted = True

        link.last_synced_at = now
        link.last_sync_status = "ok"
        run.status = "ok"
    except Exception as exc:  # keep going for other locations
        log.exception("sync failed for review_sources.id=%s (%s)", link.id, link.display_name)
        link.last_sync_status = "error"
        run.status = "error"
        run.error = str(exc)[:2000]
    finally:
        run.finished_at = datetime.utcnow()
        session.flush()
    return run


def sync_all(session: Session, full: bool = False, source: Optional[str] = None) -> Dict[str, int]:
    q = select(ReviewSourceLink).where(ReviewSourceLink.active.is_(True))
    if source:
        q = q.where(ReviewSourceLink.source == source)
    links = session.execute(q).scalars().all()
    totals = {"links": 0, "ok": 0, "error": 0, "seen": 0, "new": 0, "updated": 0}
    adapters: Dict[str, ReviewSourceAdapter] = {}
    for link in links:
        adapters.setdefault(link.source, get_adapter(link.source))
        run = sync_link(session, link, full=full, adapter=adapters[link.source])
        session.commit()
        totals["links"] += 1
        totals["ok" if run.status == "ok" else "error"] += 1
        totals["seen"] += run.reviews_seen
        totals["new"] += run.reviews_new
        totals["updated"] += run.reviews_updated
        log.info("synced %-28s seen=%-4d new=%-3d updated=%-3d %s",
                 link.display_name or link.external_location_id, run.reviews_seen,
                 run.reviews_new, run.reviews_updated, run.status)
    return totals
