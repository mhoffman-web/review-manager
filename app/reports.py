"""Reporting: the morning email data, dashboard trends, and per-site summaries."""
from __future__ import annotations

import logging
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .config import EDITIONS, settings
from .daterange import DateRange, resolve_range
from .models import Employee, Location, ReportRecipient, ReportSend, Response, Review, ReviewMention, ReviewSourceLink, User
from .text_intel import THEMES

log = logging.getLogger(__name__)

TEMPLATES = Path(__file__).parent / "templates"
_env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=select_autoescape(["html"]))

# Fixed categorical order per brand (validated palette slots 1-3, light/dark).
BRAND_COLORS = {
    "WashU": {"light": "#2a78d6", "dark": "#3987e5"},
    "ICON": {"light": "#eb6834", "dark": "#d95926"},
    "WA": {"light": "#1baf7a", "dark": "#199e70"},
}
BRAND_ORDER = ["WashU", "ICON", "WA"]


@dataclass
class SiteRow:
    name: str
    brand: str
    location_id: Optional[int] = None
    new_24h: int = 0
    avg_24h: Optional[float] = None
    count_7d: int = 0
    avg_7d: Optional[float] = None
    count_30d: int = 0
    avg_30d: Optional[float] = None
    negative_30d: int = 0
    unanswered: int = 0
    overdue: int = 0
    answered_30d: int = 0
    response_rate_30d: Optional[float] = None
    median_response_h_30d: Optional[float] = None
    listing_avg: Optional[float] = None
    listing_total: Optional[int] = None


@dataclass
class ReportData:
    as_of_local: datetime
    window_start_local: datetime
    brands: List[str]
    new_24h: int = 0
    avg_24h: Optional[float] = None
    negative_24h: List[Review] = field(default_factory=list)
    unanswered_total: int = 0
    overdue_total: int = 0
    oldest_unanswered_hours: Optional[float] = None
    overdue_list: List[Review] = field(default_factory=list)
    sites: List[SiteRow] = field(default_factory=list)
    totals_7d: Optional[float] = None
    totals_30d: Optional[float] = None
    count_30d: int = 0
    negative_30d: int = 0
    response_rate_30d: Optional[float] = None
    median_response_h_30d: Optional[float] = None
    p90_response_h_30d: Optional[float] = None
    negative_themes_30d: List[Any] = field(default_factory=list)   # [(category, count)]
    promoter_pct_30d: Optional[float] = None    # 5 star share
    passive_pct_30d: Optional[float] = None     # 4 star share
    detractor_pct_30d: Optional[float] = None   # 1-3 star share
    nps_30d: Optional[float] = None
    rating_only_pct_30d: Optional[float] = None


def _avg(values: Sequence[Optional[int]]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 2) if vals else None


