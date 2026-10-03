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
    loc = r.headers["location"]
    assert "link=" in loc and "password%2Freset%3Ftoken" in loc
    page = c.get(loc)
    assert "share this set-password link" in page.text and "/password/reset?token=" in page.text
    # admin can also hand out a reset link for an existing user
    u = db.execute(select(User).where(User.email == "lily@x.com")).scalar_one()
    r2 = c.post(f"/admin/users/{u.id}/reset-link", follow_redirects=False)
    assert "link=" in r2.headers["location"]


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
