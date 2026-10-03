"""Activity timeline helper: one call records what happened to a review."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

from .models import Review, ReviewEvent


def record(session: Session, review: Review, kind: str, actor_id: Optional[int] = None, at: Optional[datetime] = None, **detail: Any) -> ReviewEvent:
    ev = ReviewEvent(review_id=review.id, kind=kind, actor_id=actor_id, at=at or datetime.utcnow(),
                     detail_json=json.dumps({k: v for k, v in detail.items() if v is not None}, default=str) if detail else None)
    session.add(ev)
    return ev