def _median(values: Sequence[float]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return round(statistics.median(vals), 1) if vals else None


def _pct(values: Sequence[float], q: float) -> Optional[float]:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    k = max(0, min(len(vals) - 1, int(round(q * (len(vals) - 1)))))
    return round(vals[k], 1)


def _site_name(r: Review) -> str:
    loc = r.location
    return loc.name if loc else (r.source_link.display_name or r.source_link.external_location_id)


def _load_reviews(session: Session, since: datetime, brands: Optional[List[str]] = None,
                  location_id: Optional[int] = None, include_unanswered: bool = True) -> List[Review]:
    q = (
        select(Review)
        .join(ReviewSourceLink, Review.source_link_id == ReviewSourceLink.id)
        .outerjoin(Location, ReviewSourceLink.location_id == Location.id)
        .where(Review.is_deleted.is_(False), ReviewSourceLink.active.is_(True))
        .options(selectinload(Review.source_link).selectinload(ReviewSourceLink.location))
    )
    cond = Review.created_at_source >= since
    if include_unanswered:
        cond = cond | (Review.has_owner_reply.is_(False))
    q = q.where(cond)
    if brands:
        q = q.where(Location.brand.in_(brands))
    if location_id:
        q = q.where(Location.id == location_id)
    return list(session.execute(q).scalars().all())


def build_report(session: Session, brands: Optional[List[str]] = None, as_of_utc: Optional[datetime] = None) -> ReportData:
    tz = ZoneInfo(settings.timezone)
    as_of_utc = as_of_utc or datetime.utcnow()
    as_of_local = as_of_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
    d1 = as_of_utc - timedelta(hours=24)
    d7 = as_of_utc - timedelta(days=7)
    d30 = as_of_utc - timedelta(days=30)
    overdue_cut = as_of_utc - timedelta(hours=settings.overdue_hours)

    reviews = _load_reviews(session, d30, brands)
    links_q = select(ReviewSourceLink).where(ReviewSourceLink.active.is_(True)).options(selectinload(ReviewSourceLink.location))
    links = list(session.execute(links_q).scalars().all())
    if brands:
        links = [l for l in links if l.location and l.location.brand in brands]

    data = ReportData(as_of_local=as_of_local, window_start_local=as_of_local - timedelta(hours=24), brands=brands or [])
    site_rows: Dict[str, SiteRow] = {}
    for link in links:
        name = link.location.name if link.location else (link.display_name or link.external_location_id)
        brand = link.location.brand if link.location else "?"
        row = site_rows.setdefault(name, SiteRow(name=name, brand=brand, location_id=link.location_id))
        row.listing_avg = link.avg_rating
        row.listing_total = link.total_review_count

    by_site: Dict[str, List[Review]] = defaultdict(list)
    r24, r7, r30, resp_h = [], [], [], []
    themes: Counter = Counter()
    for r in reviews:
        name = _site_name(r)
        row = site_rows.setdefault(name, SiteRow(name=name, brand=r.location.brand if r.location else "?",
                                                 location_id=r.location.id if r.location else None))
        by_site[name].append(r)
        c = r.created_at_source
        if c >= d30:
            row.count_30d += 1
            r30.append(r.rating)
            if r.has_owner_reply:
                row.answered_30d += 1
                if r.response_hours is not None:
                    resp_h.append(r.response_hours)
            if r.is_negative:
                row.negative_30d += 1
                if r.category:
                    themes[r.category] += 1
        if c >= d7:
            row.count_7d += 1
            r7.append(r.rating)
        if c >= d1:
            row.new_24h += 1
            r24.append(r.rating)
            if r.is_negative:
                data.negative_24h.append(r)
        if not r.has_owner_reply and not r.is_archived:
            row.unanswered += 1
            data.unanswered_total += 1
            if c < overdue_cut:
                row.overdue += 1
                data.overdue_total += 1
                data.overdue_list.append(r)
            age_h = (as_of_utc - c).total_seconds() / 3600
            if data.oldest_unanswered_hours is None or age_h > data.oldest_unanswered_hours:
                data.oldest_unanswered_hours = round(age_h, 1)

    for name, row in site_rows.items():
        rs = by_site.get(name, [])
        row.avg_24h = _avg([r.rating for r in rs if r.created_at_source >= d1])
        row.avg_7d = _avg([r.rating for r in rs if r.created_at_source >= d7])
        row.avg_30d = _avg([r.rating for r in rs if r.created_at_source >= d30])
        row.median_response_h_30d = _median([r.response_hours for r in rs if r.created_at_source >= d30 and r.has_owner_reply])
        if row.count_30d:
            row.response_rate_30d = round(100.0 * row.answered_30d / row.count_30d, 0)

    data.new_24h = len(r24)
    data.avg_24h = _avg(r24)
    data.totals_7d = _avg(r7)
    data.totals_30d = _avg(r30)
    data.count_30d = len(r30)
    data.negative_30d = sum(row.negative_30d for row in site_rows.values())
    answered_30d = sum(row.answered_30d for row in site_rows.values())
    data.response_rate_30d = round(100.0 * answered_30d / data.count_30d, 0) if data.count_30d else None
    data.median_response_h_30d = _median(resp_h)
    data.p90_response_h_30d = _pct(resp_h, 0.9)
    rated = [x for x in r30 if x]
    if rated:
        pro = sum(1 for x in rated if x == 5) / len(rated)
        det = sum(1 for x in rated if x <= 3) / len(rated)
        data.promoter_pct_30d = round(100 * pro, 1)
        data.passive_pct_30d = round(100 * (1 - pro - det), 1)
        data.detractor_pct_30d = round(100 * det, 1)
        data.nps_30d = round(100 * (pro - det), 1)
    in30 = [r for r in reviews if r.created_at_source >= d30]
    if in30:
        data.rating_only_pct_30d = round(100.0 * sum(1 for r in in30 if r.is_rating_only) / len(in30), 0)
    data.negative_themes_30d = themes.most_common(6)
    data.negative_24h.sort(key=lambda r: (r.rating or 0, r.created_at_source))
    data.overdue_list.sort(key=lambda r: r.created_at_source)
    data.sites = sorted(site_rows.values(), key=lambda s: (BRAND_ORDER.index(s.brand) if s.brand in BRAND_ORDER else 9, s.name))
    return data


# ----------------------------------------------------------------------------- windowed report (date-range driven)
@dataclass
class WindowReport:
    dr: DateRange
    brands: List[str]
    count: int = 0
    avg: Optional[float] = None
    negatives: int = 0
    negative_pct: Optional[float] = None
    answered: int = 0
    response_rate: Optional[float] = None
    median_response_h: Optional[float] = None
    p90_response_h: Optional[float] = None
    promoter_pct: Optional[float] = None
    passive_pct: Optional[float] = None
    detractor_pct: Optional[float] = None
    nps: Optional[float] = None
    rating_only_pct: Optional[float] = None
    unanswered_now: int = 0          # open reviews from the window still without a reply
    overdue_now: int = 0
    sites: List[SiteRow] = field(default_factory=list)
    categories: List[str] = field(default_factory=lambda: list(THEMES))
    themes: List[Any] = field(default_factory=list)                 # [(category, count)] negatives in window
    theme_matrix: Dict[str, Dict[str, int]] = field(default_factory=dict)   # site -> category -> count
    negative_reviews: List[Review] = field(default_factory=list)
    mention_reviews: int = 0


def _window_reviews(session: Session, dr: DateRange, brands: Optional[List[str]], location_ids: Optional[List[int]]) -> List[Review]:
    q = (select(Review)
         .join(ReviewSourceLink, Review.source_link_id == ReviewSourceLink.id)
         .outerjoin(Location, ReviewSourceLink.location_id == Location.id)
         .where(Review.is_deleted.is_(False), ReviewSourceLink.active.is_(True),
                Review.created_at_source >= dr.start, Review.created_at_source < dr.end)
         .options(selectinload(Review.source_link).selectinload(ReviewSourceLink.location), selectinload(Review.mentions)))
    if brands:
        q = q.where(Location.brand.in_(brands))
    if location_ids:
        q = q.where(Location.id.in_(location_ids))
    return list(session.execute(q).scalars().all())


def window_report(session: Session, dr: DateRange, brands: Optional[List[str]] = None, location_ids: Optional[List[int]] = None,
                  as_of_utc: Optional[datetime] = None) -> WindowReport:
    as_of_utc = as_of_utc or datetime.utcnow()
    overdue_cut = as_of_utc - timedelta(hours=settings.overdue_hours)
    reviews = _window_reviews(session, dr, brands, location_ids)
    links = list(session.execute(select(ReviewSourceLink).where(ReviewSourceLink.active.is_(True))
                                 .options(selectinload(ReviewSourceLink.location))).scalars().all())
    if brands:
        links = [l for l in links if l.location and l.location.brand in brands]
    if location_ids:
        links = [l for l in links if l.location and l.location.id in location_ids]
    rep = WindowReport(dr=dr, brands=brands or [])
    rows: Dict[str, SiteRow] = {}
    for link in links:
        name = link.location.name if link.location else (link.display_name or link.external_location_id)
        row = rows.setdefault(name, SiteRow(name=name, brand=link.location.brand if link.location else "?", location_id=link.location_id))
        row.listing_avg, row.listing_total = link.avg_rating, link.total_review_count
    by_site: Dict[str, List[Review]] = defaultdict(list)
    ratings, resp_h = [], []
    themes: Counter = Counter()
    matrix: Dict[str, Counter] = defaultdict(Counter)
    for r in reviews:
        name = _site_name(r)
        row = rows.setdefault(name, SiteRow(name=name, brand=r.location.brand if r.location else "?", location_id=r.location.id if r.location else None))
        by_site[name].append(r)
        row.count_30d += 1
        if r.rating:
            ratings.append(r.rating)
        if r.has_owner_reply:
            row.answered_30d += 1
            if r.response_hours is not None:
                resp_h.append(r.response_hours)
        elif not r.is_archived:
            row.unanswered += 1
            rep.unanswered_now += 1
            if r.created_at_source < overdue_cut:
                row.overdue += 1
                rep.overdue_now += 1
        if r.is_negative:
            row.negative_30d += 1
            cat = r.category or ("No Content" if r.is_rating_only else "Unknown")
            themes[cat] += 1
            matrix[name][cat] += 1
            rep.negative_reviews.append(r)
        if r.mentions:
            rep.mention_reviews += 1
    for name, row in rows.items():
        rs = by_site.get(name, [])
        row.avg_30d = _avg([r.rating for r in rs])
        row.median_response_h_30d = _median([r.response_hours for r in rs if r.has_owner_reply])
        if row.count_30d:
            row.response_rate_30d = round(100.0 * row.answered_30d / row.count_30d, 0)
    rep.count = len(reviews)
    rep.avg = _avg(ratings)
    rep.negatives = sum(1 for r in reviews if r.is_negative)
    rep.negative_pct = round(100.0 * rep.negatives / rep.count, 1) if rep.count else None
    rep.answered = sum(1 for r in reviews if r.has_owner_reply)
    rep.response_rate = round(100.0 * rep.answered / rep.count, 0) if rep.count else None
    rep.median_response_h = _median(resp_h)
    rep.p90_response_h = _pct(resp_h, 0.9)
    if ratings:
        pro = sum(1 for x in ratings if x == 5) / len(ratings)
        det = sum(1 for x in ratings if x <= 3) / len(ratings)
        rep.promoter_pct, rep.detractor_pct = round(100 * pro, 1), round(100 * det, 1)
        rep.passive_pct = round(100 * (1 - pro - det), 1)
        rep.nps = round(100 * (pro - det), 1)
    if reviews:
        rep.rating_only_pct = round(100.0 * sum(1 for r in reviews if r.is_rating_only) / len(reviews), 0)
    rep.themes = [(c, themes[c]) for c in THEMES if themes[c]] + [(c, n) for c, n in themes.items() if c not in THEMES]
    rep.theme_matrix = {k: dict(v) for k, v in matrix.items()}
    rep.negative_reviews.sort(key=lambda r: r.created_at_source, reverse=True)
    rep.sites = sorted(rows.values(), key=lambda s: (BRAND_ORDER.index(s.brand) if s.brand in BRAND_ORDER else 9, s.name))
    return rep


# ----------------------------------------------------------------------------- trends (daily / weekly / monthly buckets)
def _local_date(utc_naive: datetime):
    return utc_naive.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(settings.timezone)).date()


