"""Review platform adapters. Register new platforms in `ADAPTERS`."""
from __future__ import annotations

from typing import Dict, Type

from .base import NormalizedReview, ReviewSourceAdapter, SourceSummary  # noqa: F401
from .google import GoogleBusinessProfileAdapter

ADAPTERS: Dict[str, Type[ReviewSourceAdapter]] = {
    GoogleBusinessProfileAdapter.name: GoogleBusinessProfileAdapter,
}


def get_adapter(source: str) -> ReviewSourceAdapter:
    try:
        return ADAPTERS[source]()
    except KeyError:
        raise ValueError(f"No adapter registered for source '{source}'. Known: {sorted(ADAPTERS)}")
