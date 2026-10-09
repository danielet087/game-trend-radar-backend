"""Steam Store transport and adult-ledger ports for official promotion.

Shared Store helpers keep the maintenance and publisher compatibility paths.
The official use cases never import the historical hourly worker.
"""
from __future__ import annotations


def fetch_store_release_details(session, appids, *, today, interval=1.5):
    from scripts.steam_master_date_gate import fetch_store_release_details as implementation
    return implementation(session, appids, today=today, interval=interval)


def excluded_appids():
    from scripts.steam_adult_exclusions import excluded_appids as implementation
    return implementation()


def is_disallowed(row, blocked):
    from scripts.steam_adult_exclusions import is_disallowed as implementation
    return implementation(row, blocked)
