"""Steam credentials must not appear in errors."""
from datetime import date
import pytest
import requests
from scripts.steam_candidate_pipeline import query_one_day


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


def test_steam_query_sanitizes_transport_exception_containing_api_key(monkeypatch):
    secret = "steam-api-key-test-value"
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    class Session:
        calls = 0

        def get(self, url, *, params, timeout):
            self.calls += 1
            raise requests.ConnectionError(f"Connect error: {url}?key={secret}")

    session = Session()
    with pytest.raises(RuntimeError) as error:
        query_one_day(session, secret, date(2026, 9, 22))
    # Network errors are retried (and waited out as a possible outage) first.
    assert session.calls > 1
    assert "temporary network failure" in str(error.value)
    assert secret not in str(error.value)
    assert "key=" not in str(error.value)
    assert error.value.__cause__ is None
