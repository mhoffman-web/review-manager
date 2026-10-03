# Review Manager

Small in-house tool to monitor, store, reply to, and report on Google reviews
for the 23 WashU / ICON / Wash Associates Google Business Profiles. Built so a
second platform (Yelp, Facebook) is a new adapter file, not a rewrite.

```
review_manager/
  cli.py                 all operator commands (see below)
  app/config.py          settings from .env
  app/models.py          schema: locations, review_sources, reviews, responses, sync_runs, users, report_recipients, report_sends
  app/sources/base.py    adapter interface every platform implements
  app/sources/google.py  Google Business Profile: OAuth, discovery, reviews, reply/delete
  app/sync.py            backfill + incremental upsert
  app/reports.py         morning report (24h / 7d / 30d, negatives, overdue) + email send
  app/web.py             FastAPI UI: inbox, reply, reports, locations/sync admin
  app/worker.py          always-on loop: sync every N min, report once a day
  data/locations_seed.csv  the 23 sites with brand/state/Snowflake ids
  tests/                 offline tests with a fake adapter
```

## What the app does today (verified with placeholder data, 2026-10-03, second pass)

* **Inbox** – tabs for *Needs attention* (unanswered negatives first, then anything
  overdue in the last 90 days), *Unanswered*, *Mine*, *Negative*, *Last 7 days*,
  *All*; brand/site/rating filters and text search; one-click **Claim** so Lily
  and Sarah never answer the same review twice.
* **Review page** – reply composer with canned **templates** (placeholders
  `{first_name} {site} {brand} {agent}` filled in), character counter against
  Google's 4,096 limit, draft autosave, Cmd/Ctrl+Enter to post, *Post & next* to
  walk the unanswered queue, remove reply, internal note + theme tag, same-author
  history, previous/next unanswered links.
* **Reports** – 24h / 7d / 30d tiles, response rate, median and p90 response
  time, weekly charts (rating by brand, volume, response rate, response time),
  30-day rating mix, negative themes, by-site table that links to each site.
* **Site page** – per-location trend, rating mix, themes, recent reviews.
* **Listings** – source listings, mapping to sites, sync status, manual sync.
* **Templates / Admin** – manage templates, users, and morning-report recipients
  in the browser (no CLI needed day to day).
* **Morning email** – yesterday's reviews: a by-site table of 1★–5★ counts with
  totals and averages, a week-to-date (Mon–Sun) table of the same shape with no
  review text (on Mondays it covers the full week just ended), then every review
  received yesterday, grouped by site. Three editions: Illinois (WashU),
  Tennessee (ICON + Wash Associates) and Corporate (everything, grouped by
  brand). Admin → Report recipients has a paste box per edition; one address can
  be on several editions. Preview at `/reports/morning?edition=il|tn|all`.
* **Rating distribution report** (`/reports/distribution`) – reviews by star
  rating per site for any window, stacked bars plus a table, filtered by brand,
  group and site.
* **Filters are multi-select** – brands, groups, sites and ratings on the inbox;
  brands, groups and sites on every report, always with the date range.
* **Saved views and site groups** – any filter combination can be saved as a
  tab (shared with the team or private), e.g. "LW Negatives". Site groups
  (Admin → Site groups) let a regional manager filter to their stores.
