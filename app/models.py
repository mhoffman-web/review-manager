"""Database schema.

Design rule: nothing in here is Google-specific. A review always belongs to a
`ReviewSourceLink` which records which platform ("google", later "yelp", ...)
and which external listing it came from. Adding a new platform means adding a
new adapter under app/sources/ and new ReviewSourceLink rows, not new tables.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey, Integer, String, Table, Text, UniqueConstraint, func, Index,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.utcnow()


class Location(Base):
    """A physical car wash site, independent of any review platform."""
    __tablename__ = "locations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    brand: Mapped[str] = mapped_column(String(40))          # WashU / ICON / WA
    state: Mapped[str] = mapped_column(String(2))
    city: Mapped[Optional[str]] = mapped_column(String(80))
    # Semicolon-separated Snowflake location_ids (old + new ids where migrated).
    snowflake_location_ids: Mapped[Optional[str]] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    sources: Mapped[List["ReviewSourceLink"]] = relationship(back_populates="location")


class ReviewSourceLink(Base):
    """One listing on one platform (e.g. the Google Business Profile for Berwyn)."""
    __tablename__ = "review_sources"
    __table_args__ = (UniqueConstraint("source", "external_location_id", name="uq_source_listing"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    location_id: Mapped[Optional[int]] = mapped_column(ForeignKey("locations.id"), nullable=True)
    source: Mapped[str] = mapped_column(String(20))         # "google"
    external_account_id: Mapped[Optional[str]] = mapped_column(String(120))
    external_location_id: Mapped[str] = mapped_column(String(120))
    display_name: Mapped[Optional[str]] = mapped_column(String(200))
    address: Mapped[Optional[str]] = mapped_column(String(300))
    listing_url: Mapped[Optional[str]] = mapped_column(String(500))
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Platform-reported summary, refreshed each sync.
    avg_rating: Mapped[Optional[float]] = mapped_column()
    total_review_count: Mapped[Optional[int]] = mapped_column(Integer)
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    last_sync_status: Mapped[Optional[str]] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # Sync health: consecutive failures, the last error text, and when we last emailed about it.
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    failure_notified_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    # Source-specific extras as JSON (e.g. the Facebook page id); never secrets.
    extra_json: Mapped[Optional[str]] = mapped_column(Text)

    location: Mapped[Optional[Location]] = relationship(back_populates="sources")
    reviews: Mapped[List["Review"]] = relationship(back_populates="source_link")

    @property
    def extra(self) -> dict:
        import json
        try:
            return json.loads(self.extra_json or "{}")
        except ValueError:
            return {}


class Review(Base):
    __tablename__ = "reviews"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_review_external"),
        Index("ix_reviews_created", "created_at_source"),
        Index("ix_reviews_unanswered", "has_owner_reply", "is_deleted"),
        Index("ix_reviews_link", "source_link_id"),        # daily full pull, per-site pages
        Index("ix_reviews_removed", "removed_at"),
        Index("ix_reviews_alerted", "alerted_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_link_id: Mapped[int] = mapped_column(ForeignKey("review_sources.id"))
    source: Mapped[str] = mapped_column(String(20))
    external_id: Mapped[str] = mapped_column(String(200))

    author_name: Mapped[Optional[str]] = mapped_column(String(200))
    author_is_anonymous: Mapped[bool] = mapped_column(Boolean, default=False)
    rating: Mapped[Optional[int]] = mapped_column(Integer)   # 1..5, None if unrated
    text: Mapped[Optional[str]] = mapped_column(Text)
    created_at_source: Mapped[datetime] = mapped_column(DateTime)
    updated_at_source: Mapped[datetime] = mapped_column(DateTime)

    # Owner reply as the platform currently shows it.
    has_owner_reply: Mapped[bool] = mapped_column(Boolean, default=False)
    owner_reply_text: Mapped[Optional[str]] = mapped_column(Text)
    owner_reply_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    # Room for later automation (Claude tagging etc.). Unused by v1 code paths.
    category: Mapped[Optional[str]] = mapped_column(String(60))
    sentiment: Mapped[Optional[str]] = mapped_column(String(20))
    internal_note: Mapped[Optional[str]] = mapped_column(Text)

    raw_json: Mapped[Optional[str]] = mapped_column(Text)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    # Who is handling it (avoids two agents replying to the same review).
    assigned_to_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    assigned_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    # Archived = deliberately not replying (spam, duplicate, rating-only we skip).
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False)
    archived_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    archived_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))

    # When our FIRST reply went up. Editing a reply later moves owner_reply_updated_at
    # on the platform but must not move the response time, so metrics use this.
    first_replied_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    # The reviewer changed the star rating after posting: what it was, and when we noticed.
    prev_rating: Mapped[Optional[int]] = mapped_column(Integer)
    rating_changed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    # The review stopped coming back from the platform (reviewer deleted it or Google took it down).
    removed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    removal_notified_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    # Instant alert for a new negative review was sent.
    alerted_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    # We reported it to the platform for removal: None / reported / removed / kept.
    report_status: Mapped[Optional[str]] = mapped_column(String(20))
    reported_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    reported_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    report_note: Mapped[Optional[str]] = mapped_column(Text)
    # Where the theme came from: keyword / ai / manual. Manual wins and is never overwritten.
    category_source: Mapped[Optional[str]] = mapped_column(String(10))

    source_link: Mapped[ReviewSourceLink] = relationship(back_populates="reviews")
    responses: Mapped[List["Response"]] = relationship(back_populates="review", order_by="Response.created_at")
    assigned_to: Mapped[Optional["User"]] = relationship(foreign_keys=[assigned_to_id])
    mentions: Mapped[List["ReviewMention"]] = relationship(back_populates="review", cascade="all, delete-orphan")
    events: Mapped[List["ReviewEvent"]] = relationship(back_populates="review", cascade="all, delete-orphan", order_by="ReviewEvent.at")
    reported_by: Mapped[Optional["User"]] = relationship(foreign_keys=[reported_by_id])

    @property
    def rating_delta(self) -> Optional[int]:
        """+n if the reviewer raised the rating, -n if they lowered it, None if unchanged."""
        if self.prev_rating is None or self.rating is None or self.rating == self.prev_rating:
            return None
        return self.rating - self.prev_rating

    @property
    def is_disputed(self) -> bool:
        return self.report_status == "reported"

    @property
    def replied_at(self) -> Optional[datetime]:
        return self.first_replied_at or self.owner_reply_updated_at

    @property
    def is_rating_only(self) -> bool:
        return not (self.text or "").strip()

    @property
    def was_edited(self) -> bool:
        return bool(self.updated_at_source and self.created_at_source and self.updated_at_source > self.created_at_source + __import__("datetime").timedelta(minutes=1))

    @property
    def mention_names(self) -> List[str]:
        return [m.name for m in self.mentions]

    @property
    def response_hours(self) -> Optional[float]:
        at = self.replied_at
        if self.has_owner_reply and at and self.created_at_source:
            return max(0.0, (at - self.created_at_source).total_seconds() / 3600)
        return None

    @property
    def first_name(self) -> str:
        if not self.author_name or self.author_is_anonymous:
            return "there"
        return self.author_name.split()[0].rstrip(".,")

    @property
    def is_negative(self) -> bool:
        from .config import settings
        return self.rating is not None and self.rating <= settings.negative_rating_max

    @property
    def location(self) -> Optional[Location]:
        return self.source_link.location if self.source_link else None


class Response(Base):
    """A reply we wrote. One review can have several rows (edits, failures)."""
    __tablename__ = "responses"
    __table_args__ = (Index("ix_responses_review", "review_id"), Index("ix_responses_created", "created_at"))

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    review_id: Mapped[int] = mapped_column(ForeignKey("reviews.id"))
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft/posted/failed/deleted
    created_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    posted_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    error: Mapped[Optional[str]] = mapped_column(Text)
    template_id: Mapped[Optional[int]] = mapped_column(ForeignKey("reply_templates.id"))
    ai_generated: Mapped[bool] = mapped_column(Boolean, default=False)

    review: Mapped[Review] = relationship(back_populates="responses")
    created_by: Mapped[Optional["User"]] = relationship()


class ReviewEvent(Base):
    """Activity timeline: everything that happened to a review, by whom, when.
    `actor_id` is None for things the sync noticed (rating changed, removed)."""
    __tablename__ = "review_events"
    __table_args__ = (Index("ix_review_events_review", "review_id", "at"), Index("ix_review_events_kind", "kind", "at"))

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    review_id: Mapped[int] = mapped_column(ForeignKey("reviews.id"))
    kind: Mapped[str] = mapped_column(String(30))
    actor_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    detail_json: Mapped[Optional[str]] = mapped_column(Text)

    review: Mapped["Review"] = relationship(back_populates="events")
    actor: Mapped[Optional["User"]] = relationship()

    @property
    def detail(self) -> dict:
        import json
        try:
            return json.loads(self.detail_json or "{}")
        except ValueError:
            return {}

    LABELS = {
        "reply_posted": "Reply posted", "reply_edited": "Reply edited", "reply_deleted": "Reply removed", "reply_failed": "Reply failed to post",
        "archived": "Archived", "unarchived": "Restored to inbox", "note_saved": "Internal note saved", "category_changed": "Theme changed",
        "mention_added": "Employee tagged", "mention_removed": "Employee tag removed", "rating_changed": "Reviewer changed the rating",
        "text_changed": "Reviewer edited the text", "removed": "No longer on the platform", "restored": "Back on the platform",
        "reported": "Reported for removal", "report_outcome": "Report outcome recorded", "alert_sent": "Alert emailed",
        "assigned": "Assigned", "unassigned": "Unassigned",
    }

    @property
    def label(self) -> str:
        return self.LABELS.get(self.kind, self.kind.replace("_", " ").capitalize())


class SyncRun(Base):
    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_link_id: Mapped[Optional[int]] = mapped_column(ForeignKey("review_sources.id"))
    full: Mapped[bool] = mapped_column(Boolean, default=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(20), default="running")  # running/ok/error
    # False when a full pull returned nothing or fewer reviews than the platform's own total:
    # the pull succeeded but proves nothing about removals, so it never confirms one.
    complete: Mapped[bool] = mapped_column(Boolean, default=True)
    reviews_seen: Mapped[int] = mapped_column(Integer, default=0)
    reviews_new: Mapped[int] = mapped_column(Integer, default=0)
    reviews_updated: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[Optional[str]] = mapped_column(Text)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(200), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[Optional[str]] = mapped_column(String(200))   # None = SSO-only account
    role: Mapped[str] = mapped_column(String(20), default="agent")  # agent / admin
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    # Microsoft Entra identity, filled on first SSO sign-in.
    ms_oid: Mapped[Optional[str]] = mapped_column(String(64), unique=True)
    ms_tenant_id: Mapped[Optional[str]] = mapped_column(String(64))
    auth_provider: Mapped[str] = mapped_column(String(20), default="local")  # local / microsoft

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def first_name(self) -> str:
        """First word of the name, or the email's local part when the name is blank."""
        parts = (self.name or "").split()
        return parts[0] if parts else (self.email or "").split("@")[0]


