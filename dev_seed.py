#!/usr/bin/env python3
"""LOCAL DEVELOPMENT ONLY. Fills the SQLite database with realistic placeholder
data calibrated to the real SYNC numbers (Oct 2026): ~4% negative, ~99% of
reviews older than two days answered, 300-450 reviews/month network-wide,
~1.7-day response time, ~35% rating-only, employees named in many reviews.

Demo logins (local only):
  lily@example.test   / dev-password-lily-2026
  sarah@example.test  / dev-password-sarah-2026
  admin@example.test  / dev-password-admin-2026   (admin)

Re-run with --reset to wipe and regenerate.
"""
import csv
import json
import math
import os
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import selectinload  # noqa: E402

from app.ai import DEFAULT_RULES  # noqa: E402
from app.auth import hash_password  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import engine, init_db, session_scope  # noqa: E402
from app.events import record  # noqa: E402
from app.models import (AiRule, Base, Employee, Location, ReplyTemplate, ReportRecipient, Response, Review,  # noqa: E402
                        ReviewSourceLink, SavedView, SiteGroup, SyncRun, User)
from app.text_intel import apply_intel, build_roster  # noqa: E402

if "sqlite" not in settings.database_url and not os.getenv("DEMO_MODE"):
    sys.exit("refusing to seed a non-sqlite database")

RESET = "--reset" in sys.argv
MONTHS = int(os.getenv("DEMO_MONTHS", "18"))
NOW = datetime.utcnow()

# Hosted demo: set DEMO_PASSWORD in the environment so the public demo does not use the passwords printed above.
_PW = os.getenv("DEMO_PASSWORD")
DEMO_USERS = [("lily@example.test", "Lily Collins", _PW or "dev-password-lily-2026", "agent"),
              ("sarah@example.test", "Sarah Stuart", _PW or "dev-password-sarah-2026", "agent"),
              ("admin@example.test", "Mitch Hoffman", _PW or "dev-password-admin-2026", "admin")]

FIRST = ["Jordan", "Alex", "Taylor", "Casey", "Sam", "Morgan", "Riley", "Jamie", "Chris", "Pat", "Dana", "Devin",
         "Maria", "Luis", "Ana", "Mike", "Jen", "Tom", "Kim", "Ray", "Nina", "Omar", "Priya", "Derek", "Tasha", "Brandon",
         "Kimberlee", "Bradley", "Isabel", "Kent", "Nicholas", "Marissa", "Lisa", "Jeff", "Zain", "Trevor", "Debra", "Hunter"]
LAST = ["Brady", "Marshall", "Benitez", "Gray", "McClure", "Wheeler", "Street", "Bice", "Crain", "Owens", "Smith", "Campbell",
        "Johnson", "Kim", "Reza", "Turea", "Hill", "Nathaniel", "Fudge", "Arias", "Cook", "Rippy", "Freeland", "Marzullo"]

# Staff per site (first names customers actually use). Shared across brands where plausible.
STAFF = {
    "WashU Berwyn": ["Jay", "Marcus", "Elena"], "WashU Burbank": ["Gregory Banks", "Tony", "Alyssa"], "WashU Carol Stream": ["Nardo", "Kevin"],
    "WashU Des Plaines": ["Gregory Banks", "Moustafa", "Danny"], "WashU Evergreen Park": ["Justin", "Jay", "Tiana"], "WashU Joliet": ["Ellinna", "Chris P"],
    "WashU Naperville": ["Mia", "Jordan R"], "WashU Plainfield": ["Ellinna", "Omar"], "WashU Villa Park": ["Karla", "Luis"], "WashU Niles": ["Sofia", "Andre"],
    "WashU Wheaton": ["Karla", "Nardo"], "ICON Nolensville": ["Abu", "Alex", "Zach", "Britten", "Solomon"], "ICON Dickson": ["Ethan", "Cody"],
    "ICON Fairview": ["Tyler", "Grace"], "Wash Associates Jackson": ["Marcus", "Kayla"], "ICON Madison": ["Lucien", "Damon"], "ICON McMinnville": ["Caleb", "Jenna"],
    "ICON Decherd": ["Wyatt", "Hannah"], "ICON Manchester": ["Ethan", "Logan"], "ICON Goodlettsville": ["James M", "Trey"], "ICON Thompson Lane": ["Lucien", "Damon", "Jared", "Eli", "Sarah"],
    "ICON Charlotte Pike": ["Devon", "Aaliyah"], "ICON Antioch": ["Miguel", "Brianna"],
}

