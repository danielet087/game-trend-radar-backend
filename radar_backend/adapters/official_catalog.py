"""Compose official Store/catalog/content ports without the historical worker."""
from __future__ import annotations

from datetime import datetime
import os

import requests

from radar_backend.adapters import steam_store
from radar_backend.adapters.github_content_dispatch import compose_services
from radar_backend.application import content_dispatch as dispatch_application
from radar_backend.application import official_catalog as catalog_application
from radar_backend.domain import content_dispatch as dispatch_rules
from radar_backend.domain import official_catalog as catalog_rules
from radar_backend.domain.official_queue import TAIPEI


def _clock():
    return datetime.now(TAIPEI)


def reverify_pending_store_dates(checkpoint, master, limit=25, *, clock=None, session_factory=None):
    current_clock = clock or _clock
    return catalog_application.reverify_pending_store_dates(
        checkpoint, master, limit, clock=current_clock,
        session_factory=session_factory or requests.Session,
        fetch_store_release_details=steam_store.fetch_store_release_details,
        upsert_qualified_master=lambda data, result: upsert_qualified_master(data, result, clock=current_clock),
        dispatch_content_event=lambda data, result: dispatch_content_event(data, result, clock=current_clock),
    )


def retry_pending_content_dispatches(checkpoint, limit=25, *, clock=None):
    return dispatch_application.retry_pending_content_dispatches(
        checkpoint, limit,
        dispatch=lambda data, result: dispatch_content_event(data, result, clock=clock),
        signature=dispatch_rules.content_dispatch_signature,
    )


def verify_store_date_for_result(result, session, *, clock=None):
    return catalog_application.verify_store_date_for_result(
        result, session, clock=clock or _clock,
        fetch_store_release_details=steam_store.fetch_store_release_details,
    )


def upsert_qualified_master(master, result, *, clock=None):
    if not catalog_rules.qualifies_for_master(result):
        return False
    blocked = steam_store.excluded_appids()
    return catalog_rules.upsert_qualified_master(
        master, result, now=(clock or _clock)(), blocked=blocked,
        is_disallowed=steam_store.is_disallowed,
    )


def dispatch_content_event(checkpoint, result, *, clock=None):
    return dispatch_application.dispatch_content_event(
        checkpoint, result,
        services=compose_services(clock or _clock, os.environ, requests.post, emit=print),
        signature=dispatch_rules.content_dispatch_signature,
    )


def is_twitch_queue_candidate(row, *, now):
    from scripts.twitch_official_queue import is_twitch_queue_candidate as implementation
    return implementation(row, now=now)


def cached_follower(appid, sources, now):
    from scripts.import_twitch_steam_discoveries import cached_follower as implementation
    return implementation(appid, sources, now)


def dashboard_output():
    from scripts.export_scheduler_queue_status import OUTPUT
    return OUTPUT


def export_status():
    from scripts.export_scheduler_queue_status import export_status as implementation
    return implementation()