class ReportRecipient(Base):
    """Who gets the morning email. `brands` optionally limits to e.g. "WashU" or "ICON;WA"."""
    __tablename__ = "report_recipients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(200), unique=True)
    name: Mapped[Optional[str]] = mapped_column(String(120))
    brands: Mapped[Optional[str]] = mapped_column(String(120))      # legacy; edition is what the digest uses now
    edition: Mapped[str] = mapped_column(String(40), default="all")  # "il", "tn", "all", "alerts" or a ";" list of them
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    def brand_list(self) -> List[str]:
        return [b.strip() for b in (self.brands or "").split(";") if b.strip()]

    @property
    def editions(self) -> List[str]:
        """Digest editions this person gets (il / tn / all). Stored as a ';' list so one
        address can be on more than one edition; a legacy NULL means Corporate."""
        raw = "all" if self.edition is None else self.edition
        return [e.strip() for e in raw.split(";") if e.strip()]

    def set_editions(self, eds) -> None:
        wanted = set(eds)
        self.edition = ";".join(e for e in ("il", "tn", "all", "alerts") if e in wanted)


class ReportSend(Base):
    """Audit log of morning reports, also used to avoid double-sending."""
    __tablename__ = "report_sends"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_date: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD local
    brands: Mapped[Optional[str]] = mapped_column(String(120))
    recipients: Mapped[str] = mapped_column(Text)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    status: Mapped[str] = mapped_column(String(20), default="ok")
    error: Mapped[Optional[str]] = mapped_column(Text)


