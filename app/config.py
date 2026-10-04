"""Settings loaded from environment variables (.env is loaded if present)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _env(name: str, default: str) -> str:
    """The variable's value, or `default` when it is unset OR blank. Hosting dashboards (Render's
    Blueprint prompts) create variables with an empty value; that must mean "use the default"."""
    value = os.getenv(name)
    return default if value is None or value.strip() == "" else value


def _bool(value: Optional[str], default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _csv(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


def _base_url(url: str) -> str:
    """Public address for links in emails: no trailing slash, https when no scheme was given."""
    url = (url or "").strip().rstrip("/")
    return url if "://" in url else f"https://{url}"


def normalize_database_url(url: str) -> str:
    """Point any Postgres URL at the psycopg 3 driver this app installs.

    Render, Heroku and Supabase hand out postgres:// (a name SQLAlchemy 2 refuses) or
    postgresql:// (which SQLAlchemy maps to psycopg2, not installed here)."""
    url = (url or "").strip()
    for prefix in ("postgres://", "postgresql://", "postgresql+psycopg2://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


DEFAULT_SECRET_KEY = "dev-only-insecure-key"


@dataclass
class Settings:
    database_url: str = normalize_database_url(os.getenv("DATABASE_URL") or "sqlite:///./review_manager.db")
    secret_key: str = os.getenv("SECRET_KEY") or DEFAULT_SECRET_KEY
    # Render sets RENDER_EXTERNAL_URL automatically; APP_BASE_URL overrides it.
    app_base_url: str = _base_url(os.getenv("APP_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "http://localhost:8000")
    # DEMO_MODE=true seeds placeholder data on startup when the database is empty (hosted demo only).
    demo_mode: bool = _bool(os.getenv("DEMO_MODE"), False)
    timezone: str = _env("TIMEZONE", "America/Chicago")

    google_client_secrets_file: str = _env("GOOGLE_CLIENT_SECRETS_FILE", "./client_secret.json")
    google_token_file: str = _env("GOOGLE_TOKEN_FILE", "./google_token.json")
    google_token_json: Optional[str] = os.getenv("GOOGLE_TOKEN_JSON") or None

    sync_interval_minutes: int = int(_env("SYNC_INTERVAL_MINUTES", "20"))
    # One full re-pull per day (catches reviews that were removed) at or after this local hour.
    full_sync_hour_local: int = int(_env("FULL_SYNC_HOUR_LOCAL", "3"))
    # A review is marked removed only after it has been missing from this many consecutive
    # successful full pulls (2 = today's and yesterday's). 1 trusts a single pull.
    removal_confirm_pulls: int = int(_env("REMOVAL_CONFIRM_PULLS", "2"))
    # Instant alerts (new negative reviews, removed reviews, sync failures) go to the "alerts" recipient list.
    alert_negatives: bool = _bool(os.getenv("ALERT_NEGATIVES"), True)
    alert_fail_threshold: int = int(_env("ALERT_FAIL_THRESHOLD", "3"))
    # Reviews we have reported to Google and are awaiting a decision on are left out of averages.
    exclude_disputed: bool = _bool(os.getenv("EXCLUDE_DISPUTED"), True)
    # Signed-in sessions expire after this many hours without activity.
    session_hours: int = int(_env("SESSION_HOURS", "12"))
    # Hard cap on one sign-in, however actively it is used (the idle timeout above slides).
    session_max_days: int = int(_env("SESSION_MAX_DAYS", "7"))
    # Password login: this many failed attempts per email or address within 15 minutes locks it for 15 minutes.
    login_max_attempts: int = int(_env("LOGIN_MAX_ATTEMPTS", "5"))
    # How many proxies sit in front of the app and append to X-Forwarded-For. Render runs one;
    # 0 = use the socket address and ignore the header entirely.
    trusted_proxy_hops: int = int(os.getenv("TRUSTED_PROXY_HOPS") or ("1" if os.getenv("RENDER") else "0"))
    # Facebook Page recommendations (second source). A system-user token with pages_read_user_content,
    # pages_read_engagement and pages_manage_engagement on every brand page.
    facebook_access_token: Optional[str] = os.getenv("FACEBOOK_ACCESS_TOKEN") or None
    # Browser origins allowed to call the read-only JSON API (the Teams Portal), comma-separated.
    api_cors_origins: List[str] = field(default_factory=lambda: _csv(_env("API_CORS_ORIGINS", "https://chris-stacks.washucarwash.com")))
    facebook_api_version: str = _env("FACEBOOK_API_VERSION", "v21.0")
    report_hour_local: int = int(_env("REPORT_HOUR_LOCAL", "7"))
    report_recipients_fallback: List[str] = field(default_factory=lambda: _csv(os.getenv("REPORT_RECIPIENTS")))

    smtp_host: str = _env("SMTP_HOST", "smtp.office365.com")
    smtp_port: int = int(_env("SMTP_PORT", "587"))
    smtp_starttls: bool = _bool(os.getenv("SMTP_STARTTLS"), True)
    smtp_user: Optional[str] = os.getenv("SMTP_USER") or None
    smtp_password: Optional[str] = os.getenv("SMTP_PASSWORD") or None
    smtp_from: str = _env("SMTP_FROM", _env("SMTP_USER", "reviews@example.com"))

    # Reviews at or below this star rating are treated as negative.
    negative_rating_max: int = int(_env("NEGATIVE_RATING_MAX", "3"))
    # Unanswered reviews older than this many hours are flagged as overdue.
    overdue_hours: int = int(_env("OVERDUE_HOURS", "48"))

    # AI drafting (Anthropic). Leave the key unset to hide the Draft button.
    anthropic_api_key: Optional[str] = os.getenv("ANTHROPIC_API_KEY") or None
    ai_model: str = _env("AI_MODEL", "claude-opus-5-5")
    # Phone number the AI may include in replies to negative reviews, per brand.
    brand_phones: Dict[str, str] = field(default_factory=lambda: dict(
        kv.split("=", 1) for kv in _csv(_env("BRAND_PHONES", "WashU=(815) 205-3492;ICON=(615) 776-7837;WA=(615) 776-7837").replace(";", ",")) if "=" in kv))

    # Group negative reviews into the workbook categories with Claude as they arrive (needs the key).
    ai_classify: bool = _bool(os.getenv("AI_CLASSIFY"), True)

    @property
    def ai_enabled(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def mail_enabled(self) -> bool:
        """Outbound email works once SMTP credentials exist; without them links are shown on screen instead."""
        return bool(self.smtp_user and self.smtp_password)

    # Microsoft Entra ID single sign-on (OpenID Connect via MSAL).
    ms_client_id: Optional[str] = os.getenv("MS_CLIENT_ID") or None
    ms_client_secret: Optional[str] = os.getenv("MS_CLIENT_SECRET") or None
    # Tenant id for a single-tenant registration, or "organizations" for any work account.
    ms_tenant: str = _env("MS_TENANT_ID", "organizations")
    # Only these email domains may sign in with Microsoft.
    sso_allowed_domains: List[str] = field(default_factory=lambda: [d.lower() for d in _csv(
        _env("SSO_ALLOWED_DOMAINS", "washucarwash.com,washassociates.com,iconcarwash.com"))])
    # Entra tenant ids (comma-separated) allowed to sign in. Required when MS_TENANT_ID is the
    # multi-tenant "organizations"/"common": an email claim is only trustworthy from a tenant we
    # know, because any stranger's own tenant can mint a user whose mail is one of ours.
    sso_allowed_tenants: List[str] = field(default_factory=lambda: _csv(os.getenv("SSO_ALLOWED_TENANTS")))
    # Create an agent account automatically on first Microsoft sign-in from an allowed domain.
    sso_auto_provision: bool = _bool(os.getenv("SSO_AUTO_PROVISION"), True)
    # Keep the email + password form available (break-glass). Set false once SSO is proven.
    password_login_enabled: bool = _bool(os.getenv("PASSWORD_LOGIN_ENABLED"), True)

    @property
    def is_local(self) -> bool:
        """Running on this machine only (APP_BASE_URL points at localhost)."""
        host = self.app_base_url.split("://", 1)[-1].split("/", 1)[0].rsplit(":", 1)[0].strip("[]").lower()
        return host in ("localhost", "127.0.0.1", "::1") or host.endswith(".localhost")

    def fatal_config_problems(self) -> List[str]:
        """Settings that make a deployment unsafe. Web, worker and init-db refuse to start on any."""
        problems: List[str] = []
        if not self.is_local and (self.secret_key == DEFAULT_SECRET_KEY or len(self.secret_key) < 24):
            problems.append("SECRET_KEY is missing, the built-in default, or shorter than 24 characters. Anyone could "
                            "sign their own admin session. Set a long random value, e.g. "
                            "`python -c \"import secrets; print(secrets.token_urlsafe(48))\"`.")
        if os.getenv("RENDER") and self.is_local:
            # The worker has no public address of its own: without APP_BASE_URL every link in the
            # morning report and the alerts would point at localhost.
            problems.append("APP_BASE_URL is not set, so links in emails would point at localhost. Set it on the "
                            "web service (e.g. https://review-manager.onrender.com); the worker reads it from there.")
        if self.demo_mode:
            real_source = bool(self.google_token_json or Path(self.google_token_file).exists() or self.facebook_access_token)
            if real_source:
                problems.append("DEMO_MODE is on while Google or Facebook credentials are configured. In demo mode replies are "
                                "recorded as posted but never reach the platform. Turn DEMO_MODE off for a real deployment.")
            if not self.database_url.startswith("sqlite"):
                problems.append("DEMO_MODE is on against a non-SQLite database. Demo mode seeds fake reviews and users into "
                                "an empty database; it is only for the throwaway demo.")
            if not self.is_local and not os.getenv("DEMO_PASSWORD"):
                problems.append("DEMO_MODE on a public host requires DEMO_PASSWORD, otherwise the seeded accounts use "
                                "the passwords printed in dev_seed.py.")
        return problems

    def check_or_exit(self, what: str) -> None:
        problems = self.fatal_config_problems()
        if problems:
            raise SystemExit(f"Refusing to start {what}:\n  - " + "\n  - ".join(problems))

    @property
    def sso_accepted_tenants(self) -> List[str]:
        """Tenant ids whose id tokens we accept: the explicit allow-list, else the single
        tenant the registration is pinned to. Empty means SSO is not safely configured."""
        if self.sso_allowed_tenants:
            return list(self.sso_allowed_tenants)
        if self.ms_tenant and self.ms_tenant.lower() not in ("organizations", "common", "consumers"):
            return [self.ms_tenant]
        return []

    @property
    def sso_config_problem(self) -> Optional[str]:
        """Why Microsoft sign-in is off although credentials exist, or None."""
        if not (self.ms_client_id and self.ms_client_secret):
            return None
        if not self.sso_accepted_tenants:
            return ("MS_TENANT_ID is multi-tenant and SSO_ALLOWED_TENANTS is empty; set SSO_ALLOWED_TENANTS "
                    "to the Directory (tenant) IDs that may sign in. Microsoft sign-in stays off until then.")
        return None

    @property
    def sso_enabled(self) -> bool:
        return bool(self.ms_client_id and self.ms_client_secret and self.sso_accepted_tenants)

    @property
    def ms_authority(self) -> str:
        return f"https://login.microsoftonline.com/{self.ms_tenant}"

    @property
    def ms_redirect_uri(self) -> str:
        return f"{self.app_base_url}/auth/microsoft/callback"

    # Listings whose Google title matches any of these (case-insensitive) are
    # recorded by discover-locations but left INACTIVE and unmapped. The legacy
    # Wash N' Roll profiles are intentionally left alone; the ICON profiles were
    # started fresh on purpose. Override with LISTING_EXCLUDE_PATTERNS=a,b,c
    listing_exclude_patterns: List[str] = field(default_factory=lambda: _csv(
        _env("LISTING_EXCLUDE_PATTERNS", "wash n' roll,wash n roll,wash n’ roll")))


settings = Settings()

# Morning digest editions. Brands map to the markets the owner reports on.
EDITIONS = {
    "il": {"label": "Illinois", "short": "IL", "brands": ["WashU"]},
    "tn": {"label": "Tennessee", "short": "TN", "brands": ["ICON", "WA"]},
    "all": {"label": "All locations", "short": "Corporate", "brands": None},
}
# Every recipient list the admin page manages: the three digest editions plus instant alerts.
ALERTS_KEY = "alerts"
RECIPIENT_LISTS = dict(EDITIONS)
RECIPIENT_LISTS[ALERTS_KEY] = {"label": "Instant alerts", "short": "Alerts", "brands": None}
