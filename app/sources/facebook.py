"""Facebook Page recommendations via the Graph API.

A Facebook "review" is a Recommendation: positive or negative, with optional text. Legacy
reviews carry a 1-5 `rating`. To keep one inbox, a recommendation without a star rating is
stored as 5 (recommends) or 1 (does not recommend); the raw payload keeps the real type.

Auth: FACEBOOK_ACCESS_TOKEN is a long-lived System User token from the Business Manager with
pages_read_user_content, pages_read_engagement and pages_manage_engagement on each brand page.
Per-page tokens are derived from it on demand. `ReviewSourceLink.external_location_id` is the
page id; the owner reply is the page's own comment on the recommendation's story.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional, Tuple

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

from ..config import settings
from ..models import ReviewSourceLink
from .base import NormalizedReview, SourceSummary

log = logging.getLogger(__name__)


class FacebookAPIError(RuntimeError):
    pass


def _without_token(url: str) -> str:
    """Drop only the access_token query parameter from a Graph `paging.next` URL.

    The URL carries fields, limit and the `after` cursor; _request re-adds the token
    as a parameter so it never appears in a logged URL."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "access_token"]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _next_page(data: Dict[str, Any], seen: set) -> Optional[str]:
    """The next page URL, or None at the end. A cursor that repeats would loop forever,
    so it is treated as the end of the list (and logged)."""
    nxt = (data.get("paging") or {}).get("next")
    if not nxt:
        return None
    nxt = _without_token(nxt)
    if nxt in seen:
        log.warning("facebook paging returned a URL already fetched; stopping: %s", nxt)
        return None
    seen.add(nxt)
    return nxt


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    """Graph timestamps look like 2026-09-30T14:05:42+0000."""
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            return datetime.strptime(value, fmt).astimezone(__import__("datetime").timezone.utc).replace(tzinfo=None)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(__import__("datetime").timezone.utc).replace(tzinfo=None)
    except ValueError:
        return None


