import os, sys, tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="rm_test_")
# Pin every setting that reaches the outside world BEFORE app.config loads .env.
# load_dotenv never overrides a variable that is already set, so these win over a
# developer's real .env: no test can call Claude, Google, Facebook, SMTP or Microsoft.
_PINNED = {
    # TEST_DATABASE_URL runs the whole suite on another database (e.g. a throwaway Postgres).
    "DATABASE_URL": os.getenv("TEST_DATABASE_URL") or f"sqlite:///{_tmp}/test.db",
    "SECRET_KEY": "test-secret-key-not-for-prod",
    "APP_BASE_URL": "http://localhost:8000",
    "TIMEZONE": "America/Chicago",
    "DEMO_MODE": "false",
    "ANTHROPIC_API_KEY": "",
    "AI_CLASSIFY": "false",
    "GOOGLE_TOKEN_JSON": "",
    "GOOGLE_TOKEN_FILE": f"{_tmp}/no-google-token.json",
    "GOOGLE_CLIENT_SECRETS_FILE": f"{_tmp}/no-client-secret.json",
    "FACEBOOK_ACCESS_TOKEN": "",
    "SMTP_HOST": "", "SMTP_USER": "", "SMTP_PASSWORD": "", "SMTP_FROM": "",
    "MS_CLIENT_ID": "", "MS_CLIENT_SECRET": "", "MS_TENANT_ID": "organizations", "SSO_ALLOWED_TENANTS": "",
    "REPORT_RECIPIENTS": "",
    "RENDER": "", "RENDER_EXTERNAL_URL": "", "TRUSTED_PROXY_HOPS": "0",
}
os.environ.update(_PINNED)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