class ReplyTemplate(Base):
    """Canned replies. Placeholders: {first_name} {site} {brand} {agent}."""
    __tablename__ = "reply_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    brand: Mapped[Optional[str]] = mapped_column(String(40))      # None = all brands
    min_rating: Mapped[int] = mapped_column(Integer, default=1)
    max_rating: Mapped[int] = mapped_column(Integer, default=5)
    body: Mapped[str] = mapped_column(Text)
    # Semicolon-separated hints used for suggestions: general, no_comment, team,
    # employee, amenities, mixed, wait, quality, damage, billing, staff, plate, hours
    tags: Mapped[Optional[str]] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=100)
    usage_count: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    updated_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    updated_by: Mapped[Optional["User"]] = relationship()

    def tag_list(self) -> List[str]:
        return [t.strip().lower() for t in (self.tags or "").split(";") if t.strip()]

    @property
    def rating_band(self) -> str:
        if self.min_rating >= 5:
            return "5 star"
        if self.min_rating >= 4:
            return "4 star"
        if self.max_rating <= 3:
            return "1-3 star"
        return "Any rating"

    def applies_to(self, review: "Review") -> bool:
        if self.active is False:  # None = unsaved row, treat as active
            return False
        if self.brand and review.location and review.location.brand != self.brand:
            return False
        if review.rating is None:
            return True
        return self.min_rating <= review.rating <= self.max_rating

    def render(self, review: "Review", agent_name: str = "", employee: str = "") -> str:
        loc = review.location
        names = review.mention_names if review.mentions is not None else []
        emp = employee or (" and ".join(names[:2]) if names else "our team")
        return (self.body
                .replace("{first_name}", review.first_name)
                .replace("{site}", loc.name if loc else "our location")
                .replace("{brand}", loc.brand if loc else "")
                .replace("{employee}", emp)
                .replace("{agent}", agent_name))


