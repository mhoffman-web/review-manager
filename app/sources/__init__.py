"""Review platform adapters. Register new platforms in `ADAPTERS`."""
from __future__ import annotations

from datetime import datetime
from typing import Dict, Iterator, Optional, Tuple, Type

from .base import NormalizedReview, ReviewSourceAdapter, SourceSummary  # noqa: F401
from .facebook import FacebookPageAdapter
from .google import GoogleBusinessProfileAdapter

ADAPTERS: Dict[str, Type[ReviewSourceAdapter]] = {
    GoogleBusinessProfileAdapter.name: GoogleBusinessProfileAdapter,
    FacebookPageAdapter.name: FacebookPageAdapter,
}


class DemoAdapter:
    """Hosted demo: nothing to pull, and posting a reply just succeeds so the inbox can be tried."""
    name = "demo"

    def fetch_reviews(self, link, since: Optional[datetime] = None) -> Iterator[Tuple[NormalizedReview, Optional[SourceSummary]]]:
        return iter(())

    def post_reply(self, link, external_review_id: str, text: str) -> datetime:
        return datetime.utcnow()

    def delete_reply(self, link, external_review_id: str) -> None:
        return None


def get_adapter(source: str) -> ReviewSourceAdapter:
    from ..config import settings
    if settings.demo_mode:
        return DemoAdapter()  # type: ignore[return-value]
    try:
        return ADAPTERS[source]()
    except KeyError:
        raise ValueError(f"No adapter registered for source '{source}'. Known: {sorted(ADAPTERS)}")
