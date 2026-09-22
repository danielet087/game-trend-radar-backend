"""Regression tests: credentials must never be exposed in request URLs or errors."""
from datetime import date

import pytest
import requests

from collectors.twitch_live import TwitchClient
from scripts.steam_candidate_pipeline import query_one_day


def test_twitch_oauth_posts_secret_in_body_and_sanitizes_http_error(monkeypatch):
    client = TwitchClient("client-example", "twitch-secret-test-value")
    seen = {}

    class Response:
        status_code = 401

        def raise_for_status(self):
            raise requests.HTTPError(
                "unauthorized https://id.twitch.tv/oauth2/token?"
                "client_secret=twitch-secret-test-value",
                response=self,
            )

    def post(url, **kwargs):
        seen.update(kwargs)
        assert "twitch-secret-test-value" not in url
        return Response()

    monkeypatch.setattr(client.session, "post", post)
    with pytest.raises(RuntimeError) as error:
        client.authenticate()
    assert seen["data"]["client_secret"] == "twitch-secret-test-value"
    assert "params" not in seen
    assert "HTTP 401" in str(error.value)
    assert "twitch-secret-test-value" not in str(error.value)


def test_steam_query_sanitizes_http_error_containing_api_key():
    secret = "steam-api-key-test-value"

    class Response:
        status_code = 403

        def raise_for_status(self):
            raise requests.HTTPError(
                f"Forbidden for url: https://api.steampowered.com/?key={secret}",
                response=self,
            )

    class Session:
        def get(self, url, *, params, timeout):
            assert params["key"] == secret
            return Response()

    with pytest.raises(RuntimeError) as error:
        query_one_day(Session(), secret, date(2026, 9, 22))
    assert "HTTP 403" in str(error.value)
    assert secret not in str(error.value)
    assert "key=" not in str(error.value)


def test_steam_query_sanitizes_transport_exception_containing_api_key():
    secret = "steam-api-key-test-value"

    class Session:
        def get(self, url, *, params, timeout):
            raise requests.ConnectionError(f"Connect error: {url}?key={secret}")

    with pytest.raises(RuntimeError) as error:
        query_one_day(Session(), secret, date(2026, 9, 22))
    assert "ConnectionError" in str(error.value)
    assert secret not in str(error.value)
    assert "key=" not in str(error.value)

def test_youtube_api_error_does_not_echo_key(monkeypatch):
    from collectors.youtube_live import YouTubeClient

    secret = "youtube-api-key-test-value"
    client = YouTubeClient(secret)

    class Response:
        status_code = 403

        def raise_for_status(self):
            raise requests.HTTPError(
                f"Forbidden for url: https://www.googleapis.com/youtube/v3/search?key={secret}",
                response=self,
            )

    def get(url, *, params, timeout):
        assert params["key"] == secret
        return Response()

    monkeypatch.setattr(client.session, "get", get)
    with pytest.raises(RuntimeError) as error:
        client.get("search", {"part": "snippet"})
    assert "HTTP 403" in str(error.value)
    assert secret not in str(error.value)
    assert "key=" not in str(error.value)
