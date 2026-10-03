"""AI reply drafting with Claude. The agent always reviews before posting.

Uses the official Anthropic SDK. The key comes from ANTHROPIC_API_KEY (or an
`ant auth login` profile); when no key is configured the UI hides the button.
"""
from __future__ import annotations

import logging
from typing import List, Optional, Sequence

from .config import settings
from .models import AiRule, ReplyTemplate, Review

log = logging.getLogger(__name__)

DEFAULT_RULES = [
    "Incorporate important phrases from the review if present into the response.",
    "If the review includes a business-related question, avoid answering it directly and instead guide the customer to contact the business. Do not ask the customer to reach out if the review is partially positive or rated above 3.",
    "For negative reviews (rating below 3), include the business phone number or contact details if necessary.",
    "If the review has mixed sentiment, address negative points first, then positive points.",
    "Keep all responses under 25 words. Remove unnecessary adjectives, adverbs, and pleasantries. Avoid greetings and valedictions.",
]


class AiUnavailable(RuntimeError):
    pass


def build_prompt(review: Review, rules: Sequence[str], examples: Sequence[ReplyTemplate], agent_name: str = "") -> tuple:
    loc = review.location
    brand = loc.brand if loc else ""
    site = loc.name if loc else "our location"
    phone = settings.brand_phones.get(brand, "")
    mentions = ", ".join(review.mention_names) if review.mentions else ""
    system = (
        f"You write owner replies to Google reviews for {brand or 'a'} car wash locations. "
        "Reply as the business, in plain, warm, specific English. Never invent facts, offers, refunds or promises. "
        "Never include placeholders or brackets. Output only the reply text.\n\nHouse rules:\n"
        + "\n".join(f"- {r}" for r in rules)
    )
    if examples:
        system += "\n\nStyle examples the team already uses (match tone, do not copy verbatim):\n" + "\n".join(
            f"- {t.body}" for t in list(examples)[:6])
    rating = f"{review.rating} of 5 stars" if review.rating else "no star rating"
    text = (review.text or "").strip() or "(The customer left a rating with no written review.)"
    user = (
        f"Location: {site}\nBrand: {brand}\nBusiness phone for this brand: {phone or 'n/a'}\n"
        f"Reviewer first name: {review.first_name}\nRating: {rating}\n"
        f"Employees named in the review: {mentions or 'none'}\n\nReview:\n{text}\n\nWrite the reply."
    )
    return system, user


def draft_reply(review: Review, rules: Optional[Sequence[AiRule]] = None,
                examples: Sequence[ReplyTemplate] = (), agent_name: str = "") -> str:
    if not settings.ai_enabled:
        raise AiUnavailable("ANTHROPIC_API_KEY is not configured")
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover
        raise AiUnavailable("anthropic SDK not installed (pip install anthropic)") from exc

    rule_texts: List[str] = [r.text for r in rules if r.active] if rules else list(DEFAULT_RULES)
    system, user = build_prompt(review, rule_texts, examples, agent_name)
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    try:
        response = client.messages.create(
            model=settings.ai_model,
            max_tokens=400,          # replies are deliberately short
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.RateLimitError as exc:
        raise AiUnavailable("Rate limited by the AI service, try again in a minute") from exc
    except anthropic.APIStatusError as exc:
        raise AiUnavailable(f"AI service error {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise AiUnavailable("Could not reach the AI service") from exc
    if response.stop_reason == "refusal":
        raise AiUnavailable("The model declined to draft this one; write it by hand.")
    text = "".join(block.text for block in response.content if block.type == "text").strip()
    if not text:
        raise AiUnavailable("Empty draft returned")
    return text.strip().strip('"')
