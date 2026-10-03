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

from .config import settings
from .models import Employee, Location, ReportRecipient, ReportSend, Response, Review, ReviewMention, ReviewSourceLink, User

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


# ----------------------------------------------------------------------------- trends
def week_start(d: datetime) -> datetime:
    d = d.replace(hour=0, minute=0, second=0, microsecond=0)
    return d - timedelta(days=d.weekday())


def build_trends(session: Session, brands: Optional[List[str]] = None, weeks: int = 12,
                 location_id: Optional[int] = None, as_of_utc: Optional[datetime] = None) -> Dict[str, Any]:
    """Weekly series for the dashboard charts. JSON-serializable."""
    as_of_utc = as_of_utc or datetime.utcnow()
    start = week_start(as_of_utc - timedelta(weeks=weeks - 1))
    reviews = _load_reviews(session, start, brands, location_id=location_id, include_unanswered=False)
    labels = [start + timedelta(weeks=i) for i in range(weeks)]
    idx = {wk: i for i, wk in enumerate(labels)}

    present_brands = sorted({r.location.brand for r in reviews if r.location}, key=lambda b: BRAND_ORDER.index(b) if b in BRAND_ORDER else 9)
    ratings: Dict[str, List[List[int]]] = {b: [[] for _ in labels] for b in present_brands}
    counts: Dict[str, List[int]] = {b: [0] * weeks for b in present_brands}
    answered = [0] * weeks
    total = [0] * weeks
    resp_hours: List[List[float]] = [[] for _ in labels]
    dist = [0, 0, 0, 0, 0]
    d30 = as_of_utc - timedelta(days=30)
    for r in reviews:
        wk = week_start(r.created_at_source)
        i = idx.get(wk)
        if i is None or not r.location:
            continue
        b = r.location.brand
        if r.rating:
            ratings[b][i].append(r.rating)
            if r.created_at_source >= d30:
                dist[r.rating - 1] += 1
        counts[b][i] += 1
        total[i] += 1
        if r.has_owner_reply:
            answered[i] += 1
            if r.response_hours is not None:
                resp_hours[i].append(r.response_hours)

    return {
        "labels": [d.strftime("%b %-d") for d in labels],
        "brands": present_brands,
        "colors": {b: BRAND_COLORS.get(b, {"light": "#52514e", "dark": "#c3c2b7"}) for b in present_brands},
        "avg_rating": {b: [_avg(cell) for cell in ratings[b]] for b in present_brands},
        "counts": counts,
        "response_rate": [round(100.0 * a / t, 0) if t else None for a, t in zip(answered, total)],
        "median_response_h": [_median(h) for h in resp_hours],
        "rating_dist_30d": dist,
    }


# ----------------------------------------------------------------------------- site page
def site_summary(session: Session, location: Location, days: int = 90, as_of_utc: Optional[datetime] = None) -> Dict[str, Any]:
    as_of_utc = as_of_utc or datetime.utcnow()
    since = as_of_utc - timedelta(days=days)
    reviews = _load_reviews(session, since, location_id=location.id)
    in_window = [r for r in reviews if r.created_at_source >= since]
    answered = [r for r in in_window if r.has_owner_reply]
    themes = Counter(r.category for r in in_window if r.is_negative and r.category)
    links = [l for l in location.sources if l.active]
    return {
        "count": len(in_window),
        "avg": _avg([r.rating for r in in_window]),
        "negative": sum(1 for r in in_window if r.is_negative),
        "response_rate": round(100.0 * len(answered) / len(in_window), 0) if in_window else None,
        "median_response_h": _median([r.response_hours for r in answered]),
        "unanswered": sum(1 for r in reviews if not r.has_owner_reply and not r.is_archived),
        "dist": [sum(1 for r in in_window if r.rating == s) for s in (1, 2, 3, 4, 5)],
        "themes": themes.most_common(5),
        "recent": sorted(reviews, key=lambda r: r.created_at_source, reverse=True)[:25],
        "listing_avg": links[0].avg_rating if links else None,
        "listing_total": links[0].total_review_count if links else None,
        "listing_url": links[0].listing_url if links else None,
    }


# ----------------------------------------------------------------------------- email
def render_report_html(data: ReportData) -> str:
    tpl = _env.get_template("report_email.html")
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
    rows = session.execute(select(ReportRecipient).where(ReportRecipient.active.is_(True))).scalars().all()
    groups: Dict[str, List[str]] = {}
    for r in rows:
        key = ";".join(sorted(r.brand_list()))
        groups.setdefault(key, []).append(r.email)
    if not groups and settings.report_recipients_fallback:
        groups[""] = list(settings.report_recipients_fallback)
    return groups


