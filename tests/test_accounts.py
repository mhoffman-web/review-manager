"""Welcome email on user creation, set/reset password links, change password, forgot flow."""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import auth
from app.db import SessionLocal, engine, init_db
from app.models import Base, User
from app.web import app


@pytest.fixture()
def db():
    Base.metadata.drop_all(engine)
    init_db()
    s = SessionLocal()
    s.add(User(email="admin@x.com", name="Mitch Hoffman", password_hash=auth.hash_password("dev-password-admin-2026"), role="admin"))
    s.commit()
    yield s
    s.close()


def admin_client(s):
    c = TestClient(app)
    u = s.execute(select(User).where(User.email == "admin@x.com")).scalar_one()
    c.cookies.set(auth.COOKIE_NAME, auth.make_session_cookie(u))
    return c


def test_welcome_link_and_set_password(db, monkeypatch):
    import app.account_mail as am
    sent = []
    monkeypatch.setattr(am.settings, "smtp_user", "mailer@x.com")
    monkeypatch.setattr(am.settings, "smtp_password", "pw")
    monkeypatch.setattr("app.mailer.send_email", lambda to, subject, html, text: sent.append((to, subject, html, text)))
    c = admin_client(db)
    r = c.post("/admin/users", data={"email": "New.Person@x.com", "name": "New Person", "password": "", "role": "agent"}, follow_redirects=False)
    assert r.status_code == 303 and "Welcome+email+sent" in r.headers["location"]
    assert sent and sent[0][0] == ["new.person@x.com"] and "Set your password" in sent[0][2] and "/password/reset?token=" in sent[0][3]
    # follow the link from the email: the form knows who it is for, saves the password and signs them in
    link = sent[0][3].split("link valid 48 hours): ")[1].split()[0]
    token = link.split("token=")[1]
    anon = TestClient(app)
    page = anon.get(f"/password/reset?token={token}")
    assert page.status_code == 200 and "new.person@x.com" in page.text and "Welcome" in page.text
    bad = anon.post("/password/reset", data={"token": token, "password": "short", "confirm": "short"})
    assert "at least 10 characters" in bad.text
    ok = anon.post("/password/reset", data={"token": token, "password": "a-good-long-password", "confirm": "a-good-long-password"}, follow_redirects=False)
    assert ok.status_code == 303 and auth.COOKIE_NAME in ok.headers.get("set-cookie", "")
    db.expire_all()
    u = db.execute(select(User).where(User.email == "new.person@x.com")).scalar_one()
    assert auth.verify_password(u.password_hash and "a-good-long-password", u.password_hash)
    # the link is single-use: the hash changed, so the same token is now invalid
    assert "not valid any more" in anon.get(f"/password/reset?token={token}").text
    # and the new password signs in
    assert anon.post("/login", data={"email": "new.person@x.com", "password": "a-good-long-password"}, follow_redirects=False).status_code == 303


def test_link_shown_when_mail_not_configured(db, monkeypatch):
    import app.account_mail as am
    monkeypatch.setattr(am.settings, "smtp_user", None)
    c = admin_client(db)
    r = c.post("/admin/users", data={"email": "lily@x.com", "name": "Lily Collins", "password": "", "role": "agent"}, follow_redirects=False)
    # the link is in the page that answers the POST, never in a redirect URL (logs, history)
    assert r.status_code == 200 and "location" not in r.headers and r.headers["cache-control"] == "no-store"
    assert "share this set-password link" in r.text and "/password/reset?token=" in r.text
    assert "/password/reset?token=" not in c.get("/admin/users").text
    # admin can also hand out a reset link for an existing user
    u = db.execute(select(User).where(User.email == "lily@x.com")).scalar_one()
    r2 = c.post(f"/admin/users/{u.id}/reset-link", follow_redirects=False)
    assert r2.status_code == 200 and "/password/reset?token=" in r2.text


def test_forgot_and_change_password(db, monkeypatch):
    import app.account_mail as am
    monkeypatch.setattr(am.settings, "smtp_user", None)       # link shown on screen in dev
    anon = TestClient(app)
    # unknown address gets the same answer, no link
    r = anon.post("/login/forgot", data={"email": "nobody@x.com"})
    assert "a reset link is on its way" in r.text and "token=" not in r.text
    r = anon.post("/login/forgot", data={"email": "ADMIN@x.com"})
    assert "token=" in r.text
    token = r.text.split("token=")[1].split('"')[0]
    ok = anon.post("/password/reset", data={"token": token, "password": "brand-new-password-1", "confirm": "brand-new-password-1"}, follow_redirects=False)
    assert ok.status_code == 303
    assert anon.post("/login", data={"email": "admin@x.com", "password": "brand-new-password-1"}, follow_redirects=False).status_code == 303
    assert "Wrong email" in anon.post("/login", data={"email": "admin@x.com", "password": "dev-password-admin-2026"}).text
    # change password while signed in: wrong current rejected, right one accepted
    c = admin_client(db)
    assert c.get("/account").status_code == 200
    bad = c.post("/account/password", data={"current": "nope", "password": "another-long-password", "confirm": "another-long-password"}, follow_redirects=False)
    assert "current+password+is+wrong" in bad.headers["location"]
    good = c.post("/account/password", data={"current": "brand-new-password-1", "password": "another-long-password", "confirm": "another-long-password"}, follow_redirects=False)
    assert "Password+saved" in good.headers["location"]
    assert anon.post("/login", data={"email": "admin@x.com", "password": "another-long-password"}, follow_redirects=False).status_code == 303
    # a forged token is refused
    assert "not valid any more" in anon.get("/password/reset?token=abc.def.ghi").text


