"""Shared date-range presets (the same windows SYNC offers) plus custom from/to.
All ranges are computed in the app's local timezone and returned as naive UTC
datetimes [start, end) ready for created_at_source comparisons."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, time
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

from .config import settings

PRESETS: List[Tuple[str, str]] = [
    ("today", "Today"), ("yesterday", "Yesterday"), ("last7", "Last 7 days"), ("last30", "Last 30 days"),
    ("this_week", "This week (Mon–Sun)"), ("last_week", "Last week (Mon–Sun)"), ("this_month", "This month"), ("last_month", "Last month"),
    ("this_quarter", "This quarter"), ("last_quarter", "Last quarter"), ("ytd", "Year to date"), ("last12m", "Last 12 months"),
    ("all", "All time"), ("custom", "Custom range"),
]
PRESET_KEYS = {k for k, _ in PRESETS}


@dataclass
class DateRange:
    preset: str
    start_date: date            # local, inclusive
    end_date: date              # local, inclusive
    start: datetime             # naive UTC, inclusive
    end: datetime               # naive UTC, exclusive
    label: str

    @property
    def days(self) -> int:
        return (self.end_date - self.start_date).days + 1

    @property
    def granularity(self) -> str:
        if self.days <= 31:
            return "day"
        if self.days <= 190:
            return "week"
        return "month"

    def query(self) -> str:
        if self.preset == "custom":
            return f"range=custom&start={self.start_date.isoformat()}&end={self.end_date.isoformat()}"
        return f"range={self.preset}"

    @property
    def is_custom(self) -> bool:
        return self.preset == "custom"


def _to_utc(d: date, tz: ZoneInfo, end: bool = False) -> datetime:
    local = datetime.combine(d + timedelta(days=1 if end else 0), time.min).replace(tzinfo=tz)
    return local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def _parse(s: str) -> Optional[date]:
    try:
        return date.fromisoformat(s[:10]) if s else None
    except ValueError:
        return None


def resolve_range(preset: str = "last30", start: str = "", end: str = "", now_local: Optional[datetime] = None) -> DateRange:
    tz = ZoneInfo(settings.timezone)
    now_local = now_local or datetime.now(tz)
    today = now_local.date()
    preset = preset if preset in PRESET_KEYS else "last30"
    s = e = None
    if preset == "custom":
        s, e = _parse(start), _parse(end)
        if s is None and e is None:
            preset = "last30"
        else:
            s = s or e
            e = e or s
            if e < s:
                s, e = e, s
    if preset == "today":
        s = e = today
    elif preset == "yesterday":
        s = e = today - timedelta(days=1)
    elif preset == "last7":
        s, e = today - timedelta(days=6), today
    elif preset == "last30":
        s, e = today - timedelta(days=29), today
    elif preset == "this_week":
        s, e = today - timedelta(days=today.weekday()), today
    elif preset == "last_week":
        this_mon = today - timedelta(days=today.weekday())
        s, e = this_mon - timedelta(days=7), this_mon - timedelta(days=1)
    elif preset == "this_month":
        s, e = today.replace(day=1), today
    elif preset == "last_month":
        first = today.replace(day=1)
        e = first - timedelta(days=1)
        s = e.replace(day=1)
    elif preset == "this_quarter":
        qm = 3 * ((today.month - 1) // 3) + 1
        s, e = today.replace(month=qm, day=1), today
    elif preset == "last_quarter":
        qm = 3 * ((today.month - 1) // 3) + 1
        first = today.replace(month=qm, day=1)
        e = first - timedelta(days=1)
        lqm = 3 * ((e.month - 1) // 3) + 1
        s = e.replace(month=lqm, day=1)
    elif preset == "ytd":
        s, e = today.replace(month=1, day=1), today
    elif preset == "last12m":
        s = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
        for _ in range(10):
            s = (s - timedelta(days=1)).replace(day=1)
        e = today
    elif preset == "all":
        s, e = date(2015, 1, 1), today
    label = dict(PRESETS).get(preset, "Custom range")
    if preset == "custom":
        label = f"{s:%b %-d, %Y} – {e:%b %-d, %Y}" if s != e else f"{s:%b %-d, %Y}"
    return DateRange(preset=preset, start_date=s, end_date=e, start=_to_utc(s, tz), end=_to_utc(e, tz, end=True), label=label)
