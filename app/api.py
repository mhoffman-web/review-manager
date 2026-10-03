"""Read-only JSON API for other systems (the Teams Portal screens). Auth: an API key from
Admin -> API keys, sent as `X-API-Key: <key>` or `Authorization: Bearer <key>`. Keys are never
accepted in the query string, where they would land in access logs and browser history.

GET /api/v1/sites                       every active site with the platform's own rating
GET /api/v1/summary?range=last30        KPIs for the window, in total and per site
GET /api/v1/reviews?range=last7         individual reviews, newest first (limit <= 500)
GET /api/v1/leaderboard?range=this_month  employees named in reviews
Filters on every endpoint: brand=, location_id=, group_id= (repeatable), range= preset or custom with start=/end=.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta
from typing import List, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .config import settings
from .daterange import PRESET_KEYS, resolve_range
from .db import get_db
from .models import ApiKey, Location, Review, ReviewSourceLink, SiteGroup
from .reports import employee_report, listing_summaries, rating_distribution, recovery_stats, window_report

router = APIRouter(prefix="/api/v1", tags=["api"])
_tz = ZoneInfo(settings.timezone)


def new_key() -> str:
    return "rm_" + secrets.token_urlsafe(30)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def api_key(request: Request, db: Session = Depends(get_db)) -> ApiKey:
    raw = (request.headers.get("x-api-key") or "").strip()
    auth = request.headers.get("authorization") or ""
    if not raw and auth.lower().startswith("bearer "):
        raw = auth[7:].strip()
    if not raw:
        if "key" in request.query_params:
            raise HTTPException(400, "API keys are not accepted in the URL. Send the header X-API-Key: <key> instead, "
                                     "and revoke this key in Admin -> API keys because it has been logged.")
        raise HTTPException(401, "Missing API key. Send X-API-Key: <key>.")
    k = db.execute(select(ApiKey).where(ApiKey.key_hash == hash_key(raw), ApiKey.active.is_(True))).scalar_one_or_none()
    if not k:
        raise HTTPException(403, "Unknown or revoked API key.")
    if not k.last_used_at or datetime.utcnow() - k.last_used_at > timedelta(minutes=5):
        k.last_used_at = datetime.utcnow()
        db.commit()
    return k


def _ints(values) -> List[int]:
    out = []
    for v in values or []:
        for part in str(v).split(","):
            if part.strip().isdigit():
                out.append(int(part))
    return out


def _scope(db: Session, group_ids: List[int], location_ids: List[int]) -> Optional[List[int]]:
    ids: Optional[List[int]] = None
    if group_ids:
        ids = []
        for g in db.execute(select(SiteGroup).where(SiteGroup.id.in_(group_ids)).options(selectinload(SiteGroup.locations))).scalars().all():
            ids.extend(l.id for l in g.locations)
        ids = sorted(set(ids))
    if location_ids:
        ids = sorted(set(location_ids) if ids is None else set(ids) & set(location_ids))
    return ids


def _range(range: str, start: str, end: str):
    if range and range not in PRESET_KEYS:
        raise HTTPException(400, f"Unknown range '{range}'. One of: {', '.join(sorted(PRESET_KEYS))}")
    return resolve_range(range or "last30", start or "", end or "")


def _local(dt: Optional[datetime]) -> Optional[str]:
    return dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(_tz).isoformat() if dt else None


def _range_json(dr) -> dict:
    return {"preset": dr.preset, "label": dr.label, "start": dr.start_date.isoformat(), "end": dr.end_date.isoformat(), "timezone": settings.timezone}


@router.get("/sites")
def sites(db: Session = Depends(get_db), _k: ApiKey = Depends(api_key)):
    listing = listing_summaries(db)
    out = []
    for l in db.execute(select(Location).where(Location.active.is_(True)).order_by(Location.brand, Location.name)).scalars().all():
        ls = listing.get(l.id) or {}
        out.append({"id": l.id, "name": l.name, "brand": l.brand, "state": l.state, "city": l.city,
                    "snowflake_location_ids": [x for x in (l.snowflake_location_ids or "").split(";") if x],
                    "google": {"avg": ls.get("avg"), "total": ls.get("total")} if ls else None})
    return {"sites": out}


@router.get("/summary")
def summary(db: Session = Depends(get_db), _k: ApiKey = Depends(api_key), range: str = "last30", start: str = "", end: str = "",
            brand: List[str] = Query([]), location_id: List[str] = Query([]), group_id: List[str] = Query([])):
    dr = _range(range, start, end)
    ids = _scope(db, _ints(group_id), _ints(location_id))
    brands = [b for b in brand if b] or None
    w = window_report(db, dr, brands, ids)
    dist = rating_distribution(db, dr, brands, ids)
    rec = recovery_stats(db, dr, brands, ids)
    by_name = {r["name"]: r for r in dist["rows"]}
    sites_out = []
    for srow in w.sites:
        d = by_name.get(srow.name, {})
        rs = rec["by_site"].get(srow.name, {})
        sites_out.append({"id": srow.location_id, "name": srow.name, "brand": srow.brand, "reviews": srow.count_30d, "avg": srow.avg_30d,
                          "distribution": d.get("counts", [0, 0, 0, 0, 0]), "negatives": srow.negative_30d,
                          "negative_pct": round(100.0 * srow.negative_30d / srow.count_30d, 1) if srow.count_30d else None,
                          "answered": srow.answered_30d, "response_rate": srow.response_rate_30d, "median_response_hours": srow.median_response_h_30d,
                          "unanswered_now": srow.unanswered, "overdue_now": srow.overdue, "recovered": rs.get("recovered", 0),
                          "google": {"avg": srow.listing_avg, "total": srow.listing_total}})
    return {"range": _range_json(dr), "generated_at": datetime.utcnow().isoformat() + "Z",
            "totals": {"reviews": w.count, "avg": w.avg, "distribution": dist["totals"], "negatives": w.negatives, "negative_pct": w.negative_pct,
                       "answered": w.answered, "response_rate": w.response_rate, "median_response_hours": w.median_response_h,
                       "p90_response_hours": w.p90_response_h, "five_star_pct": w.promoter_pct, "rating_only_pct": w.rating_only_pct,
                       "unanswered_now": w.unanswered_now, "overdue_now": w.overdue_now,
                       "recovered": rec["recovered"], "ratings_raised": rec["improved"], "ratings_lowered": rec["worsened"]},
            "negative_reasons": [{"theme": c, "count": n} for c, n in w.themes],
            "sites": sites_out}


@router.get("/reviews")
def reviews(db: Session = Depends(get_db), _k: ApiKey = Depends(api_key), range: str = "last7", start: str = "", end: str = "",
            brand: List[str] = Query([]), location_id: List[str] = Query([]), group_id: List[str] = Query([]), rating: List[str] = Query([]),
            limit: int = 100, include_text: int = 1):
    dr = _range(range, start, end)
    ids = _scope(db, _ints(group_id), _ints(location_id))
    limit = max(1, min(500, limit))
    q = (select(Review).join(ReviewSourceLink, Review.source_link_id == ReviewSourceLink.id)
         .outerjoin(Location, ReviewSourceLink.location_id == Location.id)
         .where(Review.is_deleted.is_(False), ReviewSourceLink.active.is_(True), Review.created_at_source >= dr.start, Review.created_at_source < dr.end)
         .options(selectinload(Review.source_link).selectinload(ReviewSourceLink.location), selectinload(Review.mentions))
         .order_by(Review.created_at_source.desc()).limit(limit))
    if [b for b in brand if b]:
        q = q.where(Location.brand.in_([b for b in brand if b]))
    if ids is not None:
        q = q.where(Location.id.in_(ids or [-1]))
    if _ints(rating):
        q = q.where(Review.rating.in_(_ints(rating)))
    out = []
    for r in db.execute(q).scalars().all():
        out.append({"id": r.id, "site_id": r.location.id if r.location else None, "site": r.location.name if r.location else r.source_link.display_name,
                    "brand": r.location.brand if r.location else None, "source": r.source, "rating": r.rating, "previous_rating": r.prev_rating,
                    "reviewer": r.author_name, "posted_at": _local(r.created_at_source), "text": (r.text if include_text else None),
                    "replied": r.has_owner_reply, "replied_at": _local(r.replied_at) if r.has_owner_reply else None,
                    "response_hours": round(r.response_hours, 1) if r.response_hours is not None else None,
                    "employees": r.mention_names, "theme": r.category, "link": f"{settings.app_base_url}/reviews/{r.id}"})
    return {"range": _range_json(dr), "count": len(out), "reviews": out}


@router.get("/leaderboard")
def leaderboard(db: Session = Depends(get_db), _k: ApiKey = Depends(api_key), range: str = "this_month", start: str = "", end: str = "",
                brand: List[str] = Query([]), location_id: List[str] = Query([]), group_id: List[str] = Query([]), limit: int = 25):
    dr = _range(range, start, end)
    ids = _scope(db, _ints(group_id), _ints(location_id))
    rep = employee_report(db, dr, [b for b in brand if b] or None, ids)
    rows = [{"name": e.get("name"), "mentions": e.get("count"), "site": e.get("primary_site") or None, "sites": e.get("sites"),
             "on_roster": e.get("on_roster"), "avg_rating": e.get("avg")} for e in rep.get("rows", [])[:max(1, min(100, limit))]]
    return {"range": _range_json(dr), "pct_of_written_reviews_naming_someone": rep.get("pct_of_text_reviews"), "employees": rows}
