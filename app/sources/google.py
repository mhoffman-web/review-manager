"""Google Business Profile adapter.

Reviews and replies live on the legacy v4 "Google My Business API"
(mybusiness.googleapis.com/v4). Account and location discovery use the newer
split APIs. All three need the `business.manage` OAuth scope and the Cloud
project must have been granted Business Profile API access by Google.

Docs: https://developers.google.com/my-business/content/review-data
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional, Tuple

import requests
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials

from ..config import settings
from ..models import ReviewSourceLink
from .base import NormalizedReview, SourceSummary

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/business.manage"]
V4 = "https://mybusiness.googleapis.com/v4"
ACCOUNTS_API = "https://mybusinessaccountmanagement.googleapis.com/v1"
INFO_API = "https://mybusinessbusinessinformation.googleapis.com/v1"

STAR_MAP = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}


# --------------------------------------------------------------------------- auth
def run_oauth_flow(client_secrets_file: Optional[str] = None, token_file: Optional[str] = None) -> str:
    """Interactive one-time consent. Opens a browser; stores the refresh token."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    client_secrets_file = client_secrets_file or settings.google_client_secrets_file
    token_file = token_file or settings.google_token_file
    flow = InstalledAppFlow.from_client_secrets_file(client_secrets_file, scopes=SCOPES)
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    with open(token_file, "w") as fh:
        fh.write(creds.to_json())
    return token_file


def load_credentials() -> Credentials:
    if settings.google_token_json:
        info = json.loads(settings.google_token_json)
    else:
        with open(settings.google_token_file) as fh:
            info = json.load(fh)
    creds = Credentials.from_authorized_user_info(info, scopes=SCOPES)
    if not creds.valid:
        creds.refresh(GoogleAuthRequest())
        # Persist the refreshed token when we are file-backed.
        if not settings.google_token_json:
            with open(settings.google_token_file, "w") as fh:
                fh.write(creds.to_json())
    return creds


# --------------------------------------------------------------------------- http
class GoogleAPIError(RuntimeError):
    pass


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    """RFC3339 (e.g. 2026-09-30T14:05:42.056Z) -> naive UTC datetime."""
    if not value:
        return None
    value = value.rstrip("Z")
    if "." in value:
        head, frac = value.split(".", 1)
        value = f"{head}.{frac[:6].ljust(6, '0')}"
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f")
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S")


class GoogleBusinessProfileAdapter:
    name = "google"

    def __init__(self, credentials: Optional[Credentials] = None):
        self._creds = credentials
        self._session = requests.Session()

    # -- plumbing -----------------------------------------------------------
    def _headers(self) -> Dict[str, str]:
        if self._creds is None:
            self._creds = load_credentials()
        if not self._creds.valid:
            self._creds.refresh(GoogleAuthRequest())
        return {"Authorization": f"Bearer {self._creds.token}", "Content-Type": "application/json"}

    def _request(self, method: str, url: str, *, params=None, json_body=None, retries: int = 4) -> Dict[str, Any]:
        backoff = 2.0
        for attempt in range(retries + 1):
            resp = self._session.request(method, url, headers=self._headers(), params=params, json=json_body, timeout=60)
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                log.warning("Google API %s %s -> %s, retrying in %.0fs", method, url, resp.status_code, backoff)
                time.sleep(backoff)
                backoff *= 2
                continue
            if resp.status_code >= 400:
                raise GoogleAPIError(f"{method} {url} -> {resp.status_code}: {resp.text[:500]}")
            if not resp.content:
                return {}
            return resp.json()
        raise GoogleAPIError("unreachable")  # pragma: no cover

    # -- discovery ----------------------------------------------------------
    def list_accounts(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        token = None
        while True:
            data = self._request("GET", f"{ACCOUNTS_API}/accounts", params={"pageSize": 20, "pageToken": token})
            out.extend(data.get("accounts", []))
            token = data.get("nextPageToken")
            if not token:
                return out

    def list_locations(self, account_name: str) -> List[Dict[str, Any]]:
        """`account_name` looks like 'accounts/1234567890'."""
        out: List[Dict[str, Any]] = []
        token = None
        read_mask = "name,title,storefrontAddress,metadata,storeCode"
        while True:
            data = self._request(
                "GET", f"{INFO_API}/{account_name}/locations",
                params={"pageSize": 100, "readMask": read_mask, "pageToken": token},
            )
            out.extend(data.get("locations", []))
            token = data.get("nextPageToken")
            if not token:
                return out

    # -- reviews ------------------------------------------------------------
    @staticmethod
    def _v4_location_path(link: ReviewSourceLink) -> str:
        acct = (link.external_account_id or "").replace("accounts/", "")
        loc = link.external_location_id.replace("locations/", "")
        if not acct:
            raise GoogleAPIError(f"review_sources.id={link.id} has no external_account_id; run discover-locations")
        return f"accounts/{acct}/locations/{loc}"

    @staticmethod
    def normalize(r: Dict[str, Any]) -> NormalizedReview:
        reviewer = r.get("reviewer") or {}
        reply = r.get("reviewReply") or {}
        return NormalizedReview(
            external_id=r["reviewId"],
            author_name=reviewer.get("displayName"),
            author_is_anonymous=bool(reviewer.get("isAnonymous", False)),
            rating=STAR_MAP.get(r.get("starRating", "")),
            text=r.get("comment"),
            created_at=_parse_ts(r.get("createTime")) or datetime.utcnow(),
            updated_at=_parse_ts(r.get("updateTime")) or _parse_ts(r.get("createTime")) or datetime.utcnow(),
            owner_reply_text=reply.get("comment"),
            owner_reply_updated_at=_parse_ts(reply.get("updateTime")),
            raw_json=json.dumps(r, separators=(",", ":")),
        )

    def fetch_reviews(
        self, link: ReviewSourceLink, since: Optional[datetime] = None
    ) -> Iterator[Tuple[NormalizedReview, Optional[SourceSummary]]]:
        path = self._v4_location_path(link)
        token = None
        first = True
        while True:
            data = self._request(
                "GET", f"{V4}/{path}/reviews",
                params={"pageSize": 50, "orderBy": "updateTime desc", "pageToken": token},
            )
            summary = None
            if first:
                summary = SourceSummary(
                    avg_rating=data.get("averageRating"),
                    total_review_count=data.get("totalReviewCount"),
                )
                first = False
            reviews = data.get("reviews", [])
            if not reviews and summary is not None:
                # Empty listing: still surface the summary so the link row updates.
                yield (NormalizedReview("__none__", None, True, None, None, datetime.utcnow(), datetime.utcnow(), None, None, "{}"), summary)
                return
            for raw in reviews:
                nr = self.normalize(raw)
                if since is not None and nr.updated_at < since:
                    return
                yield (nr, summary)
                summary = None
            token = data.get("nextPageToken")
            if not token:
                return

    def post_reply(self, link: ReviewSourceLink, external_review_id: str, text: str) -> datetime:
        path = self._v4_location_path(link)
        data = self._request("PUT", f"{V4}/{path}/reviews/{external_review_id}/reply", json_body={"comment": text})
        return _parse_ts(data.get("updateTime")) or datetime.utcnow()

    def delete_reply(self, link: ReviewSourceLink, external_review_id: str) -> None:
        path = self._v4_location_path(link)
        self._request("DELETE", f"{V4}/{path}/reviews/{external_review_id}/reply")
