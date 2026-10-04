#!/usr/bin/env python3
"""Review Manager command line.

  python cli.py init-db
  python cli.py seed-locations
  python cli.py create-user --email lily@... --name "Lily" [--admin]
  python cli.py google-auth
  python cli.py discover-locations
  python cli.py link-location --source-id 3 --location-id 7
  python cli.py backfill
  python cli.py sync
  python cli.py add-recipient --email gm@... --name "..." [--edition il] [--edition tn] [--remove]
  python cli.py send-report [--dry-run] [--out report.html] [--to a@x,b@y]
  python cli.py worker
  python cli.py web [--port 8000]
"""
from __future__ import annotations

import argparse
import csv
import getpass
import logging
import sys
from pathlib import Path

from sqlalchemy import select

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.models import Location, ReportRecipient, ReviewSourceLink, User  # noqa: E402

from app.logging_setup import setup_logging  # noqa: E402
setup_logging()
log = logging.getLogger("cli")


def cmd_init_db(_a):
    from app.config import settings
    settings.check_or_exit("init-db")
    init_db()
    from sqlalchemy.engine import make_url
    # Never print the password: this line lands in the hosting provider's deploy logs.
    print(f"tables ready in {make_url(settings.database_url).render_as_string(hide_password=True)}")


def cmd_seed_locations(a):
    path = Path(a.csv) if a.csv else ROOT / "data" / "locations_seed.csv"
    with session_scope() as s, open(path, newline="") as fh:
        n_new = n_upd = 0
        for row in csv.DictReader(fh):
            loc = s.execute(select(Location).where(Location.name == row["name"])).scalar_one_or_none()
            if loc is None:
                loc = Location(name=row["name"])
                s.add(loc)
                n_new += 1
            else:
                n_upd += 1
            loc.brand, loc.state, loc.city = row["brand"], row["state"], row.get("city") or None
            loc.snowflake_location_ids = row.get("snowflake_location_ids") or None
    print(f"locations: {n_new} added, {n_upd} updated from {path.name}")


def cmd_create_user(a):
    from app.auth import hash_password, password_link
    if a.invite:
        from app.account_mail import send_welcome
        with session_scope() as s:
            u = s.execute(select(User).where(User.email == a.email.lower())).scalar_one_or_none()
            if u is None:
                u = User(email=a.email.lower(), name=a.name, password_hash=None)
                s.add(u)
            u.name, u.role = a.name, "admin" if a.admin else "agent"
            s.flush()
            link = password_link(u, "welcome")
            sent = send_welcome(u, link, by="the Review Manager admin")
        print(f"{'welcome email sent to ' + a.email if sent else 'email not configured; send them this link:'}\n{'' if sent else link}")
        return
    pw = a.password or getpass.getpass("Password: ")
    if len(pw) < 10:
        sys.exit("use at least 10 characters")
    with session_scope() as s:
        u = s.execute(select(User).where(User.email == a.email.lower())).scalar_one_or_none()
        if u is None:
            u = User(email=a.email.lower(), name=a.name, password_hash=hash_password(pw))
            s.add(u)
        else:
            u.name, u.password_hash = a.name, hash_password(pw)
        u.role = "admin" if a.admin else "agent"
        u.active = True
    print(f"user {a.email} ready ({'admin' if a.admin else 'agent'})")


def cmd_google_auth(_a):
    from app.sources.google import run_oauth_flow
    path = run_oauth_flow()
    print(f"token saved to {path}. For hosted deploys, set GOOGLE_TOKEN_JSON to its contents.")


def cmd_discover_locations(_a):
    """List every GBP account + location the token can see and upsert review_sources rows."""
    from app.discovery import discover_google, excluded
    with session_scope() as s:
        totals = discover_google(s)
        links = s.execute(select(ReviewSourceLink).where(ReviewSourceLink.source == "google").order_by(ReviewSourceLink.display_name)).scalars().all()
        for link in links:
            state = "EXCLUDED (left alone)" if not link.active and link.location_id is None and excluded(link.display_name or "") \
                else (link.location.name if link.location else "UNMAPPED (inactive)")
            print(f"  {link.external_location_id:<22} {(link.display_name or ''):<40} {(link.address or ''):<45} -> {state}")
    print(f"\n{totals['listings']} listings across {totals['accounts']} account(s); {totals['mapped']} mapped. Map the rest on Admin -> Sites & listings.")


