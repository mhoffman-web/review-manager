"""Password hashing and signed-cookie sessions for a 2-3 person team."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

import bcrypt
from fastapi import Depends, HTTPException, Request, status
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import User

COOKIE_NAME = "rm_session"
SESSION_MAX_AGE = max(1, settings.session_hours) * 3600   # idle timeout; the cookie is re-issued on activity
RENEW_AFTER = 3600                                        # re-issue at most hourly
_serializer = URLSafeTimedSerializer(settings.secret_key, salt="review-manager-session")

# ---- login throttling (per email and per client address, in-process)
import time as _time
from collections import defaultdict as _dd
LOGIN_WINDOW = 15 * 60
_failures: "dict[str, list[float]]" = _dd(list)


def login_blocked(*keys: str) -> int:
    """Seconds until another attempt is allowed, 0 when not blocked."""
    now = _time.time()
    worst = 0
    for k in keys:
        stamps = [t for t in _failures.get(k, []) if now - t < LOGIN_WINDOW]
        _failures[k] = stamps
        if len(stamps) >= settings.login_max_attempts:
            worst = max(worst, int(LOGIN_WINDOW - (now - stamps[0])) + 1)
    return worst


def note_login_failure(*keys: str) -> None:
    for k in keys:
        _failures[k].append(_time.time())


def clear_login_failures(*keys: str) -> None:
    for k in keys:
        _failures.pop(k, None)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


def authenticate(db: Session, email: str, password: str) -> Optional[User]:
    if not settings.password_login_enabled:
        return None
    user = db.execute(select(User).where(User.email == email.strip().lower())).scalar_one_or_none()
    if user and user.active and user.password_hash and verify_password(password, user.password_hash):
        user.last_login_at = datetime.utcnow()
        db.commit()
        return user
    return None


def make_session_cookie(user: User) -> str:
    return _serializer.dumps({"uid": user.id})


def read_session_cookie(value: Optional[str]) -> Optional[int]:
    uid, _issued = read_session(value)
    return uid


def read_session(value: Optional[str]):
    """(user id, issued-at datetime) or (None, None) for a missing, forged or expired cookie."""
    if not value:
        return None, None
    try:
        data, ts = _serializer.loads(value, max_age=SESSION_MAX_AGE, return_timestamp=True)
        return data.get("uid"), ts
    except BadSignature:
        return None, None


def current_user_optional(request: Request, db: Session = Depends(get_db)) -> Optional[User]:
    uid, issued = read_session(request.cookies.get(COOKIE_NAME))
    if uid is None:
        return None
    user = db.get(User, uid)
    if not (user and user.active):
        return None
    if issued is not None and (datetime.now(issued.tzinfo) - issued).total_seconds() > RENEW_AFTER:
        request.state.renew_session_uid = user.id      # web.py middleware re-issues the cookie
    return user


def current_user(user: Optional[User] = Depends(current_user_optional)) -> User:
    if user is None:
        # Redirect handled by the exception handler in web.py
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return user


def admin_user(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admins only")
    return user


# ----------------------------------------------------------------- Microsoft Entra ID (OIDC via MSAL)
class SsoError(Exception):
    """User-facing reason a Microsoft sign-in was rejected."""


def email_domain_allowed(email: str) -> bool:
    domain = email.rsplit("@", 1)[-1].lower() if "@" in email else ""
    return domain in settings.sso_allowed_domains


def resolve_sso_user(db: Session, claims: dict) -> User:
    """Match or provision a user from verified id_token claims. Raises SsoError.

    Identity is the (tenant id, object id) pair Microsoft asserts, never the email
    alone: the mail attribute is set by whoever administers the signing tenant, so
    an email match is only trusted from a tenant on our list, and never overrides
    an account that is already bound to a different Microsoft identity."""
    email = (claims.get("email") or claims.get("preferred_username") or "").strip().lower()
    oid = (claims.get("oid") or "").strip() or None
    tid = (claims.get("tid") or "").strip() or None
    name = (claims.get("name") or email.split("@")[0]).strip()
    accepted = settings.sso_accepted_tenants
    if not accepted:
        raise SsoError("Microsoft sign-in is not pinned to a tenant. An admin must set SSO_ALLOWED_TENANTS.")
    if not oid or not tid:
        raise SsoError("Microsoft did not return a tenant and object id for this account.")
    if tid not in accepted:
        raise SsoError("This Microsoft organization is not allowed to sign in.")
    if not email or "@" not in email:
        raise SsoError("Microsoft did not return an email address for this account.")
    if not email_domain_allowed(email):
        raise SsoError(f"Sign-in is limited to {', '.join(settings.sso_allowed_domains)} accounts.")
    # 1. The account this Microsoft identity already signed in as.
    user = db.execute(select(User).where(User.ms_oid == oid)).scalar_one_or_none()
    if user is not None and user.ms_tenant_id and user.ms_tenant_id != tid:
        raise SsoError("This Microsoft account belongs to a different organization than the one on file.")
    if user is None:
        # 2. First Microsoft sign-in for an account an admin created (or a password user
        #    moving to SSO): link it by email, from an accepted tenant only (checked above).
        #    An account already bound to another object id is never re-bound this way.
        user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if user is not None and user.ms_oid and user.ms_oid != oid:
            raise SsoError("This email is already linked to a different Microsoft account. Ask an admin to reset the link.")
    if user is None:
        if not settings.sso_auto_provision:
            raise SsoError("No account exists for this email yet. Ask an admin to add you.")
        user = User(email=email, name=name, password_hash=None, role="agent", auth_provider="microsoft", active=True)
        db.add(user)
    if user.active is False:
        raise SsoError("This account has been deactivated.")
    user.ms_oid = oid
    user.ms_tenant_id = tid
    if user.auth_provider == "local":
        user.auth_provider = "microsoft"
    if not user.name:
        user.name = name
    user.last_login_at = datetime.utcnow()
    db.commit()
    return user


_flow_serializer = URLSafeTimedSerializer(settings.secret_key, salt="review-manager-msal-flow")
FLOW_COOKIE = "rm_msal_flow"
FLOW_MAX_AGE = 600  # seconds to complete the Microsoft round trip


def msal_app():
    """Confidential client for the configured tenant. Imported lazily so the
    app runs without msal when SSO is not configured."""
    import msal
    return msal.ConfidentialClientApplication(
        settings.ms_client_id, authority=settings.ms_authority, client_credential=settings.ms_client_secret)


def start_microsoft_flow() -> tuple:
    """Returns (auth_url, signed_flow_cookie_value)."""
    flow = msal_app().initiate_auth_code_flow(scopes=["User.Read"], redirect_uri=settings.ms_redirect_uri)
    return flow["auth_uri"], _flow_serializer.dumps(flow)


def finish_microsoft_flow(flow_cookie: Optional[str], query_params: dict) -> dict:
    """Exchange the auth code; returns verified id_token claims. Raises SsoError."""
    if not flow_cookie:
        raise SsoError("Sign-in session expired. Please try again.")
    try:
        flow = _flow_serializer.loads(flow_cookie, max_age=FLOW_MAX_AGE)
    except BadSignature:
        raise SsoError("Sign-in session expired. Please try again.")
    result = msal_app().acquire_token_by_auth_code_flow(flow, dict(query_params))
    if "error" in result:
        raise SsoError(result.get("error_description") or result["error"])
    claims = result.get("id_token_claims") or {}
    if not claims:
        raise SsoError("Microsoft did not return an identity token.")
    return claims


# ----------------------------------------------------------------- password set / reset links
_pw_serializer = URLSafeTimedSerializer(settings.secret_key, salt="review-manager-password")
WELCOME_MAX_AGE = 48 * 3600     # a new account's set-password link
RESET_MAX_AGE = 2 * 3600        # a forgot-password link
MIN_PASSWORD = 10


def _hash_tag(user: User) -> str:
    return (user.password_hash or "none")[-16:]


def make_password_token(user: User, purpose: str = "reset") -> str:
    """Signed, time-limited and single-use: it carries a fragment of the current hash, so it
    stops working the moment the password changes."""
    return _pw_serializer.dumps({"uid": user.id, "tag": _hash_tag(user), "p": "welcome" if purpose == "welcome" else "reset"})


def password_link(user: User, purpose: str = "reset") -> str:
    return f"{settings.app_base_url}/password/reset?token={make_password_token(user, purpose)}"


def verify_password_token(db: Session, token: Optional[str]):
    """The user the token belongs to, or None when it is missing, forged, expired or already used."""
    if not token:
        return None
    try:
        data, issued = _pw_serializer.loads(token, max_age=WELCOME_MAX_AGE, return_timestamp=True)
    except BadSignature:
        return None
    age = (datetime.now(issued.tzinfo) - issued).total_seconds()
    if data.get("p") != "welcome" and age > RESET_MAX_AGE:
        return None
    user = db.get(User, data.get("uid"))
    if not (user and user.active and data.get("tag") == _hash_tag(user)):
        return None
    return user


def password_problem(password: str, confirm: Optional[str] = None) -> Optional[str]:
    if len(password or "") < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters."
    if confirm is not None and password != confirm:
        return "The two passwords do not match."
    return None