def test_unsafe_deployments_refuse_to_start(monkeypatch, tmp_path):
    import pytest
    from app.config import DEFAULT_SECRET_KEY, settings as st
    monkeypatch.setattr(st, "app_base_url", "https://reviews.example.com")
    monkeypatch.setattr(st, "secret_key", DEFAULT_SECRET_KEY)
    monkeypatch.setattr(st, "demo_mode", False)
    monkeypatch.setattr(st, "database_url", "sqlite:///./x.db")
    assert any("SECRET_KEY" in p for p in st.fatal_config_problems())
    with pytest.raises(SystemExit):
        st.check_or_exit("the web app")
    monkeypatch.setattr(st, "secret_key", "x" * 48)
    assert st.fatal_config_problems() == []
    # the default key is tolerated only on localhost
    monkeypatch.setattr(st, "app_base_url", "http://localhost:8000")
    monkeypatch.setattr(st, "secret_key", DEFAULT_SECRET_KEY)
    assert st.fatal_config_problems() == []
    # demo mode: never with real credentials, never on Postgres, needs DEMO_PASSWORD in public
    monkeypatch.setattr(st, "app_base_url", "https://demo.example.com")
    monkeypatch.setattr(st, "secret_key", "x" * 48)
    monkeypatch.setattr(st, "demo_mode", True)
    monkeypatch.setattr(st, "google_token_file", str(tmp_path / "none.json"))
    monkeypatch.setattr(st, "google_token_json", None)
    monkeypatch.setattr(st, "facebook_access_token", None)
    monkeypatch.delenv("DEMO_PASSWORD", raising=False)
    assert [p for p in st.fatal_config_problems() if "DEMO_PASSWORD" in p]
    monkeypatch.setenv("DEMO_PASSWORD", "long-random")
    assert st.fatal_config_problems() == []
    monkeypatch.setattr(st, "facebook_access_token", "EAAB...")
    assert any("credentials" in p for p in st.fatal_config_problems())
    monkeypatch.setattr(st, "facebook_access_token", None)
    monkeypatch.setattr(st, "database_url", "postgresql+psycopg://u@h/db")
    assert any("non-SQLite" in p for p in st.fatal_config_problems())


def test_login_throttle_resists_spoofing_and_lockout(monkeypatch):
    from starlette.requests import Request
    from app import auth
    from app.config import settings as st
    monkeypatch.setattr(auth, "_failures", {})
    monkeypatch.setattr(st, "login_max_attempts", 5)

    def req(xff=None, peer="10.0.0.9"):
        headers = [(b"x-forwarded-for", xff.encode())] if xff else []
        return Request({"type": "http", "headers": headers, "client": (peer, 1234)})

    # behind one trusted proxy, only the address the proxy appended counts
    monkeypatch.setattr(st, "trusted_proxy_hops", 1)
    assert auth.client_ip(req("1.2.3.4, 203.0.113.7")) == "203.0.113.7"
    assert auth.client_ip(req("spoofed-1, 203.0.113.7")) == auth.client_ip(req("spoofed-2, 203.0.113.7"))
    monkeypatch.setattr(st, "trusted_proxy_hops", 0)
    assert auth.client_ip(req("1.2.3.4")) == "10.0.0.9"

    # one attacker machine: blocked after 5 guesses at an account, whatever header it sends
    attacker = auth.login_keys("lily@x.com", "203.0.113.7")
    for _ in range(5):
        auth.note_login_failure(*attacker)
    assert auth.login_blocked(*attacker) > 0
    # the real user on another address is NOT locked out by those 5
    assert auth.login_blocked(*auth.login_keys("Lily@x.com", "198.51.100.2")) == 0
    # a distributed attack on the account still hits the per-account cap (4x)
    for i in range(15):
        auth.note_login_failure(*auth.login_keys("lily@x.com", f"192.0.2.{i}"))
    assert auth.login_blocked(*auth.login_keys("lily@x.com", "198.51.100.2")) > 0
    # a correct guess does not wipe the per-account count; a reset link does
    auth.clear_login_failures(*auth.login_keys("lily@x.com", "192.0.2.1"))
    assert auth.login_blocked(*auth.login_keys("lily@x.com", "198.51.100.2")) > 0
    auth.clear_login_failures("e:lily@x.com", include_account=True)
    assert auth.login_blocked(*auth.login_keys("lily@x.com", "198.51.100.2")) == 0

    # expired entries are pruned
    monkeypatch.setattr(auth, "_last_prune", 0.0)
    old = auth._time.time() - auth.LOGIN_WINDOW - 5
    auth._failures["ep:stale|x"] = [old]
    auth._prune(auth._time.time())
    assert "ep:stale|x" not in auth._failures


