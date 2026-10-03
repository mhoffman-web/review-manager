"""Microsoft SSO: domain allowlist, provisioning, callback flow with a mocked MSAL client."""
import pytest
from sqlalchemy import select

from app import auth
from app.db import SessionLocal, engine, init_db
from app.models import Base, User


@pytest.fixture()
def db(monkeypatch):
    Base.metadata.drop_all(engine)
    init_db()
    monkeypatch.setattr(auth.settings, "ms_client_id", "client-id")
    monkeypatch.setattr(auth.settings, "ms_client_secret", "secret")
    monkeypatch.setattr(auth.settings, "sso_allowed_domains", ["washucarwash.com", "washassociates.com", "iconcarwash.com"])
    monkeypatch.setattr(auth.settings, "sso_allowed_tenants", [])
    monkeypatch.setattr(auth.settings, "sso_auto_provision", True)
    s = SessionLocal()
    s.add(User(email="mhoffman@washucarwash.com", name="Mitch Hoffman", password_hash=auth.hash_password("dev-password-admin-2026"), role="admin"))
    s.commit()
    yield s
    s.close()


def claims(email, name="Lily Collins", oid="oid-1", tid="tenant-a"):
    return {"preferred_username": email, "email": email, "name": name, "oid": oid, "tid": tid}


def test_resolve_matches_existing_and_provisions_new(db):
    u = auth.resolve_sso_user(db, claims("MHoffman@WashUCarWash.com", name="M H", oid="oid-admin"))
    assert u.role == "admin" and u.ms_oid == "oid-admin" and u.auth_provider == "microsoft"
    lily = auth.resolve_sso_user(db, claims("lily@iconcarwash.com"))
    assert lily.role == "agent" and lily.password_hash is None and lily.email == "lily@iconcarwash.com"
    sarah = auth.resolve_sso_user(db, claims("sarah@washassociates.com", name="Sarah Stuart", oid="oid-2"))
    assert sarah.name == "Sarah Stuart"
    # second sign-in by oid even if the email changed
    again = auth.resolve_sso_user(db, claims("lily.collins@iconcarwash.com", oid="oid-1"))
    assert again.id == lily.id
    assert db.execute(select(User)).scalars().all().__len__() == 3


def test_resolve_rejects_other_domains_tenants_and_inactive(db, monkeypatch):
    with pytest.raises(auth.SsoError):
        auth.resolve_sso_user(db, claims("someone@gmail.com"))
    with pytest.raises(auth.SsoError):
        auth.resolve_sso_user(db, claims("x@slamcarwashmarketing.com"))
    monkeypatch.setattr(auth.settings, "sso_allowed_tenants", ["tenant-a"])
    with pytest.raises(auth.SsoError):
        auth.resolve_sso_user(db, claims("ok@washucarwash.com", tid="tenant-b"))
    monkeypatch.setattr(auth.settings, "sso_auto_provision", False)
    with pytest.raises(auth.SsoError):
        auth.resolve_sso_user(db, claims("new@washucarwash.com", oid="oid-9"))
    u = db.execute(select(User)).scalar_one()
    u.active = False
    db.commit()
    with pytest.raises(auth.SsoError):
        auth.resolve_sso_user(db, claims("mhoffman@washucarwash.com"))
    # password login refuses SSO-only accounts and respects the kill switch
    monkeypatch.setattr(auth.settings, "sso_auto_provision", True)
    lily = auth.resolve_sso_user(db, claims("lily@iconcarwash.com"))
    assert auth.authenticate(db, lily.email, "anything") is None
    monkeypatch.setattr(auth.settings, "password_login_enabled", False)
    assert auth.authenticate(db, "mhoffman@washucarwash.com", "dev-password-admin-2026") is None


def test_login_page_and_callback_flow(db, monkeypatch):
    from fastapi.testclient import TestClient
    from app.web import app
    import app.web as web
    monkeypatch.setattr(web.settings, "ms_client_id", "client-id")
    monkeypatch.setattr(web.settings, "ms_client_secret", "secret")

    class FakeMsal:
        def initiate_auth_code_flow(self, scopes, redirect_uri):
            assert redirect_uri.endswith("/auth/microsoft/callback")
            return {"auth_uri": "https://login.microsoftonline.com/organizations/oauth2/v2.0/authorize?x=1", "state": "st", "code_verifier": "v"}

        def acquire_token_by_auth_code_flow(self, flow, params):
            assert flow["state"] == "st" and params.get("code") == "abc"
            return {"id_token_claims": claims("sarah@washassociates.com", name="Sarah Stuart", oid="oid-2")}

    monkeypatch.setattr(auth, "msal_app", lambda: FakeMsal())
    c = TestClient(app)
    page = c.get("/login")
    assert "Sign in with Microsoft" in page.text and "washassociates.com" in page.text
    r = c.get("/auth/microsoft?next=/reports", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("https://login.microsoftonline.com/")
    assert auth.FLOW_COOKIE in r.cookies
    cb = c.get("/auth/microsoft/callback?code=abc&state=st", follow_redirects=False)
    assert cb.status_code == 303 and cb.headers["location"] == "/reports"
    assert auth.COOKIE_NAME in cb.cookies
    user = db.execute(select(User).where(User.email == "sarah@washassociates.com")).scalar_one()
    assert user.role == "agent" and user.ms_oid == "oid-2"
    # a rejected domain lands back on the login page with the reason
    monkeypatch.setattr(FakeMsal, "acquire_token_by_auth_code_flow", lambda self, f, p: {"id_token_claims": claims("bad@gmail.com", oid="oid-3")})
    c.get("/auth/microsoft", follow_redirects=False)
    bad = c.get("/auth/microsoft/callback?code=abc&state=st", follow_redirects=False)
    assert bad.status_code == 303 and "limited+to" in bad.headers["location"]
    # expired / missing flow cookie
    c.cookies.clear()
    gone = c.get("/auth/microsoft/callback?code=abc&state=st", follow_redirects=False)
    assert "expired" in gone.headers["location"]
