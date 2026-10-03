"""Pull reviews from every active source link and upsert them."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Dict, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
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
        # Back on the platform. Clear the removal stamps so a later disappearance alerts
        # again, and reopen a report we auto-closed as "removed" when it vanished.
        review.is_deleted, review.removed_at, review.removal_notified_at = False, None, None
        record(session, review, "restored", at=now)
        if review.report_status == "removed":
            review.report_status = "reported"
            record(session, review, "report_outcome", at=now, outcome="reported", auto=True,
                   note="review reappeared on the platform; report reopened")
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


def _mark_removed(session: Session, link: ReviewSourceLink, run: SyncRun, seen_ids: set,
                  total: Optional[int], now: datetime) -> int:
    """After a full pull, flag reviews the platform no longer returns.

    Three guards keep one bad pull from emailing about a mass deletion and
    auto-closing every open report:
      * a pull that returned nothing is a platform hiccup, not "every review vanished";
      * a pull that saw fewer reviews than the platform's own total is truncated
        (a dropped page token, Google hiding reviews mid-pull), so it proves nothing;
      * a review is removed only once it has been missing from REMOVAL_CONFIRM_PULLS
        consecutive successful full pulls, i.e. it was last seen before the previous
        one started. One miss is a hold, not a verdict.
    Returns how many reviews were marked."""
    stale = session.execute(
        select(Review).where(Review.source_link_id == link.id, Review.is_deleted.is_(False))
    ).scalars().all()
    missing = [r for r in stale if r.external_id not in seen_ids]
    if not missing:
        return 0
    name = link.display_name or link.external_location_id
    if run.reviews_seen == 0:
        run.complete = False
        log.warning("full pull of %s returned no reviews; not marking %d reviews removed", name, len(missing))
        return 0
    if total is not None and run.reviews_seen < total - max(1, total // 50):
        run.complete = False
        log.warning("full pull of %s saw %d of the %d reviews the platform reports; treating it as truncated, "
                    "not marking %d reviews removed", name, run.reviews_seen, total, len(missing))
        return 0
    confirm = max(1, settings.removal_confirm_pulls)
    if confirm > 1:
        prev_starts = session.execute(
            select(SyncRun.started_at).where(SyncRun.source_link_id == link.id, SyncRun.full.is_(True),
                                             SyncRun.status == "ok", SyncRun.complete.is_(True), SyncRun.id != run.id)
            .order_by(SyncRun.started_at.desc()).limit(confirm - 1)
        ).scalars().all()
        if len(prev_starts) < confirm - 1:
            log.info("full pull of %s: %d reviews unseen, holding until %d consecutive full pulls agree",
                     name, len(missing), confirm)
            return 0
        cutoff = prev_starts[-1]
        held = [r for r in missing if r.last_seen_at is None or r.last_seen_at >= cutoff]
        missing = [r for r in missing if r.last_seen_at is not None and r.last_seen_at < cutoff]
        if held:
            log.info("full pull of %s: %d reviews missed once, holding for the next full pull", name, len(held))
    for r in missing:
        r.is_deleted, r.removed_at = True, now
        record(session, r, "removed", at=now, rating=r.rating, text=(r.text or "")[:500])
        if r.report_status == "reported":
            r.report_status = "removed"
            record(session, r, "report_outcome", at=now, outcome="removed", auto=True)
    return len(missing)


def _note_failure(link: Optional[ReviewSourceLink], run: SyncRun, err: str) -> None:
    run.status, run.error, run.finished_at = "error", err, datetime.utcnow()
    if link is not None:
        link.last_sync_status = "error"
        link.fail_count = (link.fail_count or 0) + 1
        link.last_error = err


def sync_link(
    session: Session,
    link: ReviewSourceLink,
    full: bool = False,
    adapter: Optional[ReviewSourceAdapter] = None,
) -> SyncRun:
    adapter = adapter or get_adapter(link.source)
    link_id, name = link.id, link.display_name or link.external_location_id
    run = SyncRun(source_link_id=link.id, full=full)
    session.add(run)
    session.flush()
    started = run.started_at

    since = None
    if not full and link.last_synced_at is not None:
        since = link.last_synced_at - INCREMENTAL_OVERLAP

    now = datetime.utcnow()
    seen_ids = set()
    total: Optional[int] = None
    roster = build_roster(session)
    try:
        for nr, summary in adapter.fetch_reviews(link, since=since):
            if summary is not None:
                link.avg_rating = summary.avg_rating
                link.total_review_count = summary.total_review_count
                total = summary.total_review_count
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
            _mark_removed(session, link, run, seen_ids, total, now)

        link.last_synced_at = now
        link.last_sync_status = "ok"
        link.fail_count, link.last_error, link.failure_notified_at = 0, None, None
        run.status = "ok"
        run.finished_at = datetime.utcnow()
        session.flush()
    except Exception as exc:  # keep going for other locations
        # `link` may already be expired by a failed flush, so only use what was read up front.
        log.exception("sync failed for review_sources.id=%s (%s)", link_id, name)
        err = str(exc)[:2000]
        try:
            # Keep whatever this pull already wrote and record the failure beside it.
            _note_failure(link, run, err)
            session.flush()
        except Exception:
            # The session itself is broken (a failed flush, e.g. the admin's manual Sync racing the
            # worker on the same review). Only a rollback makes it usable again, and the rollback
            # discards everything this pull wrote, `run` included. Start clean so the failure and
            # the listing's fail_count survive the caller's commit instead of poisoning it.
            log.warning("session unusable after sync failure of review_sources.id=%s; rolling back", link_id)
            session.rollback()
            link = session.get(ReviewSourceLink, link_id)
            run = SyncRun(source_link_id=link_id, full=full, started_at=started,
                          reviews_seen=0, reviews_new=0, reviews_updated=0)
            session.add(run)
            _note_failure(link, run, err)
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
        lid = link.id
        try:
            adapters.setdefault(link.source, get_adapter(link.source))
            run = sync_link(session, link, full=full, adapter=adapters[link.source])
            session.commit()
        except Exception:
            # sync_link already copes with its own failures; this catches a broken commit or a
            # missing adapter so one listing can never stop the others or the tick.
            log.exception("sync of review_sources.id=%s could not be saved; skipping it this tick", lid)
            session.rollback()
            totals["links"] += 1
            totals["error"] += 1
            continue
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
