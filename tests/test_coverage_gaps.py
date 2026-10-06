"""Behaviour that had no test before the October review: worker scheduling, session renewal,
reply removal, the sync trigger, user admin, the distribution export, the duplicate-reply
guardrail, recovery stats, API filters, Google paging, the CLI, guardrail redirects, and an
optional run against a real Postgres (set TEST_POSTGRES_URL)."""
import io
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import auth
from app.db import SessionLocal, engine, init_db
from app.models import (ApiKey, Base, Location, ReportRecipient, Response, Review, ReviewEvent, ReviewSourceLink,
                        SyncRun, User)
from app.web import app

PW = "dev-password-admin-2026"


@pytest.fixture()
def env():
    Base.metadata.drop_all(engine)
    init_db()
    s = SessionLocal()
    il = Location(name="WashU Berwyn", brand="WashU", state="IL", city="Berwyn")
    tn = Location(name="ICON Thompson Lane", brand="ICON", state="TN", city="Nashville")
    s.add_all([il, tn]); s.flush()
    l_il = ReviewSourceLink(location_id=il.id, source="google", external_account_id="1", external_location_id="L1", display_name="WashU Berwyn")
    l_tn = ReviewSourceLink(location_id=tn.id, source="google", external_account_id="1", external_location_id="L2", display_name="ICON Thompson Lane")
    admin = User(email="admin@x.com", name="Ada Admin", password_hash=auth.hash_password(PW), role="admin")
    s.add_all([l_il, l_tn, admin]); s.commit()
    c = TestClient(app)
    c.post("/login", data={"email": "admin@x.com", "password": PW})
    yield s, c, (il, tn), (l_il, l_tn), admin
    s.close()


def review(s, link, ext, rating, text="", days_ago=1.0, replied=False):
    t = datetime.utcnow() - timedelta(days=days_ago)
    r = Review(source_link_id=link.id, source="google", external_id=ext, author_name="Pat Q", rating=rating, text=text,
               created_at_source=t, updated_at_source=t, has_owner_reply=replied, owner_reply_text="thanks" if replied else None,
               owner_reply_updated_at=t + timedelta(hours=2) if replied else None, first_replied_at=t + timedelta(hours=2) if replied else None,
               raw_json="{}")
    s.add(r); s.commit()
    return r


def test_worker_full_pull_and_report_schedule(env, monkeypatch):
    from app import worker
    s, *_ = env
    tz = ZoneInfo("America/Chicago")
    early, late = datetime(2026, 10, 5, 2, 0, tzinfo=tz), datetime(2026, 10, 5, 4, 0, tzinfo=tz)
    assert worker.needs_full_pull(s, early) is False                 # before FULL_SYNC_HOUR_LOCAL
    assert worker.needs_full_pull(s, late) is True                   # never had one
    s.add(SyncRun(full=True, status="ok", started_at=datetime(2026, 10, 5, 9, 30)))   # 04:30 Central today
    s.commit()
    assert worker.needs_full_pull(s, late.replace(hour=23)) is False # already done today
    assert worker.needs_full_pull(s, late + timedelta(days=1)) is True
    # tick: syncs, then sends only the editions still pending
    calls = []
    monkeypatch.setattr(worker, "sync_all", lambda sess, full=False: calls.append(("sync", full)) or {})
    monkeypatch.setattr(worker, "pending_editions", lambda sess, d=None: ["tn"])
    monkeypatch.setattr(worker, "missed_report_date", lambda sess, d=None: None)
    monkeypatch.setattr(worker, "send_morning_report", lambda sess, **kw: calls.append(("report", kw.get("editions"))))
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 6, 8, 0, tzinfo=tz)
    monkeypatch.setattr(worker, "datetime", Clock)
    worker.tick()
    assert ("report", ["tn"]) in calls and calls[0][0] == "sync"


def test_session_renewal_keeps_the_original_sign_in_time(env):
    """A cookie re-issued by the sliding-session middleware keeps the original sign-in time,
    so the absolute lifetime cannot be extended by activity."""
    import time
    from itsdangerous import TimestampSigner, URLSafeTimedSerializer
    s, c, _, _, admin = env
    iat = int(time.time()) - 3 * 86400

    class Earlier(TimestampSigner):                 # sign as if 90 minutes ago (past RENEW_AFTER)
        def get_timestamp(self):
            return int(time.time()) - 5400
    old = URLSafeTimedSerializer(auth.settings.secret_key, salt="review-manager-session", signer=Earlier).dumps({"uid": admin.id, "iat": iat})
    c.cookies.clear(); c.cookies.set(auth.COOKIE_NAME, old)
    r = c.get("/")
    assert r.status_code == 200
    renewed = r.cookies.get(auth.COOKIE_NAME)
    assert renewed and auth._serializer.loads(renewed)["iat"] == iat