def _bucket_of(d, gran: str):
    """Bucket key (a local date) for a local date."""
    if gran == "week":
        return d - timedelta(days=d.weekday())
    if gran == "month":
        return d.replace(day=1)
    return d


def _bucket_labels(dr: DateRange, gran: str) -> list:
    cur = _bucket_of(dr.start_date, gran)
    out = []
    while cur <= dr.end_date:
        out.append(cur)
        if gran == "day":
            cur = cur + timedelta(days=1)
        elif gran == "week":
            cur = cur + timedelta(weeks=1)
        else:
            cur = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
    return out


def build_trends(session: Session, dr: DateRange, brands: Optional[List[str]] = None, location_ids: Optional[List[int]] = None) -> Dict[str, Any]:
    gran = dr.granularity
    reviews = _window_reviews(session, dr, brands, location_ids)
    labels = _bucket_labels(dr, gran)
    idx = {b: i for i, b in enumerate(labels)}
    n = len(labels)
    present = sorted({r.location.brand for r in reviews if r.location}, key=lambda b: BRAND_ORDER.index(b) if b in BRAND_ORDER else 9)
    ratings: Dict[str, List[List[int]]] = {b: [[] for _ in labels] for b in present}
    counts: Dict[str, List[int]] = {b: [0] * n for b in present}
    negatives = [0] * n
    answered = [0] * n
    total = [0] * n
    resp_hours: List[List[float]] = [[] for _ in labels]
    dist = [0, 0, 0, 0, 0]
    for r in reviews:
        i = idx.get(_bucket_of(_local_date(r.created_at_source), gran))
        if i is None or not r.location:
            continue
        b = r.location.brand
        if r.rating:
            ratings[b][i].append(r.rating)
            dist[r.rating - 1] += 1
            if r.rating <= settings.negative_rating_max:
                negatives[i] += 1
        counts[b][i] += 1
        total[i] += 1
        if r.has_owner_reply:
            answered[i] += 1
            if r.response_hours is not None:
                resp_hours[i].append(r.response_hours)
    fmt = {"day": "%b %-d", "week": "%b %-d", "month": "%b %Y"}[gran]
    return {
        "granularity": gran,
        "labels": [d.strftime(fmt) for d in labels],
        "brands": present,
        "colors": {b: BRAND_COLORS.get(b, {"light": "#52514e", "dark": "#c3c2b7"}) for b in present},
        "avg_rating": {b: [_avg(cell) for cell in ratings[b]] for b in present},
        "counts": counts,
        "negatives": negatives,
        "response_rate": [round(100.0 * a / t, 0) if t else None for a, t in zip(answered, total)],
        "median_response_h": [_median(h) for h in resp_hours],
        "rating_dist": dist,
    }


