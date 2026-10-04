"""Lightweight text intelligence: employee-name detection and template suggestion.
Pure Python, no model calls, so it runs on every sync."""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .models import ReplyTemplate, Review

# Words that look like names (capitalised) but are not people.
STOP = {
    "i", "the", "a", "an", "and", "or", "but", "so", "if", "my", "me", "we", "our", "us", "you", "your", "they",
    "it", "he", "she", "his", "her", "this", "that", "these", "those", "there", "here", "then", "when", "what",
    "who", "how", "why", "very", "also", "always", "never", "great", "good", "best", "nice", "love", "loved",
    "car", "wash", "washu", "icon", "wash associates", "google", "membership", "member", "unlimited", "express",
    "clean", "protect", "ushine", "shine", "plus", "premium", "super", "platinum", "ceramic", "family", "vip",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "january", "february", "march",
    "april", "may", "june", "july", "august", "september", "october", "november", "december", "today", "yesterday",
    "thanks", "thank", "mr", "mrs", "ms", "miss", "sir", "staff", "team", "crew", "manager", "attendant", "guy",
    "guys", "lady", "man", "woman", "customer", "service", "wow", "ok", "okay", "amazing", "awesome", "highly",
    "recommend", "definitely", "shout", "out", "big", "little", "new", "first", "every", "time", "place",
    "location", "berwyn", "burbank", "niles", "wheaton", "joliet", "plainfield", "naperville", "evergreen",
    "park", "villa", "carol", "stream", "des", "plaines", "jackson", "dickson", "fairview", "nolensville",
    "madison", "mcminnville", "decherd", "manchester", "goodlettsville", "antioch", "thompson", "lane",
    "charlotte", "pike", "nashville", "tn", "il", "illinois", "tennessee", "chicago", "rivergate", "gallatin",
    "hwy", "rd", "st", "ave", "blvd", "lol", "omg", "ps", "update", "edit", "pros", "cons", "tldr",
    # common sentence starters / domain nouns that precede was/were/is
    "everything", "everyone", "nothing", "nobody", "someone", "something", "overall", "honestly", "place", "price",
    "prices", "quality", "truck", "line", "wait", "management", "owner", "gate", "machine", "vacuum", "vacuums",
    "plan", "experience", "visit", "excellent", "fast", "quick", "worst", "bad", "terrible", "horrible", "water",
    "dryer", "pretty", "solid", "recently", "went", "had", "got", "took", "tried", "been", "still", "paid", "drove",
    "closed", "charged", "cancelled", "canceled", "brush", "antenna", "half", "plate", "three", "two", "one", "in",
    "members", "free", "mat", "switched", "give", "definitely", "absolutely", "really", "super", "will", "would",
    "came", "left", "waited", "filed", "called", "tried", "love", "loved", "great", "glad", "happy", "wish", "hope",
    "cars", "wheels", "tires", "windows", "mirror", "door", "bumper", "soap", "wax", "ceramic", "rain", "snow", "salt",
}
# Patterns that strongly indicate a person follows / precedes.
CUES_BEFORE = r"(?i:shout[- ]?out to|thanks to|thank you to|thanks|thank you|kudos to|props to|ask for|by|with|from|named|name is|employee|attendant|manager|rep|associate|cashier|worker|guy|gentleman|young man|lady|girl|woman|man)\s+"
CUES_AFTER = r"\s+(?i:was|were|is|did|helped|help|assisted|took|made|went|explained|greeted|gave|got|showed|set|signed|walked|hooked|is the|are the|and the|at the|from the|@|!)"
STRONG_AFTER = r"\s+(?i:was|were|helped|assisted|explained|greeted|hooked|took care|went above|is the man|is awesome|is great|rocks|deserves)"
NAME = r"([A-Z][a-z]{1,15}(?:\s+[A-Z](?:[a-z]{1,15}|\.)?)?)"
PAIR = NAME + r"\s+(?:and|&)\s+" + NAME + r"\b"


def _clean(tok: str) -> str:
    return tok.strip(" .,!?:;'\"()[]").strip()


def _candidate_ok(name: str, author: Optional[str]) -> bool:
    parts = name.split()
    if not parts or any(p.lower().strip(".") in STOP for p in parts):
        return False
    if author:
        a = {x.lower().strip(".") for x in author.split()}
        if parts[0].lower().strip(".") in a:
            return False
    return 2 <= len(parts[0]) <= 16