POSITIVE = [
    ("Wash quality", [5, 5, 5, 4], ["Car came out spotless, even the wheels.", "Best wash in the area. Truck looks brand new every time.",
                                    "Love how shiny the ceramic leaves it.", "Clean wash, no missed spots, dryers actually dry.",
                                    "Excellent car wash! Water still beads on my car after two weeks of rain."]),
    ("Staff", [5, 5, 4], ["Staff were friendly and guided me in perfectly.", "Great crew here, always a wave and a smile.",
                          "Attendant prepped my bumper without me even asking.", "Staff is always wonderful, knowledgeable, helpful, and friendly."]),
    ("Membership value", [5, 5, 4], ["Unlimited plan pays for itself in two visits.", "Members line moves fast. Worth every penny.",
                                     "Switched my membership here from another chain and so glad I did.", "Plate reader picks me up instantly, no fumbling for a tag."]),
    ("Vacuums", [5, 4, 4], ["Vacuums are strong and there are tons of stalls.", "Free vacuums with real suction, finally.", "Mat cleaner and air guns are a nice touch."]),
    ("Wait time", [5, 4, 4], ["In and out in 10 minutes on a Saturday.", "Quick and easy, no line at lunch.", "Fast service, friendly staff, car looks spotless."]),
]
# {E} = employee first name, {E2} = second employee
EMPLOYEE_POS = [
    "Shout out to {E} for being so informative on the membership deals!", "{E} was super helpful and friendly. My truck looked great.",
    "{E} is the man, fast service and great attitude.", "{E} and {E2} are the real deal! Great place.", "Thanks to {E} for explaining all of the plans so well.",
    "{E} helped me set up my plan in two minutes. Great customer service.", "Had a great experience being assisted by {E}. I forgot my card and he gladly looked me up.",
    "{E} was very respectful and nice, explained everything. First time here and I'll be back.", "Ask for {E}. Best service I've had at a car wash.",
    "Great wash and {E} went above and beyond prepping the car.", "{E} greeted us with a smile and got us through quick.",
]
NEGATIVE = [
    ("Long Line", [1, 2, 2, 3], ["Waited 25 minutes in the members lane and still had soap on the back window.",
                                 "Line wrapped around the building, one lane open. Left without a wash.", "Three cars ahead of me took 20 minutes. Something was wrong with the conveyor."]),
    ("Wash Quality", [1, 2, 3, 3], ["Still had bugs on the front bumper and dirt on the rocker panels.", "Paid for the top package and the car was barely wet in spots.",
                                    "The machine needs to be upgraded. Good for $5 though."]),
    ("Dryer", [2, 3, 3], ["Dryer left water streaks everywhere.", "Came out still wet, the blowers barely did anything."]),
    ("Pricing", [2, 3], ["Price went up again. Not worth it anymore for a basic wash.", "Too expensive compared to the place down the road."]),
    ("POS", [1, 2, 3], ["Pay station froze and charged my card twice before it printed a receipt.", "Kiosk screen would not take my card, had to back out."]),
    ("Damage", [1, 1, 2], ["Brush knocked my mirror cover loose and nobody at the exit to talk to.", "Antenna snapped off. Filed a claim and have not heard back in a week.",
                                 "A month ago I noticed a chip on my door handle after a wash. Filed a claim, still no call back."]),
    ("Billing/Cancellation", [1, 1, 2], ["Charged twice this month. Called and nobody answered.", "Cancelled my plan and was still billed the next month.",
                                           "Still being billed two months after I cancelled online."]),
    ("Vacuum", [2, 3, 3], ["Half the vacuums were broken again.", "Vacuum sucked up my mat piece and they said they'd call back. Never did."]),
    ("LPR/Access Issues", [2, 3], ["Plate reader did not recognize me and the attendant made me pay retail.", "Had to back out and re-enter twice before the gate opened."]),
    ("Customer Service", [1, 2, 3], ["Attendant was on his phone and waved me through without prepping.", "Rude at the pay station when I asked about the plans."]),
    ("Closure", [2, 3], ["Drove over at 7:30 and it was closed with no sign.", "Closed for weather but the website said open."]),
]
REPLY_POS = ["Hi {name}, thank you for the kind words and the 5 stars! We're looking forward to your next visit!",
             "We're happy you enjoyed your visit, {name}! Thanks so much for the 5 stars.",
             "Hi {name}, thank you for taking the time to leave a review! We're glad to hear you had a great experience.",
             "Thank you for the 5 stars, {name}! We're glad to hear you had a great experience."]