def test_delete_reply_sync_trigger_and_user_admin(env, monkeypatch):
    import app.web as web
    s, c, _, (l_il, _l), admin = env
    r = review(s, l_il, "d1", 4, "nice", replied=True)
    deleted = []
    monkeypatch.setattr(web, "get_adapter", lambda src: type("A", (), {"delete_reply": lambda self, link, ext: deleted.append(ext)})())
    c.post(f"/reviews/{r.id}/reply/delete", follow_redirects=False)
    s.expire_all()
    r = s.get(Review, r.id)
    assert deleted == ["d1"] and not r.has_owner_reply and [e.kind for e in r.events][-1] == "reply_deleted"
    ran = []
    monkeypatch.setattr(web, "_background_sync", lambda full: ran.append(full))
    assert c.post("/sync", data={"full": "1"}, follow_redirects=False).status_code == 303 and ran == [True]
    # user admin: create, toggle off, reset link only for active users, never toggle yourself
    c.post("/admin/users", data={"email": "sam@x.com", "name": "Sam", "role": "agent"})
    sam = s.execute(select(User).where(User.email == "sam@x.com")).scalar_one()
    c.post(f"/admin/users/{sam.id}/toggle"); s.expire_all()
    assert s.get(User, sam.id).active is False
    c.post(f"/admin/users/{admin.id}/toggle"); s.expire_all()
    assert s.get(User, admin.id).active is True
    assert "not+found" in c.post(f"/admin/users/{sam.id}/reset-link", follow_redirects=False).headers["location"]


def test_distribution_export_duplicate_rule_and_recovery(env):
    from app.daterange import resolve_range
    from app.events import record
    from app.reports import recovery_stats
    from app.web import _reply_warnings
    s, c, (il, tn), (l_il, l_tn), admin = env
    a = review(s, l_il, "x1", 5, "great", days_ago=2)
    b = review(s, l_tn, "x2", 1, "bad", days_ago=2)
    csv = c.get("/export/distribution.csv?range=last30").text
    assert "WashU Berwyn" in csv and "ICON Thompson Lane" in csv
    assert c.get("/export/distribution.xlsx?range=last30").headers["content-type"].startswith("application/vnd.openxml")
    # the same reply posted twice today at a site: the third time warns
    now = datetime.utcnow()
    for ext in ("y1", "y2"):
        rr = review(s, l_il, ext, 5, "ok")
        s.add(Response(review_id=rr.id, text="Thanks so much for visiting!", status="posted", posted_at=now, created_by_id=admin.id))
    s.commit()
    third = review(s, l_il, "y3", 5, "ok")
    assert any("already posted 2 times" in w for w in _reply_warnings(s, third, "Thanks so much  for visiting!"))
    assert not any("already posted" in w for w in _reply_warnings(s, b, "Thanks so much for visiting!"))   # other site
    # recovery: a rating raised after our reply counts; one raised before any reply does not
    b.first_replied_at = now - timedelta(hours=5)
    s.add(Response(review_id=b.id, text="Sorry!", status="posted", posted_at=now - timedelta(hours=5), created_by_id=admin.id))
    record(s, b, "rating_changed", at=now, **{"from": 1, "to": 4})
    record(s, a, "rating_changed", at=now, **{"from": 4, "to": 5})
    s.commit()
    rec = recovery_stats(s, resolve_range("last7"))
    assert rec["improved"] == 2 and rec["recovered"] == 1 and rec["by_responder"]["Ada Admin"] == 1
    assert recovery_stats(s, resolve_range("last7"), brands=["WashU"])["recovered"] == 0


