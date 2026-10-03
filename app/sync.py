"""Pull reviews from every active source link and upsert them."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Dict, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .events import record
from .models import Review, ReviewSourceLink, SyncRun
from .sources import get_adapter
from .sources.base import NormalizedReview, ReviewSourceAdapter
from .text_intel import THEMES, apply_intel, build_roster

log = logging.getLogger(__name__)

# Incremental syncs re-read this much history so late edits and clock skew
# never cause a miss. Cheap: it is a page or two per location.
INCREMENTAL_OVERLAP = timedelta(days=3)


def _apply(session: Session, review: Review, nr: NormalizedReview, now: datetime, is_new: bool = False) -> bool:
    """Copy normalized fields onto the row, noting rating/text changes and first replies.
    Returns True if anything changed."""
    changed = False
    if not is_new:
        if review.rating is not None and nr.rating is not None and review.rating != nr.rating:
            review.prev_rating, review.rating_changed_at = review.rating, now
            record(session, review, "rating_changed", at=now, **{"from": review.rating, "to": nr.rating})
        if (review.text or "").strip() and (review.text or "") != (nr.text or ""):
            record(session, review, "text_changed", at=now, before=(review.text or "")[:500])
        if nr.owner_reply_text and not review.has_owner_reply and review.first_replied_at is None:
            review.first_replied_at = nr.owner_reply_updated_at or now      # replied outside this app
    elif nr.owner_reply_text:
        review.first_replied_at = nr.owner_reply_updated_at or nr.created_at
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
        review.is_deleted, review.removed_at = False, None
        record(session, review, "restored", at=now)
        changed = True
    review.last_seen_at = now
    return changed


def _manual_examples(session: Session):
    """Recent hand-corrected themes, fed to the AI classifier as examples."""
    rows = session.execute(select(Review.text, Review.category).where(Review.category_source == "manual", Review.text.isnot(None),
                                                                        Review.category.isnot(None)).order_by(Review.id.desc()).limit(12)).all()
    return [(t, c) for t, c in rows if (t or "").strip()]


def _ai_classify(session: Session, review: Review) -> None:
    """Replace the keyword theme with Claude's category when AI is configured. Never fails the sync,
    and never touches a theme someone set by hand."""
    from .config import settings
    if review.category_source == "manual":
        return
    if not (settings.ai_enabled and settings.ai_classify and review.is_negative and (review.text or "").strip()):
        return
    try:
        from .ai import classify_negative
        review.category = classify_negative(review, THEMES, examples=_manual_examples(session))
        review.category_source = "ai"
    except Exception as exc:  # keyword category from apply_intel stays
        log.warning("AI classification skipped for review %s: %s", review.external_id, exc)


def _intel(session: Session, review: Review, roster) -> None:
    """Mentions + keyword theme, preserving a hand-set theme."""
    keep = review.category if review.category_source == "manual" else None
    apply_intel(session, review, roster)
    if keep is not None:
        review.category = keep
    elif review.category and not review.category_source:
        review.category_source = "keyword"


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
                _apply(session, review, nr, now, is_new=True)
                session.add(review)
                session.flush()
                _intel(session, review, roster)
                _ai_classify(session, review)
                run.reviews_new += 1
            else:
                if review.source_link_id != link.id:
                    review.source_link_id = link.id  # listing moved accounts; follow it
                old_text = review.text
                if _apply(session, review, nr, now):
                    run.reviews_updated += 1
                    if review.text != old_text:
                        _intel(session, review, roster)

        if full:
            # Anything we did not see on a full pull is gone from the platform (the reviewer deleted
            # it, or the platform removed it). A full pull that returned nothing at all is treated as
            # a platform hiccup, never as "every review vanished".
            stale = session.execute(
                select(Review).where(Review.source_link_id == link.id, Review.is_deleted.is_(False))
            ).scalars().all()
            missing = [r for r in stale if r.external_id not in seen_ids]
            if missing and run.reviews_seen == 0:
                log.warning("full pull of %s returned no reviews; not marking %d reviews removed", link.display_name, len(missing))
            else:
                for r in missing:
                    r.is_deleted, r.removed_at = True, now
                    record(session, r, "removed", at=now, rating=r.rating, text=(r.text or "")[:500])
                    if r.report_status == "reported":
                        r.report_status = "removed"
                        record(session, r, "report_outcome", at=now, outcome="removed", auto=True)

        link.last_synced_at = now
        link.last_sync_status = "ok"
        link.fail_count, link.last_error, link.failure_notified_at = 0, None, None
        run.status = "ok"
    except Exception as exc:  # keep going for other locations
        log.exception("sync failed for review_sources.id=%s (%s)", link.id, link.display_name)
        link.last_sync_status = "error"
        link.fail_count = (link.fail_count or 0) + 1
        link.last_error = str(exc)[:2000]
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
    if links:
        try:
            from .alerts import send_alerts
            sent = send_alerts(session)
            session.commit()
            if sent:
                totals["alerts"] = sent.get("count", 0)
        except Exception:  # alerts must never break a sync
            log.exception("alert send failed")
            session.rollback()
    return totals