def cmd_discover_facebook(_a):
    """Record every Facebook Page the FACEBOOK_ACCESS_TOKEN can manage."""
    from app.discovery import discover_facebook
    with session_scope() as s:
        totals = discover_facebook(s)
    print(f"{totals['pages']} pages, {totals['mapped']} mapped to sites")


def cmd_send_alerts(a):
    from app.alerts import send_alerts
    with session_scope() as s:
        out = send_alerts(s, dry_run=a.dry_run, to_override=[x.strip() for x in a.to.split(",")] if a.to else None)
    print("nothing waiting (or no alert recipients)" if not out else f"{'DRY RUN ' if a.dry_run else ''}{out['subject']} -> {', '.join(out['to'])}")


def cmd_link_location(a):
    with session_scope() as s:
        link = s.get(ReviewSourceLink, a.source_id)
        loc = s.get(Location, a.location_id)
        if not link or not loc:
            sys.exit("bad id")
        link.location_id = loc.id
        print(f"{link.display_name} -> {loc.name}")


def cmd_sync(a, full=False):
    from app.sync import sync_all
    with session_scope() as s:
        totals = sync_all(s, full=full, source=a.source)
    print(totals)


def cmd_add_recipient(a):
    eds = a.edition or ["all"]
    with session_scope() as s:
        r = s.execute(select(ReportRecipient).where(ReportRecipient.email == a.email.lower())).scalar_one_or_none()
        if r is None:
            if a.remove:
                print(f"{a.email} is not a recipient"); return
            r = ReportRecipient(email=a.email.lower(), edition="")
            s.add(r)
        have = r.editions
        have = [e for e in have if e not in eds] if a.remove else have + [e for e in eds if e not in have]
        if not have:
            s.delete(r); print(f"recipient {a.email} removed"); return
        r.set_editions(have)
        r.name = a.name or r.name
        r.active = True
        r.brands = None if "all" in have else ";".join(sorted({b for e in have for b in {"il": ["WashU"], "tn": ["ICON", "WA"], "alerts": []}[e]})) or None
        final = r.editions
    print(f"recipient {a.email} gets: {', '.join(final)}")


def cmd_send_report(a):
    from app.reports import send_morning_report
    to = [x.strip() for x in a.to.split(",")] if a.to else None
    with session_scope() as s:
        sends = send_morning_report(s, dry_run=a.dry_run, to_override=to, out_file=a.out, edition=a.edition)
    for snd in sends:
        print(f"{snd.status}: {snd.recipients}" + (f" ({snd.error})" if snd.error else ""))


def cmd_classify(a):
    """Group negative reviews in a window into the workbook categories with Claude."""
    from app.daterange import resolve_range
    from app.models import Review
    from app.sync import classify_window
    dr = resolve_range(a.range, a.start or "", a.end or "")
    with session_scope() as s:
        q = select(Review).where(Review.is_deleted.is_(False), Review.rating <= settings.negative_rating_max,
                                 Review.created_at_source >= dr.start, Review.created_at_source < dr.end)
        out = classify_window(s, s.execute(q).scalars().all(), force=a.force)
    print(f"{dr.label}: classified {out['classified']}, kept {out['kept']}, set by hand {out['manual']}, failed {out['failed']}")


def cmd_worker(_a):
    from app.config import settings
    settings.check_or_exit("the worker")
    from app.worker import run_forever
    run_forever()


def cmd_web(a):
    import uvicorn
    uvicorn.run("app.web:app", host=a.host, port=a.port, reload=a.reload)


def cmd_backup(a):
    from datetime import datetime
    from pathlib import Path
    from app.backup import dump
    out = Path(a.out or f"backups/review-manager-{datetime.now():%Y%m%d-%H%M}.json.gz")
    counts = dump(out)
    print(f"wrote {out} ({out.stat().st_size // 1024} KB): " + ", ".join(f"{k} {v}" for k, v in counts.items() if v))


