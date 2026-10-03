"""Mention detection, template suggestion, saved views, archive, employee report, AI draft (mocked)."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app import auth
from app.db import SessionLocal, engine, init_db
from app.models import (AiRule, Base, Employee, Location, ReplyTemplate, Response, Review, ReviewMention,
                        ReviewSourceLink, SavedView, SiteGroup, User)
from app.reports import employee_report, monthly_summary, responder_stats
from app.text_intel import apply_intel, build_roster, classify_theme, detect_mentions, suggest_templates


@pytest.fixture()
def db():
    Base.metadata.drop_all(engine)
    init_db()
    s = SessionLocal()
    loc = Location(name="ICON Thompson Lane", brand="ICON", state="TN", city="Nashville")
    s.add(loc); s.flush()
    link = ReviewSourceLink(location_id=loc.id, source="google", external_account_id="1", external_location_id="L1", display_name="Icon Car Wash", active=True)
    s.add(link)
    s.add(Employee(name="Jared", location_id=loc.id))
    s.add(Employee(name="Gregory Banks", aliases="Mr. Banks;Greg", location_id=loc.id))
    s.add(User(email="lily@x.com", name="Lily Collins", password_hash=auth.hash_password("dev-password-lily-2026"), role="admin"))
    s.commit()
    yield s, loc, link
    s.close()


def mk(s, link, ext, rating, text, author="Pat Q", days_ago=1.0, replied=False):
    t = datetime.utcnow() - timedelta(days=days_ago)
    r = Review(source_link_id=link.id, source="google", external_id=ext, author_name=author, rating=rating, text=text,
               created_at_source=t, updated_at_source=t, has_owner_reply=replied,
               owner_reply_text="thanks" if replied else None, owner_reply_updated_at=t + timedelta(hours=20) if replied else None, raw_json="{}")
    s.add(r); s.flush()
    return r


def test_detect_mentions_cues_and_roster():
    roster = {"jared": "Jared", "gregory banks": "Gregory Banks", "mr. banks": "Gregory Banks", "greg": "Gregory Banks"}
    assert detect_mentions("Jared is the man fast Service and great attitude", "Eric Crain", roster) == [("Jared", "Jared")]
    assert detect_mentions("Today I had great help from Mr. Banks, he explained everything.", "Chin Suk Kim", roster) == [("Gregory Banks", "Gregory Banks")]
    found = detect_mentions("shout out to Karla for being informative on the great deals", "Isabel Benitez", roster)
    assert found == [("Karla", None)]
    # author's own name and ordinary words are not mentions
    assert detect_mentions("Great wash great staff. Isabel was here Tuesday.", "Isabel Benitez", roster) == []
    assert detect_mentions("The user didn't write a review", None, roster) == []
    assert detect_mentions(None, None, roster) == []
    # two names
    names = [n for n, _ in detect_mentions("Lucien and Damon were super helpful as well.", "Michael McClure", {})]
    assert "Lucien" in names and "Damon" in names


def test_classify_theme_and_suggestions(db):
    s, loc, link = db
    assert classify_theme("Waited 25 minutes in line, one lane open") == "Wait time"
    assert classify_theme("Charged twice and nobody answered the phone") == "Billing / cancellation"
    assert classify_theme("Great wash") is None
    tpls = [
        ReplyTemplate(name="5 general", min_rating=5, max_rating=5, tags="general", body="Thanks {first_name}"),
        ReplyTemplate(name="5 employee", min_rating=5, max_rating=5, tags="employee", body="Thanks for recognising {employee}"),
        ReplyTemplate(name="5 rating only", min_rating=5, max_rating=5, tags="no_comment;general", body="Thanks for the 5 stars {first_name}"),
        ReplyTemplate(name="Sorry wait", min_rating=1, max_rating=3, tags="wait", body="Sorry about the wait at {site}"),
        ReplyTemplate(name="Sorry billing", min_rating=1, max_rating=3, tags="billing", body="Sorry about billing"),
    ]
    for t in tpls:
        s.add(t)
    s.commit()
    r = mk(s, link, "a", 5, "Jared is the man fast Service and great attitude", author="Eric Crain")
    assert apply_intel(s, r, build_roster(s)) == 1
    s.refresh(r)
    ranked = suggest_templates(r, tpls, r.mention_names)
    assert ranked[0][0].name == "5 employee"
    assert ranked[0][0].render(r, "Lily") == "Thanks for recognising Jared"
    r2 = mk(s, link, "b", 5, None, author="Derek")
    assert suggest_templates(r2, tpls, [])[0][0].name == "5 rating only"
    r3 = mk(s, link, "c", 1, "Charged twice this month. Called and nobody answered.", author="Sam K")
    apply_intel(s, r3, build_roster(s))
    assert r3.category == "Billing / cancellation"
    assert suggest_templates(r3, tpls, [])[0][0].name == "Sorry billing"


def test_employee_report_and_monthly(db):
    s, loc, link = db
    roster = build_roster(s)
    for i, text in enumerate(["Jared was awesome", "Thanks to Jared and Eli for the help", "Greg explained the plans well", "Great wash", None]):
        r = mk(s, link, f"m{i}", 5, text, author="Casey L", days_ago=2 + i, replied=True)
        apply_intel(s, r, roster)
    s.commit()
    rep = employee_report(s, days=30)
    names = {row["name"]: row for row in rep["rows"]}
    assert names["Jared"]["count"] == 2 and names["Jared"]["on_roster"]
    assert names["Gregory Banks"]["count"] == 1
    assert names["Eli"]["on_roster"] is False
    assert rep["reviews_with_mentions"] == 3 and rep["reviews_with_text"] == 4
    months = monthly_summary(s, months=3)
    assert months and months[0]["n"] >= 1 and months[0]["response_rate"] == 100


def test_web_views_archive_draft_and_admin(db, monkeypatch):
    from fastapi.testclient import TestClient
    from app.web import app
    import app.web as web
    s, loc, link = db
    u = s.execute(select(User)).scalar_one()
    r = mk(s, link, "w1", 1, "Line wrapped around the building", author="Taylor R")
    r_ok = mk(s, link, "w2", 5, None, author="Derek", days_ago=0.1)
    s.add(ReplyTemplate(name="Sorry wait", min_rating=1, max_rating=3, tags="wait", body="Sorry about the wait at {site}"))
    s.add(AiRule(text="Keep it under 25 words."))
    s.commit()
    c = TestClient(app)
    c.cookies.set(auth.COOKIE_NAME, auth.make_session_cookie(u))
    # saved view round trip
    resp = c.post("/views", data={"name": "LW Negatives", "is_shared": "1", "params": '{"view":"negative","days":7}'}, follow_redirects=False)
    assert resp.status_code == 303 and "sv=" in resp.headers["location"]
    page = c.get(resp.headers["location"])
    assert page.status_code == 200 and "LW Negatives" in page.text and "Line wrapped" in page.text
    # archive removes from unanswered and from counts
    before = c.get("/?view=unanswered").text.count("Rating only, no written review")
    c.post(f"/reviews/{r_ok.id}/archive", data={"back": ""}, follow_redirects=False)
    assert c.get("/?view=unanswered").text.count("Rating only, no written review") == before - 1
    assert "Archived" in c.get(f"/reviews/{r_ok.id}").text
    # draft endpoint: disabled without a key, works with a mocked model
    assert c.post(f"/reviews/{r.id}/draft").status_code == 503
    monkeypatch.setattr(web, "draft_reply", lambda review, rules, examples, agent: "We're sorry about the line, Taylor. Call (615) 776-7837.")
    monkeypatch.setattr(web.settings, "anthropic_api_key", "test-key")
    j = c.post(f"/reviews/{r.id}/draft").json()
    assert j["ok"] and "Taylor" in j["text"]
    # template usage increments on post (fake adapter)
    monkeypatch.setattr(web, "get_adapter", lambda src: type("A", (), {"post_reply": lambda self, l, e, t: datetime.utcnow()})())
    tpl = s.execute(select(ReplyTemplate)).scalar_one()
    c.post(f"/reviews/{r.id}/reply", data={"text": "Sorry about the wait", "template_id": str(tpl.id), "ai_generated": "0"}, follow_redirects=False)
    s.expire_all()
    assert s.get(ReplyTemplate, tpl.id).usage_count == 1
    assert responder_stats(s, days=1)[0]["count"] == 1
    # admin pages + employee promote + group
    for path in ["/admin/employees", "/admin/groups", "/admin/ai", "/admin/templates", "/reports", "/reports/employees", f"/sites/{loc.id}"]:
        assert c.get(path).status_code == 200, path
    c.post("/admin/employees", data={"name": "Eli", "location_id": str(loc.id), "aliases": "", "role": "", "active": "1"}, follow_redirects=False)
    c.post("/admin/groups", data={"name": "Nashville", "description": "", "location_ids": [str(loc.id)]}, follow_redirects=False)
    s.expire_all()
    assert s.execute(select(Employee).where(Employee.name == "Eli")).scalar_one_or_none() is not None
    g = s.execute(select(SiteGroup)).scalar_one()
    assert [l.id for l in g.locations] == [loc.id]
    assert c.get(f"/?group_id={g.id}&view=all").status_code == 200


def test_inbox_server_sort(db):
    from fastapi.testclient import TestClient
    from app.web import app
    s, loc, link = db
    u = s.execute(select(User)).scalar_one()
    mk(s, link, "s1", 5, "Alpha great", author="Zed A", days_ago=3)
    mk(s, link, "s2", 1, "Beta awful", author="Amy B", days_ago=1)
    mk(s, link, "s3", 3, "Gamma meh", author="Mel C", days_ago=2)
    s.commit()
    c = TestClient(app)
    c.cookies.set(auth.COOKIE_NAME, auth.make_session_cookie(u))
    html = c.get("/?view=all&sort=rating&dir=asc").text
    assert html.index("Beta awful") < html.index("Gamma meh") < html.index("Alpha great")
    html = c.get("/?view=all&sort=rating&dir=desc").text
    assert html.index("Alpha great") < html.index("Gamma meh") < html.index("Beta awful")
    html = c.get("/?view=all&sort=author&dir=asc").text
    assert html.index("Amy B") < html.index("Mel C") < html.index("Zed A")
    assert "sorted by author" in html and 'data-server-sort' in html
    assert c.get("/?view=all&sort=bogus").status_code == 200
