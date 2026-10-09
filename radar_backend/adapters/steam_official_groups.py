"""Steam ResolveVanityURL transport; credentials remain request headers only."""
from __future__ import annotations

from radar_backend.domain.official_groups import API_URL


def configure_session(session):
    session.headers.update({"User-Agent": "GameTrendRadarOfficialGroupResolver/1.0"})


def request(session, aid, key, remaining, *, api_url=API_URL):
    return session.get(
        api_url, params={"vanityurl": aid, "url_type": 3, "format": "json"},
        headers={"x-webapi-key": key},
        timeout=min(15.0, max(0.1, remaining / 2)), allow_redirects=False,
    )