REPLY_EMP = ["Hi {name}, thank you so much for the great review! We're thrilled to hear {emp} made your visit such a great experience.",
             "Hi {name}, thanks for the great review! We're happy to hear {emp} gave you such great service.",
             "Hi {name}, thank you for the wonderful feedback! We're glad {emp} was helpful. See you next time!"]
REPLY_NEG = ["{name}, we're sorry about that. That's not the experience we want at {site}. Please reach out to us at support@washucarwash.com so we can make it right.",
             "Thanks for letting us know, {name}. We've shared this with the {site} manager and would like to follow up with you directly.",
             "We apologize, {name}. Please send us your plate or membership details so we can look into this right away."]

# Site tweaks: (negative share, monthly volume) calibrated to SYNC Jul 2026 distribution.
SITE_PROFILE = {
    "WashU Berwyn": (0.035, 80), "WashU Villa Park": (0.03, 65), "WashU Des Plaines": (0.07, 55), "WashU Niles": (0.09, 45),
    "WashU Burbank": (0.06, 32), "WashU Evergreen Park": (0.03, 24), "Wash Associates Jackson": (0.05, 22), "WashU Joliet": (0.045, 22),
    "WashU Carol Stream": (0.05, 18), "ICON Fairview": (0.04, 18), "WashU Naperville": (0.06, 16), "ICON Dickson": (0.05, 12),
    "WashU Wheaton": (0.03, 12), "WashU Plainfield": (0.09, 11), "ICON Nolensville": (0.035, 10),
    "ICON Madison": (0.02, 14), "ICON McMinnville": (0.02, 12), "ICON Decherd": (0.03, 12), "ICON Manchester": (0.02, 9),
    "ICON Goodlettsville": (0.02, 16), "ICON Thompson Lane": (0.02, 22), "ICON Charlotte Pike": (0.03, 12), "ICON Antioch": (0.02, 14),
}
NEW_ICON = {"ICON Madison", "ICON McMinnville", "ICON Decherd", "ICON Manchester", "ICON Goodlettsville", "ICON Thompson Lane", "ICON Charlotte Pike", "ICON Antioch"}
MENTION_RATE = {"WashU": 0.14, "ICON": 0.32, "WA": 0.18}      # share of written reviews that name staff
RATING_ONLY = {"WashU": 0.38, "ICON": 0.30, "WA": 0.35}

