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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("cli")


def cmd_init_db(_a):
    init_db()
    print(f"tables ready in {settings.database_url}")


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
    from app.auth import hash_password
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
    from app.sources.google import GoogleBusinessProfileAdapter
    g = GoogleBusinessProfileAdapter()
    accounts = g.list_accounts()
    if not accounts:
        sys.exit("no accounts visible to this token")
    with session_scope() as s:
        locs = s.execute(select(Location)).scalars().all()
        for acct in accounts:
            print(f"\n{acct.get('accountName')}  ({acct['name']}, {acct.get('type')})")
            for loc in g.list_locations(acct["name"]):
                loc_id = loc["name"].split("/")[-1]
                addr = loc.get("storefrontAddress") or {}
                address = ", ".join(filter(None, [" ".join(addr.get("addressLines", [])), addr.get("locality"), addr.get("administrativeArea")]))
                maps_url = (loc.get("metadata") or {}).get("mapsUri")
                link = s.execute(select(ReviewSourceLink).where(
                    ReviewSourceLink.source == "google", ReviewSourceLink.external_location_id == loc_id)).scalar_one_or_none()
                if link is None:
                    link = ReviewSourceLink(source="google", external_location_id=loc_id)
                    s.add(link)
                link.external_account_id = acct["name"].split("/")[-1]
                link.display_name = loc.get("title")
                link.address = address or None
                link.listing_url = maps_url
                title = (loc.get("title") or "").lower()
                excluded = any(pat.lower() in title for pat in settings.listing_exclude_patterns)
                # Best-effort auto-map by brand keyword + city/name; confirm on the Locations page.
                if link.location_id is None and not excluded:
                    city = (addr.get("locality") or "").lower()
                    state = addr.get("administrativeArea")
                    for cand in locs:
                        brand_kw = {"WashU": "washu", "ICON": "icon", "WA": "wash 38301"}.get(cand.brand, cand.brand.lower())
                        site_kw = cand.name.split(" ", 1)[-1].lower()
                        if brand_kw in title and (site_kw in title or (cand.city and cand.city.lower() == city and cand.state == state)):
                            link.location_id = cand.id
                            break
                # Only mapped listings are synced. Excluded/unmapped ones are recorded but inactive.
                link.active = bool(link.location_id) and not excluded
                mapped = "EXCLUDED (left alone)" if excluded else next((c.name for c in locs if c.id == link.location_id), "UNMAPPED (inactive)")
                print(f"  {loc_id:<22} {loc.get('title', ''):<40} {address:<45} -> {mapped}")
    print("\nUNMAPPED rows stay inactive until mapped with `link-location` or on the /locations page. EXCLUDED rows are never synced. Then run `backfill`.")


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
        r.brands = None if "all" in have else ";".join(sorted({b for e in have for b in {"il": ["WashU"], "tn": ["ICON", "WA"]}[e]}))
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
    from app.ai import AiUnavailable, classify_negative
    from app.daterange import resolve_range
    from app.models import Review
    from app.text_intel import THEMES
    dr = resolve_range(a.range, a.start or "", a.end or "")
    done = skipped = failed = 0
    with session_scope() as s:
        q = select(Review).where(Review.is_deleted.is_(False), Review.rating <= settings.negative_rating_max,
                                 Review.created_at_source >= dr.start, Review.created_at_source < dr.end)
        for r in s.execute(q).scalars().all():
            if not (r.text or "").strip():
                if r.category != "No Content":
                    r.category = "No Content"; done += 1
                continue
            if r.category and not a.force and r.category not in ("Unknown", None):
                skipped += 1; continue
            try:
                r.category = classify_negative(r, THEMES); done += 1
            except AiUnavailable as exc:
                failed += 1
                log.warning("%s", exc)
                if "not configured" in str(exc):
                    break
    print(f"{dr.label}: classified {done}, kept {skipped}, failed {failed}")


def cmd_worker(_a):
    from app.worker import run_forever
    run_forever()


def cmd_web(a):
    import uvicorn
    uvicorn.run("app.web:app", host=a.host, port=a.port, reload=a.reload)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db").set_defaults(fn=cmd_init_db)
    sp = sub.add_parser("seed-locations"); sp.add_argument("--csv"); sp.set_defaults(fn=cmd_seed_locations)
    sp = sub.add_parser("create-user"); sp.add_argument("--email", required=True); sp.add_argument("--name", required=True)
    sp.add_argument("--admin", action="store_true"); sp.add_argument("--password", help="omit to be prompted"); sp.set_defaults(fn=cmd_create_user)
    sub.add_parser("google-auth").set_defaults(fn=cmd_google_auth)
    sub.add_parser("discover-locations").set_defaults(fn=cmd_discover_locations)
    sp = sub.add_parser("link-location"); sp.add_argument("--source-id", type=int, required=True); sp.add_argument("--location-id", type=int, required=True); sp.set_defaults(fn=cmd_link_location)
    sp = sub.add_parser("backfill", help="full pull of all history"); sp.add_argument("--source"); sp.set_defaults(fn=lambda a: cmd_sync(a, full=True))
    sp = sub.add_parser("sync", help="incremental pull"); sp.add_argument("--source"); sp.add_argument("--full", action="store_true"); sp.set_defaults(fn=lambda a: cmd_sync(a, full=a.full))
    sp = sub.add_parser("add-recipient"); sp.add_argument("--email", required=True); sp.add_argument("--name"); sp.add_argument("--edition", action="append", choices=["il", "tn", "all"], help="il = Illinois, tn = Tennessee, all = Corporate; repeat for several (default all)"); sp.add_argument("--remove", action="store_true", help="drop from the given editions (or from everything)"); sp.set_defaults(fn=cmd_add_recipient)
    sp = sub.add_parser("send-report"); sp.add_argument("--dry-run", action="store_true"); sp.add_argument("--out", help="also write the HTML here"); sp.add_argument("--to", help="comma list, overrides stored recipients"); sp.add_argument("--edition", default="all", choices=["il", "tn", "all"], help="with --to: which edition to send"); sp.set_defaults(fn=cmd_send_report)
    sp = sub.add_parser("classify-negatives", help="AI-group negative reviews into workbook categories"); sp.add_argument("--range", default="last30"); sp.add_argument("--start"); sp.add_argument("--end"); sp.add_argument("--force", action="store_true", help="re-classify reviews that already have a category"); sp.set_defaults(fn=cmd_classify)
    sub.add_parser("worker").set_defaults(fn=cmd_worker)
    sp = sub.add_parser("web"); sp.add_argument("--host", default="127.0.0.1"); sp.add_argument("--port", type=int, default=8000); sp.add_argument("--reload", action="store_true"); sp.set_defaults(fn=cmd_web)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