def location_rank(session: Session, dr: DateRange, brands: Optional[List[str]] = None, location_ids: Optional[List[int]] = None) -> List[Dict[str, Any]]:
    by: Dict[str, Dict[str, Any]] = {}
    for r in _window_reviews(session, dr, brands, location_ids):
        name = _site_name(r)
        b = by.setdefault(name, {"name": name, "brand": r.location.brand if r.location else "?", "location_id": r.location.id if r.location else None, "ratings": []})
        if r.rating:
            b["ratings"].append(r.rating)
    rows = [{"name": b["name"], "brand": b["brand"], "location_id": b["location_id"], "avg": _avg(b["ratings"]), "n": len(b["ratings"])} for b in by.values()]
    rows.sort(key=lambda x: (-(x["avg"] or 0), -x["n"]))
    return rows


def responder_stats(session: Session, dr: DateRange) -> List[Dict[str, Any]]:
    """Replies posted through this app in the window, by agent."""
    rows = session.execute(
        select(Response).where(Response.status == "posted", Response.posted_at >= dr.start, Response.posted_at < dr.end)
        .options(selectinload(Response.review), selectinload(Response.created_by))
    ).scalars().all()
    by: Dict[str, Dict[str, Any]] = {}
    for resp in rows:
        who = resp.created_by.name if resp.created_by else "Unknown"
        b = by.setdefault(who, {"name": who, "count": 0, "hours": [], "ai": 0, "template": 0, "negatives": 0})
        b["count"] += 1
        if resp.review and resp.posted_at and resp.review.created_at_source:
            b["hours"].append(max(0.0, (resp.posted_at - resp.review.created_at_source).total_seconds() / 3600))
        if resp.ai_generated:
            b["ai"] += 1
        if resp.template_id:
            b["template"] += 1
        if resp.review and resp.review.is_negative:
            b["negatives"] += 1
    out = [{"name": b["name"], "count": b["count"], "median_h": _median(b["hours"]), "p90_h": _pct(b["hours"], 0.9),
            "ai_pct": round(100.0 * b["ai"] / b["count"]) if b["count"] else 0,
            "template_pct": round(100.0 * b["template"] / b["count"]) if b["count"] else 0, "negatives": b["negatives"]} for b in by.values()]
    out.sort(key=lambda x: -x["count"])
    return out