class Employee(Base):
    """Roster of staff whose names we want to recognise in reviews."""
    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    aliases: Mapped[Optional[str]] = mapped_column(String(200))   # semicolon-separated nicknames
    location_id: Mapped[Optional[int]] = mapped_column(ForeignKey("locations.id"))
    role: Mapped[Optional[str]] = mapped_column(String(60))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    location: Mapped[Optional[Location]] = relationship()

    def all_names(self) -> List[str]:
        return [self.name] + [a.strip() for a in (self.aliases or "").split(";") if a.strip()]


class ReviewMention(Base):
    """An employee name detected in a review's text."""
    __tablename__ = "review_mentions"
    __table_args__ = (UniqueConstraint("review_id", "name", name="uq_mention"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    review_id: Mapped[int] = mapped_column(ForeignKey("reviews.id"))
    name: Mapped[str] = mapped_column(String(80))
    employee_id: Mapped[Optional[int]] = mapped_column(ForeignKey("employees.id"))
    source: Mapped[str] = mapped_column(String(20), default="auto")  # auto / manual
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    review: Mapped[Review] = relationship(back_populates="mentions")
    employee: Mapped[Optional[Employee]] = relationship()


site_group_members = Table(
    "site_group_members", Base.metadata,
    Column("group_id", ForeignKey("site_groups.id"), primary_key=True),
    Column("location_id", ForeignKey("locations.id"), primary_key=True),
)


class SiteGroup(Base):
    """A named set of sites, e.g. a regional manager's stores."""
    __tablename__ = "site_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    description: Mapped[Optional[str]] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    locations: Mapped[List[Location]] = relationship(secondary=site_group_members, order_by=Location.name)


class SavedView(Base):
    """A saved inbox filter shown as a tab. Shared views are visible to everyone."""
    __tablename__ = "saved_views"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    owner_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    is_shared: Mapped[bool] = mapped_column(Boolean, default=True)
    params_json: Mapped[str] = mapped_column(Text, default="{}")
    sort_order: Mapped[int] = mapped_column(Integer, default=100)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    owner: Mapped[Optional["User"]] = relationship()

    def params(self) -> dict:
        import json
        try:
            return json.loads(self.params_json or "{}")
        except ValueError:
            return {}


class AiRule(Base):
    """House rules fed to the AI when drafting replies (one sentence each)."""
    __tablename__ = "ai_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, default=100)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ApiKey(Base):
    """Read-only API access for other systems (e.g. the Teams Portal). Only a hash is stored;
    the full key is shown once when created."""
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    prefix: Mapped[str] = mapped_column(String(12))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_by: Mapped[Optional["User"]] = relationship()


# ---- every String(n) column is truncated to n on write. SQLite ignores the length, Postgres
# rejects the row (a 500), so a long template name or city must never reach the database whole.
from sqlalchemy import String as _String, event as _event
from sqlalchemy.orm import Session as _Session


@_event.listens_for(_Session, "before_flush")
def _truncate_strings(session, _ctx, _instances):
    for obj in list(session.new) + list(session.dirty):
        table = getattr(obj, "__table__", None)
        if table is None:
            continue
        for col in table.columns:
            n = getattr(col.type, "length", None)
            if not n or not isinstance(col.type, _String):
                continue
            key = col.key
            val = obj.__dict__.get(key)
            if isinstance(val, str) and len(val) > n:
                setattr(obj, key, val[:n])
