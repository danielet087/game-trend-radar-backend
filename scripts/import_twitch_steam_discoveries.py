"""Import confirmed Twitch discoveries and queue missing official Followers.

Collect caches a bounded API batch. Apply merges that batch into latest main
without network calls. Dispatch runs only after the accepted master is saved.
Both accepted records and dispatch receipts use the same conflict-safe apply.
"""
from __future__ import annotations
import argparse
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
import requests
from collectors.steam_upcoming import parse_release_window
from radar_backend.adapters.public_catalog import keep_newer_release
from scripts.screen_steam_candidates_before_followers import is_explicit_sex_game
from scripts.steam_adult_exclusions import excluded_appids, is_disallowed
from scripts.steam_master_date_gate import parse_store_release_detail
from scripts.steam_retry_policy import rate_limit_policy, transient_retry_policy
from scripts.twitch_steam_admission import METHOD, aware_time, decimal_id, is_twitch_qualified, normalize_twitch_admission, preserve_twitch_admission, valid_enrollment, validate_twitch_snapshot, resolve_store_release_day, TW_STORE_DATE_AUTHORITY, TW_STORE_DATE_PROVIDER
from radar_backend.application import twitch_intake as _intake_application
from radar_backend.domain import twitch_intake as _intake_rules
from radar_backend.state import twitch_intake as _intake_state
from radar_backend.adapters import steam_twitch_intake as _intake_transport
from radar_backend.adapters.steam_twitch_intake import RateLimited
TAIPEI = ZoneInfo('Asia/Taipei')
STORE_BROWSE = 'https://api.steampowered.com/IStoreBrowseService/GetItems/v1/'
APPDETAILS = 'https://store.steampowered.com/api/appdetails'
STATE_VALIDATION_VERSION = 5

def stamp(now: datetime) -> str:
    return _intake_rules.stamp(now, timezone_type=timezone)

def read_json(path: Path, *, optional: bool=False) -> dict:
    return _intake_state.read_json(path, optional=optional, json_module=json)

def write_json(path: Path, value: dict) -> None:
    return _intake_state.write_json(path, value, json_module=json)

def validate_snapshot(
    discovery: dict,
    tracking: dict,
    catalog: dict,
    commit: str,
    now: datetime,
) -> list[tuple[int, dict]]:
    return _intake_rules.validate_snapshot(
        discovery,
        tracking,
        catalog,
        commit,
        now,
        decimal_id=decimal_id,
        aware_time=aware_time,
        valid_enrollment=valid_enrollment,
        normalize_twitch_admission=normalize_twitch_admission,
        validate_twitch_snapshot=validate_twitch_snapshot,
        method=METHOD,
        regex_module=re,
    )

def build_candidate(
    appid: int,
    proof: dict,
    item: dict,
    details: dict,
    followers: int | None,
    checked_at: str | None,
    now: datetime,
    blocked: set[int],
) -> tuple[dict | None, str]:
    return _intake_rules.build_candidate(
        appid,
        proof,
        item,
        details,
        followers,
        checked_at,
        now,
        blocked,
        decimal_id=decimal_id,
        is_disallowed=is_disallowed,
        is_explicit_sex_game=is_explicit_sex_game,
        parse_store_release_detail=parse_store_release_detail,
        parse_release_window=parse_release_window,
        resolve_store_release_day=resolve_store_release_day,
        aware_time=aware_time,
        is_twitch_qualified=is_twitch_qualified,
        stamp=stamp,
        taipei=TAIPEI,
        tw_store_date_authority=TW_STORE_DATE_AUTHORITY,
        tw_store_date_provider=TW_STORE_DATE_PROVIDER,
        datetime_type=datetime,
        timedelta_type=timedelta,
        timezone_type=timezone,
        regex_module=re,
        deepcopy=deepcopy,
    )

def request(
    session,
    url: str,
    *,
    params: dict | None = None,
    timeout: float = 25,
    now: datetime | None = None,
    clock = None,
    prior_cooldown: dict | None = None,
):
    return _intake_transport.request(
        session,
        url,
        params=params,
        timeout=timeout,
        now=now,
        clock=clock,
        prior_cooldown=prior_cooldown,
        rate_limit_policy=rate_limit_policy,
        store_browse=STORE_BROWSE,
        datetime_type=datetime,
        timezone_type=timezone,
        urlsplit_fn=urlsplit,
        rate_limited_type=RateLimited,
    )

def cached_follower(appid: int, documents: list[dict], now: datetime | None=None) -> tuple[int, str] | None:
    observed = now or datetime.now(timezone.utc)
    return _intake_rules.cached_follower(appid, documents, observed, aware_time=aware_time)

def signature(row: dict) -> str:
    return _intake_rules.signature(row, json_module=json, hashlib_module=hashlib)

def identity_signature(proof: dict) -> str:
    return _intake_rules.identity_signature(proof, json_module=json, hashlib_module=hashlib)

def follower_candidate(probe: dict) -> dict:
    return _intake_rules.follower_candidate(probe, deepcopy=deepcopy)

def retained_follower_candidate(appid: int, proof: dict, prior: dict, now: datetime, blocked: set[int]) -> dict | None:
    return _intake_rules.retained_follower_candidate(
        appid,
        proof,
        prior,
        now,
        blocked,
        decimal_id=decimal_id,
        normalize_twitch_admission=normalize_twitch_admission,
        identity_signature=identity_signature,
        is_disallowed=is_disallowed,
        aware_time=aware_time,
        stamp=stamp,
        is_twitch_qualified=is_twitch_qualified,
        follower_candidate=follower_candidate,
        taipei=TAIPEI,
        datetime_type=datetime,
        timedelta_type=timedelta,
        deepcopy=deepcopy,
    )

