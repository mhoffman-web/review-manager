"""Parsing of the brand / group / site / rating multi-selects shared by the web pages and the API."""
from __future__ import annotations

from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .models import SiteGroup


def ints(values) -> List[int]:
    out: List[int] = []
    for v in values or []:
        for part in str(v).split(","):
            part = part.strip()
            if part and part != "0":
                try:
                    out.append(int(part))
                except ValueError:
                    pass
    return out


def strs(values) -> List[str]:
    out: List[str] = []
    for v in values or []:
        for part in str(v).split(","):
            if part.strip():
                out.append(part.strip())
    return out


def scope(db: Session, group_ids: List[int], location_ids: List[int]) -> Optional[List[int]]:
    """Resolve group + site multi-selects into one location id list (None = no restriction)."""
    ids: Optional[List[int]] = None
    if group_ids:
        ids = []
        for g in db.execute(select(SiteGroup).where(SiteGroup.id.in_(group_ids)).options(selectinload(SiteGroup.locations))).scalars().all():
            ids.extend(l.id for l in g.locations)
        ids = sorted(set(ids))
    if location_ids:
        ids = sorted(set(location_ids) if ids is None else set(ids) & set(location_ids))
    return ids
