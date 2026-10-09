"""Compose shared Store rules, metadata HTTP and adult-ledger state ports."""
from __future__ import annotations

from datetime import datetime, timezone

from radar_core.domain.twitch_admission import (
    has_taiwan_store_date_authority, is_twitch_qualified,
)
from radar_backend.adapters import steam_metadata
from radar_backend.application import candidate_screening, store_release
from radar_backend.domain import adult_exclusions, candidate_screening as screening_rules
from radar_backend.domain import store_release as release_rules
from radar_backend.state import adult_exclusions as adult_state


def _utc_now():
    return datetime.now(timezone.utc)


def fetch_metadata(session, ids, *, batch_size=steam_metadata.BATCH_SIZE, interval=1.5):
    return steam_metadata.fetch_metadata(session, ids, batch_size=batch_size, interval=interval)


def build_snapshot(catalog, store_items):
    return candidate_screening.build_snapshot(
        catalog, store_items, clock=_utc_now,
        exclusion_loader=excluded_appids, classifier=screening_rules.classify,
    )


def fetch_store_release_details(session, appids, *, today, interval=1.5):
    return store_release.fetch_store_release_details(
        session, appids, today=today, interval=interval,
        fetch_metadata=fetch_metadata, parse_store_release_detail=release_rules.parse_store_release_detail,
    )


def apply_store_release_detail(row, detail):
    return store_release.apply_store_release_detail(
        row, detail, clock=_utc_now,
        has_taiwan_store_date_authority=has_taiwan_store_date_authority,
    )


def filter_confirmed_master_games(games, eligible_rows, *, today):
    return store_release.filter_confirmed_master_games(
        games, eligible_rows, today=today, excluded_appids=excluded_appids,
        is_disallowed=is_disallowed, is_twitch_qualified=is_twitch_qualified,
    )


def excluded_appids():
    return adult_state.excluded_appids()


def is_disallowed(row, blocked):
    return adult_exclusions.is_disallowed(row, blocked)