def test_validation_gaps_and_error_pages(monkeypatch):
    import json, time
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from app import auth
    from app.db import SessionLocal, engine, init_db
    from app.models import Base, ReplyTemplate, SavedView, User
    from app.web import app
    Base.metadata.drop_all(engine); init_db()
    s = SessionLocal()
    admin = User(email="admin@x.com", name="   ", password_hash=auth.hash_password("dev-password-admin-2026"), role="admin")
    s.add(admin); s.commit()
    assert admin.first_name == "admin"                       # blank name no longer crashes .split()[0]
    # String(n) columns are truncated on write, so Postgres never 500s on a long value
    t = ReplyTemplate(name="x" * 500, body="hi"); s.add(t); s.commit()
    assert len(s.get(ReplyTemplate, t.id).name) == ReplyTemplate.__table__.c.name.type.length
    # bcrypt's 72-byte limit is a friendly message, never a crash
    assert "72" in auth.password_problem("é" * 40)
    assert auth.verify_password("a" * 100, admin.password_hash) is False
    c = TestClient(app)
    c.post("/login", data={"email": "admin@x.com", "password": "dev-password-admin-2026"})
    assert c.get("/?page=0").status_code == 200 and c.get("/?page=-5").status_code == 200
    # a shared saved view with junk JSON cannot break the inbox
    v = SavedView(name="bad", owner_id=admin.id, is_shared=True, params_json=json.dumps([1, 2, {"q": None}])); s.add(v)
    v2 = SavedView(name="bad2", owner_id=admin.id, is_shared=True, params_json=json.dumps({"view": {"x": 1}, "brands": "WashU", "days": "abc"})); s.add(v2)
    s.commit()
    assert c.get(f"/?sv={v.id}").status_code == 200 and c.get(f"/?sv={v2.id}").status_code == 200
    # unknown platform, unknown category, bad role
    r = c.post("/admin/sites/listing", data={"source": "myspace", "external_location_id": "1"})
    assert r.status_code == 400 and "Unknown platform" in r.text and 'nav class="top"' in r.text
    c.post("/admin/users", data={"email": "new@x.com", "name": "", "role": "superuser"})
    nu = s.execute(select(User).where(User.email == "new@x.com")).scalar_one()
    assert nu.role == "agent" and nu.name == "new"
    # error pages keep the frame and escape the detail
    r = c.get("/reviews/999999")
    assert r.status_code == 404 and 'nav class="top"' in r.text and "Back to the inbox" in r.text
    r = c.post("/reviews/999999/note", data={"note": "x"})
    assert r.status_code == 404
    r = c.post("/views", data={})
    assert r.status_code == 422 and "Please fill in" in r.text and "name" in r.text
    # absolute session lifetime: an old sign-in is refused even if renewed recently
    old = auth._serializer.dumps({"uid": admin.id, "iat": int(time.time()) - 8 * 86400})
    assert auth.read_session(old) == (None, None)
    fresh = auth.make_session_cookie(admin)
    assert auth.read_session(fresh)[0] == admin.id
    s.close()


def test_cross_site_posts_are_refused():
    from fastapi.testclient import TestClient
    from app import auth
    from app.db import SessionLocal, engine, init_db
    from app.models import Base, User
    from app.web import app
    Base.metadata.drop_all(engine); init_db()
    s = SessionLocal(); s.add(User(email="admin@x.com", name="A", password_hash=auth.hash_password("dev-password-admin-2026"), role="admin")); s.commit(); s.close()
    c = TestClient(app)
    c.post("/login", data={"email": "admin@x.com", "password": "dev-password-admin-2026"})
    for hdrs in ({"Origin": "https://evil.example"}, {"Origin": "https://reviews.evil.testserver"}, {"Origin": "null"},
                 {"Referer": "https://evil.example/page"}):
        r = c.post("/admin/api", data={"name": "x"}, headers=hdrs)
        assert r.status_code == 403, hdrs
    ok = c.post("/admin/api", data={"name": "x"}, headers={"Origin": "http://testserver"})
    assert ok.status_code == 200 and "New key created" in ok.text
    assert c.post("/admin/api", data={"name": "y"}, headers={"Referer": "http://testserver/admin/api"}).status_code == 200
    # health: liveness always 200; sync health speaks in status codes
    assert c.get("/health").status_code == 200 and c.get("/health/sync").status_code in (200, 503)
