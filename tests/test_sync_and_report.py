"""Exercises the sync upsert logic and the report builder with a fake adapter.
No network, in-memory SQLite."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.db import SessionLocal, engine, init_db
from app.models import Base, Location, ReplyTemplate, Review, ReviewSourceLink, User
from app.daterange import resolve_range
from app.reports import build_digest, build_report, build_trends, digest_subject, render_digest_html, render_digest_text, render_report_html, render_report_text, window_report
from app.sources.base import NormalizedReview, SourceSummary
from app.sources.google import GoogleBusinessProfileAdapter, _parse_ts
from app.sync import sync_link


class FakeAdapter:
    name = "google"

    def __init__(self, reviews):
        self.reviews = reviews
        self.posted = {}

    def fetch_reviews(self, link, since=None):
        summary = SourceSummary(avg_rating=4.4, total_review_count=len(self.reviews))
        for i, r in enumerate(sorted(self.reviews, key=lambda x: x.updated_at, reverse=True)):
            if since and r.updated_at < since:
                return
            yield r, (summary if i == 0 else None)

    def post_reply(self, link, external_review_id, text):
        self.posted[external_review_id] = text
        return datetime.utcnow()

    def delete_reply(self, link, external_review_id):
        self.posted.pop(external_review_id, None)


def mk_yesterday(s, link):
    """Insert one 2-star review stamped yesterday noon local time."""
    from zoneinfo import ZoneInfo
    from app.config import settings
    tz = ZoneInfo(settings.timezone)
    local = (datetime.now(tz) - timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
    t = local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    r = Review(source_link_id=link.id, source="google", external_id="yday", author_name="Dee P", rating=2, text="Gate would not open for my plate",
               created_at_source=t, updated_at_source=t, has_owner_reply=False, raw_json="{}")
    s.add(r); s.commit()
    return r


def nr(ext, rating, text, days_ago, reply=None):
    t = datetime.utcnow() - timedelta(days=days_ago)
    return NormalizedReview(ext, "Pat", False, rating, text, t, t, reply, t if reply else None, "{}")


@pytest.fixture()
def db():
    Base.metadata.drop_all(engine)
    init_db()
    s = SessionLocal()
    loc = Location(name="WashU Berwyn", brand="WashU", state="IL", city="Berwyn")
    s.add(loc); s.flush()
    link = ReviewSourceLink(location_id=loc.id, source="google", external_account_id="1", external_location_id="L1", display_name="WashU Berwyn")
    s.add(link); s.add(User(email="lily@x.com", name="Lily", password_hash="x")); s.commit()
    yield s, link
    s.close()


def test_backfill_then_incremental(db):
    s, link = db
    fake = FakeAdapter([nr("a", 5, "great", 10, reply="thanks"), nr("b", 2, "slow line", 0.5), nr("c", 4, "ok", 40)])
    run = sync_link(s, link, full=True, adapter=fake); s.commit()
    assert (run.reviews_new, run.reviews_updated, run.status) == (3, 0, "ok")
    assert link.avg_rating == 4.4 and link.total_review_count == 3

    # Second pass with nothing changed: idempotent.
    run = sync_link(s, link, adapter=fake); s.commit()
    assert (run.reviews_new, run.reviews_updated) == (0, 0)

    # Customer edits review b and we reply to it on Google directly.
    fake.reviews[1] = nr("b", 3, "slow line, but staff fixed it", 0, reply="sorry about that")
    run = sync_link(s, link, adapter=fake); s.commit()
    assert (run.reviews_new, run.reviews_updated) == (0, 1)
    b = s.execute(select(Review).where(Review.external_id == "b")).scalar_one()
    assert b.rating == 3 and b.has_owner_reply and b.owner_reply_text == "sorry about that"

    # Review c disappears from Google: incremental pulls never remove, one full pull is a hold,
    # the second consecutive full pull that misses it marks it removed.
    fake.reviews = fake.reviews[:2]
    sync_link(s, link, adapter=fake); s.commit()
    assert not s.execute(select(Review).where(Review.external_id == "c")).scalar_one().is_deleted
    sync_link(s, link, full=True, adapter=fake); s.commit()
    assert not s.execute(select(Review).where(Review.external_id == "c")).scalar_one().is_deleted
    sync_link(s, link, full=True, adapter=fake); s.commit()
    assert s.execute(select(Review).where(Review.external_id == "c")).scalar_one().is_deleted


def test_report_counts(db):
    s, link = db
    fake = FakeAdapter([nr("a", 5, "great", 10, reply="thanks"), nr("b", 1, "awful", 0.2), nr("d", 4, "fine", 3)])
    sync_link(s, link, full=True, adapter=fake); s.commit()
    d = build_report(s)
    assert d.new_24h == 1 and d.avg_24h == 1.0
    assert [r.external_id for r in d.negative_24h] == ["b"]
    assert d.unanswered_total == 2 and d.overdue_total == 1  # d is >48h old and unanswered
    site = d.sites[0]
    assert site.name == "WashU Berwyn" and site.count_30d == 3 and site.response_rate_30d == 33
    html = render_report_html(d)
    assert "awful" in html and "WashU Berwyn" in html
    assert "Negative reviews" in render_report_text(d)
    # yesterday digest editions: IL sees the WashU site, TN sees nothing, Corporate sees everything
    yday = mk_yesterday(s, link)
    il = build_digest(s, "il")
    assert il.label == "Illinois" and il.total == 1 and il.sites[0].name == "WashU Berwyn" and il.counts[1] == 1
    assert build_digest(s, "tn").total == 0 and build_digest(s, "all").total == 1
    html = render_digest_html(il)
    assert "Reviews received yesterday" in html and "Illinois" in html and "unanswered" not in html.lower() and yday.text in html
    assert "WashU Berwyn" in render_digest_text(il)
    assert digest_subject(il).startswith("Reviews ") and "IL: 1 received" in digest_subject(il)
    # Brand filter that matches nothing yields an empty report, not an error.
    assert build_report(s, brands=["ICON"]).new_24h == 0
    # Week-to-date (Mon–Sun) block: counts by site only, no review text; absent on a Monday, "Full week" on a Sunday.
    from zoneinfo import ZoneInfo
    from app.config import settings
    tz = ZoneInfo(settings.timezone)
    for ext, day, rating, text in [("mon", 1, 5, "Monday sparkle"), ("wed", 3, 1, "Wednesday scratch")]:
        t = datetime(2026, 6, day, 12, 0, tzinfo=tz).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
        s.add(Review(source_link_id=link.id, source="google", external_id=ext, author_name="Al", rating=rating, text=text,
                     created_at_source=t, updated_at_source=t, has_owner_reply=False, raw_json="{}"))
    s.commit()
    wed = build_digest(s, "il", resolve_range("custom", "2026-06-03", "2026-06-03"))
    assert wed.total == 1 and wed.wtd is not None and wed.wtd.total == 2 and wed.wtd.counts == [1, 0, 0, 0, 1]
    assert wed.wtd_title == "Week to date · Mon Jun 1 – Wed Jun 3" and wed.wtd.sites[0].reviews == [] and wed.wtd.wtd is None
    html = render_digest_html(wed)
    assert "Week to date" in html and "Wednesday scratch" in html and "Monday sparkle" not in html
    assert "Week to date" in render_digest_text(wed) and "Monday sparkle" not in render_digest_text(wed)
    assert build_digest(s, "il", resolve_range("custom", "2026-06-01", "2026-06-01")).wtd is None
    assert build_digest(s, "il", resolve_range("custom", "2026-06-07", "2026-06-07")).wtd_title == "Full week · Mon Jun 1 – Sun Jun 7"
    assert build_digest(s, "il", resolve_range("last7")).wtd is None


def test_google_normalize_and_timestamps():
    raw = {"reviewId": "abc", "reviewer": {"displayName": "Sam", "isAnonymous": False}, "starRating": "FOUR",
           "comment": "Nice", "createTime": "2026-09-30T14:05:42.056Z", "updateTime": "2026-10-01T09:00:00Z",
           "reviewReply": {"comment": "Thanks!", "updateTime": "2026-10-01T10:30:00.5Z"}}
    n = GoogleBusinessProfileAdapter.normalize(raw)
    assert n.rating == 4 and n.external_id == "abc" and n.owner_reply_text == "Thanks!"
    assert n.created_at == datetime(2026, 9, 30, 14, 5, 42, 56000)
    assert n.updated_at == datetime(2026, 10, 1, 9, 0, 0)
    assert _parse_ts("2026-10-01T10:30:00.5Z") == datetime(2026, 10, 1, 10, 30, 0, 500000)
    assert GoogleBusinessProfileAdapter.normalize({"reviewId": "z", "starRating": "STAR_RATING_UNSPECIFIED", "createTime": "2026-01-01T00:00:00Z"}).rating is None


def test_response_time_trends_and_templates(db):
    s, link = db
    fake = FakeAdapter([nr("a", 5, "great", 10, reply="thanks"), nr("b", 1, "awful", 0.2), nr("d", 4, "fine", 3, reply="ty")])
    sync_link(s, link, full=True, adapter=fake); s.commit()
    a = s.execute(select(Review).where(Review.external_id == "a")).scalar_one()
    a.owner_reply_updated_at = a.first_replied_at = a.created_at_source + timedelta(hours=6)
    d = s.execute(select(Review).where(Review.external_id == "d")).scalar_one()
    d.owner_reply_updated_at = d.first_replied_at = d.created_at_source + timedelta(hours=30)
    d.category = "Long Line"; d.rating = 2
    s.commit()
    rep = build_report(s)
    assert rep.median_response_h_30d == 18.0 and rep.response_rate_30d == 67
    assert set(rep.negative_themes_30d) == {("Long Line", 1), ("Unknown", 1)}
    t = build_trends(s, resolve_range("last30"))
    assert t["brands"] == ["WashU"] and len(t["labels"]) == 30 and sum(t["counts"]["WashU"]) == 3
    assert t["rating_dist"] == [1, 1, 0, 0, 1]
    loc = s.execute(select(Location)).scalar_one()
    w = window_report(s, resolve_range("last30"), location_ids=[loc.id])
    assert w.count == 3 and w.unanswered_now == 1 and w.themes == [("Long Line", 1), ("Unknown", 1)]
    assert w.theme_matrix["WashU Berwyn"]["Long Line"] == 1
    tpl = ReplyTemplate(name="Sorry", min_rating=1, max_rating=3, body="Hi {first_name}, sorry about {site}. – {agent}")
    b = s.execute(select(Review).where(Review.external_id == "b")).scalar_one()
    assert tpl.applies_to(b) and not tpl.applies_to(a)
    assert tpl.render(b, "Lily") == "Hi Pat, sorry about WashU Berwyn. – Lily"
    assert ReplyTemplate(name="x", brand="ICON", body="").applies_to(b) is False


def test_web_pages_render(db):
    from fastapi.testclient import TestClient
    from app import auth
    from app.web import app
    s, link = db
    fake = FakeAdapter([nr("a", 5, "great", 10, reply="thanks"), nr("b", 1, "awful", 0.2)])
    sync_link(s, link, full=True, adapter=fake); s.commit()
    u = s.execute(select(User)).scalar_one()
    s.add(ReplyTemplate(name="Sorry", min_rating=1, max_rating=3, body="Hi {first_name}")); s.commit()
    c = TestClient(app)
    c.cookies.set(auth.COOKIE_NAME, auth.make_session_cookie(u))
    for path in ["/", "/?view=all&q=awful", "/reports", "/reports/morning", "/locations", "/admin/templates", f"/sites/{link.location_id}",
                 "/reviews/%d" % s.execute(select(Review.id).where(Review.external_id == "b")).scalar()]:
        r = c.get(path)
        assert r.status_code == 200, (path, r.status_code)
    assert "awful" in c.get("/?view=all&q=awful").text
    assert c.get("/admin/users").status_code == 403   # agent, not admin
    rid = s.execute(select(Review.id).where(Review.external_id == "b")).scalar()
    r = c.post(f"/reviews/{rid}/claim", data={"back": "/"}, follow_redirects=False)
    assert r.status_code == 303
    s.expire_all()
    assert s.get(Review, rid).assigned_to_id == u.id


def test_rating_change_removal_and_alerts(db):
    from app.alerts import collect, send_alerts
    from app.models import ReportRecipient, ReviewEvent
    from app.sources.facebook import FacebookPageAdapter
    s, link = db
    fake = FakeAdapter([nr("p", 4, "pretty good", 0.3), nr("q", 1, "terrible", 0.2)])
    sync_link(s, link, full=True, adapter=fake); s.commit()
    # the reviewer lowers p to 2 and q disappears; two consecutive full pulls confirm the removal
    fake2 = FakeAdapter([nr("p", 2, "pretty good", 0.3)])
    sync_link(s, link, full=True, adapter=fake2); s.commit()
    assert not s.execute(select(Review).where(Review.external_id == "q")).scalar_one().is_deleted
    sync_link(s, link, full=True, adapter=fake2); s.commit()
    p = s.execute(select(Review).where(Review.external_id == "p")).scalar_one()
    q = s.execute(select(Review).where(Review.external_id == "q")).scalar_one()
    assert p.rating == 2 and p.prev_rating == 4 and p.rating_delta == -2 and p.rating_changed_at is not None
    assert q.is_deleted and q.removed_at is not None and [e.kind for e in q.events] == ["removed"]
    assert any(e.kind == "rating_changed" and e.detail == {"from": 4, "to": 2} for e in p.events)
    # a full pull that returns nothing is a hiccup, not a mass deletion
    sync_link(s, link, full=True, adapter=FakeAdapter([])); s.commit()
    s.expire_all()
    assert s.get(Review, p.id).is_deleted is False
    # alerts: the new negative (p is now 2 stars, first seen recently) and the removed q are waiting
    b = collect(s)
    assert {r.external_id for r in b.negatives} == {"p"} and {r.external_id for r in b.removed} == {"q"}
    assert send_alerts(s, dry_run=True) is None                       # nobody on the alerts list yet -> nothing stamped
    s.add(ReportRecipient(email="ops@x.com", edition="alerts")); s.commit()
    out = send_alerts(s, dry_run=True)
    assert out and out["count"] == 2 and "1 new negative review" in out["subject"] and "1 review removed" in out["subject"] and "ops@x.com" in out["to"]
    assert "terrible" in out["html"] and "pretty good" in out["html"]
    s.commit(); s.expire_all()
    assert s.get(Review, p.id).alerted_at is not None and s.get(Review, q.id).removal_notified_at is not None and collect(s).empty
    # failing listing: three errors in a row raise an alert once
    class Boom:
        def fetch_reviews(self, link, since=None):
            raise RuntimeError("401 token revoked")
    for _ in range(3):
        sync_link(s, link, adapter=Boom()); s.commit()
    s.expire_all()
    assert link.fail_count == 3 and "token revoked" in link.last_error
    b = collect(s)
    assert [l.id for l in b.failing] == [link.id]
    sync_link(s, link, full=True, adapter=FakeAdapter([nr("p", 2, "pretty good", 0.3)])); s.commit(); s.expire_all()
    assert link.fail_count == 0 and link.last_error is None
    # Facebook recommendations normalise into the same shape; the page's own comment is the owner reply
    raw = {"created_time": "2026-09-30T14:05:42+0000", "recommendation_type": "negative", "review_text": "Dryer left the car soaked",
           "reviewer": {"name": "Jo Bloggs", "id": "77"}, "open_graph_story": {"id": "1001_2002", "comments": {"data": [
               {"message": "So sorry Jo, call us.", "created_time": "2026-09-30T16:00:00+0000", "from": {"id": "PAGE1", "name": "ICON Car Wash"}}]}}}
    n = FacebookPageAdapter.normalize(raw, "PAGE1")
    assert n.external_id == "1001_2002" and n.rating == 1 and n.owner_reply_text == "So sorry Jo, call us." and n.created_at == datetime(2026, 9, 30, 14, 5, 42)
    assert FacebookPageAdapter.normalize({"created_time": "2026-09-30T14:05:42+0000", "recommendation_type": "positive", "reviewer": {"name": "A"}}, "PAGE1").rating == 5


class TruncatedAdapter(FakeAdapter):
    """Yields its reviews but reports a platform total that says pages were dropped."""
    def __init__(self, reviews, total):
        super().__init__(reviews)
        self.total = total

    def fetch_reviews(self, link, since=None):
        summary = SourceSummary(avg_rating=4.4, total_review_count=self.total)
        for i, r in enumerate(self.reviews):
            yield r, (summary if i == 0 else None)


def test_removal_guards_and_restore(db, monkeypatch):
    from app.alerts import collect, send_alerts
    from app.models import ReportRecipient
    s, link = db
    s.add(ReportRecipient(email="ops@x.com", edition="alerts")); s.commit()
    everything = [nr("a", 5, "great", 10), nr("b", 2, "slow", 5), nr("c", 4, "ok", 3), nr("d", 1, "awful", 2)]
    sync_link(s, link, full=True, adapter=FakeAdapter(everything)); s.commit()
    sync_link(s, link, full=True, adapter=FakeAdapter(everything)); s.commit()
    # a truncated pull (saw 2, platform says 4) marks nothing even on the second miss
    for _ in range(2):
        sync_link(s, link, full=True, adapter=TruncatedAdapter(everything[:2], total=4)); s.commit()
    s.expire_all()
    assert not any(r.is_deleted for r in s.execute(select(Review)).scalars())
    # a small discrepancy (platform total 4, we saw 3 of 3 remaining) is a real removal after two misses
    three = everything[:3]
    sync_link(s, link, full=True, adapter=FakeAdapter(three)); s.commit()
    s.expire_all()
    assert not s.execute(select(Review).where(Review.external_id == "d")).scalar_one().is_deleted
    d = s.execute(select(Review).where(Review.external_id == "d")).scalar_one()
    d.report_status = "reported"; s.commit()
    sync_link(s, link, full=True, adapter=FakeAdapter(three)); s.commit()
    s.expire_all()
    d = s.execute(select(Review).where(Review.external_id == "d")).scalar_one()
    assert d.is_deleted and d.report_status == "removed"
    out = send_alerts(s, dry_run=True); s.commit(); s.expire_all()
    assert out and "1 review removed" in out["subject"]
    d = s.get(Review, d.id)
    assert d.removal_notified_at is not None
    # d comes back: restored, the removal alert is re-armed and the auto-closed report reopens
    sync_link(s, link, full=True, adapter=FakeAdapter(everything)); s.commit(); s.expire_all()
    d = s.get(Review, d.id)
    assert d.is_deleted is False and d.removed_at is None and d.removal_notified_at is None and d.report_status == "reported"
    kinds = [e.kind for e in d.events]
    assert kinds.count("removed") == 1 and "restored" in kinds and kinds.count("report_outcome") == 2
    assert collect(s).removed == []
    # REMOVAL_CONFIRM_PULLS=1 restores the old single-pull behaviour
    from app import sync as sync_mod
    monkeypatch.setattr(sync_mod.settings, "removal_confirm_pulls", 1)
    sync_link(s, link, full=True, adapter=FakeAdapter(three)); s.commit(); s.expire_all()
    d = s.get(Review, d.id)
    assert d.is_deleted and d.removal_notified_at is None     # it will be alerted again
    assert {r.external_id for r in collect(s).removed} == {"d"}


def test_sync_all_survives_a_poisoned_session(db, monkeypatch):
    """A failed flush inside one listing's pull (the admin's manual Sync racing the worker) must
    not abort the tick, lose the listing's fail_count, or skip the remaining listings."""
    from app import sync as sync_mod
    from app.models import SyncRun
    from app.sync import sync_all
    s, link = db
    loc2 = Location(name="WashU Burbank", brand="WashU", state="IL", city="Burbank")
    s.add(loc2); s.flush()
    link2 = ReviewSourceLink(location_id=loc2.id, source="google", external_account_id="1", external_location_id="L2", display_name="WashU Burbank")
    s.add(link2); s.commit()
    poison = {"on": True}

    class Racing(FakeAdapter):
        def fetch_reviews(self, link, since=None):
            yield from super().fetch_reviews(link, since)
            if poison["on"] and link.external_location_id == "L1":
                # a write that fails at flush: the session is unusable until rolled back
                t = datetime.utcnow()
                s.add(Review(source_link_id=999999, source="google", external_id="ghost", created_at_source=t, updated_at_source=t, raw_json="{}"))
                s.flush()

    monkeypatch.setattr(sync_mod, "get_adapter", lambda source: Racing([nr("x", 5, "fine", 1), nr("y", 3, "meh", 2)]))
    totals = sync_all(s, full=True)
    assert (totals["links"], totals["ok"], totals["error"]) == (2, 1, 1)
    s.expire_all()
    l1 = s.get(ReviewSourceLink, link.id); l2 = s.get(ReviewSourceLink, link2.id)
    assert l1.last_sync_status == "error" and l1.fail_count == 1 and "FOREIGN KEY" in l1.last_error
    assert l2.last_sync_status == "ok" and l2.fail_count == 0
    assert s.execute(select(Review).where(Review.source_link_id == link2.id)).scalars().all().__len__() == 2
    runs = s.execute(select(SyncRun).where(SyncRun.source_link_id == link.id)).scalars().all()
    assert len(runs) == 1 and runs[0].status == "error" and runs[0].finished_at is not None
    assert not s.execute(select(Review).where(Review.external_id == "ghost")).scalar_one_or_none()
    # the next clean tick heals the listing
    poison["on"] = False
    totals = sync_all(s, full=True)
    s.expire_all()
    assert totals["error"] == 0 and s.get(ReviewSourceLink, link.id).fail_count == 0