def employee_report(session: Session, dr: DateRange, brands: Optional[List[str]] = None, location_ids: Optional[List[int]] = None) -> Dict[str, Any]:
    """Who customers name in reviews. Powered by review_mentions."""
    q = (select(ReviewMention)
         .join(Review, ReviewMention.review_id == Review.id)
         .join(ReviewSourceLink, Review.source_link_id == ReviewSourceLink.id)
         .outerjoin(Location, ReviewSourceLink.location_id == Location.id)
         .where(Review.is_deleted.is_(False), Review.created_at_source >= dr.start, Review.created_at_source < dr.end)
         .options(selectinload(ReviewMention.review).selectinload(Review.source_link).selectinload(ReviewSourceLink.location),
                  selectinload(ReviewMention.employee)))
    if brands:
        q = q.where(Location.brand.in_(brands))
    if location_ids:
        q = q.where(Location.id.in_(location_ids))
    mentions = session.execute(q).scalars().all()
    by: Dict[str, Dict[str, Any]] = {}
    with_mentions: Set[int] = set()
    for m in mentions:
        r = m.review
        key = m.employee.name if m.employee else m.name
        b = by.setdefault(key, {"name": key, "on_roster": m.employee is not None, "employee_id": m.employee_id, "sites": Counter(),
                                "count": 0, "ratings": [], "last": None, "negatives": 0, "examples": []})
        b["count"] += 1
        b["sites"][_site_name(r)] += 1
        if r.rating:
            b["ratings"].append(r.rating)
            if r.rating <= settings.negative_rating_max:
                b["negatives"] += 1
        if b["last"] is None or r.created_at_source > b["last"]:
            b["last"] = r.created_at_source
        if len(b["examples"]) < 3 and r.text:
            b["examples"].append(r)
        with_mentions.add(r.id)
    rows = [{"name": b["name"], "on_roster": b["on_roster"], "employee_id": b["employee_id"], "count": b["count"], "avg": _avg(b["ratings"]),
             "negatives": b["negatives"], "last": b["last"],
             "sites": ", ".join(f"{k} ({v})" if len(b["sites"]) > 1 else k for k, v in b["sites"].most_common(3)),
             "primary_site": b["sites"].most_common(1)[0][0] if b["sites"] else "", "examples": b["examples"]} for b in by.values()]
    rows.sort(key=lambda x: (-x["count"], x["name"]))
    reviews = _window_reviews(session, dr, brands, location_ids)
    with_text = [r for r in reviews if not r.is_rating_only]
    site_rate: Dict[str, Dict[str, int]] = {}
    for r in reviews:
        sr = site_rate.setdefault(_site_name(r), {"reviews": 0, "with_mentions": 0})
        sr["reviews"] += 1
        if r.id in with_mentions:
            sr["with_mentions"] += 1
    sites = sorted(({"name": k, **v, "pct": round(100.0 * v["with_mentions"] / v["reviews"]) if v["reviews"] else 0} for k, v in site_rate.items()), key=lambda x: -x["pct"])
    return {"rows": rows, "total_mentions": len(mentions), "reviews_with_mentions": len(with_mentions), "reviews_with_text": len(with_text),
            "pct_of_text_reviews": round(100.0 * len(with_mentions) / len(with_text)) if with_text else 0, "sites": sites}




