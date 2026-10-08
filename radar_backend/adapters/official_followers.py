"""Steam Community HTTP/XML boundary, with an injected session and clock."""
from __future__ import annotations

from datetime import datetime
from typing import Callable
import xml.etree.ElementTree as ET

import requests

from radar_backend.domain.official_queue import TAIPEI, FollowerOutcome, valid_group_id64
from radar_backend.state.official_followers import CooldownStore

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
        response = None
        try:
            response = self.session.get(f"https://steamcommunity.com/gid/{gid}/memberslistxml/?xml=1", timeout=(8, 24))
            observed = self.clock()
            if response.status_code == 429:
                if self.cooldown:
                    self.cooldown.rate_limited(response, observed)
                return FollowerOutcome("rate_limited", observed, http=429, retry_after=(getattr(response, "headers", None) or {}).get("Retry-After"))
            if response.status_code in (401, 403) or response.status_code >= 500:
                if self.cooldown:
                    self.cooldown.temporary_failure(observed, access_error=True)
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
            if self.cooldown:
                self.cooldown.success(observed)
            return FollowerOutcome("ok", observed, followers=count, http=200)
        except (requests.RequestException, ET.ParseError) as exc:
            observed = self.clock()
            if self.cooldown:
                self.cooldown.temporary_failure(observed)
            body = getattr(response, "content", b"") if response is not None else b""
            if isinstance(body, str):
                body = body.encode()
            headers = (getattr(response, "headers", None) or {}) if response is not None else {}
            return FollowerOutcome("transport_or_xml_error", observed,
                http=getattr(response, "status_code", None), error_type=type(exc).__name__,
                content_type=headers.get("Content-Type", "")[:100] if isinstance(exc, ET.ParseError) else None,
                response_bytes=len(body) if isinstance(exc, ET.ParseError) else None,
                response_prefix=body[:180].decode("utf-8", errors="replace") if isinstance(exc, ET.ParseError) else None)
