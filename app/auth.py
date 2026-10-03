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
SESSION_MAX_AGE = 60 * 60 * 24 * 14  # 14 days
_serializer = URLSafeTimedSerializer(settings.secret_key, salt="review-manager-session")


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
    if not value:
        return None
    try:
        return _serializer.loads(value, max_age=SESSION_MAX_AGE).get("uid")
    except BadSignature:
        return None


def current_user_optional(request: Request, db: Session = Depends(get_db)) -> Optional[User]:
    uid = read_session_cookie(request.cookies.get(COOKIE_NAME))
    if uid is None:
        return None
    user = db.get(User, uid)
    return user if (user and user.active) else None


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
    """Match or provision a user from verified id_token claims. Raises SsoError."""
    email = (claims.get("email") or claims.get("preferred_username") or "").strip().lower()
    oid = claims.get("oid")
    tid = claims.get("tid")
    name = (claims.get("name") or email.split("@")[0]).strip()
    if not email or "@" not in email:
        raise SsoError("Microsoft did not return an email address for this account.")
    if settings.sso_allowed_tenants and tid not in settings.sso_allowed_tenants:
        raise SsoError("This Microsoft organization is not allowed to sign in.")
    if not email_domain_allowed(email):
        raise SsoError(f"Sign-in is limited to {', '.join(settings.sso_allowed_domains)} accounts.")
    user = None
    if oid:
        user = db.execute(select(User).where(User.ms_oid == oid)).scalar_one_or_none()
    if user is None:
        user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if user is None:
        if not settings.sso_auto_provision:
            raise SsoError("No account exists for this email yet. Ask an admin to add you.")
        user = User(email=email, name=name, password_hash=None, role="agent", auth_provider="microsoft", active=True)
        db.add(user)
    if user.active is False:
        raise SsoError("This account has been deactivated.")
    user.ms_oid = user.ms_oid or oid
    user.ms_tenant_id = tid or user.ms_tenant_id
    if user.auth_provider == "local" and oid:
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
