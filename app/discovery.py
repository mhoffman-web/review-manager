"""Find listings on each platform and record them as ReviewSourceLink rows, mapping them to
sites by name where that is unambiguous. Shared by the CLI and Admin -> Sites."""
from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import Location, ReviewSourceLink

log = logging.getLogger(__name__)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def excluded(title: str) -> bool:
    t = (title or "").lower()
    return any(p.lower() in t for p in settings.listing_exclude_patterns)


def guess_location(title: str, address: str, locations: List[Location]) -> Optional[Location]:
    """Match a listing to a site when exactly one site name (or city) appears in the title/address."""
    hay = _norm(f"{title} {address}")
    hits = [l for l in locations if _norm(l.name.split(" ", 1)[-1]) and _norm(l.name.split(" ", 1)[-1]) in hay]
    if len(hits) == 1:
        return hits[0]
    hits = [l for l in locations if l.city and _norm(l.city) in hay]
    return hits[0] if len(hits) == 1 else None


def upsert_link(session: Session, source: str, external_location_id: str, display_name: str, *, external_account_id: Optional[str] = None,
                address: Optional[str] = None, listing_url: Optional[str] = None, location: Optional[Location] = None, auto_map: bool = True) -> ReviewSourceLink:
    link = session.execute(select(ReviewSourceLink).where(ReviewSourceLink.source == source,
                                                           ReviewSourceLink.external_location_id == external_location_id)).scalar_one_or_none()
    new = link is None
    if new:
        link = ReviewSourceLink(source=source, external_location_id=external_location_id, active=False)
        session.add(link)
    link.display_name, link.address, link.listing_url = display_name, address or link.address, listing_url or link.listing_url
    if external_account_id:
        link.external_account_id = external_account_id
    if excluded(display_name):
        link.active, link.location_id = False, None      # legacy Wash N' Roll profiles stay untouched
    elif link.location_id is None:
        loc = location or (guess_location(display_name, address or "", session.execute(select(Location).where(Location.active.is_(True))).scalars().all()) if auto_map else None)
        if loc:
            link.location_id, link.active = loc.id, True
    session.flush()
    log.info("%s listing %s '%s' -> %s", "new" if new else "seen", source, display_name, link.location.name if link.location else "unmapped")
    return link


def discover_google(session: Session) -> Dict[str, int]:
    from .sources.google import GoogleBusinessProfileAdapter
    api = GoogleBusinessProfileAdapter()
    totals = {"accounts": 0, "listings": 0, "mapped": 0, "excluded": 0}
    for acct in api.list_accounts():
        totals["accounts"] += 1
        for loc in api.list_locations(acct["name"]):
            totals["listings"] += 1
            addr = loc.get("storefrontAddress") or {}
            address = ", ".join([*(addr.get("addressLines") or []), addr.get("locality") or "", addr.get("administrativeArea") or ""]).strip(", ")
            link = upsert_link(session, "google", loc["name"].split("/")[-1], loc.get("title") or loc["name"], external_account_id=acct["name"],
                               address=address, listing_url=(loc.get("metadata") or {}).get("mapsUri"))
            totals["mapped" if link.location_id else "excluded" if excluded(link.display_name or "") else "listings"] += 1 if link.location_id or excluded(link.display_name or "") else 0
    return totals


def discover_facebook(session: Session) -> Dict[str, int]:
    from .sources.facebook import FacebookPageAdapter
    api = FacebookPageAdapter()
    totals = {"pages": 0, "mapped": 0}
    for page in api.list_pages():
        totals["pages"] += 1
        link = upsert_link(session, "facebook", str(page["id"]), page.get("name") or str(page["id"]), listing_url=page.get("link"))
        if page.get("overall_star_rating") is not None:
            link.avg_rating, link.total_review_count = page.get("overall_star_rating"), page.get("rating_count")
        if link.location_id:
            totals["mapped"] += 1
    return totals
