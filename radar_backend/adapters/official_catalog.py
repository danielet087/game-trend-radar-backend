"""Explicit transition ports for the existing Store/content integrations.

The hourly CLI uses the new queue, Community, state and application owners.
Only the post-Followers Store gate, master promotion, content dispatch and
shared Twitch helpers still belong to historical integrations. Resolve those
functions when called; never invoke a historical CLI or alias its module.
"""
from __future__ import annotations


def reverify_pending_store_dates(checkpoint, master):
    from scripts.experiment_official_daily_catchup_250 import reverify_pending_store_dates as implementation
    return implementation(checkpoint, master)


def retry_pending_content_dispatches(checkpoint):
    from scripts.experiment_official_daily_catchup_250 import retry_pending_content_dispatches as implementation
    return implementation(checkpoint)


def verify_store_date_for_result(result, session):
    from scripts.experiment_official_daily_catchup_250 import verify_store_date_for_result as implementation
    return implementation(result, session)


def upsert_qualified_master(master, result):
    from scripts.experiment_official_daily_catchup_250 import upsert_qualified_master as implementation
    return implementation(master, result)


def dispatch_content_event(checkpoint, result):
    from scripts.experiment_official_daily_catchup_250 import dispatch_content_event as implementation
    return implementation(checkpoint, result)


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
