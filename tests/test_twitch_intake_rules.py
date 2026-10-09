"""Boundary tests for frozen Twitch intake evidence without legacy scripts."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from radar_core.domain import twitch_admission as core
from radar_backend.domain import twitch_intake as rules
from radar_backend.domain.adult_exclusions import is_disallowed
from radar_backend.domain.candidate_screening import is_explicit_sex_game
from radar_backend.domain.release_window import parse_release_window
from radar_backend.domain.store_release import parse_store_release_detail

NOW = datetime(2026, 10, 2, 9, tzinfo=timezone.utc)
TAIPEI = ZoneInfo("Asia/Taipei")
SHA = "a" * 40


def enrollment():
    return {"source": "igdb_first_release_date", "observed_at": "2026-10-01T08:00:00Z",
            "viewer_count": 8000, "min_viewers": 7000}


def proof():
    return {"schema_version": 1, "method": core.METHOD, "appid": 123,
            "twitch_game_id": "22", "igdb_id": "33", "checked_at": "2026-10-02T08:00:00Z",
            "source_frontend_commit": SHA, "source_enrollment": enrollment()}


def snapshot():
    tracking = {"schema_version": 1, "games": {"22": {
        "game_id": "22", "igdb_id": "33", "tracking_sources": {"twitch_new": {
            "source": "twitch_new", "status": "active", "expires_at": "2026-10-20T00:00:00Z",
            "enrollment": enrollment()}}}}}
    discovery = {"schema_version": 1, "steam_source_id": "777", "games": {"22": {
        "twitch_game_id": "22", "igdb_id": "33", "active": True, "status": "matched",
        "method": core.METHOD, "checked_at": "2026-10-02T08:00:00Z",
        "twitch_enrollment": enrollment(), "steam_appids": ["123"], "links": [{
            "external_game_id": "444", "external_game_source": "777", "uid": "123",
            "steam_appid": "123", "game": "33"}]}}}
    return discovery, tracking, {"count": 0, "games": []}


def validate(discovery, tracking, catalog, commit=SHA, now=NOW, **overrides):
    ports = {"decimal_id": core.decimal_id, "aware_time": core.aware_time,
             "valid_enrollment": core.valid_enrollment,
             "normalize_twitch_admission": core.normalize_twitch_admission,
             "validate_twitch_snapshot": core.validate_twitch_snapshot, "method": core.METHOD}
    return rules.validate_snapshot(discovery, tracking, catalog, commit, now, **(ports | overrides))


def steam(day=None):
    day = day or NOW.astimezone(TAIPEI).date()
    instant = datetime.combine(day, datetime.min.time(), tzinfo=TAIPEI).astimezone(timezone.utc)
    item = {"appid": 123, "success": 1, "name": "Test game", "content_descriptorids": [],
            "release": {"coming_soon_display": "date_full", "is_coming_soon": False,
                        "steam_release_date": int(instant.timestamp())},
            "basic_info": {"short_description": "An adventure."}}
    details = {"steam_appid": 123, "type": "game", "name": "Fallback name",
               "release_date": {"date": day.isoformat()}, "content_descriptors": {"ids": []}}
    return item, details


def candidate(item=None, details=None, followers=12, checked_at="2026-10-02T06:00:00Z",
              admission=None, blocked=None, **overrides):
    if item is None:
        item, default_details = steam()
        details = default_details if details is None else details
    ports = {"decimal_id": core.decimal_id, "is_disallowed": is_disallowed,
             "is_explicit_sex_game": is_explicit_sex_game,
             "parse_store_release_detail": parse_store_release_detail,
             "parse_release_window": parse_release_window,
             "resolve_store_release_day": core.resolve_store_release_day,
             "aware_time": core.aware_time, "is_twitch_qualified": core.is_twitch_qualified,
             "stamp": rules.stamp, "taipei": TAIPEI,
             "tw_store_date_authority": core.TW_STORE_DATE_AUTHORITY,
             "tw_store_date_provider": core.TW_STORE_DATE_PROVIDER}
    return rules.build_candidate(123, proof() if admission is None else admission,
                                 item, details, followers, checked_at, NOW,
                                 set() if blocked is None else blocked, **(ports | overrides))


def retained(prior, admission=None, now=NOW, blocked=None, **overrides):
    ports = {"decimal_id": core.decimal_id,
             "normalize_twitch_admission": core.normalize_twitch_admission,
             "identity_signature": rules.identity_signature, "is_disallowed": is_disallowed,
             "aware_time": core.aware_time, "stamp": rules.stamp,
             "is_twitch_qualified": core.is_twitch_qualified,
             "follower_candidate": rules.follower_candidate, "taipei": TAIPEI}
    return rules.retained_follower_candidate(
        123, proof() if admission is None else admission, prior, now,
        set() if blocked is None else blocked, **(ports | overrides))


@pytest.mark.parametrize("offset", [-12, -8, 0, 8, 14])
def test_stamp_preserves_instant_across_timezone_offsets(offset):
    instant = NOW.astimezone(timezone(timedelta(hours=offset)))
    assert rules.stamp(instant) == "2026-10-02T09:00:00Z"


def test_stamp_uses_timezone_port_and_does_not_round_microseconds():
    zone = object()
    encoded = SimpleNamespace(isoformat=Mock(return_value="2026-01-01T12:01:02.123456+00:00"))
    instant = SimpleNamespace(astimezone=Mock(return_value=encoded))
    assert rules.stamp(instant, timezone_type=SimpleNamespace(utc=zone)) == "2026-01-01T12:01:02.123456Z"
    instant.astimezone.assert_called_once_with(zone)


def test_snapshot_accepts_cross_checked_ids_and_keeps_inputs_unchanged():
    documents = snapshot()
    before = deepcopy(documents)
    assert validate(*documents) == [(123, proof())]
    assert documents == before


@pytest.mark.parametrize("commit", [None, 0, True, "main", "a" * 39, "a" * 41, "A" * 40, "g" * 40])
def test_snapshot_requires_exact_immutable_lowercase_commit(commit):
    decimal = Mock(side_effect=AssertionError("must reject before rule collaborators"))
    with pytest.raises(ValueError, match="immutable frontend commit"):
        validate(*snapshot(), commit=commit, decimal_id=decimal)
    decimal.assert_not_called()


@pytest.mark.parametrize("document,field,value,message", [
    (0, "schema_version", 2, "discovery"), (0, "games", [], "discovery"),
    (1, "schema_version", None, "tracking"), (1, "games", [], "tracking"),
    (2, "games", {}, "public catalog"), (2, "count", 1, "public catalog"),
])
def test_snapshot_shape_gates_before_rule_collaborators(document, field, value, message):
    documents = snapshot()
    documents[document][field] = value
    decimal = Mock(side_effect=AssertionError("unexpected collaborator"))
    with pytest.raises(ValueError, match=message):
        validate(*documents, decimal_id=decimal)


@pytest.mark.parametrize("field,value", [
    ("status", "pending"), ("active", 1), ("active", False),
    ("twitch_game_id", "23"), ("twitch_game_id", True), ("method", "name_search"),
    ("igdb_id", "0"), ("checked_at", "2026-10-03T00:00:00Z"),
    ("checked_at", "2026-10-02T08:00:00"), ("steam_appids", []),
    ("steam_appids", [True]), ("steam_appids", ["１２３"]),
    ("steam_appids", "123"), ("links", []), ("links", {}), ("links", [None]),
])
def test_snapshot_discovery_row_must_be_authoritative(field, value):
    discovery, tracking, catalog = snapshot()
    discovery["games"]["22"][field] = value
    assert validate(discovery, tracking, catalog) == []


@pytest.mark.parametrize("field,value", [
    ("source", "steam_recent_release"), ("status", "expired"),
    ("expires_at", "2026-10-02T09:00:00Z"), ("enrollment", None),
])
def test_snapshot_tracking_membership_gate(field, value):
    discovery, tracking, catalog = snapshot()
    tracking["games"]["22"]["tracking_sources"]["twitch_new"][field] = value
    assert validate(discovery, tracking, catalog) == []


@pytest.mark.parametrize("field,value", [
    ("steam_appid", "124"), ("uid", "124"), ("external_game_id", "0"),
    ("external_game_source", "778"), ("game", "34"),
])
def test_snapshot_every_external_link_must_match_declared_identity(field, value):
    discovery, tracking, catalog = snapshot()
    discovery["games"]["22"]["links"][0][field] = value
    assert validate(discovery, tracking, catalog) == []


def test_snapshot_deduplicates_categories_selects_newest_proof_and_sorts_appids():
    discovery, tracking, catalog = snapshot()
    second = deepcopy(discovery["games"]["22"])
    second.update(twitch_game_id="23", checked_at="2026-10-02T08:30:00Z", steam_appids=["123", "2"])
    second["links"].append({**second["links"][0], "steam_appid": "2", "uid": "2"})
    discovery["games"]["23"] = second
    tracking["games"]["23"] = {**deepcopy(tracking["games"]["22"]), "game_id": "23"}
    accepted = validate(discovery, tracking, catalog)
    assert [appid for appid, _ in accepted] == [2, 123]
    assert all(row["twitch_game_id"] == "23" for _, row in accepted)


def test_snapshot_equal_checked_time_keeps_first_category_and_normalizer_reference():
    discovery, tracking, catalog = snapshot()
    discovery["games"]["23"] = {**deepcopy(discovery["games"]["22"]), "twitch_game_id": "23"}
    tracking["games"]["23"] = {**deepcopy(tracking["games"]["22"]), "game_id": "23"}
    returned = []
    def normalize(value, appid):
        returned.append(value)
        return value
    accepted = validate(discovery, tracking, catalog, normalize_twitch_admission=normalize)
    assert accepted[0][1] is returned[0]
    assert len(returned) == 2


@pytest.mark.parametrize("accept_normalization,accept_snapshot", [(False, True), (True, False)])
def test_snapshot_requires_both_core_normalization_and_cross_validation(accept_normalization, accept_snapshot):
    normalizer = Mock(side_effect=core.normalize_twitch_admission if accept_normalization else lambda *_: None)
    validation = Mock(return_value=accept_snapshot)
    assert validate(*snapshot(), normalize_twitch_admission=normalizer, validate_twitch_snapshot=validation) == []
    assert validation.call_count == int(accept_normalization)


def test_candidate_keeps_proof_reference_and_never_changes_steam_documents():
    item, details = steam()
    admission = proof()
    originals = deepcopy((item, details, admission))
    row, status = candidate(item, details, admission=admission)
    assert status == "accepted" and row["twitch_admission"] is admission
    assert (item, details, admission) == originals
    assert row["followers"] == 12 and row["official_ge5000"] is False
    assert row["release_date_verified_at"] == row["post_followers_store_verified_at"] == rules.stamp(NOW)


@pytest.mark.parametrize("target,field,value,reason", [
    ("item", "appid", True, "steam_store_unavailable"),
    ("item", "appid", 124, "steam_store_unavailable"),
    ("item", "success", 0, "steam_store_unavailable"),
    ("item", "visible", False, "steam_store_unavailable"),
    ("details", "steam_appid", "123", "steam_identity_mismatch"),
    ("details", "steam_appid", 124, "steam_identity_mismatch"),
    ("details", "type", "dlc", "not_a_steam_game"),
    ("details", "content_descriptors", None, "steam_content_descriptors_unavailable"),
    ("details", "content_descriptors", {"ids": {}}, "steam_content_descriptors_unavailable"),
    ("details", "release_date", None, "uncertain_taiwan_store_date"),
    ("details", "release_date", {"date": "October 2026"}, "uncertain_taiwan_store_date"),
    ("details", "release_date", {"date": "Coming soon"}, "uncertain_taiwan_store_date"),
])
def test_candidate_identity_content_and_date_gates(target, field, value, reason):
    item, details = steam()
    (item if target == "item" else details)[field] = value
    assert candidate(item, details) == (None, reason)


@pytest.mark.parametrize("details", [{}, None, [], "bad"])
def test_candidate_requires_official_appdetails_object(details):
    item, _ = steam()
    assert candidate(item, details) == (None, "steam_type_unavailable")


@pytest.mark.parametrize("followers", [None, True, False, -1, 3.0, "3"])
def test_candidate_requires_an_official_nonnegative_integer_count(followers):
    assert candidate(followers=followers) == (None, "official_followers_unavailable")


@pytest.mark.parametrize("followers,ge5000", [(0, False), (1, False), (4999, False), (5000, True), (5001, True)])
def test_candidate_twitch_membership_preserves_independent_official_count(followers, ge5000):
    row, reason = candidate(followers=followers)
    assert reason == "accepted" and row["official_ge5000"] is ge5000
    assert row["followers"] == followers


@pytest.mark.parametrize("checked", [None, "", "2026-10-02T06:00:00", "invalid"])
def test_candidate_rejects_count_without_an_aware_check_time(checked):
    assert candidate(checked_at=checked) == (None, "official_followers_unavailable")


def test_candidate_preserves_existing_future_checked_at_contract():
    row, reason = candidate(checked_at="2027-10-02T06:00:00Z")
    assert reason == "accepted" and row["follower_checked_at"] == "2027-10-02T06:00:00Z"


@pytest.mark.parametrize("offset,reason", [(-31, "outside_new_game_window"), (-30, "accepted"),
                                         (0, "accepted"), (365, "accepted"), (366, "outside_new_game_window")])
def test_candidate_window_has_exact_inclusive_taiwan_boundaries(offset, reason):
    item, details = steam(NOW.astimezone(TAIPEI).date() + timedelta(days=offset))
    assert candidate(item, details)[1] == reason


@pytest.mark.parametrize("adult_source", ["descriptor", "item_descriptor", "blocked", "strong_tags"])
def test_candidate_keeps_all_adult_exclusion_sources(adult_source):
    item, details = steam()
    blocked = set()
    if adult_source == "descriptor":
        details["content_descriptors"]["ids"] = [3]
    elif adult_source == "item_descriptor":
        item["content_descriptorids"] = [4]
    elif adult_source == "blocked":
        blocked = {123}
    else:
        item["tagids"] = [12095, 9130]
        item["basic_info"]["short_description"] = "An explicit sexual game."
    assert candidate(item, details, blocked=blocked) == (None, "adult_content")


def test_candidate_adult_short_circuit_precedes_screening_and_dates():
    screening = Mock(side_effect=AssertionError("adult ledger must short-circuit"))
    dates = Mock(side_effect=AssertionError("adult ledger must precede dates"))
    assert candidate(is_disallowed=lambda *_: True, is_explicit_sex_game=screening,
                     parse_store_release_detail=dates) == (None, "adult_content")
    screening.assert_not_called()
    dates.assert_not_called()


def test_candidate_screening_receives_copy_with_tagids_fallback_without_mutation():
    item, details = steam()
    item["tagids"] = [17, 18]
    screening = Mock(return_value=False)
    candidate(item, details, is_explicit_sex_game=screening)
    screened = screening.call_args.args[0]
    assert screened is not item and screened["tags"] == [{"tagid": 17}, {"tagid": 18}]
    assert screened["release"] is item["release"] and "tags" not in item


@pytest.mark.parametrize("store_confirmed", [True, False])
def test_candidate_missing_protobuf_false_requires_same_app_tw_release_confirmation(store_confirmed):
    item, details = steam()
    item["release"].pop("is_coming_soon")
    item["release"]["coming_soon_display"] = "date_quarter"
    details["release_date"]["coming_soon"] = not store_confirmed
    parser = Mock(wraps=parse_store_release_detail)
    row, reason = candidate(item, details, parse_store_release_detail=parser)
    assert reason == ("accepted" if store_confirmed else "uncertain_steam_date")
    store_item = parser.call_args.args[0]
    assert (store_item is not item) is store_confirmed
    assert "is_coming_soon" not in item["release"]
    if store_confirmed:
        assert store_item["release"]["is_coming_soon"] is False


@pytest.mark.parametrize("raw", [True, None, [], "bad", "999999999999999999999999999999999"])
def test_candidate_released_timestamp_fallback_retains_invalid_timestamp_gates(raw):
    item, details = steam()
    item["release"]["steam_release_date"] = raw
    details["release_date"]["coming_soon"] = False
    assert candidate(item, details, parse_store_release_detail=lambda *_a, **_k: {"exact": False}) == (None, "uncertain_steam_date")


def test_candidate_released_exact_tw_day_retains_genuine_future_instant():
    item, details = steam()
    item["release"]["steam_release_date"] += 2 * 86400
    item["release"].update(coming_soon_display="date_quarter", is_coming_soon=True)
    details["release_date"]["coming_soon"] = False
    before = deepcopy(item)
    row, reason = candidate(item, details)
    assert reason == "accepted"
    assert row["release_start"] == "2026-10-02" and row["release_timestamp_taipei_date"] == "2026-10-04"
    assert row["release_date_conflict"] is True
    assert row["release_display_provider"] == core.TW_STORE_DATE_PROVIDER
    assert row["release_date_basis"] == "steam_store_browse_release_timestamp"
    assert row["release_time_utc"] == "2026-10-03T16:00:00Z"
    assert item == before


@pytest.mark.parametrize("raw", ["2026 年 10 月 2 日", "2026年10月2日", " 2026 年 10 月 2 日 ", "2 Oct 2026"])
def test_candidate_normalizes_only_exact_visible_day_formats(raw):
    item, details = steam()
    details["release_date"]["date"] = raw
    row, reason = candidate(item, details)
    assert reason == "accepted" and row["release_start"] == "2026-10-02"


@pytest.mark.parametrize("item_name,details_name,expected", [("Name", "Fallback", "Name"),
                                                           ("", "Fallback", "Fallback"),
                                                           (None, "", "Steam App 123")])
def test_candidate_official_name_fallback_order(item_name, details_name, expected):
    item, details = steam()
    item["name"], details["name"] = item_name, details_name
    row, reason = candidate(item, details)
    assert reason == "accepted" and row["name"] == row["name_en"] == expected


def test_candidate_core_date_conflict_and_final_admission_ports():
    assert candidate(resolve_store_release_day=lambda *_a, **_k: None) == (None, "steam_date_conflict")
    qualify = Mock(return_value=False)
    assert candidate(is_twitch_qualified=qualify) == (None, "invalid_admission")
    qualify.assert_called_once()
    assert qualify.call_args.args[0]["followers"] == 12


@pytest.mark.parametrize("source", ["games", "official_results", "verified"])
def test_cached_follower_reads_supported_document_maps(source):
    rows = {source: {"123": {"official_followers": 0, "official_checked_at_taipei": "2026-10-02T14:00:00+08:00"}}}
    assert rules.cached_follower(123, [rows], NOW, aware_time=core.aware_time) == (0, "2026-10-02T14:00:00+08:00")


@pytest.mark.parametrize("count", [None, True, False, -1, "12", 12.0])
def test_cached_follower_excludes_nonofficial_count_types(count):
    document = {"games": {"123": {"followers": count, "checked_at": "2026-10-02T06:00:00Z"}}}
    assert rules.cached_follower(123, [document], NOW, aware_time=core.aware_time) is None


@pytest.mark.parametrize("checked", [None, "bad", "2026-10-02T06:00:00", "2026-10-02T09:00:01Z"])
def test_cached_follower_excludes_bad_or_future_check_time(checked):
    document = {"games": {"123": {"followers": 1, "checked_at": checked}}}
    assert rules.cached_follower(123, [document], NOW, aware_time=core.aware_time) is None


def test_cached_follower_latest_time_and_equal_time_tie_preserve_input_order():
    def document(count, checked):
        return {"games": {"123": {"followers": count, "checked_at": checked}}}
    documents = [document(100, "2026-10-02T06:00:00Z"), document(1, "2026-10-02T09:00:00Z"),
                 document(99, "2026-10-02T17:00:00+08:00")]
    before = deepcopy(documents)
    assert rules.cached_follower(123, documents, NOW, aware_time=core.aware_time) == (1, "2026-10-02T09:00:00Z")
    assert documents == before


def test_cached_follower_does_not_fall_back_after_present_null_followers_or_truthy_wrong_map():
    document = {"games": {"123": {"followers": None, "official_followers": 20,
                                "checked_at": "2026-10-02T06:00:00Z"}}}
    assert rules.cached_follower(123, [document], NOW, aware_time=core.aware_time) is None
    document = {"games": [1], "verified": {"123": {"followers": 20, "checked_at": "2026-10-02T06:00:00Z"}}}
    assert rules.cached_follower(123, [document], NOW, aware_time=core.aware_time) is None


def test_signature_has_exact_json_wire_shape_and_ignores_unrelated_metadata():
    row = {"appid": 123, "followers": 12, "release_start": "2026-10-02", "twitch_admission": proof(), "name": "別名"}
    expected = hashlib.sha256(json.dumps([123, 12, "2026-10-02", proof()], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert rules.signature(row) == expected
    assert rules.signature(row | {"name": "new", "other": object()}) == expected


@pytest.mark.parametrize("field,value", [("appid", 124), ("followers", 13), ("release_start", "2026-10-03"), ("twitch_admission", {})])
def test_signature_changes_only_the_four_frozen_content_fields(field, value):
    row = {"appid": 123, "followers": 12, "release_start": "2026-10-02", "twitch_admission": proof()}
    assert rules.signature(row | {field: value}) != rules.signature(row)


@pytest.mark.parametrize("field", ["appid", "followers", "release_start", "twitch_admission"])
def test_signature_missing_fields_remain_key_errors(field):
    row = {"appid": 123, "followers": 12, "release_start": "2026-10-02", "twitch_admission": proof()}
    row.pop(field)
    with pytest.raises(KeyError, match=field):
        rules.signature(row)


def test_identity_signature_ignores_only_hourly_frontend_commit_and_keeps_inputs():
    admission = proof()
    before = deepcopy(admission)
    assert rules.identity_signature(admission) == rules.identity_signature(admission | {"source_frontend_commit": "b" * 40})
    assert rules.identity_signature(admission) != rules.identity_signature(admission | {"checked_at": "2026-10-02T08:01:00Z"})
    assert admission == before


@pytest.mark.parametrize("helper", [rules.signature, rules.identity_signature])
def test_signature_ports_keep_json_arguments_byte_encoding_and_hash_order(helper):
    row = {"appid": 123, "followers": 0, "release_start": "2026-10-02", "twitch_admission": proof()}
    encoder = SimpleNamespace(dumps=Mock(return_value="遊戲"))
    digest = SimpleNamespace(hexdigest=Mock(return_value="digest"))
    hashing = SimpleNamespace(sha256=Mock(return_value=digest))
    assert helper(row, json_module=encoder, hashlib_module=hashing) == "digest"
    assert encoder.dumps.call_args.kwargs == {"sort_keys": True, "separators": (",", ":")}
    hashing.sha256.assert_called_once_with("遊戲".encode())


def test_follower_candidate_removes_only_count_evidence_and_keeps_independent_deepcopies():
    row, _ = candidate()
    row["custom"] = {"nested": [1]}
    original = deepcopy(row)
    queued = rules.follower_candidate(row)
    metadata = queued["steam_candidate"]
    assert row == original
    for field in ("followers", "follower_checked_at", "follower_source", "official_ge5000"):
        assert field not in metadata
    assert metadata["custom"] == {"nested": [1]} and metadata["custom"] is not row["custom"]
    assert queued["twitch_admission"] == metadata["twitch_admission"]
    assert queued["twitch_admission"] is not metadata["twitch_admission"]
    assert queued["queue_source"] == "twitch_steam_discovery" and queued["group_id64"] is None
    assert queued["release_date"] == metadata["release_start"] and queued["steam_url"] == metadata["store_url"]


def test_retained_candidate_rebinds_new_frontend_proof_without_restoring_zero_count():
    row, _ = candidate()
    prior = {"status": "pending", "reason": "steam_store_unavailable", "follower_candidate": rules.follower_candidate(row)}
    before = deepcopy(prior)
    admission = proof() | {"source_frontend_commit": "b" * 40}
    queued = retained(prior, admission)
    assert queued is not None and prior == before
    assert queued["twitch_admission"] == admission and queued["twitch_admission"] is not admission
    assert "followers" not in queued["steam_candidate"]
    assert queued["steam_candidate"]["release_time_utc"] == row["release_time_utc"]


@pytest.mark.parametrize("reason", ["adult_content", "not_a_steam_game", "outside_new_game_window",
    "uncertain_steam_date", "uncertain_taiwan_store_date", "steam_date_conflict",
    "steam_identity_mismatch", "invalid_admission"])
def test_retention_cannot_revive_a_permanent_metadata_rejection(reason):
    row, _ = candidate()
    prior = {"reason": reason, "follower_candidate": rules.follower_candidate(row)}
    normalize = Mock(side_effect=AssertionError("reason gate must precede proof"))
    assert retained(prior, normalize_twitch_admission=normalize) is None
    normalize.assert_not_called()


@pytest.mark.parametrize("reason", ["steam_store_unavailable", "steam_type_unavailable", "steam_content_descriptors_unavailable", "http_error", "deadline", None])
def test_retention_keeps_exact_prior_evidence_through_temporary_failures(reason):
    row, _ = candidate()
    prior = {"reason": reason, "follower_candidate": rules.follower_candidate(row)}
    assert retained(prior) == prior["follower_candidate"]


@pytest.mark.parametrize("target,field,value", [
    ("prior", "status", "excluded"), ("candidate", "queue_source", "other"),
    ("candidate", "appid", 124), ("candidate", "appid", True),
    ("candidate", "steam_candidate", []), ("candidate", "twitch_admission", {}),
    ("candidate", "release_date", "2026-10-03"), ("candidate", "steam_url", "https://example.test/"),
    ("metadata", "appid", 124), ("metadata", "twitch_admission", {}),
    ("metadata", "store_url", "https://example.test/"),
    ("metadata", "release_date_verified_at", "2026-10-02T09:00:01Z"),
    ("metadata", "release_date_verified_at", "bad"), ("metadata", "release_time_utc", "bad"),
    ("metadata", "release_start", "bad"), ("metadata", "sexual_content_screened", False),
    ("metadata", "content_descriptorids", [3]),
])
def test_retention_rechecks_queue_identity_adult_date_and_admission(target, field, value):
    row, _ = candidate()
    prior = {"status": "pending", "follower_candidate": rules.follower_candidate(row)}
    target_value = prior if target == "prior" else prior["follower_candidate"]
    if target == "metadata":
        target_value = target_value["steam_candidate"]
    target_value[field] = value
    assert retained(prior) is None


@pytest.mark.parametrize("offset,accepted", [(-31, False), (-30, True), (365, True), (366, False)])
def test_retention_rechecks_window_at_current_frozen_observation(offset, accepted):
    day = NOW.astimezone(TAIPEI).date() + timedelta(days=offset)
    item, details = steam(day)
    # The older in-window intake observation differs from the retained observation.
    row, _ = candidate(item, details, is_twitch_qualified=lambda _row: True)
    if row is None:
        row, _ = candidate()
        row.update(release_start=day.isoformat(), release_end=day.isoformat(), release_raw=day.isoformat(),
                   release_time_utc=rules.stamp(datetime.combine(day, datetime.min.time(), tzinfo=TAIPEI)),
                   release_timestamp_taipei_date=day.isoformat())
    prior = {"follower_candidate": rules.follower_candidate(row)}
    assert (retained(prior) is not None) is accepted


def test_retention_current_identity_change_and_new_adult_ledger_cannot_reuse_prior():
    row, _ = candidate()
    prior = {"follower_candidate": rules.follower_candidate(row)}
    assert retained(prior, proof() | {"igdb_id": "34"}) is None
    assert retained(prior, blocked={123}) is None


def test_retention_reuses_current_final_gate_with_placeholder_only_in_memory():
    row, _ = candidate()
    prior = {"follower_candidate": rules.follower_candidate(row)}
    qualifier = Mock(return_value=True)
    outgoing = Mock(return_value={"sentinel": True})
    result = retained(prior, is_twitch_qualified=qualifier, follower_candidate=outgoing)
    assert result == {"sentinel": True}
    metadata = qualifier.call_args.args[0]
    assert metadata["followers"] == 0 and metadata["follower_checked_at"] == rules.stamp(NOW)
    assert outgoing.call_args.args[0] is metadata
    assert "followers" not in prior["follower_candidate"]["steam_candidate"]


def test_retention_invalid_date_types_remain_guarded_without_broad_error_swallowing():
    row, _ = candidate()
    prior = {"follower_candidate": rules.follower_candidate(row)}
    for day in (None, 0, "bad"):
        broken = deepcopy(prior)
        broken["follower_candidate"]["release_date"] = day
        broken["follower_candidate"]["steam_candidate"]["release_start"] = day
        assert retained(broken) is None
    with pytest.raises(RuntimeError, match="date collaborator"):
        retained(prior, datetime_type=SimpleNamespace(fromisoformat=Mock(side_effect=RuntimeError("date collaborator"))))