def send_morning_report(session: Session, dry_run: bool = False, to_override: Optional[List[str]] = None,
                        out_file: Optional[str] = None) -> List[ReportSend]:
    from .mailer import send_email

    tz = ZoneInfo(settings.timezone)
    today = datetime.now(tz).strftime("%Y-%m-%d")
    groups = {"": to_override} if to_override else recipient_groups(session)
    sends: List[ReportSend] = []
    if not groups:
        log.warning("no report recipients configured; nothing sent")
        return sends
    for key, emails in groups.items():
        brands = [b for b in key.split(";") if b]
        data = build_report(session, brands=brands or None)
        html = render_report_html(data)
        text = render_report_text(data)
        scope = ", ".join(brands) if brands else "All sites"
        subject = f"Reviews {data.as_of_local:%a %b %-d}: {data.new_24h} new, {len(data.negative_24h)} negative, {data.unanswered_total} unanswered ({scope})"
        if out_file:
            suffix = f".{key.replace(';', '_')}" if key else ""
            p = Path(out_file)
            target = p.with_name(p.stem + suffix + p.suffix) if key else p
            target.write_text(html)
            log.info("wrote %s", target)
        send = ReportSend(report_date=today, brands=key or None, recipients=", ".join(emails))
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


# ----------------------------------------------------------------------------- extra report builders
def monthly_summary(session: Session, brands: Optional[List[str]] = None, months: int = 12,
                    as_of_utc: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Synup-style 'summary by time period': one row per month, newest first."""
    as_of_utc = as_of_utc or datetime.utcnow()
    first = (as_of_utc.replace(day=1, hour=0, minute=0, second=0, microsecond=0))
    # walk back months-1 months
    y, m = first.year, first.month
    for _ in range(months - 1):
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    start = first.replace(year=y, month=m)
    reviews = _load_reviews(session, start, brands, include_unanswered=False)
    buckets: Dict[str, Dict[str, Any]] = {}
    for r in reviews:
        key = r.created_at_source.strftime("%Y-%m")
        b = buckets.setdefault(key, {"month": key, "label": r.created_at_source.strftime("%b %Y"), "ratings": [], "n": 0,
                                     "positive": 0, "neutral": 0, "negative": 0, "answered": 0, "resp_h": []})
        b["n"] += 1
        if r.rating:
            b["ratings"].append(r.rating)
            if r.rating >= 4:
                b["positive"] += 1
            elif r.rating == 3:
                b["neutral"] += 1
            else:
                b["negative"] += 1
        if r.has_owner_reply:
            b["answered"] += 1
            if r.response_hours is not None:
                b["resp_h"].append(r.response_hours)
    rows = []
    for key in sorted(buckets, reverse=True):
        b = buckets[key]
        rows.append({"month": b["month"], "label": b["label"], "n": b["n"], "avg": _avg(b["ratings"]),
                     "positive": b["positive"], "neutral": b["neutral"], "negative": b["negative"],
                     "response_rate": round(100.0 * b["answered"] / b["n"], 0) if b["n"] else None,
                     "median_response_h": _median(b["resp_h"])})
    return rows


def responder_stats(session: Session, days: int = 30, as_of_utc: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Replies posted through this app, by agent: count, median hours from review to reply, AI/template share."""
    as_of_utc = as_of_utc or datetime.utcnow()
    since = as_of_utc - timedelta(days=days)
    rows = session.execute(
        select(Response).where(Response.status == "posted", Response.posted_at >= since)
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
    out = []
    for b in by.values():
        out.append({"name": b["name"], "count": b["count"], "median_h": _median(b["hours"]), "p90_h": _pct(b["hours"], 0.9),
                    "ai_pct": round(100.0 * b["ai"] / b["count"]) if b["count"] else 0,
                    "template_pct": round(100.0 * b["template"] / b["count"]) if b["count"] else 0, "negatives": b["negatives"]})
    out.sort(key=lambda x: -x["count"])
    return out


def location_rank(session: Session, days: int = 365, brands: Optional[List[str]] = None,
                  as_of_utc: Optional[datetime] = None) -> List[Dict[str, Any]]:
    as_of_utc = as_of_utc or datetime.utcnow()
    since = as_of_utc - timedelta(days=days)
    reviews = _load_reviews(session, since, brands, include_unanswered=False)
    by: Dict[str, Dict[str, Any]] = {}
    for r in reviews:
        if r.created_at_source < since:
            continue
        name = _site_name(r)
        b = by.setdefault(name, {"name": name, "brand": r.location.brand if r.location else "?", "location_id": r.location.id if r.location else None, "ratings": []})
        if r.rating:
            b["ratings"].append(r.rating)
    rows = [{"name": b["name"], "brand": b["brand"], "location_id": b["location_id"], "avg": _avg(b["ratings"]), "n": len(b["ratings"])} for b in by.values()]
    rows.sort(key=lambda x: (-(x["avg"] or 0), -x["n"]))
    return rows


def employee_report(session: Session, days: int = 90, brands: Optional[List[str]] = None,
                    location_id: Optional[int] = None, as_of_utc: Optional[datetime] = None) -> Dict[str, Any]:
    """Who customers name in reviews. Powered by review_mentions."""
    as_of_utc = as_of_utc or datetime.utcnow()
    since = as_of_utc - timedelta(days=days)
    q = (select(ReviewMention)
         .join(Review, ReviewMention.review_id == Review.id)
         .join(ReviewSourceLink, Review.source_link_id == ReviewSourceLink.id)
         .outerjoin(Location, ReviewSourceLink.location_id == Location.id)
         .where(Review.is_deleted.is_(False), Review.created_at_source >= since)
         .options(selectinload(ReviewMention.review).selectinload(Review.source_link).selectinload(ReviewSourceLink.location),
                  selectinload(ReviewMention.employee)))
    if brands:
        q = q.where(Location.brand.in_(brands))
    if location_id:
        q = q.where(Location.id == location_id)
    mentions = session.execute(q).scalars().all()
    by: Dict[str, Dict[str, Any]] = {}
    total_reviews_with_mentions: Set[int] = set()
    for m in mentions:
        r = m.review
        key = (m.employee.name if m.employee else m.name)
        site = _site_name(r)
        b = by.setdefault(key, {"name": key, "on_roster": m.employee is not None, "employee_id": m.employee_id, "sites": Counter(),
                                "count": 0, "ratings": [], "last": None, "negatives": 0, "examples": []})
        b["count"] += 1
        b["sites"][site] += 1
        if r.rating:
            b["ratings"].append(r.rating)
            if r.rating <= settings.negative_rating_max:
                b["negatives"] += 1
        if b["last"] is None or r.created_at_source > b["last"]:
            b["last"] = r.created_at_source
        if len(b["examples"]) < 3 and r.text:
            b["examples"].append(r)
        total_reviews_with_mentions.add(r.id)
    rows = []
    for b in by.values():
        rows.append({"name": b["name"], "on_roster": b["on_roster"], "employee_id": b["employee_id"], "count": b["count"],
                     "avg": _avg(b["ratings"]), "negatives": b["negatives"], "last": b["last"],
                     "sites": ", ".join(f"{k} ({v})" if len(b["sites"]) > 1 else k for k, v in b["sites"].most_common(3)),
                     "primary_site": b["sites"].most_common(1)[0][0] if b["sites"] else "", "examples": b["examples"]})
    rows.sort(key=lambda x: (-x["count"], x["name"]))
    # per-site mention rate
    reviews = _load_reviews(session, since, brands, location_id=location_id, include_unanswered=False)
    in_window = [r for r in reviews if r.created_at_source >= since]
    with_text = [r for r in in_window if not r.is_rating_only]
    site_rate: Dict[str, Dict[str, int]] = {}
    for r in in_window:
        sr = site_rate.setdefault(_site_name(r), {"reviews": 0, "with_mentions": 0})
        sr["reviews"] += 1
        if r.id in total_reviews_with_mentions:
            sr["with_mentions"] += 1
    sites = sorted(({"name": k, **v, "pct": round(100.0 * v["with_mentions"] / v["reviews"]) if v["reviews"] else 0} for k, v in site_rate.items()),
                   key=lambda x: -x["pct"])
    return {"rows": rows, "total_mentions": len(mentions), "reviews_with_mentions": len(total_reviews_with_mentions),
            "reviews_with_text": len(with_text), "pct_of_text_reviews": round(100.0 * len(total_reviews_with_mentions) / len(with_text)) if with_text else 0,
            "sites": sites}