# ----------------------------------------------------------------------------- daily digest (the morning email)
@dataclass
class DigestSite:
    name: str
    brand: str
    location_id: Optional[int] = None
    counts: List[int] = field(default_factory=lambda: [0, 0, 0, 0, 0])   # 1★..5★
    unrated: int = 0
    reviews: List[Review] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(self.counts) + self.unrated

    @property
    def avg(self) -> Optional[float]:
        n = sum(self.counts)
        return round(sum((i + 1) * c for i, c in enumerate(self.counts)) / n, 2) if n else None

    @property
    def negatives(self) -> int:
        return sum(self.counts[:settings.negative_rating_max])


@dataclass
class Digest:
    edition: str
    label: str
    dr: DateRange
    brands: Optional[List[str]]
    sites: List[DigestSite] = field(default_factory=list)      # sites with reviews in the window, brand order
    counts: List[int] = field(default_factory=lambda: [0, 0, 0, 0, 0])
    unrated: int = 0
    reviews: List[Review] = field(default_factory=list)        # all reviews in the window, brand/site/time order

    @property
    def total(self) -> int:
        return sum(self.counts) + self.unrated

    @property
    def avg(self) -> Optional[float]:
        n = sum(self.counts)
        return round(sum((i + 1) * c for i, c in enumerate(self.counts)) / n, 2) if n else None

    @property
    def negatives(self) -> int:
        return sum(self.counts[:settings.negative_rating_max])

    def by_brand(self) -> List[Any]:
        out: Dict[str, List[DigestSite]] = {}
        for site in self.sites:
            out.setdefault(site.brand, []).append(site)
        return [(b, out[b]) for b in sorted(out, key=lambda b: BRAND_ORDER.index(b) if b in BRAND_ORDER else 9)]


def build_digest(session: Session, edition: str = "all", dr: Optional[DateRange] = None) -> Digest:
    ed = EDITIONS.get(edition, EDITIONS["all"])
    dr = dr or resolve_range("yesterday")
    brands = ed["brands"]
    reviews = _window_reviews(session, dr, brands, None)
    sites: Dict[str, DigestSite] = {}
    for r in reviews:
        name = _site_name(r)
        site = sites.setdefault(name, DigestSite(name=name, brand=r.location.brand if r.location else "?", location_id=r.location.id if r.location else None))
        site.reviews.append(r)
        if r.rating:
            site.counts[r.rating - 1] += 1
        else:
            site.unrated += 1
    d = Digest(edition=edition, label=ed["label"], dr=dr, brands=brands)
    d.sites = sorted(sites.values(), key=lambda s: (BRAND_ORDER.index(s.brand) if s.brand in BRAND_ORDER else 9, s.name))
    for site in d.sites:
        site.reviews.sort(key=lambda r: r.created_at_source)
        for i in range(5):
            d.counts[i] += site.counts[i]
        d.unrated += site.unrated
        d.reviews.extend(site.reviews)
    return d


def render_digest_html(d: Digest) -> str:
    tz = ZoneInfo(settings.timezone)
    return _env.get_template("report_email.html").render(d=d, settings=settings, to_local=lambda dt: dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz))


