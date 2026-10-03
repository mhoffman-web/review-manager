"""Exercises the sync upsert logic and the report builder with a fake adapter.
No network, in-memory SQLite."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.db import SessionLocal, engine, init_db
from app.models import Base, Location, ReplyTemplate, Review, ReviewSourceLink, User
from app.reports import build_report, build_trends, render_report_html, render_report_text, site_summary
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

    # Review c disappears from Google: a full pull marks it deleted, incremental does not.
    fake.reviews = fake.reviews[:2]
    sync_link(s, link, adapter=fake); s.commit()
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
    # Brand filter that matches nothing yields an empty report, not an error.
    assert build_report(s, brands=["ICON"]).new_24h == 0


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
    a.owner_reply_updated_at = a.created_at_source + timedelta(hours=6)
    d = s.execute(select(Review).where(Review.external_id == "d")).scalar_one()
    d.owner_reply_updated_at = d.created_at_source + timedelta(hours=30)
    d.category = "Wait time"; d.rating = 2
    s.commit()
    rep = build_report(s)
    assert rep.median_response_h_30d == 18.0 and rep.response_rate_30d == 67
    assert rep.negative_themes_30d == [("Wait time", 1)]
    t = build_trends(s, weeks=4)
    assert t["brands"] == ["WashU"] and len(t["labels"]) == 4 and sum(t["counts"]["WashU"]) == 3
    assert t["rating_dist_30d"] == [1, 1, 0, 0, 1]
    loc = s.execute(select(Location)).scalar_one()
    summ = site_summary(s, loc, days=30)
    assert summ["count"] == 3 and summ["unanswered"] == 1 and summ["themes"] == [("Wait time", 1)]
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