def detect_mentions(text: Optional[str], author_name: Optional[str] = None,
                    roster: Optional[Dict[str, str]] = None) -> List[Tuple[str, Optional[str]]]:
    """Return [(display_name, roster_canonical_or_None)].

    roster: lower-cased alias -> canonical employee name. Roster matches are
    accepted anywhere in the text; other names need a capital letter plus a cue
    (\"shout out to Karla\", \"Jared was the man\")."""
    if not text:
        return []
    found: Dict[str, Optional[str]] = {}
    low = text.lower()
    if roster:
        for alias, canonical in roster.items():
            if re.search(r"\b" + re.escape(alias) + r"\b", low):
                found[canonical] = canonical
    # cue-based detection for unknown names
    for m in re.finditer(CUES_BEFORE + NAME, text):
        c = _clean(m.group(1))
        if _candidate_ok(c, author_name):
            found.setdefault(c, None)
    for m in re.finditer(NAME + CUES_AFTER, text):
        c = _clean(m.group(1))
        sentence_start = m.start() == 0 or text[max(0, m.start() - 2):m.start()].strip() in (".", "!", "?")
        strong = re.match(NAME + STRONG_AFTER, text[m.start():]) is not None
        parts = c.split()
        if len(parts) == 2 and not _candidate_ok(c, author_name) and _candidate_ok(parts[1], author_name) and parts[0].lower() in STOP:
            # "And Zach was ..." -> the first word is a connector, keep the real name
            c, sentence_start = parts[1], False
        if _candidate_ok(c, author_name) and (not sentence_start or len(c.split()) == 2 or strong):
            found.setdefault(c, None)
    # "Lucien and Damon were ..." / "Abu & Alex are the real deal"
    for m in re.finditer(PAIR, text):
        a, b = _clean(m.group(1)), _clean(m.group(2))
        tail = text[m.end():m.end() + 40]
        if re.match(r"\s*(?:were|are|was|is|helped|made|both|did|took|rock|deserve|the real deal|always)", tail) or re.search(CUES_BEFORE + r"$", text[:m.start()] + " "):
            if _candidate_ok(a, author_name):
                found.setdefault(a, None)
            if _candidate_ok(b, author_name):
                found.setdefault(b, None)
    # "Thanks to Jared and Eli for the help" -> second name after a cue-led pair
    for m in re.finditer(CUES_BEFORE + PAIR, text):
        for g in (m.group(1), m.group(2)):
            c = _clean(g)
            if _candidate_ok(c, author_name):
                found.setdefault(c, None)
    # fold unknown names that are actually roster first names in different case
    if roster:
        for k in list(found):
            if found[k] is None and k.lower() in roster:
                found[roster[k.lower()]] = roster[k.lower()]
                del found[k]
    return sorted(found.items(), key=lambda kv: kv[0])


# ----------------------------------------------------------------------------- template suggestion
KEYWORDS = {
    "wait": ["wait", "line", "lane", "slow", "minutes", "backed up", "conveyor", "stuck", "forever", "took 20", "took 30"],
    "quality": ["spot", "dirty", "soap", "missed", "not clean", "still had", "bugs", "bird", "salt", "film", "rinse"],
    "dryer": ["dryer", "dry ", "streak", "streaks", "water spots", "still wet", "dripping", "blower"],
    "damage": ["scratch", "damage", "broke", "broken", "mirror", "antenna", "chip", "dent", "claim", "cracked", "trim", "wiper"],
    "billing": ["charged", "charge", "bill", "billed", "refund", "cancel", "cancelled", "canceled", "membership fee", "double", "auto renew", "autorenew"],
    "pricing": ["price", "prices", "expensive", "overpriced", "too much", "cost", "went up", "increase", "raised"],
    "pos": ["pay station", "kiosk", "card reader", "credit card", "declined", "receipt", "machine", "touch screen", "screen", "terminal"],
    "staff": ["rude", "attitude", "ignored", "unprofessional", "phone", "nobody", "no one", "manager", "customer service", "unhelpful"],
    "plate": ["plate", "license", "gate", "reader", "recognize", "recognise", "scan", "tag", "rfid", "wouldn't open", "didn't open", "arm"],
    "hours": ["closed", "hours", "open", "weather", "shut", "out of order", "down"],
    "vacuums": ["vacuum", "vacuums", "suction", "hose", "mat", "air gun", "towel", "towels"],
    "amenities": ["vacuum", "vacuums", "towel", "towels", "mat cleaner", "air gun", "free", "amenities", "ceramic", "tire shine", "wax"],
    "team": ["staff", "team", "crew", "guys", "employees", "everyone", "workers", "people"],
    "membership": ["membership", "unlimited", "plan", "monthly", "member"],
}