def test_database_url_normalised_for_psycopg3():
    from app.config import normalize_database_url as n
    assert n("postgres://u:p@h:5432/db") == "postgresql+psycopg://u:p@h:5432/db"
    assert n("postgresql://u:p@h/db?sslmode=require") == "postgresql+psycopg://u:p@h/db?sslmode=require"
    assert n("postgresql+psycopg2://u@h/db") == "postgresql+psycopg://u@h/db"
    assert n("postgresql+psycopg://u@h/db") == "postgresql+psycopg://u@h/db"
    assert n("sqlite:///./review_manager.db") == "sqlite:///./review_manager.db"


def test_facebook_paging_keeps_cursor_and_stops_on_repeat():
    from app.sources.facebook import FacebookPageAdapter
    pages = {
        "first": {"data": [{"created_time": "2026-09-30T14:05:42+0000", "recommendation_type": "positive", "review_text": "one",
                            "reviewer": {"name": "A", "id": "1"}, "open_graph_story": {"id": "s1"}}],
                  "paging": {"next": "https://graph.facebook.com/v21.0/123/ratings?access_token=SECRET&fields=a%2Cb&limit=100&after=CUR2"}},
        "CUR2": {"data": [{"created_time": "2026-09-29T14:05:42+0000", "recommendation_type": "negative", "review_text": "two",
                           "reviewer": {"name": "B", "id": "2"}, "open_graph_story": {"id": "s2"}}],
                 # a buggy cursor that points back at itself must not loop forever
                 "paging": {"next": "https://graph.facebook.com/v21.0/123/ratings?fields=a%2Cb&limit=100&after=CUR2&access_token=SECRET"}},
    }
    calls = []

    class FB(FacebookPageAdapter):
        def _page_token(self, page_id):
            return "PAGE"

        def _request(self, method, url, *, params=None, data=None, token=None, retries=3):
            calls.append((url, params))
            if url.endswith("/123"):
                return {"overall_star_rating": 4.5, "rating_count": 2}
            assert "access_token" not in url
            if "after=CUR2" in url:
                assert "fields=a%2Cb" in url and "limit=100" in url
                return pages["CUR2"]
            return pages["first"]

    link = ReviewSourceLink(source="facebook", external_location_id="123")
    got = [nr.external_id for nr, _ in FB(token="x").fetch_reviews(link)]
    assert got == ["s1", "s2"] and len(calls) == 3


def test_empty_site_scope_matches_nothing(db):
    """A group with no sites (or no overlap with the chosen site) must not fall back to every site."""
    from app.daterange import resolve_range
    from app.reports import rating_distribution, window_report
    from app.web import _scope
    from app.models import SiteGroup
    s, link = db
    sync_link(s, link, full=True, adapter=FakeAdapter([nr("a", 5, "great", 1), nr("b", 1, "bad", 2)])); s.commit()
    empty = SiteGroup(name="Nobody yet"); s.add(empty); s.commit()
    dr = resolve_range("last30")
    ids = _scope(s, [empty.id], [])
    assert ids == []
    assert window_report(s, dr, location_ids=ids).count == 0
    assert window_report(s, dr, location_ids=None).count == 2
    assert rating_distribution(s, dr, location_ids=[])["rows"] == []
    assert _scope(s, [empty.id], [link.location_id]) == []
