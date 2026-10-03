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

    @property
    def ai_enabled(self) -> bool:
        return bool(self.anthropic_api_key)

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