def test_api_filters(env):
    from app.api import hash_key, new_key
    s, c, (il, tn), (l_il, l_tn), _ = env
    review(s, l_il, "a1", 5, "great"); review(s, l_tn, "a2", 1, "bad"); review(s, l_tn, "a3", 2, "meh", days_ago=40)
    raw = new_key(); s.add(ApiKey(name="t", prefix=raw[:11], key_hash=hash_key(raw))); s.commit()
    h = {"X-API-Key": raw}
    api = TestClient(app)
    assert len(api.get("/api/v1/reviews?range=last7", headers=h).json()["reviews"]) == 2
    assert [r["rating"] for r in api.get("/api/v1/reviews?range=last7&brand=ICON", headers=h).json()["reviews"]] == [1]
    assert len(api.get(f"/api/v1/reviews?range=last7&location_id={il.id}", headers=h).json()["reviews"]) == 1
    assert len(api.get("/api/v1/reviews?range=last7&rating=1&rating=2", headers=h).json()["reviews"]) == 1
    assert len(api.get("/api/v1/reviews?range=last12m", headers=h).json()["reviews"]) == 3
    summ = api.get("/api/v1/summary?range=last7&brand=WashU", headers=h).json()
    assert summ["totals"]["reviews"] == 1 and [x["brand"] for x in summ["sites"]] == ["WashU"]


def test_google_adapter_follows_page_tokens_and_stops_at_since():
    from app.sources.google import GoogleBusinessProfileAdapter
    pages = {None: {"averageRating": 4.5, "totalReviewCount": 3, "nextPageToken": "p2",
                    "reviews": [{"reviewId": "r1", "starRating": "FIVE", "createTime": "2026-10-03T10:00:00Z", "updateTime": "2026-10-03T10:00:00Z"}]},
             "p2": {"nextPageToken": "p3", "reviews": [{"reviewId": "r2", "starRating": "ONE", "createTime": "2026-10-02T10:00:00Z", "updateTime": "2026-10-02T10:00:00Z"}]},
             "p3": {"reviews": [{"reviewId": "r3", "starRating": "TWO", "createTime": "2026-09-01T10:00:00Z", "updateTime": "2026-09-01T10:00:00Z"}]}}
    seen = []

    class G(GoogleBusinessProfileAdapter):
        def _request(self, method, url, params=None, json_body=None, **kw):
            seen.append(params.get("pageToken"))
            return pages[params.get("pageToken")]
    link = ReviewSourceLink(source="google", external_account_id="1", external_location_id="L1")
    out = list(G().fetch_reviews(link))
    assert [nr.external_id for nr, _ in out] == ["r1", "r2", "r3"] and seen == [None, "p2", "p3"]
    assert out[0][1].total_review_count == 3 and out[1][1] is None
    seen.clear()
    assert [nr.external_id for nr, _ in G().fetch_reviews(link, since=datetime(2026, 9, 15))] == ["r1", "r2"] and seen == [None, "p2", "p3"]


def test_cli_commands(env, monkeypatch, tmp_path, capsys):
    import cli
    s, *_ = env
    def run(*args):
        monkeypatch.setattr(sys, "argv", ["cli.py", *args])
        cli.main()
        return capsys.readouterr().out
    run("init-db")
    assert "gets: il, alerts" in run("add-recipient", "--email", "ops@x.com", "--edition", "il", "--edition", "alerts")
    out = run("send-report", "--dry-run")
    assert "dry-run" in out and "ops@x.com" in out
    s.expire_all()
    assert s.execute(select(ReportRecipient).where(ReportRecipient.email == "ops@x.com")).scalar_one().editions == ["il", "alerts"]
    out = run("backup", "--out", str(tmp_path / "b.json.gz"))
    assert "wrote" in out and (tmp_path / "b.json.gz").exists()
    with pytest.raises(SystemExit):
        run("restore", str(tmp_path / "b.json.gz"))                    # needs --yes


def test_guardrail_warnings_redirect_instead_of_rerendering(env, monkeypatch):
    s, c, _, (l_il, _l), _ = env
    r = review(s, l_il, "g1", 5, "great", )
    resp = c.post(f"/reviews/{r.id}/reply", data={"text": "Hi {first_name}, thanks!", "ctx": "view=all"}, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].startswith(f"/reviews/{r.id}?check=1")
    s.expire_all()
    assert not s.get(Review, r.id).has_owner_reply


