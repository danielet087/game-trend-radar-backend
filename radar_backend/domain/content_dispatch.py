"""Pure qualification, identity and payload rules for official content events."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterator, Mapping

from radar_backend.domain.official_queue import queue_checked_numeric as checked_numeric, valid_date

EVENT_TYPE = "steam_game_qualified"


@dataclass(frozen=True)
class DispatchConfiguration:
    target_repository: str
    token: str = field(repr=False)
    source_repository: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.target_repository and self.token)


@dataclass(frozen=True)
class DispatchDelivery:
    """Transport evidence; only GitHub's 204 response acknowledges delivery."""
    http: int | None = None
    error_type: str | None = None

    @property
    def accepted(self) -> bool:
        return self.http == 204 and self.error_type is None


def qualification_status(result: Mapping) -> str | None:
    if not checked_numeric(result.get("official_followers")) or result["official_followers"] < 5000:
        return "not_qualified"
    if not valid_date(result.get("release_date")):
        return "invalid_release_date"
    if result.get("store_date_exact") is not True or result.get("release_display_precision") != "date_full":
        return "store_date_not_verified"
    return None


def content_dispatch_signature(result: Mapping) -> str:
    return (
        f"{int(result['appid'])}:"
        f"{int(result['official_followers'])}:"
        f"{result['release_date']}:store-v2"
    )


def content_dispatch_payload(result: Mapping, *, source_repository: str | None,
                             signature: str) -> dict:
    return {
        "event_type": EVENT_TYPE,
        "client_payload": {
            "appid": int(result["appid"]),
            "official_followers": int(result["official_followers"]),
            "release_date": result["release_date"],
            "official_checked_at_taipei": result.get("official_checked_at_taipei"),
            "official_source": result.get("official_source"),
            "source_repository": source_repository,
            "signature": signature,
        },
    }


def retry_candidates(checkpoint: Mapping, limit: int = 25, *,
                     signature: Callable | None = None) -> Iterator[dict]:
    """Keep the historical retry selection separate from the delivery gate.

    Invalid or unverified dates are selected and counted as attempted retries;
    the dispatch use case then reports its established gate status.
    """
    signature = signature if signature is not None else content_dispatch_signature
    selected = 0
    for result in sorted(
        checkpoint.get("official_results", {}).values(),
        key=lambda value: (value.get("release_date", "9999-12-31"), int(value.get("appid", 0))),
    ):
        if selected >= limit:
            break
        if result.get("queue_source") == "twitch_steam_discovery":
            continue
        if not checked_numeric(result.get("official_followers")) or result["official_followers"] < 5000:
            continue
        aid = str(int(result["appid"]))
        prior = (checkpoint.get("content_dispatches") or {}).get(aid) or {}
        if prior.get("signature") == signature(result) and prior.get("status") == "dispatched":
            continue
        yield result
        selected += 1
