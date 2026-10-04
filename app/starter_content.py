"""Starter content for a fresh real database: reply templates, AI house rules, site groups
and shared saved views. No reviews, users or staff (those are real data, or demo-only).

    python cli.py seed-content        # adds whatever is missing; never overwrites or duplicates
"""
from __future__ import annotations

import json
from typing import Dict

from sqlalchemy import select
from sqlalchemy.orm import Session

from .ai import DEFAULT_RULES
from .models import AiRule, Location, ReplyTemplate, SavedView, SiteGroup, User

NEW_ICON = {"ICON Madison", "ICON McMinnville", "ICON Decherd", "ICON Manchester", "ICON Goodlettsville",
            "ICON Thompson Lane", "ICON Charlotte Pike", "ICON Antioch"}

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


GROUPS = [
    # name, description, rule for which sites belong
    ("IL – WashU", "All Illinois stores", lambda n: n.startswith("WashU")),
    ("TN – ICON legacy", "Fairview, Dickson, Nolensville", lambda n: n in {"ICON Fairview", "ICON Dickson", "ICON Nolensville"}),
    ("TN – ex-WNR (new ICON profiles)", "8 stores acquired Sept 2026", lambda n: n in NEW_ICON),
    ("Nashville metro", "Antioch, Goodlettsville, Madison, Thompson Ln, Charlotte Pike, Nolensville",
     lambda n: n in {"ICON Antioch", "ICON Goodlettsville", "ICON Madison", "ICON Thompson Lane", "ICON Charlotte Pike", "ICON Nolensville"}),
]

VIEWS = [
    ("LW Negatives", 10, {"view": "negative", "range": "last_week"}),
    ("Yesterday & today", 20, {"view": "all", "days": 1}),
    ("TN unanswered", 30, {"view": "unanswered", "brands": ["ICON", "WA"]}),
]


def seed_content(s: Session, owner_email: str = "") -> Dict[str, int]:
    """Add every starter item that is missing (matched by name/text). Returns counts added."""
    out = {"templates": 0, "ai_rules": 0, "groups": 0, "views": 0}
    have = {t.name for t in s.execute(select(ReplyTemplate)).scalars()}
    for name, brand, lo, hi, tags, body, order in TEMPLATES:
        if name not in have:
            s.add(ReplyTemplate(name=name, brand=brand, min_rating=lo, max_rating=hi, tags=tags, body=body, sort_order=order))
            out["templates"] += 1
    have = {r.text for r in s.execute(select(AiRule)).scalars()}
    for i, text in enumerate(DEFAULT_RULES):
        if text not in have:
            s.add(AiRule(text=text, sort_order=(i + 1) * 10))
            out["ai_rules"] += 1
    locs = s.execute(select(Location).where(Location.active.is_(True))).scalars().all()
    have = {g.name for g in s.execute(select(SiteGroup)).scalars()}
    for name, desc, rule in GROUPS:
        members = [l for l in locs if rule(l.name)]
        if name not in have and members:
            g = SiteGroup(name=name, description=desc)
            g.locations = members
            s.add(g)
            out["groups"] += 1
    owner = None
    if owner_email:
        owner = s.execute(select(User).where(User.email == owner_email.strip().lower())).scalar_one_or_none()
    owner = owner or s.execute(select(User).where(User.role == "admin").order_by(User.id)).scalars().first()
    if owner is not None:
        have = {v.name for v in s.execute(select(SavedView)).scalars()}
        for name, order, params in VIEWS:
            if name not in have:
                s.add(SavedView(name=name, owner_id=owner.id, is_shared=True, sort_order=order, params_json=json.dumps(params)))
                out["views"] += 1
    s.flush()
    return out