@pytest.mark.skipif(not os.getenv("TEST_POSTGRES_URL"), reason="set TEST_POSTGRES_URL=postgresql://... to run against a real Postgres")
def test_against_real_postgres(tmp_path):
    """Schema creation, the additive migration, the backfill and a backup round trip on Postgres.
    Point it at a throwaway database: it drops every table first."""
    from sqlalchemy import create_engine, text
    from app import backup, db as dbmod
    from app.config import normalize_database_url
    pg = create_engine(normalize_database_url(os.environ["TEST_POSTGRES_URL"]), future=True)
    monkey_engine = dbmod.engine
    try:
        dbmod.engine = pg; backup.engine = pg
        Base.metadata.drop_all(pg)
        dbmod.init_db(); dbmod.init_db()                                # twice: idempotent
        with pg.begin() as conn:
            conn.execute(text("ALTER TABLE reviews DROP COLUMN author_is_anonymous"))
        dbmod.init_db()
        with pg.connect() as conn:
            default = conn.execute(text("SELECT column_default FROM information_schema.columns WHERE table_name='reviews' AND column_name='author_is_anonymous'")).scalar()
        assert default == "false"
        path = tmp_path / "pg.json.gz"
        backup.dump(path)
        Base.metadata.drop_all(pg)
        backup.restore(path)
    finally:
        dbmod.engine = monkey_engine; backup.engine = monkey_engine


def test_init_db_never_prints_the_database_password(monkeypatch, capsys):
    import cli
    from app.config import settings as st
    monkeypatch.setattr(st, "database_url", "postgresql+psycopg://user:s3cret-pw@db.example:5432/rm")
    monkeypatch.setattr(cli, "init_db", lambda: None)
    monkeypatch.setattr(sys, "argv", ["cli.py", "init-db"])
    cli.main()
    out = capsys.readouterr().out
    assert "s3cret-pw" not in out and "db.example" in out


def test_seed_content_is_complete_and_idempotent(env):
    from app.models import AiRule, ReplyTemplate, SavedView, SiteGroup
    from app.starter_content import TEMPLATES, seed_content
    s, *_ = env
    first = seed_content(s); s.commit()
    assert first["templates"] == len(TEMPLATES) == 21 and first["ai_rules"] == 5 and first["views"] == 3
    assert {g.name for g in s.query(SiteGroup)} >= {"IL – WashU"}             # groups only where the sites exist
    again = seed_content(s); s.commit()
    assert sum(again.values()) == 0
    assert s.query(ReplyTemplate).count() == 21 and s.query(AiRule).count() == 5 and s.query(SavedView).count() == 3


def test_templates_use_the_brands_contact_email(env):
    from app.models import ReplyTemplate
    from app.starter_content import TEMPLATES, seed_content
    s, c, (il, tn), (l_il, l_tn), _ = env
    tpl = ReplyTemplate(name="t", body="Write to {email} or call {phone}.", min_rating=1, max_rating=3)
    r_tn = review(s, l_tn, "e1", 1, "bad"); r_il = review(s, l_il, "e2", 1, "bad")
    assert "info@iconcarwash.com" in tpl.render(r_tn) and "info@washucarwash.com" in tpl.render(r_il)
    assert not any("support@" in body for *_x, body, _o in [(t[0], t[5], t[6]) for t in TEMPLATES])
    # a starter template still holding the old single address is updated; an edited one is not
    seed_content(s); s.commit()
    billing = s.query(ReplyTemplate).filter_by(name="Sorry – Billing").one()
    damage = s.query(ReplyTemplate).filter_by(name="Sorry – Damage claim").one()
    from app.starter_content import _PREVIOUS_V2
    billing.body = _PREVIOUS_V2["Sorry – Billing"]                     # the earlier long wording, never edited
    damage.body = "Our own wording, support@washucarwash.com"
    s.commit()
    out = seed_content(s); s.commit()
    assert out["templates_updated"] == 1 and "{email}" in billing.body and damage.body == "Our own wording, support@washucarwash.com"


def test_starter_templates_are_short_and_make_no_promises():
    """House rule: under 25 words. And no commitments the business has not agreed to."""
    import re
    from app.starter_content import TEMPLATES
    for name, _b, _l, _h, _t, body, _o in TEMPLATES:
        filled = (body.replace("{first_name}", "Jordan").replace("{site}", "WashU Evergreen Park")
                  .replace("{employee}", "Karla and Jared").replace("{email}", "info@washucarwash.com"))
        assert len(filled.split()) <= 25, (name, len(filled.split()))
        assert not re.search(r"rewash|refund|credit|free (wash|month)|on us|within .* (day|hour)|guarantee", body, re.I), name