TEMPLATES = [
    # name, brand, lo, hi, tags, body, order
    ("5-Star – General", None, 5, 5, "general", "Hi {first_name}, thank you for the kind words and the 5 stars! We're looking forward to your next visit!", 10),
    ("5-Star – General (short)", None, 5, 5, "general", "We're happy you enjoyed your visit, {first_name}! Thanks so much for the 5 stars.", 11),
    ("5-Star – Rating only", None, 5, 5, "no_comment;general", "Thank you for the 5 stars, {first_name}! We're glad to hear you had a great experience.", 12),
    ("5-Star – Team mention", None, 5, 5, "team", "Hi {first_name}, thank you for your kind words! We're thrilled to hear our team makes your visits enjoyable. We look forward to seeing you again soon!", 20),
    ("5-Star – Employee mention", None, 5, 5, "employee", "Hi {first_name}, thank you so much for the great review! We're thrilled to hear {employee} made your visit such a great experience. We look forward to seeing you again soon!", 21),
    ("5-Star – Employee mention (short)", None, 5, 5, "employee", "Hi {first_name}, thanks for the great review! We're happy to hear {employee} gave you such great service.", 22),
    ("5-Star – Amenities mention", None, 5, 5, "amenities;vacuums", "Hi {first_name}, thank you for the kind words! We're glad you enjoyed the wash, our friendly staff, and the free vacuums. We look forward to seeing you again soon!", 23),
    ("5-Star – Membership mention", None, 5, 5, "membership", "Hi {first_name}, thank you! We're glad the unlimited plan is working out for you. See you on your next wash!", 24),
    ("4-Star – General", None, 4, 4, "general", "Hi {first_name}, thank you for your feedback and the 4 stars! We're always striving to make your next experience even better.", 30),
    ("4-Star – Rating only", None, 4, 4, "no_comment;general", "Hi {first_name}, thank you for the 4-star rating! We're glad you had a good experience, and we'd love to hear what we can do to earn that fifth star next time.", 31),
    ("4-Star – Mixed feedback", None, 4, 4, "mixed;quality;wait", "Hi {first_name}, thank you for your feedback! We're glad you enjoyed your visit overall, and we appreciate you letting us know what fell short. We'll share it with the {site} team as we keep improving.", 32),
    ("Sorry – Wait time", None, 1, 3, "wait", "{first_name}, we're sorry about the wait at {site}. That's not the experience we want for you. We're looking at staffing and lane flow during peak hours. Please email support@washucarwash.com with your visit time and we'll make it right.", 40),
    ("Sorry – Wash quality", None, 1, 3, "quality", "We apologize, {first_name}. A rewash is on us. Just let the attendant at {site} know, or reply with a good time to come by and we'll have it ready.", 41),
    ("Sorry – Damage claim", None, 1, 3, "damage", "{first_name}, we're sorry to hear this. Please email support@washucarwash.com with your plate, visit date and photos so our team can open a claim and follow up within one business day.", 42),
    ("Sorry – Billing", None, 1, 3, "billing", "{first_name}, we're sorry about the billing trouble. Please send your plate or membership barcode to support@washucarwash.com and we'll review the charges and refund anything that shouldn't be there.", 43),
    ("Sorry – Vacuums / amenities", None, 1, 3, "vacuums;amenities", "{first_name}, thanks for flagging the vacuums at {site}. We've passed it to the site lead to get them checked today. We're sorry for the hassle.", 44),
    ("Sorry – Staff", None, 1, 3, "staff", "{first_name}, we're sorry about how you were treated at {site}. That's not our standard. The site manager has been looped in, and we'd like to follow up directly at support@washucarwash.com.", 45),
    ("Sorry – Plate / gate", None, 1, 3, "plate", "{first_name}, sorry the plate reader gave you trouble at {site}. Please send your plate number to support@washucarwash.com and we'll fix it on your account and credit any retail charge.", 46),
    ("Sorry – Rating only", None, 1, 3, "no_comment", "{first_name}, we're sorry your visit to {site} fell short. We'd like to understand what happened. Please reach out at support@washucarwash.com.", 47),
    ("Sorry – General", None, 1, 3, "general", "{first_name}, thank you for the honest feedback. We've shared it with the {site} manager and would like to follow up with you directly. Please reach out at support@washucarwash.com.", 49),
    ("ICON welcome (new site)", "ICON", 4, 5, "general", "Thank you, {first_name}! We're excited to be the new team at {site} and glad the first impression was a good one.", 60),
]


def lognormal_hours(median, sigma=0.8):
    return math.exp(math.log(median) + random.gauss(0, sigma))


def seasonal(month):
    return {12: 1.3, 1: 1.3, 2: 1.4, 3: 1.25, 4: 1.1, 5: 1.1, 6: 1.0, 7: 1.05, 8: 1.05, 9: 0.9, 10: 0.95, 11: 0.9}[month]


