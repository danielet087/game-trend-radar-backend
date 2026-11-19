"""Steam Community HTTP/XML boundary, with an injected session and clock."""
from __future__ import annotations

from datetime import datetime
from typing import Callable
import xml.etree.ElementTree as ET

import requests

from radar_backend.domain.official_queue import TAIPEI, FollowerOutcome, valid_group_id64
from radar_backend.state.official_followers import CooldownStore, FALLBACK_IDENTITY, PRIMARY_IDENTITY

# Steam Community throttles XML requests that carry no Accept-Language far more
# than browser-like ones. A probe on 2026-11-16 got 429 for nine of twelve
# requests with requests' default headers and 200 for every request with this
# header, from the same kind of runner at the same time. The limit follows the
# request identity; see CooldownStore for the owner's temporary zh-TW fallback.
COMMUNITY_IDENTITIES = {
    PRIMARY_IDENTITY: {"Accept-Language": "en-US,en;q=0.9"},
    FALLBACK_IDENTITY: {"Accept-Language": "zh-TW,zh;q=0.9"},
}
COMMUNITY_HEADERS = COMMUNITY_IDENTITIES[PRIMARY_IDENTITY]


def parse_official_xml(body, expected_group):
    """A numeric memberCount is evidence only for the requested clan identity."""
    expected_group = valid_group_id64(expected_group)
    if expected_group is None:
        raise ValueError("An official Group ID is required")
    root = ET.fromstring(body)
    count = (root.findtext(".//memberCount") or "").replace(",", "").strip()
    group = (root.findtext(".//groupID64") or "").strip()
    if not count.isascii() or not count.isdigit() or valid_group_id64(group) != expected_group:
        raise ValueError("Missing count or mismatched official Steam group")
    return int(count)


class OfficialFollowerClient:
    def __init__(self, *, session=None, clock: Callable = None, cooldown: CooldownStore = None):
        self.session = session if session is not None else requests.Session()
        self.clock = clock or (lambda: datetime.now(TAIPEI))
        self.cooldown = cooldown

    def fetch(self, group_id64, *, manual_override=False):
        now = self.clock()
        gid = valid_group_id64(group_id64)
        if gid is None:
            return FollowerOutcome("awaiting_group_resolution", now)
        if self.cooldown and not manual_override and self.cooldown.blocked(now):
            return FollowerOutcome("cooldown_no_request", now)
        # CooldownStore validates the stored identity; simple test stores have none.
        identity = self.cooldown.identity() if isinstance(self.cooldown, CooldownStore) else PRIMARY_IDENTITY
        return self._request(gid, COMMUNITY_IDENTITIES[identity], self.cooldown)

    def probe_primary(self, group_id64):
        """One primary-identity request. The active backoff is left alone; the
        caller records the outcome with CooldownStore.primary_probe_result."""
        gid = valid_group_id64(group_id64)
        if gid is None:
            return FollowerOutcome("awaiting_group_resolution", self.clock())
        return self._request(gid, COMMUNITY_IDENTITIES[PRIMARY_IDENTITY], None)

    def _request(self, gid, request_headers, cooldown):
        response = None
        try:
            response = self.session.get(f"https://steamcommunity.com/gid/{gid}/memberslistxml/?xml=1",
                                        headers=request_headers, timeout=(8, 24))
            observed = self.clock()
            if response.status_code == 429:
                if cooldown:
                    cooldown.rate_limited(response, observed)
                return FollowerOutcome("rate_limited", observed, http=429, retry_after=(getattr(response, "headers", None) or {}).get("Retry-After"))
            if response.status_code in (401, 403) or response.status_code >= 500:
                if cooldown:
                    cooldown.temporary_failure(observed, access_error=True)
                return FollowerOutcome("access_or_server_error", observed, http=response.status_code)
            if response.status_code != 200:
                return FollowerOutcome("unexpected_http", observed, http=response.status_code)
            body = getattr(response, "content", None)
            if body is None:
                body = response.text
            try:
                count = parse_official_xml(body, gid)
            except ValueError:
                return FollowerOutcome("missing_count_or_group_mismatch", observed, http=200)
            if cooldown:
                cooldown.success(observed)
            return FollowerOutcome("ok", observed, followers=count, http=200)
        except (requests.RequestException, ET.ParseError) as exc:
            observed = self.clock()
            if cooldown:
                cooldown.temporary_failure(observed)
            body = getattr(response, "content", b"") if response is not None else b""
            if isinstance(body, str):
                body = body.encode()
            headers = (getattr(response, "headers", None) or {}) if response is not None else {}
            return FollowerOutcome("transport_or_xml_error", observed,
                http=getattr(response, "status_code", None), error_type=type(exc).__name__,
                content_type=headers.get("Content-Type", "")[:100] if isinstance(exc, ET.ParseError) else None,
                response_bytes=len(body) if isinstance(exc, ET.ParseError) else None,
                response_prefix=body[:180].decode("utf-8", errors="replace") if isinstance(exc, ET.ParseError) else None)
