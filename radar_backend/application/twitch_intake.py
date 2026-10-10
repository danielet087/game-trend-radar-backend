"""Frozen Twitch-to-Steam intake coordinated through explicit external ports."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from radar_backend.domain.twitch_intake import cached_group

STORE_BROWSE = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"
APPDETAILS = "https://store.steampowered.com/api/appdetails"


def collect(frontend: Path, commit: str, master: dict, previous: dict, *,
            session=None, now: datetime | None = None, max_seconds: int = 900,
            caches: list[dict] | None = None, monotonic, sleep,
            blocked: set[int] | None = None, clock,
            session_factory, read_json, validate_snapshot, excluded_appids,
            retained_follower_candidate, is_twitch_qualified, signature,
            cached_follower, aware_time, identity_signature, build_candidate,
            follower_candidate, stamp, request, rate_limit_policy,
            transient_retry_policy, request_exception, rate_limited_type,
            store_browse=STORE_BROWSE, appdetails=APPDETAILS,
            state_validation_version=4, datetime_type=datetime,
            timezone_type=timezone, timedelta_type=timedelta,
            json_module=json, deepcopy_fn=deepcopy) -> dict:
    now = now or clock()
    session = session or session_factory()
    session.headers.update({"User-Agent": "GameTrendRadarTwitchSteamImport/1.0"})
    discovery = read_json(frontend / "data/twitch_steam_discovery.json")
    tracking = read_json(frontend / "data/twitch_tracking.json")
    catalog = read_json(frontend / "data/steam_upcoming.json")
    candidates = validate_snapshot(discovery, tracking, catalog, commit, now)
    by_id = {int(row["appid"]): row for row in master["games"]}
    blocked = excluded_appids() if blocked is None else blocked
    batch = {"schema_version": 1, "generated_at": stamp(now), "source_frontend_commit": commit,
             "records": [], "state_updates": {}, "cooldown_updates": {}, "stop_reason": "complete",
             "active_twitch_appids": [appid for appid, _ in candidates]}
    queued = {}
    # Seed all active rows before bounded processing. A deadline or global
    # metadata cooldown must not drop candidates that were already verified.
    for appid, proof in candidates:
        retained = retained_follower_candidate(
            appid, proof, (previous.get("games") or {}).get(str(appid), {}), now, blocked,
        )
        if retained is not None:
            queued[appid] = retained
    cooldowns = deepcopy_fn(previous.get("api_cooldowns") or {})
    deadline = monotonic() + max_seconds
    last_metadata_start = float("-inf")
    def get(url: str, *, params: dict):
        nonlocal last_metadata_start
        delay = max(0, 1.5 - (monotonic() - last_metadata_start))
        if delay:
            sleep(min(delay, max(0, deadline - monotonic())))
        last_metadata_start = monotonic()
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise RuntimeError("deadline")
        stage = "steam_store_browse" if url == store_browse else "steam_appdetails"
        response = request(session, url, params=params, timeout=min(25, remaining),
                           clock=clock, prior_cooldown=cooldowns.get(stage))
        prior_cooldown = cooldowns.get(stage) or {}
        if prior_cooldown.get("attempts"):
            recovered_at = stamp(clock())
            cooldowns[stage] = {**prior_cooldown, "attempts": 0,
                                "updated_at": recovered_at, "last_success_at": recovered_at}
            batch["cooldown_updates"][stage] = deepcopy_fn(cooldowns[stage])
        return response
    for appid, proof in candidates:
        if monotonic() >= deadline:
            batch["stop_reason"] = "deadline"
            break
        prior = (previous.get("games") or {}).get(str(appid), {})
        record = by_id.get(appid)
        count = (cached_follower(appid, caches or [], now)
                 if isinstance(record, dict) and record.get("follower_status") == "unavailable_group_id"
                 else None)
        # Persistent membership outlives Twitch expiry. No Steam fetch is needed
        # on every subsequent import; the normal content reconciler owns refresh.
        if (isinstance(record, dict) and is_twitch_qualified(record)
                and not (record.get("follower_status") == "unavailable_group_id" and count is not None)):
            queued.pop(appid, None)
            content_signature = signature(record)
            if (prior.get("status") == "accepted" and prior.get("retry_at") is None
                    and not prior.get("retry_attempts") and not prior.get("rate_limit_stage")
                    and not prior.get("rate_limit_attempts") and not prior.get("retry_source")
                    and prior.get("validation_version") == state_validation_version
                    and prior.get("content_signature") == content_signature
                    and prior.get("twitch_admission") == record["twitch_admission"]
                    and not prior.get("follower_candidate")):
                continue
            batch["state_updates"][str(appid)] = {
                "status": "accepted", "updated_at": stamp(now),
                "validation_version": state_validation_version,
                "twitch_admission": record["twitch_admission"],
                "content_signature": content_signature,
                "reason": "twitch_verified_without_group" if record.get("follower_status") == "unavailable_group_id" else "steam_verified",
                "retry_at": None, "retry_attempts": 0, "rate_limit_attempts": 0,
                "rate_limit_stage": None, "retry_source": None, "retry_after": None,
                "follower_candidate": None,
            }
            continue
        if not (isinstance(record, dict) and record.get("follower_status") == "unavailable_group_id"):
            count = cached_follower(appid, caches or [], now)
        group_id = cached_group(appid, caches or [], record, prior)
        if count is None and isinstance(record, dict):
            count = cached_follower(appid, [{"games": {str(appid): {
                "followers": record.get("followers"), "checked_at": record.get("follower_checked_at"),
            }}}], now)
        retry_at = aware_time(prior.get("retry_at"))
        prior_proof = prior.get("twitch_admission")
        group_fallback_migration = (count is None and group_id is None
                                    and prior.get("reason") == "queued_official_followers")
        parser_migration = (prior.get("validation_version") != state_validation_version
                             and prior.get("reason") in {"uncertain_steam_date", "uncertain_taiwan_store_date", "steam_date_conflict"})
        if (not parser_migration and not group_fallback_migration
                and retry_at is not None and retry_at > clock() and isinstance(prior_proof, dict)
                and identity_signature(prior_proof) == identity_signature(proof)):
            continue
        state = {"status": "pending", "updated_at": stamp(now), "twitch_admission": proof,
                 "validation_version": state_validation_version,
                 "follower_candidate": deepcopy_fn(queued.get(appid))}
        batch["state_updates"][str(appid)] = state
        metadata_blocked = next((stage for stage in ("steam_store_browse", "steam_appdetails")
            if aware_time((cooldowns.get(stage) or {}).get("retry_at")) is not None
            and aware_time(cooldowns[stage]["retry_at"]) > clock()), None)
        if metadata_blocked is not None:
            cooldown = cooldowns[metadata_blocked]
            state.update(reason="metadata_cooldown", rate_limit_stage=metadata_blocked,
                         retry_at=cooldown["retry_at"], retry_source=cooldown.get("retry_source"),
                         retry_after=cooldown.get("retry_after"), retry_attempts=0,
                         rate_limit_attempts=cooldown.get("attempts", 0))
            batch["stop_reason"] = "steam_metadata_cooldown"
            break
        try:
            payload = {"ids": [{"appid": appid}], "context": {
                "country_code": "TW", "language": "english", "steam_realm": 1,
            }, "data_request": {"include_release": True, "include_basic_info": True,
                                "include_tag_count": 20}}
            data = get(store_browse, params={"input_json": json_module.dumps(payload)}).json()
            items = (data.get("response") or {}).get("store_items") or []
            item = next((x for x in items if isinstance(x, dict) and x.get("appid") == appid), {})
            data = get(appdetails, params={"appids": appid, "cc": "TW", "l": "tchinese"}).json()
            envelope = data.get(str(appid)) or {}
            details = envelope.get("data") if envelope.get("success") is True else {}
            # Verify all metadata gates before adding work to the shared queue.
            probe, reason = build_candidate(appid, proof, item, details, 0, stamp(now), now, blocked)
            if probe is None:
                excluded = reason in {"adult_content", "not_a_steam_game", "outside_new_game_window"}
                retry = (transient_retry_policy(prior, clock()) if reason in {
                    "steam_store_unavailable", "steam_type_unavailable", "steam_content_descriptors_unavailable",
                } else {"retry_at": stamp(clock() + timedelta_type(hours=24))})
                state.update(reason=reason, **retry, status="excluded" if excluded else "pending",
                             rate_limit_stage=None, rate_limit_attempts=0, retry_after=None)
                if reason not in {"steam_store_unavailable", "steam_type_unavailable",
                                  "steam_content_descriptors_unavailable"}:
                    queued.pop(appid, None)
                    state["follower_candidate"] = None
                continue
            if count is None and group_id is not None:
                queued[appid] = follower_candidate(probe)
                queued[appid]["group_id64"] = group_id
                state.update(reason="queued_official_followers", rate_limit_stage=None,
                             retry_at=None, retry_source=None, retry_after=None,
                             retry_attempts=0, rate_limit_attempts=0,
                             follower_candidate=deepcopy_fn(queued[appid]))
                continue
            row, reason = build_candidate(appid, proof, item, details,
                                          *(count if count is not None else (None, None)), now, blocked)
            if row is None:
                state.update(reason=reason, **transient_retry_policy(prior, clock()),
                             rate_limit_stage=None, rate_limit_attempts=0, retry_after=None)
                continue
            if group_id is not None:
                row["group_id64"] = group_id
            batch["records"].append(row)
            queued.pop(appid, None)
            state.update(status="accepted", reason="twitch_verified_without_group" if count is None else "steam_verified", content_signature=signature(row),
                         followers=row["followers"], follower_checked_at=row["follower_checked_at"],
                         follower_status=row.get("follower_status"),
                         follower_unavailable_at=row.get("follower_unavailable_at"),
                         retry_at=None, retry_attempts=0, rate_limit_attempts=0,
                         rate_limit_stage=None, retry_source=None, retry_after=None)
            state["follower_candidate"] = None
        except (request_exception, ValueError, TypeError, RuntimeError) as exc:
            state.update(reason=type(exc).__name__, **transient_retry_policy(prior, clock()),
                         rate_limit_stage=None, rate_limit_attempts=0, retry_after=None)
            if isinstance(exc, rate_limited_type):
                policy = exc.policy or rate_limit_policy(exc.stage, str(exc.retry_seconds), clock())
                state.update(rate_limit_stage=exc.stage, retry_at=policy["retry_at"],
                             retry_attempts=0, rate_limit_attempts=policy["attempts"],
                             retry_source=policy["retry_source"], retry_after=policy["retry_after"],
                             updated_at=policy["observed_at"])
                cooldown = {**policy, "updated_at": policy["observed_at"]}
                cooldowns[exc.stage] = cooldown
                batch["cooldown_updates"][exc.stage] = cooldown
                batch["stop_reason"] = "steam_rate_limited"
                break
    batch["follower_candidates"] = [queued[appid] for appid in sorted(queued)]
    return batch


def dispatch(master: dict, state: dict, *, token: str, target: str,
             session=None, now: datetime | None = None, max_seconds: int = 600,
             monotonic, session_factory, clock, is_twitch_qualified, signature,
             stamp, request_exception, environ_get) -> dict:
    session = session or session_factory()
    now = now or clock()
    batch = {"schema_version": 1, "generated_at": stamp(now), "records": [],
             "state_updates": {}, "stop_reason": "complete"}
    deadline = monotonic() + max_seconds
    by_id = {str(row["appid"]): row for row in master["games"]}
    for aid, pending in (state.get("games") or {}).items():
        if monotonic() >= deadline:
            batch["stop_reason"] = "deadline"
            break
        row = by_id.get(aid)
        if pending.get("status") != "accepted" or not is_twitch_qualified(row):
            continue
        sig = signature(row)
        previous = pending.get("content_dispatch") or {}
        if previous.get("status") == "dispatched" and previous.get("signature") == sig:
            continue
        receipt = {"status": "pending", "signature": sig, "attempted_at": stamp(now),
                   "target_repository": target, "event_type": "steam_game_twitch_discovered"}
        update = {"status": "accepted", "updated_at": stamp(now),
                  "content_signature": sig, "content_dispatch": receipt}
        batch["state_updates"][aid] = update
        if not token or not target:
            receipt["reason"] = "content_dispatch_not_configured"
            continue
        payload = {"event_type": "steam_game_twitch_discovered", "client_payload": {
            "appid": int(aid), "official_followers": row["followers"],
            "release_date": row["release_start"], "official_checked_at_taipei": row["follower_checked_at"],
            "twitch_admission": row["twitch_admission"], "signature": sig,
            "source_repository": environ_get("GITHUB_REPOSITORY", "danielet087/game-trend-radar-backend"),
        }}
        if row.get("follower_status") == "unavailable_group_id":
            payload["client_payload"].update({key: row.get(key) for key in (
                "follower_status", "follower_unavailable_at", "follower_source", "group_id64",
                "official_ge5000",
            )})
        try:
            response = session.post(f"https://api.github.com/repos/{target}/dispatches", json=payload,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                         "X-GitHub-Api-Version": "2022-11-28"}, timeout=25)
            receipt["http"] = response.status_code
            receipt["status"] = "dispatched" if response.status_code == 204 else "pending"
        except request_exception as exc:
            receipt["reason"] = type(exc).__name__
    return batch


def apply_queue_batch(master: dict, state: dict, checkpoint: dict, batch: dict, *,
                      apply_batch, aware_time, sync_twitch_queue, fallback_allowed):
    """Apply the same frozen evidence before reconciling its Followers queue."""
    master, state = apply_batch(master, state, batch)
    now = aware_time(batch.get("generated_at"))
    if "follower_candidates" in batch:
        successful = batch
        if any(row.get("follower_status") == "unavailable_group_id" for row in batch.get("records", [])):
            by_id = {row["appid"]: row for row in master["games"]}
            successful = {**batch, "records": [by_id[row["appid"]] for row in batch["records"]
                         if row["appid"] in by_id and fallback_allowed(by_id[row["appid"]])
                         and by_id[row["appid"]].get("twitch_admission") == row.get("twitch_admission")]}
        checkpoint = sync_twitch_queue(checkpoint, successful, now)
    return master, state, checkpoint