class FacebookPageAdapter:
    name = "facebook"

    def __init__(self, token: Optional[str] = None):
        self._token = token or settings.facebook_access_token
        self._base = f"https://graph.facebook.com/{settings.facebook_api_version}"
        self._session = requests.Session()
        self._page_tokens: Dict[str, str] = {}

    # -- plumbing -----------------------------------------------------------
    def _request(self, method: str, url: str, *, params=None, data=None, token: Optional[str] = None, retries: int = 3) -> Dict[str, Any]:
        if not (token or self._token):
            raise FacebookAPIError("FACEBOOK_ACCESS_TOKEN is not configured")
        params = dict(params or {})
        params["access_token"] = token or self._token
        backoff = 2.0
        for attempt in range(retries + 1):
            resp = self._session.request(method, url, params=params, data=data, timeout=60)
            if resp.status_code in (429, 500, 502, 503) and attempt < retries:
                time.sleep(backoff)
                backoff *= 2
                continue
            if resp.status_code >= 400:
                raise FacebookAPIError(f"{method} {url} -> {resp.status_code}: {resp.text[:400]}")
            return resp.json() if resp.content else {}
        raise FacebookAPIError("unreachable")  # pragma: no cover

    def _page_token(self, page_id: str) -> str:
        if page_id not in self._page_tokens:
            data = self._request("GET", f"{self._base}/{page_id}", params={"fields": "access_token"})
            self._page_tokens[page_id] = data.get("access_token") or self._token
        return self._page_tokens[page_id]

    # -- discovery ----------------------------------------------------------
    def list_pages(self) -> List[Dict[str, Any]]:
        """Pages the token can manage: [{id, name, link, overall_star_rating, rating_count}]."""
        out, url, params = [], f"{self._base}/me/accounts", {"fields": "id,name,link,overall_star_rating,rating_count", "limit": 100}
        seen: set = set()
        while url:
            data = self._request("GET", url, params=params)
            out.extend(data.get("data", []))
            url, params = _next_page(data, seen), None
        return out

    # -- reviews ------------------------------------------------------------
    @staticmethod
    def normalize(r: Dict[str, Any], page_id: str) -> NormalizedReview:
        reviewer = r.get("reviewer") or {}
        story = r.get("open_graph_story") or {}
        created = _parse_ts(r.get("created_time")) or datetime.utcnow()
        rating = r.get("rating")
        if rating is None:
            rating = 5 if r.get("recommendation_type") == "positive" else 1 if r.get("recommendation_type") == "negative" else None
        reply, reply_at = None, None
        for c in ((story.get("comments") or {}).get("data") or []):
            if str((c.get("from") or {}).get("id")) == str(page_id):
                reply, reply_at = c.get("message"), _parse_ts(c.get("created_time"))
        ext = story.get("id") or f"{page_id}:{reviewer.get('id', 'anon')}:{int(created.timestamp())}"
        return NormalizedReview(
            external_id=str(ext), author_name=reviewer.get("name"), author_is_anonymous=not reviewer.get("name"),
            rating=rating, text=r.get("review_text"), created_at=created, updated_at=created,
            owner_reply_text=reply, owner_reply_updated_at=reply_at, raw_json=json.dumps(r, separators=(",", ":")))

    def fetch_reviews(self, link: ReviewSourceLink, since: Optional[datetime] = None) -> Iterator[Tuple[NormalizedReview, Optional[SourceSummary]]]:
        page_id = link.external_location_id
        token = self._page_token(page_id)
        summary_data = self._request("GET", f"{self._base}/{page_id}", params={"fields": "overall_star_rating,rating_count"}, token=token)
        summary: Optional[SourceSummary] = SourceSummary(avg_rating=summary_data.get("overall_star_rating"), total_review_count=summary_data.get("rating_count"))
        url = f"{self._base}/{page_id}/ratings"
        params: Optional[Dict[str, Any]] = {"fields": "created_time,recommendation_type,review_text,rating,reviewer{name,id},"
                                                      "open_graph_story{id,comments.limit(25){message,created_time,from}}", "limit": 100}
        any_yielded = False
        seen: set = set()
        while url:
            data = self._request("GET", url, params=params, token=token)
            for raw in data.get("data", []):
                nr = self.normalize(raw, page_id)
                if since is not None and nr.created_at < since and nr.owner_reply_updated_at is None:
                    return
                any_yielded = True
                yield (nr, summary)
                summary = None
            url, params = _next_page(data, seen), None
        if not any_yielded and summary is not None:
            yield (NormalizedReview("__none__", None, True, None, None, datetime.utcnow(), datetime.utcnow(), None, None, "{}"), summary)

    def _own_comment_id(self, story_id: str, page_id: str, token: str) -> Optional[str]:
        data = self._request("GET", f"{self._base}/{story_id}/comments", params={"fields": "id,from", "limit": 50}, token=token)
        for c in data.get("data", []):
            if str((c.get("from") or {}).get("id")) == str(page_id):
                return c.get("id")
        return None

    def post_reply(self, link: ReviewSourceLink, external_review_id: str, text: str) -> datetime:
        page_id = link.external_location_id
        token = self._page_token(page_id)
        existing = self._own_comment_id(external_review_id, page_id, token)
        if existing:
            self._request("POST", f"{self._base}/{existing}", data={"message": text}, token=token)
        else:
            self._request("POST", f"{self._base}/{external_review_id}/comments", data={"message": text}, token=token)
        return datetime.utcnow()

    def delete_reply(self, link: ReviewSourceLink, external_review_id: str) -> None:
        page_id = link.external_location_id
        token = self._page_token(page_id)
        existing = self._own_comment_id(external_review_id, page_id, token)
        if existing:
            self._request("DELETE", f"{self._base}/{existing}", token=token)
