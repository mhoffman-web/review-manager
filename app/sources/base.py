"""Platform-agnostic interface every review source must implement."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterator, Optional, Protocol, Tuple

from ..models import ReviewSourceLink


@dataclass
class NormalizedReview:
    external_id: str
    author_name: Optional[str]
    author_is_anonymous: bool
    rating: Optional[int]            # 1..5 or None
    text: Optional[str]
    created_at: datetime             # naive UTC
    updated_at: datetime             # naive UTC
    owner_reply_text: Optional[str]
    owner_reply_updated_at: Optional[datetime]
    raw_json: str


@dataclass
class SourceSummary:
    avg_rating: Optional[float]
    total_review_count: Optional[int]


class ReviewSourceAdapter(Protocol):
    name: str

    def fetch_reviews(
        self, link: ReviewSourceLink, since: Optional[datetime] = None
    ) -> Iterator[Tuple[NormalizedReview, Optional[SourceSummary]]]:
        """Yield reviews newest-updated first. Stop early once `updated_at < since`
        when `since` is given (incremental sync). The summary may be attached to
        any yielded item (adapters typically attach it to the first)."""
        ...

    def post_reply(self, link: ReviewSourceLink, external_review_id: str, text: str) -> datetime:
        """Create or replace the owner reply. Returns the platform's reply timestamp."""
        ...

    def delete_reply(self, link: ReviewSourceLink, external_review_id: str) -> None:
        ...