# The 13 negative-reason categories used in the weekly reviews workbook. Keep the names exact.
THEMES = ["Long Line", "Wash Quality", "Dryer", "Vacuum", "Damage", "Billing/Cancellation", "Pricing", "POS",
          "LPR/Access Issues", "Customer Service", "Closure", "No Content", "Unknown"]
_KEY_TO_THEME = {"wait": "Long Line", "quality": "Wash Quality", "dryer": "Dryer", "vacuums": "Vacuum", "damage": "Damage",
                 "billing": "Billing/Cancellation", "pricing": "Pricing", "pos": "POS", "plate": "LPR/Access Issues",
                 "staff": "Customer Service", "hours": "Closure"}
# Precedence when several keyword groups hit (specific before generic).
_THEME_PRIORITY = ["damage", "billing", "plate", "pos", "pricing", "dryer", "wait", "vacuums", "hours", "quality", "staff"]


def classify_theme(text: Optional[str]) -> Optional[str]:
    """Keyword classifier into the workbook categories. Rating-only -> No Content; no hit -> Unknown."""
    if not text or not text.strip():
        return "No Content"
    low = text.lower()
    scores = {k: sum(1 for w in KEYWORDS[k] if w in low) for k in _THEME_PRIORITY}
    best_n = max(scores.values())
    if best_n == 0:
        return "Unknown"
    for k in _THEME_PRIORITY:          # ties resolve by priority order
        if scores[k] == best_n:
            return _KEY_TO_THEME[k]
    return "Unknown"


def suggest_templates(review: Review, templates: Sequence[ReplyTemplate], mention_names: Sequence[str] = ()) -> List[Tuple[ReplyTemplate, int]]:
    """Rank applicable templates for this review. Higher score = better fit."""
    low = (review.text or "").lower()
    hits: Set[str] = {k for k, words in KEYWORDS.items() if any(w in low for w in words)}
    if mention_names:
        hits.add("employee")
    if review.is_rating_only:
        hits.add("no_comment")
    ranked: List[Tuple[ReplyTemplate, int]] = []
    for t in templates:
        if not t.applies_to(review):
            continue
        tags = set(t.tag_list())
        score = 0
        if "employee" in tags:
            score += 6 if "employee" in hits else -4
        if "no_comment" in tags:
            score += 6 if "no_comment" in hits else -6
        elif "no_comment" in hits and "general" in tags:
            score += 2
        for tag in ("wait", "quality", "damage", "billing", "staff", "plate", "hours", "vacuums", "amenities", "team", "membership"):
            if tag in tags:
                score += 4 if tag in hits else -1
        if "general" in tags:
            score += 1
        if review.rating is not None and t.min_rating == t.max_rating == review.rating:
            score += 1
        score += min(t.usage_count or 0, 20) // 5   # mild preference for proven templates
        ranked.append((t, score))
    ranked.sort(key=lambda ts: (-ts[1], ts[0].sort_order, ts[0].name))
    return ranked


# ----------------------------------------------------------------------------- persistence helpers
def build_roster(session) -> Dict[str, str]:
    """lower-cased alias -> canonical employee name, for active roster employees."""
    from sqlalchemy import select
    from .models import Employee
    roster: Dict[str, str] = {}
    for e in session.execute(select(Employee).where(Employee.active.is_(True))).scalars().all():
        for n in e.all_names():
            if len(n) >= 3:
                roster[n.lower()] = e.name
    return roster


def apply_intel(session, review: Review, roster: Optional[Dict[str, str]] = None, employees_by_name: Optional[Dict[str, int]] = None) -> int:
    """Recompute auto mentions (keeps manual ones) and fill a missing negative theme.
    Returns the number of mentions now on the review."""
    from sqlalchemy import select
    from .models import Employee, ReviewMention
    if roster is None:
        roster = build_roster(session)
    if employees_by_name is None:
        employees_by_name = {e.name: e.id for e in session.execute(select(Employee)).scalars().all()}
    detected = detect_mentions(review.text, review.author_name, roster)
    existing = {m.name: m for m in review.mentions}
    for name, canonical in detected:
        display = canonical or name
        if display in existing:
            m = existing[display]
            if canonical and m.employee_id is None:
                m.employee_id = employees_by_name.get(canonical)
            continue
        session.add(ReviewMention(review=review, name=display, source="auto",
                                  employee_id=employees_by_name.get(canonical) if canonical else None))
    detected_names = {(c or n) for n, c in detected}
    for name, m in existing.items():
        if m.source == "auto" and name not in detected_names:
            session.delete(m)
    if review.category is None and review.rating is not None and review.rating <= 3:
        review.category = classify_theme(review.text)
    session.flush()
    return len(detected_names | {n for n, m in existing.items() if m.source != "auto"})