def cmd_restore(a):
    from pathlib import Path
    from app.backup import restore
    if not a.yes:
        raise SystemExit("restore writes into the configured DATABASE_URL; re-run with --yes to confirm")
    counts = restore(Path(a.file))
    print("restored: " + ", ".join(f"{k} {v}" for k, v in counts.items() if v))


def cmd_seed_content(a):
    from app.starter_content import seed_content
    with session_scope() as s:
        out = seed_content(s, a.owner or "")
    print("added: " + ", ".join(f"{v} {k}" for k, v in out.items()) + " (existing items are left alone)")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db").set_defaults(fn=cmd_init_db)
    sp = sub.add_parser("backup", help="write the whole database to a gzipped JSON file"); sp.add_argument("--out"); sp.set_defaults(fn=cmd_backup)
    sp = sub.add_parser("restore", help="load a backup into an EMPTY database"); sp.add_argument("file"); sp.add_argument("--yes", action="store_true"); sp.set_defaults(fn=cmd_restore)
    sp = sub.add_parser("seed-content", help="starter templates, AI rules, site groups, shared views"); sp.add_argument("--owner", help="email of the admin who owns the shared views"); sp.set_defaults(fn=cmd_seed_content)
    sp = sub.add_parser("seed-locations"); sp.add_argument("--csv"); sp.set_defaults(fn=cmd_seed_locations)
    sp = sub.add_parser("create-user"); sp.add_argument("--email", required=True); sp.add_argument("--name", required=True)
    sp.add_argument("--admin", action="store_true"); sp.add_argument("--password", help="omit to be prompted"); sp.add_argument("--invite", action="store_true", help="no password: email (or print) a set-password link instead"); sp.set_defaults(fn=cmd_create_user)
    sub.add_parser("google-auth").set_defaults(fn=cmd_google_auth)
    sub.add_parser("discover-locations").set_defaults(fn=cmd_discover_locations)
    sub.add_parser("discover-facebook").set_defaults(fn=cmd_discover_facebook)
    sp = sub.add_parser("send-alerts", help="email waiting instant alerts now"); sp.add_argument("--dry-run", action="store_true"); sp.add_argument("--to"); sp.set_defaults(fn=cmd_send_alerts)
    sp = sub.add_parser("link-location"); sp.add_argument("--source-id", type=int, required=True); sp.add_argument("--location-id", type=int, required=True); sp.set_defaults(fn=cmd_link_location)
    sp = sub.add_parser("backfill", help="full pull of all history"); sp.add_argument("--source"); sp.set_defaults(fn=lambda a: cmd_sync(a, full=True))
    sp = sub.add_parser("sync", help="incremental pull"); sp.add_argument("--source"); sp.add_argument("--full", action="store_true"); sp.set_defaults(fn=lambda a: cmd_sync(a, full=a.full))
    sp = sub.add_parser("add-recipient"); sp.add_argument("--email", required=True); sp.add_argument("--name"); sp.add_argument("--edition", action="append", choices=["il", "tn", "all", "alerts"], help="il = Illinois, tn = Tennessee, all = Corporate, alerts = instant alerts; repeat for several (default all)"); sp.add_argument("--remove", action="store_true", help="drop from the given editions (or from everything)"); sp.set_defaults(fn=cmd_add_recipient)
    sp = sub.add_parser("send-report"); sp.add_argument("--dry-run", action="store_true"); sp.add_argument("--out", help="also write the HTML here"); sp.add_argument("--to", help="comma list, overrides stored recipients"); sp.add_argument("--edition", default="all", choices=["il", "tn", "all"], help="with --to: which edition to send"); sp.set_defaults(fn=cmd_send_report)
    sp = sub.add_parser("classify-negatives", help="AI-group negative reviews into workbook categories"); sp.add_argument("--range", default="last30"); sp.add_argument("--start"); sp.add_argument("--end"); sp.add_argument("--force", action="store_true", help="re-classify reviews that already have a category"); sp.set_defaults(fn=cmd_classify)
    sub.add_parser("worker").set_defaults(fn=cmd_worker)
    sp = sub.add_parser("web"); sp.add_argument("--host", default="127.0.0.1"); sp.add_argument("--port", type=int, default=8000); sp.add_argument("--reload", action="store_true"); sp.set_defaults(fn=cmd_web)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