* **Employee recognition** – names are detected in review text ("shout out to
  Karla", "Jared was the man") and matched to a roster (Admin → Employees).
  The Team page ranks who gets named, by site and period; review pages show
  @mentions and let anyone tag or untag a person; the `{employee}` template
  placeholder fills in automatically.
* **Smarter templates** – grouped by star band, tagged by what they fit (wait,
  billing, employee, no comment …), ranked per review with the top three
  starred, usage counts recorded when a template is posted.
* **AI drafting** – a "Draft with AI" button writes a reply with Claude using
  the house rules under Admin → AI rules (seeded with the five rules from the
  SYNC portal) and the team's own templates as style examples. Drafts land in
  the reply box for a person to read and edit; nothing posts automatically.
  Enable by setting `ANTHROPIC_API_KEY` in `.env` (model via `AI_MODEL`,
  default `claude-opus-5-5`; phones per brand via `BRAND_PHONES`).
* **Archive and Failed** – archive a review you will not answer (stops
  counting as unanswered); a Failed tab appears when a post to Google errors.
* **More reporting** – rating-only share, monthly summary table, location rank,
  replies by team member with median and p90 time and template / AI share.
* **Date ranges everywhere** – inbox, reports, site and team pages share one
  control with the SYNC-style presets (Today, Yesterday, Last 7/30 days, This/Last
  week, This/Last month, This/Last quarter, Year to date, Last 12 months, All time)
  plus a custom from/to picker. Charts bucket by day, week or month depending on
  the window. Saved views remember the range.
* **Negative review reasons** – every negative review is grouped into the 13
  categories from the weekly reviews workbook (Long Line, Wash Quality, Dryer,
  Vacuum, Damage, Billing/Cancellation, Pricing, POS, LPR/Access Issues, Customer
  Service, Closure, No Content, Unknown). Keyword rules group them on sync; with
  `ANTHROPIC_API_KEY` set, Claude does the grouping (`AI_CLASSIFY=true`, the
  default) and admins can re-group a window from the reports page or with
  `python cli.py classify-negatives --range last_month`. Reports show a site ×
  reason matrix for the selected window.
* **Inbox** shows our posted reply inline (who, when, how long after the review)
  or a "Reply to review" call to action; the Reviewer column is the customer's
  name. Site groups can be created inline from the inbox filters.
* Brand look follows the Icon Car Wash design system (tokens, Open Sans, cyan
  accent) with a text wordmark rather than the logo artwork.
* Light and dark mode follow the OS, with a toggle in the header; layout works on a phone.

## 1. Local setup (one time)

```bash
cd review_manager
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # then edit SECRET_KEY at minimum
python cli.py init-db
python cli.py seed-locations
python cli.py create-user --email lily@washucarwash.com --name "Lily"   # or skip: SSO creates agents on first sign-in
python cli.py create-user --email mhoffman@washucarwash.com --name "Mitchel" --admin
pytest -q                       # should pass with no network
python cli.py web --reload      # http://localhost:8000
```

## 2. Google API access

**Status (2026-10-02):** done through step 5 below, waiting on Google.

| Item | Value |
|------|-------|
| Cloud project | `washu-review-manager` (project number `358010811684`), owner mhoffman@washucarwash.com |
| APIs enabled | My Business Account Management API, My Business Business Information API |
| Reviews API (v4 `mybusiness.googleapis.com`) | not in the public library; Google turns it on when the request is approved |
| Access request | submitted 2026-10-02 via the GBP API access workflow, **support case 3-1378000042407**, quoted 7–10 business days. Reference profile: WashU Car Wash, 2315 South Harlem Avenue (Berwyn) |
| Check approval | Cloud Console > APIs & Services > Quotas: Google My Business API at 0 QPM = pending, 300 QPM = approved |

**Listings policy:** the account also manages the legacy Wash N' Roll profiles for the 8 Tennessee stores. Those are intentionally left untouched (the ICON profiles were started fresh). `discover-locations` records them but marks them inactive and never auto-maps them; see `LISTING_EXCLUDE_PATTERNS` in `.env.example`. Only listings mapped to a site are synced.

Original checklist for reference:

1. **Consolidate ownership.** One Google account (ideally a shared
   `reviews@washucarwash.com` style account, not a personal one) must be Owner
   or Manager on all 23 listings. Put them in one Location Group in Business
   Profile Manager. Confirm the 8 ex-WNR listings have transferred.
2. **Google Cloud project.** Create one, enable these APIs:
   *My Business Account Management API*, *My Business Business Information API*,
   and *Google My Business API* (the v4 one that serves reviews).
3. **Request Business Profile API access.** Google gates these APIs. Fill in the
   access request form linked from the API overview page using the project id;
   approval typically takes days to two weeks and raises the quota from 0.
4. **OAuth client.** APIs & Services > Credentials > *OAuth client ID* >
   application type **Desktop app**. Download the JSON as
   `review_manager/client_secret.json`. Add the shared Google account as a test
   user on the consent screen (or publish the app for internal use).
5. **Authorize once:**
   ```bash
   python cli.py google-auth           # browser opens; sign in as the shared account
   python cli.py discover-locations    # lists every listing, auto-maps to the seed sites
   ```
   Fix any `UNMAPPED` rows on the `/locations` page (admin) or with
   `python cli.py link-location --source-id N --location-id M`.
6. **Backfill all history, then incremental syncs:**
   ```bash
   python cli.py backfill
   python cli.py sync
   ```

## 3. Morning report

Recipients live in the `report_recipients` table, one row per address with the
editions it gets (`il`, `tn`, `all`, or a `;` list). The easiest way to manage
them is Admin → Report recipients, which has a paste box under each edition
(one address per line or comma-separated; `Name <email>` keeps the name). The
CLI does the same thing:

```bash
python cli.py add-recipient --email owner@washucarwash.com --name "Owner" --edition all
python cli.py add-recipient --email il-ops@washucarwash.com --name "IL Ops" --edition il
python cli.py add-recipient --email rm@iconcarwash.com --edition il --edition tn   # both regional editions
python cli.py add-recipient --email rm@iconcarwash.com --edition il --remove       # drop from one edition only
python cli.py send-report --dry-run --out /tmp/report.html         # writes report.il/.tn/.all.html
python cli.py send-report --to you@washucarwash.com --edition tn   # real send of one edition
```

Recipients of the same edition receive one email. Sending
needs the `SMTP_*` settings in `.env`. For Microsoft 365, the sending mailbox
must have SMTP AUTH enabled, or use an app password if MFA is enforced. The
worker sends once per local day at `REPORT_HOUR_LOCAL` and records each send
in `report_sends` so a restart never double-sends.

The live equivalent is `/reports`, and `/reports/morning` shows exactly what
the email looks like.

## 4. Running it for real

Two processes, one database:

```bash
python cli.py web --host 0.0.0.0 --port 8000     # UI for Lily / Sarah
python cli.py worker                             # sync + daily report
```

Recommended hosting: a small Postgres (Supabase, Neon, Render) and the two
processes on Render or Fly.io. Set `DATABASE_URL`, `SECRET_KEY`,
`APP_BASE_URL` (used for links in the email), the `SMTP_*` vars, and
`GOOGLE_TOKEN_JSON` (paste the contents of `google_token.json`). The web
process should sit behind HTTPS; the session cookie is marked `secure` when
`APP_BASE_URL` starts with https.

Quota note: the v4 reviews endpoint allows hundreds of requests per minute
once access is approved. 23 listings polled every 20 minutes is about 1% of
that, so polling is fine and Pub/Sub notifications are not needed.

## 5. Adding another review platform later

1. Create `app/sources/<platform>.py` implementing `ReviewSourceAdapter`
   (`fetch_reviews`, `post_reply`, `delete_reply`) and set `name = "<platform>"`.
2. Register it in `ADAPTERS` in `app/sources/__init__.py`.
3. Insert `review_sources` rows with `source = "<platform>"` pointing at the
   right `locations.id`. Sync, inbox, reply, and reports work unchanged.

## 6. AI drafting notes

* Uses the official `anthropic` SDK with `client.messages.create`. The SDK's
  1.x line needs Python 3.10+; on this Mac's Python 3.9 pip installs the last
  0.x release, which has the same `messages.create` surface.
* The model declining a request (`stop_reason == "refusal"`) is surfaced to the
  agent as "write it by hand"; server-side fallbacks are not wired in yet.
* Prompt = system (role, house rules, up to six matching templates as style
  examples) + user (site, brand, phone, reviewer first name, rating, detected
  employees, review text). No customer data beyond the public review is sent.

## 7. Accounts and Microsoft sign-in

**Local accounts.** Admin → Users creates or updates a user (name, email,
role, optional password). `python cli.py create-user` does the same from the
terminal. Deactivating keeps history but blocks login.

**Microsoft Entra ID SSO.** Once configured, the login page shows *Sign in
with Microsoft*. Anyone with a `washucarwash.com`, `washassociates.com` or
`iconcarwash.com` work account is signed in and, on first visit, gets an
*agent* account automatically. Roles stay in this app: promote someone to
admin on Admin → Users. Password login remains as a break-glass fallback until
you set `PASSWORD_LOGIN_ENABLED=false`.

One-time setup by whoever holds the Entra admin role (about ten minutes):

1. Azure portal → **Microsoft Entra ID → App registrations → New registration**.
   Name: `Review Manager`.
   *Supported account types*: if all three domains are verified in **one**
   tenant, choose *Accounts in this organizational directory only* and later
   set `MS_TENANT_ID` to the Directory (tenant) ID. If the brands live in
   separate tenants, choose *Accounts in any organizational directory* and
   leave `MS_TENANT_ID=organizations`.
   *Redirect URI* (type **Web**): `https://<your-app-host>/auth/microsoft/callback`.
   Add `http://localhost:8000/auth/microsoft/callback` too for local testing.
2. **Certificates & secrets → New client secret**. Copy the *Value* (not the
   id) into `MS_CLIENT_SECRET`. Note the expiry and calendar a rotation.
3. **Overview**: copy *Application (client) ID* into `MS_CLIENT_ID`.
4. **Token configuration → Add optional claim → ID → `email`** (and `upn` if
   offered). Without this, Microsoft may omit the email claim; the app then
   falls back to the sign-in name, which is normally the same address.
5. **API permissions**: the default `User.Read` (delegated) is all that is
   needed. Click *Grant admin consent* so users are not prompted. For a
   multi-tenant registration, an admin in each other tenant grants consent
   once, or users consent on first sign-in.
6. Restart the app. Optional hardening: set `SSO_ALLOWED_TENANTS` to the
   tenant ids that should be accepted.

How it works: OpenID Connect authorization-code flow via Microsoft's MSAL
library. The flow state lives in a short-lived signed cookie, the id token is
validated by MSAL, and the user is matched by Entra object id first, then by
email. The app never sees the user's password. `APP_BASE_URL` must match the
registered redirect host exactly.

## 8. Hosted demo on Render

`render.yaml` is a Render Blueprint for a free-plan demo: it installs the
app, seeds placeholder data on first start (`DEMO_MODE=true`), and serves it.
Render generates `SECRET_KEY` and `DEMO_PASSWORD`; the three demo accounts
(`admin@example.test`, `lily@example.test`, `sarah@example.test`) all use the
generated `DEMO_PASSWORD`, which you read from the service's Environment tab.

1. Push this folder to a private GitHub repository.
2. In Render: **New → Blueprint**, pick the repo, accept the defaults, Apply.
3. When the deploy finishes, open the URL Render shows. Sign in with
   `admin@example.test` and the `DEMO_PASSWORD` value from the Environment tab.

Free-plan caveats: the service sleeps after 15 minutes idle (first visit after
that takes up to a minute) and the SQLite demo data resets on every deploy or
restart. No Google token, SMTP, Microsoft, or Anthropic keys are set, so
replying, email, SSO and AI drafting are intentionally inert in the demo.

## 9. Snowflake tie-in

`locations.snowflake_location_ids` carries the old and new `location_id`
values from the Rinsed share, so review tables export cleanly for joins against
washes, signups, and churn by site. The 8 ex-WNR ICON sites have no ids yet;
fill them in once those stores are on Sonny's and flowing into the share.

## Local notes (this Mac)

* `dev_seed.py` fills the SQLite database with demo users and fake reviews so
  the UI can be exercised before Google approves API access. Demo logins are
  listed at the top of that file. Delete `review_manager.db` to start clean.
* The Claude desktop preview server is blocked by macOS privacy controls from
  reading this Desktop folder (same issue as the launchd trackers). Start the
  app from your own Terminal instead:
  ```bash
  cd "~/Desktop/Snowflake & Claude/review_manager" && .venv/bin/python cli.py web --reload
  ```
