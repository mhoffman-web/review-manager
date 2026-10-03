"""Settings loaded from environment variables (.env is loaded if present)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _bool(value: Optional[str], default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _csv(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


@dataclass
class Settings:
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./review_manager.db")
    secret_key: str = os.getenv("SECRET_KEY", "dev-only-insecure-key")
    # Render sets RENDER_EXTERNAL_URL automatically; APP_BASE_URL overrides it.
    app_base_url: str = (os.getenv("APP_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "http://localhost:8000").rstrip("/")
    # DEMO_MODE=true seeds placeholder data on startup when the database is empty (hosted demo only).
    demo_mode: bool = _bool(os.getenv("DEMO_MODE"), False)
    timezone: str = os.getenv("TIMEZONE", "America/Chicago")

    google_client_secrets_file: str = os.getenv("GOOGLE_CLIENT_SECRETS_FILE", "./client_secret.json")
    google_token_file: str = os.getenv("GOOGLE_TOKEN_FILE", "./google_token.json")
    google_token_json: Optional[str] = os.getenv("GOOGLE_TOKEN_JSON") or None

    sync_interval_minutes: int = int(os.getenv("SYNC_INTERVAL_MINUTES", "20"))
    # One full re-pull per day (catches reviews that were removed) at or after this local hour.
    full_sync_hour_local: int = int(os.getenv("FULL_SYNC_HOUR_LOCAL", "3"))
    # Instant alerts (new negative reviews, removed reviews, sync failures) go to the "alerts" recipient list.
    alert_negatives: bool = _bool(os.getenv("ALERT_NEGATIVES"), True)
    alert_fail_threshold: int = int(os.getenv("ALERT_FAIL_THRESHOLD", "3"))
    # Reviews we have reported to Google and are awaiting a decision on are left out of averages.
    exclude_disputed: bool = _bool(os.getenv("EXCLUDE_DISPUTED"), True)
    # Signed-in sessions expire after this many hours without activity.
    session_hours: int = int(os.getenv("SESSION_HOURS", "12"))
    # Password login: this many failed attempts per email or address within 15 minutes locks it for 15 minutes.
    login_max_attempts: int = int(os.getenv("LOGIN_MAX_ATTEMPTS", "5"))
    # Facebook Page recommendations (second source). A system-user token with pages_read_user_content,
    # pages_read_engagement and pages_manage_engagement on every brand page.
    facebook_access_token: Optional[str] = os.getenv("FACEBOOK_ACCESS_TOKEN") or None
    # Browser origins allowed to call the read-only JSON API (the Teams Portal), comma-separated.
    api_cors_origins: List[str] = field(default_factory=lambda: _csv(os.getenv("API_CORS_ORIGINS", "https://chris-stacks.washucarwash.com")))
    facebook_api_version: str = os.getenv("FACEBOOK_API_VERSION", "v21.0")
    report_hour_local: int = int(os.getenv("REPORT_HOUR_LOCAL", "7"))
    report_recipients_fallback: List[str] = field(default_factory=lambda: _csv(os.getenv("REPORT_RECIPIENTS")))

    smtp_host: str = os.getenv("SMTP_HOST", "smtp.office365.com")
    smtp_port: int = int(os.getenv("SMTP_PORT", "587"))
    smtp_starttls: bool = _bool(os.getenv("SMTP_STARTTLS"), True)
    smtp_user: Optional[str] = os.getenv("SMTP_USER") or None
    smtp_password: Optional[str] = os.getenv("SMTP_PASSWORD") or None
    smtp_from: str = os.getenv("SMTP_FROM", os.getenv("SMTP_USER", "reviews@example.com"))

    # Reviews at or below this star rating are treated as negative.
    negative_rating_max: int = int(os.getenv("NEGATIVE_RATING_MAX", "3"))
    # Unanswered reviews older than this many hours are flagged as overdue.
    overdue_hours: int = int(os.getenv("OVERDUE_HOURS", "48"))

    # AI drafting (Anthropic). Leave the key unset to hide the Draft button.
    anthropic_api_key: Optional[str] = os.getenv("ANTHROPIC_API_KEY") or None
    ai_model: str = os.getenv("AI_MODEL", "claude-opus-5-5")
    # Phone number the AI may include in replies to negative reviews, per brand.
    brand_phones: Dict[str, str] = field(default_factory=lambda: dict(
        kv.split("=", 1) for kv in _csv(os.getenv("BRAND_PHONES", "WashU=(815) 205-3492;ICON=(615) 776-7837;WA=(615) 776-7837").replace(";", ",")) if "=" in kv))

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
    ms_tenant: str = os.getenv("MS_TENANT_ID", "organizations")
    # Only these email domains may sign in with Microsoft.
    sso_allowed_domains: List[str] = field(default_factory=lambda: [d.lower() for d in _csv(
        os.getenv("SSO_ALLOWED_DOMAINS", "washucarwash.com,washassociates.com,iconcarwash.com"))])
    # Optional: pin to specific Entra tenant ids (comma-separated). Empty = any tenant, domain check still applies.
    sso_allowed_tenants: List[str] = field(default_factory=lambda: _csv(os.getenv("SSO_ALLOWED_TENANTS")))
    # Create an agent account automatically on first Microsoft sign-in from an allowed domain.
    sso_auto_provision: bool = _bool(os.getenv("SSO_AUTO_PROVISION"), True)
    # Keep the email + password form available (break-glass). Set false once SSO is proven.
    password_login_enabled: bool = _bool(os.getenv("PASSWORD_LOGIN_ENABLED"), True)

    @property
    def sso_enabled(self) -> bool:
        return bool(self.ms_client_id and self.ms_client_secret)

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
        os.getenv("LISTING_EXCLUDE_PATTERNS", "wash n' roll,wash n roll,wash n’ roll")))


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
