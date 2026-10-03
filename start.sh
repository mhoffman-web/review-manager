#!/usr/bin/env bash
# Render start command. Seeds placeholder data once when DEMO_MODE=true and the
# database has no reviews, then serves the app on Render's $PORT.
set -euo pipefail
python cli.py init-db
if [ "${DEMO_MODE:-false}" = "true" ]; then
  if ! python -c 'import sys; sys.path.insert(0, "."); from sqlalchemy import select, func; from app.db import SessionLocal; from app.models import Review
with SessionLocal() as s: n = s.execute(select(func.count(Review.id))).scalar() or 0
sys.exit(0 if n else 1)'; then
    echo "DEMO_MODE: seeding placeholder data (DEMO_MONTHS=${DEMO_MONTHS:-18})"
    python dev_seed.py
  fi
fi
# --proxy-headers lets the app see https from Render's proxy. The login throttle does not rely
# on uvicorn's client address; it reads X-Forwarded-For itself, trusting TRUSTED_PROXY_HOPS.
exec uvicorn app.web:app --host 0.0.0.0 --port "${PORT:-8000}" --proxy-headers --forwarded-allow-ips="*"