def test_source_and_action_tags(env):
    from app.models import ReviewEvent, ReviewTag
    from app.starter_content import seed_content
    s, c, (il, tn), (l_il, l_tn), admin = env
    seed_content(s); s.commit()
    tags = {(t.kind, t.name): t for t in s.query(ReviewTag)}
    assert {n for k, n in tags if k == "source"} == {"Site", "Corporate", "Text"}
    assert {n for k, n in tags if k == "action"} == {"Follow Up", "In Process", "Resolved"}
    a = review(s, l_il, "t1", 1, "bad wash"); b = review(s, l_tn, "t2", 5, "great"); review(s, l_il, "t3", 4, "ok")
    site, follow = tags[("source", "Site")], tags[("action", "Follow Up")]
    # set both on a; source only on b
    r = c.post(f"/reviews/{a.id}/tags", data={"source_tag_id": site.id, "action_tag_id": follow.id}, follow_redirects=False)
    assert r.status_code == 303 and "Source+and+Action+saved" in r.headers["location"]
    c.post(f"/reviews/{b.id}/tags", data={"source_tag_id": site.id, "action_tag_id": 0})
    s.expire_all()
    a = s.get(Review, a.id)
    assert a.source_tag.name == "Site" and a.action_tag.name == "Follow Up" and a.action_set_by_id == admin.id
    assert [(e.kind, e.detail["to"]) for e in a.events if e.kind.endswith("_set")] == [("source_set", "Site"), ("action_set", "Follow Up")]
    # a tag of the wrong list is refused
    assert c.post(f"/reviews/{a.id}/tags", data={"source_tag_id": follow.id}).status_code == 400
    # inbox filters: by action, by "none set", by source; export carries both columns
    page = c.get(f"/?view=all&action={follow.id}").text
    assert "bad wash" in page and "great" not in page and "via Site" in page
    assert "bad wash" not in c.get("/?view=all&action=-1").text
    assert "1 review" not in c.get(f"/?view=all&source={site.id}").text          # 2 reviews have Site
    csv = c.get(f"/export/reviews.csv?view=all&action={follow.id}").text
    assert "Platform" in csv.splitlines()[0] and ",Site,Follow Up," in csv
    # admin: add, rename, retire; retired options stay on reviews but leave the picker
    c.post("/admin/tags", data={"kind": "action", "name": "Escalated", "color": "bad"})
    esc = s.query(ReviewTag).filter_by(kind="action", name="Escalated").one()
    def order():
        s.expire_all()
        return [t.name for t in s.query(ReviewTag).filter_by(kind="action").order_by(ReviewTag.sort_order, ReviewTag.name)]
    assert order() == ["Follow Up", "In Process", "Resolved", "Escalated"]          # new options go last
    c.post(f"/admin/tags/{esc.id}/move", data={"direction": "up"})
    assert order() == ["Follow Up", "In Process", "Escalated", "Resolved"]
    c.post(f"/admin/tags/{follow.id}/move", data={"direction": "up"})              # already first: no change
    c.post(f"/admin/tags/{follow.id}/move", data={"direction": "down"})
    assert order() == ["In Process", "Follow Up", "Escalated", "Resolved"]
    assert c.post("/admin/tags", data={"kind": "action", "name": "follow up"}, follow_redirects=False).headers["location"].count("already+exists") == 1
    c.post(f"/admin/tags/{follow.id}/toggle"); s.expire_all()
    assert s.get(ReviewTag, follow.id).active is False
    review_page = c.get(f"/reviews/{a.id}").text
    assert "Follow Up (retired)" in review_page and f'value="{esc.id}"' in review_page
    assert "Escalated" in c.get("/admin/tags").text
    # API exposes both tags
    from app.api import hash_key, new_key
    raw = new_key(); s.add(ApiKey(name="t", prefix=raw[:11], key_hash=hash_key(raw))); s.commit()
    rows = TestClient(app).get("/api/v1/reviews?range=last7", headers={"X-API-Key": raw}).json()["reviews"]
    got = {x["reviewer"] + x["text"]: (x["source_tag"], x["action"]) for x in rows}
    assert ("Site", "Follow Up") in got.values() and ("Site", None) in got.values()


def test_starter_tags_appear_on_a_fresh_database(env):
    from app.models import ReviewTag
    s, *_ = env                         # env runs init_db on an empty database
    assert s.query(ReviewTag).count() == 6
    t = s.query(ReviewTag).filter_by(name="Text").one(); t.active = False
    s.query(ReviewTag).filter_by(name="Resolved").delete(); s.commit()
    from app.db import init_db
    init_db(); s.expire_all()
    assert s.query(ReviewTag).count() == 5 and s.query(ReviewTag).filter_by(name="Text").one().active is False