def render_digest_text(d: Digest) -> str:
    lines = [f"Reviews received {d.dr.label.lower()} ({d.dr.start_date:%a %b %-d}) — {d.label}",
             f"Total {d.total} · avg {d.avg or '-'} · 1★ {d.counts[0]} · 2★ {d.counts[1]} · 3★ {d.counts[2]} · 4★ {d.counts[3]} · 5★ {d.counts[4]}", ""]
    lines.append(f"{'Site':<28} {'1★':>4}{'2★':>4}{'3★':>4}{'4★':>4}{'5★':>4} {'Total':>6} {'Avg':>5}")
    for s in d.sites:
        lines.append(f"{s.name:<28} " + "".join(f"{c:>4}" for c in s.counts) + f" {s.total:>6} {s.avg or '-':>5}")
    lines.append("")
    for s in d.sites:
        lines.append(f"-- {s.name}")
        for r in s.reviews:
            tz = ZoneInfo(settings.timezone)
            t = r.created_at_source.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz).strftime("%-I:%M %p")
            lines.append(f"  {r.rating or '-'}★ {t} {r.author_name or 'Anonymous'}: {(r.text or '(rating only)')[:200]}")
    return "\n".join(lines)


def digest_subject(d: Digest) -> str:
    return (f"Reviews {d.dr.start_date:%a %b %-d} · {EDITIONS[d.edition]['short']}: {d.total} received"
            + (f", avg {d.avg}" if d.avg else "") + (f", {d.negatives} negative" if d.negatives else ""))


# ----------------------------------------------------------------------------- rating distribution by site
def rating_distribution(session: Session, dr: DateRange, brands: Optional[List[str]] = None, location_ids: Optional[List[int]] = None) -> Dict[str, Any]:
    rows: Dict[str, Dict[str, Any]] = {}
    for r in _window_reviews(session, dr, brands, location_ids):
        name = _site_name(r)
        row = rows.setdefault(name, {"name": name, "brand": r.location.brand if r.location else "?", "location_id": r.location.id if r.location else None,
                                     "counts": [0, 0, 0, 0, 0], "unrated": 0})
        if r.rating:
            row["counts"][r.rating - 1] += 1
        else:
            row["unrated"] += 1
    out = []
    totals = [0, 0, 0, 0, 0]
    for row in rows.values():
        n = sum(row["counts"])
        row["total"] = n + row["unrated"]
        row["avg"] = round(sum((i + 1) * c for i, c in enumerate(row["counts"])) / n, 2) if n else None
        row["pct"] = [round(100.0 * c / n, 1) if n else 0 for c in row["counts"]]
        row["neg_pct"] = round(100.0 * sum(row["counts"][:settings.negative_rating_max]) / n, 1) if n else 0
        for i in range(5):
            totals[i] += row["counts"][i]
        out.append(row)
    out.sort(key=lambda x: (BRAND_ORDER.index(x["brand"]) if x["brand"] in BRAND_ORDER else 9, -x["total"]))
    n = sum(totals)
    return {"rows": out, "totals": totals, "total": n + sum(r["unrated"] for r in out),
            "avg": round(sum((i + 1) * c for i, c in enumerate(totals)) / n, 2) if n else None,
            "pct": [round(100.0 * c / n, 1) if n else 0 for c in totals],
            "chart": {"labels": [r["name"] for r in out], "series": [[r["counts"][i] for r in out] for i in range(5)]}}

# ----------------------------------------------------------------------------- email (legacy renderers kept for tests)
def render_report_html(data: ReportData) -> str:
    tpl = _env.get_template("report_email_legacy.html")
    tz = ZoneInfo(settings.timezone)
    return tpl.render(d=data, settings=settings, tz=tz, to_local=lambda dt: dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz))


def render_report_text(data: ReportData) -> str:
    lines = [
        f"Review report for {data.as_of_local:%A %b %d, %Y}",
        f"New reviews (24h): {data.new_24h}  avg {data.avg_24h or '-'}",
        f"Unanswered: {data.unanswered_total}  overdue (> {settings.overdue_hours}h): {data.overdue_total}",
        f"30d: {data.count_30d} reviews, avg {data.totals_30d or '-'}, response rate {data.response_rate_30d or '-'}%, median response {data.median_response_h_30d or '-'}h",
        "",
        "Negative reviews (24h):",
    ]
    for r in data.negative_24h:
        lines.append(f"  {r.rating}* {_site_name(r)} - {(r.text or '')[:140]}")
    lines += ["", "Site                          new24h  avg7d  avg30d  unanswered"]
    for s in data.sites:
        lines.append(f"  {s.name:<28} {s.new_24h:>5}  {s.avg_7d or '-':>5}  {s.avg_30d or '-':>6}  {s.unanswered:>5}")
    return "\n".join(lines)