def main():
    if RESET:
        Base.metadata.drop_all(engine)
    init_db()
    random.seed(7)
    with session_scope() as s:
        for email, name, pw, role in DEMO_USERS:
            if not s.execute(select(User).where(User.email == email)).scalar_one_or_none():
                s.add(User(email=email, name=name, password_hash=hash_password(pw), role=role))
        s.flush()
        users = {u.name.split()[0]: u for u in s.execute(select(User)).scalars().all()}
        admin = users["Mitch"]
        if not s.execute(select(ReportRecipient)).first():
            s.add(ReportRecipient(email="owner@example.test", name="Owner", edition="all"))
            s.add(ReportRecipient(email="il-ops@example.test", name="IL Ops", edition="il", brands="WashU"))
            s.add(ReportRecipient(email="tn-ops@example.test", name="TN Ops", edition="tn", brands="ICON;WA"))
            s.add(ReportRecipient(email="lily@example.test", name="Lily Collins", edition="alerts"))
        if not s.execute(select(ReplyTemplate)).first():
            for name, brand, lo, hi, tags, body, order in TEMPLATES:
                s.add(ReplyTemplate(name=name, brand=brand, min_rating=lo, max_rating=hi, tags=tags, body=body, sort_order=order,
                                    usage_count=random.choice([0, 1, 2, 3, 6, 9, 11, 12, 13]) if lo >= 4 else random.choice([0, 1, 2]),
                                    updated_by_id=users["Lily"].id))
        if not s.execute(select(AiRule)).first():
            for i, text in enumerate(DEFAULT_RULES):
                s.add(AiRule(text=text, sort_order=(i + 1) * 10))
        locs = s.execute(select(Location).where(Location.active.is_(True))).scalars().all()
        if not locs:
            with open(Path(__file__).resolve().parent / "data" / "locations_seed.csv", newline="") as fh:
                for row in csv.DictReader(fh):
                    s.add(Location(name=row["name"], brand=row["brand"], state=row["state"], city=row.get("city") or None,
                                   snowflake_location_ids=row.get("snowflake_location_ids") or None))
            s.flush()
            locs = s.execute(select(Location).where(Location.active.is_(True))).scalars().all()
        by_name = {l.name: l for l in locs}
        if not s.execute(select(SiteGroup)).first():
            def grp(name, desc, names):
                g = SiteGroup(name=name, description=desc)
                g.locations = [by_name[n] for n in names if n in by_name]
                s.add(g)
            grp("IL – WashU", "All Illinois stores", [n for n in by_name if n.startswith("WashU")])
            grp("TN – ICON legacy", "Fairview, Dickson, Nolensville", ["ICON Fairview", "ICON Dickson", "ICON Nolensville"])
            grp("TN – ex-WNR (new ICON profiles)", "8 stores acquired Sept 2026", sorted(NEW_ICON))
            grp("Julia's sites", "Regional manager group", ["WashU Berwyn", "WashU Burbank", "WashU Evergreen Park", "WashU Villa Park"])
            grp("Nashville metro", "Antioch, Goodlettsville, Madison, Thompson Ln, Charlotte Pike, Nolensville", ["ICON Antioch", "ICON Goodlettsville", "ICON Madison", "ICON Thompson Lane", "ICON Charlotte Pike", "ICON Nolensville"])
        if not s.execute(select(Employee)).first():
            for site, names in STAFF.items():
                for n in names:
                    s.add(Employee(name=n, location_id=by_name[site].id if site in by_name else None))
        s.flush()
        if not s.execute(select(SavedView)).first():
            s.add(SavedView(name="LW Negatives", owner_id=admin.id, is_shared=True, sort_order=10, params_json=json.dumps({"view": "negative", "days": 7})))
            s.add(SavedView(name="Yesterday & today", owner_id=admin.id, is_shared=True, sort_order=20, params_json=json.dumps({"view": "all", "days": 1})))
            s.add(SavedView(name="TN unanswered", owner_id=admin.id, is_shared=True, sort_order=30, params_json=json.dumps({"view": "unanswered", "brand": "ICON"})))
            s.add(SavedView(name="Rating-only backlog", owner_id=users["Lily"].id, is_shared=False, sort_order=40, params_json=json.dumps({"view": "unanswered", "rating": "5"})))

        roster = build_roster(s)
        emps = {e.name: e.id for e in s.execute(select(Employee)).scalars().all()}
        n = 0
        tpl_rows = s.execute(select(ReplyTemplate).where(ReplyTemplate.active.is_(True))).scalars().all()
        recent_rows = []
        for loc in locs:
            link = s.execute(select(ReviewSourceLink).where(ReviewSourceLink.source == "google",
                             ReviewSourceLink.external_location_id == f"demo-{loc.id}")).scalar_one_or_none()
            if link is None:
                link = ReviewSourceLink(location_id=loc.id, source="google", external_account_id="demo",
                                        external_location_id=f"demo-{loc.id}", display_name=f"{'Icon' if loc.brand=='ICON' else loc.brand} Car Wash - {loc.city}",
                                        address=f"{loc.city}, {loc.state}", listing_url="https://maps.google.com/?cid=demo",
                                        last_synced_at=NOW - timedelta(minutes=random.randint(3, 25)), last_sync_status="ok", active=True)
                s.add(link); s.flush()
            if s.execute(select(Review).where(Review.source_link_id == link.id)).first():
                continue

            neg_share, base_month = SITE_PROFILE.get(loc.name, (0.04, 15))
            start = NOW - timedelta(days=30 * MONTHS)
            if loc.name in NEW_ICON:
                start = datetime(2026, 8, 12)
            staff = STAFF.get(loc.name, ["the team"])
            day = start
            ratings_all = []
            k = 0
            while day < NOW:
                monthly = base_month * seasonal(day.month)
                if loc.name == "Wash Associates Jackson" and day.year == 2026:
                    monthly *= 0.75
                per_day = monthly / 30
                expected = max(0.0, random.gauss(per_day, per_day * 0.6))
                todays = int(expected) + (1 if random.random() < expected - int(expected) else 0)
                for _ in range(todays):
                    ns = neg_share * (1.3 if day.month in (12, 1, 2) else 1.0)
                    if loc.name == "WashU Burbank" and day >= datetime(2026, 7, 1):
                        ns = 0.11
                    is_neg = random.random() < ns
                    hour = random.choices(range(7, 21), weights=[2, 4, 6, 8, 9, 9, 8, 8, 8, 8, 7, 6, 4, 2])[0]
                    created = day.replace(hour=hour, minute=random.randint(0, 59), second=random.randint(0, 59))
                    if created > NOW:
                        continue
                    age_h = (NOW - created).total_seconds() / 3600
                    anon = random.random() < 0.04
                    author = None if anon else (f"{random.choice(FIRST)} {random.choice(LAST)}" if random.random() < 0.7 else random.choice(FIRST))
                    first = author.split()[0] if author else "there"
                    emp_names = []
                    theme = None
                    if is_neg:
                        theme, rpool, texts = random.choice(NEGATIVE)
                        if loc.name == "WashU Burbank" and day >= datetime(2026, 7, 1) and random.random() < 0.5:
                            theme, rpool, texts = NEGATIVE[0]
                        rating = random.choice(rpool)
                        text = random.choice(texts) if random.random() < 0.9 else None
                    else:
                        rating = random.choices([5, 4], weights=[88, 12])[0]
                        if random.random() < RATING_ONLY[loc.brand]:
                            text = None
                        elif random.random() < MENTION_RATE[loc.brand] and staff and staff != ["the team"]:
                            emp_names = random.sample(staff, k=min(len(staff), random.choice([1, 1, 1, 2])))
                            tpl = random.choice(EMPLOYEE_POS)
                            if "{E2}" in tpl and len(emp_names) < 2:
                                tpl = EMPLOYEE_POS[0]
                            text = tpl.format(E=emp_names[0].split()[0] if " " in emp_names[0] and emp_names[0] != "Gregory Banks" else emp_names[0],
                                              E2=emp_names[1] if len(emp_names) > 1 else "")
                        else:
                            _, rpool, texts = random.choice(POSITIVE)
                            text = random.choice(texts)
                    # reply behaviour calibrated to ~99% answered after 2 days, 1.7-day avg
                    if age_h > 24 * 30:
                        replied = random.random() < 0.995
                    elif age_h > 60:
                        replied = random.random() < 0.985
                    elif age_h > 24:
                        replied = random.random() < 0.55
                    else:
                        replied = random.random() < 0.2
                    delay_h = lognormal_hours(26 if rating <= 3 else 34, 0.7)
                    if replied and age_h < delay_h:
                        replied = False
                    reply_at = created + timedelta(hours=delay_h) if replied else None
                    if replied:
                        if is_neg:
                            reply_text = random.choice(REPLY_NEG).format(name=first, site=loc.name)
                        elif emp_names:
                            reply_text = random.choice(REPLY_EMP).format(name=first, emp=" and ".join(e.split()[0] if e != "Gregory Banks" else "Gregory" for e in emp_names))
                        else:
                            reply_text = random.choice(REPLY_POS).format(name=first)
                    else:
                        reply_text = None
                    assigned = None
                    if not replied and age_h < 96 and random.random() < 0.3:
                        assigned = random.choice([users["Lily"], users["Sarah"]])
                    r = Review(source_link_id=link.id, source="google", external_id=f"demo-{loc.id}-{k}", author_name=author,
                               author_is_anonymous=anon, rating=rating, text=text, created_at_source=created, updated_at_source=created if random.random() > 0.03 else created + timedelta(days=random.randint(1, 20)),
                               has_owner_reply=replied, owner_reply_text=reply_text, owner_reply_updated_at=reply_at, first_replied_at=reply_at,
                               category=(theme if text else "No Content") if is_neg else None, raw_json="{}",
                               first_seen_at=created, last_seen_at=NOW,
                               assigned_to_id=assigned.id if assigned else None, assigned_at=NOW - timedelta(hours=random.random() * 6) if assigned else None)
                    s.add(r)
                    s.flush()
                    apply_intel(s, r, roster, emps)
                    # record who replied (for the responder report) on recent replies
                    if replied and age_h < 24 * 45:
                        who = users["Lily"] if random.random() < 0.65 else users["Sarah"]
                        tpl = random.choice(tpl_rows) if (tpl_rows and random.random() < 0.6) else None
                        ai = tpl is None and random.random() < 0.3
                        s.add(Response(review_id=r.id, text=reply_text, status="posted", created_by_id=who.id, created_at=reply_at, posted_at=reply_at,
                                       template_id=tpl.id if tpl else None, ai_generated=ai))
                        record(s, r, "reply_posted", actor_id=who.id, at=reply_at, text=reply_text, template=bool(tpl), ai=ai)
                        if random.random() < 0.06:   # an edit a little later
                            edit_at = reply_at + timedelta(hours=random.uniform(1, 30))
                            if edit_at < NOW:
                                r.owner_reply_updated_at = edit_at
                                record(s, r, "reply_edited", actor_id=who.id, at=edit_at, text=reply_text)
                    recent_rows.append(r)
                    ratings_all.append(rating)
                    k += 1
                    n += 1
                day += timedelta(days=1)
            # Demo texture: a few rating changes (both ways), a couple of reviews that vanished, one reported review.
            recent = [x for x in recent_rows if (NOW - x.created_at_source).days < 60 and x.rating]
            random.shuffle(recent)
            for x in recent[:2]:
                if x.rating <= 2 and x.has_owner_reply and x.first_replied_at:
                    new_rating = random.choice([4, 5]); at = x.first_replied_at + timedelta(days=random.uniform(1, 6))
                elif x.rating >= 4:
                    new_rating = random.choice([1, 2]); at = x.created_at_source + timedelta(days=random.uniform(2, 20))
                else:
                    continue
                if at >= NOW:
                    continue
                record(s, x, "rating_changed", at=at, **{"from": x.rating, "to": new_rating})
                x.prev_rating, x.rating, x.rating_changed_at, x.updated_at_source = x.rating, new_rating, at, at
                if new_rating <= 3 and not x.category:
                    x.category, x.category_source = "Unknown", "keyword"
            if random.random() < 0.35 and len(recent) > 4:
                gone = recent[3]
                gone.is_deleted, gone.removed_at = True, NOW - timedelta(days=random.uniform(0.2, 12))
                record(s, gone, "removed", at=gone.removed_at, rating=gone.rating, text=(gone.text or "")[:500])
                gone.removal_notified_at = gone.removed_at + timedelta(minutes=9)
                record(s, gone, "alert_sent", at=gone.removal_notified_at, what="removed", to=1)
            if loc.name in ("WashU Plainfield", "ICON Thompson Lane", "WashU Des Plaines") and len(recent) > 5:
                bad = next((x for x in recent if x.rating == 1 and x.text), None)
                if bad is not None:
                    bad.report_status, bad.reported_at, bad.reported_by_id = "reported", NOW - timedelta(days=2), users["Mitch"].id
                    bad.report_note = "Not a customer: no plate or visit on the day described"
                    record(s, bad, "reported", actor_id=users["Mitch"].id, at=bad.reported_at, note=bad.report_note)
            recent_rows.clear()
            prior_n = 0 if loc.name in NEW_ICON else random.randint(300, 1500)
            prior_avg = random.uniform(4.3, 4.8)
            tot = prior_n + len(ratings_all)
            link.total_review_count = tot
            link.avg_rating = round((prior_n * prior_avg + sum(ratings_all)) / tot, 1) if tot else None
            s.add(SyncRun(source_link_id=link.id, started_at=NOW - timedelta(minutes=12), finished_at=NOW - timedelta(minutes=11, seconds=40),
                          status="ok", reviews_seen=min(50, len(ratings_all)), reviews_new=random.randint(0, 3), reviews_updated=random.randint(0, 2)))
        print(f"seeded {n} demo reviews across {len(locs)} sites into {settings.database_url}")


if __name__ == "__main__":
    main()