def collect(
    frontend: Path,
    commit: str,
    master: dict,
    previous: dict,
    *,
    session = None,
    now: datetime | None = None,
    max_seconds: int = 900,
    caches: list[dict] | None = None,
    monotonic = time.monotonic,
    sleep = time.sleep,
    blocked: set[int] | None = None,
    clock = None,
) -> dict:
    application_collect = _intake_application.collect
    ports = {
        'session': session,
        'now': now,
        'max_seconds': max_seconds,
        'caches': caches,
        'monotonic': monotonic,
        'sleep': sleep,
        'blocked': blocked,
        'clock': clock,
        'session_factory': requests.Session,
        'read_json': read_json,
        'validate_snapshot': validate_snapshot,
        'excluded_appids': excluded_appids,
        'retained_follower_candidate': retained_follower_candidate,
        'is_twitch_qualified': is_twitch_qualified,
        'signature': signature,
        'cached_follower': cached_follower,
        'aware_time': aware_time,
        'identity_signature': identity_signature,
        'build_candidate': build_candidate,
        'follower_candidate': follower_candidate,
        'stamp': stamp,
        'request': request,
        'rate_limit_policy': rate_limit_policy,
        'transient_retry_policy': transient_retry_policy,
        'request_exception': requests.RequestException,
        'rate_limited_type': RateLimited,
        'store_browse': STORE_BROWSE,
        'appdetails': APPDETAILS,
        'state_validation_version': STATE_VALIDATION_VERSION,
        'datetime_type': datetime,
        'timezone_type': timezone,
        'timedelta_type': timedelta,
        'json_module': json,
        'deepcopy_fn': deepcopy,
    }
    fixed_now = ports['now']
    datetime_type, timezone_type = ports['datetime_type'], ports['timezone_type']
    ports['clock'] = ports['clock'] or (lambda: fixed_now if fixed_now is not None else datetime_type.now(timezone_type.utc))
    return application_collect(frontend, commit, master, previous, **ports)

def apply_batch(master: dict, state: dict, batch: dict) -> tuple[dict, dict]:
    return _intake_state.apply_batch(
        master,
        state,
        batch,
        excluded_appids=excluded_appids,
        is_twitch_qualified=is_twitch_qualified,
        is_disallowed=is_disallowed,
        preserve_twitch_admission=preserve_twitch_admission,
        keep_newer_release=keep_newer_release,
        aware_time=aware_time,
        decimal_id=decimal_id,
        deepcopy_fn=deepcopy,
    )

def dispatch(
    master: dict,
    state: dict,
    *,
    token: str,
    target: str,
    session = None,
    now: datetime | None = None,
    max_seconds: int = 600,
    monotonic = time.monotonic,
) -> dict:
    return _intake_application.dispatch(
        master,
        state,
        token=token,
        target=target,
        session=session,
        now=now,
        max_seconds=max_seconds,
        monotonic=monotonic,
        session_factory=requests.Session,
        clock=lambda: datetime.now(timezone.utc),
        is_twitch_qualified=is_twitch_qualified,
        signature=signature,
        stamp=stamp,
        request_exception=requests.RequestException,
        environ_get=os.environ.get,
    )

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--phase', required=True, choices=['collect', 'apply', 'dispatch'])
    parser.add_argument('--frontend-path', type=Path)
    parser.add_argument('--frontend-commit')
    parser.add_argument('--master', type=Path, default=Path('data/steam_upcoming_master.json'))
    parser.add_argument('--state', type=Path, default=Path('data/twitch_steam_import_state.json'))
    parser.add_argument('--batch', type=Path, default=Path('output/twitch_steam_import_batch.json'))
    parser.add_argument('--max-seconds', type=int, default=900)
    args = parser.parse_args()
    if not 1 <= args.max_seconds <= 900:
        parser.error('max-seconds must be 1..900')
    master = read_json(args.master)
    if not isinstance(master.get('games'), list):
        parser.error('master games must be an array')
    state = read_json(args.state, optional=True)
    if args.phase == 'collect':
        if args.frontend_path is None or args.frontend_commit is None:
            parser.error('collect requires frontend-path and immutable frontend-commit')
        actual_commit = subprocess.check_output(['git', '-C', str(args.frontend_path), 'rev-parse', 'HEAD'], text=True).strip()
        if actual_commit != args.frontend_commit:
            parser.error('frontend-commit differs from the checked-out immutable snapshot')
        cache_paths = [Path('data/steam_followers_cache.json'), Path('data/steam_followers_checkpoint.json'), Path('experiments/steam_official_daily_catchup/checkpoint.json'), Path('experiments/steam_official_followers_20260922/checkpoint.json'), Path('experiments/steam_official_nearfirst_20260922/checkpoint.json')]
        caches = [read_json(path) for path in cache_paths if path.is_file()]
        batch = collect(args.frontend_path, args.frontend_commit, master, state, max_seconds=args.max_seconds, caches=caches)
        write_json(args.batch, batch)
    elif args.phase == 'apply':
        batch = read_json(args.batch)
        master, state = apply_batch(master, state, batch)
        write_json(args.master, master)
        write_json(args.state, state)
    else:
        batch = dispatch(master, state, token=os.environ.get('CONTENT_BACKEND_TOKEN', ''), target=os.environ.get('CONTENT_BACKEND_REPOSITORY', 'danielet087/game-trend-radar-content-backend'), max_seconds=args.max_seconds)
        write_json(args.batch, batch)
    print('TWITCH_STEAM_IMPORT', args.phase, 'records', len(batch['records']), 'states', len(batch['state_updates']), 'stop', batch.get('stop_reason', 'complete'), flush=True)
if __name__ == '__main__':
    main()