def recipient_groups(session: Session) -> Dict[str, List[str]]:
    """Active recipients by digest edition (il / tn / all)."""
    rows = session.execute(select(ReportRecipient).where(ReportRecipient.active.is_(True))).scalars().all()
    groups: Dict[str, List[str]] = {}
    for r in rows:
        ed = r.edition if r.edition in EDITIONS else "all"
        groups.setdefault(ed, []).append(r.email)
    if not groups and settings.report_recipients_fallback:
        groups["all"] = list(settings.report_recipients_fallback)
    return groups


def send_morning_report(session: Session, dry_run: bool = False, to_override: Optional[List[str]] = None,
                        out_file: Optional[str] = None, edition: Optional[str] = None) -> List[ReportSend]:
    """Yesterday's reviews, one email per edition (Illinois, Tennessee, Corporate)."""
    from .mailer import send_email

    tz = ZoneInfo(settings.timezone)
    today = datetime.now(tz).strftime("%Y-%m-%d")
    groups = {edition or "all": to_override} if to_override else recipient_groups(session)
    sends: List[ReportSend] = []
    if not groups:
        log.warning("no report recipients configured; nothing sent")
        return sends
    for ed, emails in groups.items():
        d = build_digest(session, ed)
        html, text, subject = render_digest_html(d), render_digest_text(d), digest_subject(d)
        if out_file:
            p = Path(out_file)
            p.with_name(f"{p.stem}.{ed}{p.suffix}").write_text(html)
            log.info("wrote %s", p.with_name(f"{p.stem}.{ed}{p.suffix}"))
        send = ReportSend(report_date=today, brands=ed, recipients=", ".join(emails))
        if dry_run:
            send.status = "dry-run"
            print(f"--- {subject}\n--- to: {', '.join(emails)}\n{text}\n")
        else:
            try:
                send_email(emails, subject, html, text)
            except Exception as exc:
                log.exception("report send failed")
                send.status = "error"
                send.error = str(exc)[:2000]
        session.add(send)
        sends.append(send)
    session.commit()
    return sends


def already_sent_today(session: Session) -> bool:
    tz = ZoneInfo(settings.timezone)
    today = datetime.now(tz).strftime("%Y-%m-%d")
    row = session.execute(select(ReportSend).where(ReportSend.report_date == today, ReportSend.status == "ok")).first()
    return row is not None


# ----------------------------------------------------------------------------- monthly summary (fixed 12 months, for the table)
def monthly_summary(session: Session, brands: Optional[List[str]] = None, months: int = 12,
                    as_of_utc: Optional[datetime] = None, location_ids: Optional[List[int]] = None) -> List[Dict[str, Any]]:
    as_of_utc = as_of_utc or datetime.utcnow()
    first = as_of_utc.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    y, m = first.year, first.month
    for _ in range(months - 1):
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    start = first.replace(year=y, month=m)
    from .daterange import DateRange as _DR
    dr = _DR(preset="custom", start_date=start.date(), end_date=as_of_utc.date(), start=start, end=as_of_utc + timedelta(seconds=1), label="")
    buckets: Dict[str, Dict[str, Any]] = {}
    for r in _window_reviews(session, dr, brands, location_ids):
        key = r.created_at_source.strftime("%Y-%m")
        b = buckets.setdefault(key, {"month": key, "label": r.created_at_source.strftime("%b %Y"), "ratings": [], "n": 0, "positive": 0, "neutral": 0, "negative": 0, "answered": 0, "resp_h": []})
        b["n"] += 1
        if r.rating:
            b["ratings"].append(r.rating)
            if r.rating >= 4: b["positive"] += 1
            elif r.rating == 3: b["neutral"] += 1
            else: b["negative"] += 1
        if r.has_owner_reply:
            b["answered"] += 1
            if r.response_hours is not None:
                b["resp_h"].append(r.response_hours)
    rows = []
    for key in sorted(buckets, reverse=True):
        b = buckets[key]
        rows.append({"month": b["month"], "label": b["label"], "n": b["n"], "avg": _avg(b["ratings"]), "positive": b["positive"], "neutral": b["neutral"],
                     "negative": b["negative"], "response_rate": round(100.0 * b["answered"] / b["n"], 0) if b["n"] else None, "median_response_h": _median(b["resp_h"])})
    return rows
