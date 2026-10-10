"""Missing GroupID falls back only to immutable, independently qualified Twitch evidence."""
from copy import deepcopy
from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import Mock

import pytest

from radar_core.domain.twitch_admission import has_unavailable_group_followers, is_twitch_qualified
from radar_core.publication import SubprocessGitRepository, snapshot_revision
from radar_backend.adapters import twitch_intake as canonical
from scripts import import_twitch_steam_discoveries as legacy
from radar_backend.domain.twitch_intake import cached_group
from radar_backend.state.official_merge import merge_master
from radar_backend.publication.steam import publish_catalog, read_json, write_json
from tests.test_twitch_steam_admission import NOW, SHA, proof, steam, fake_frontend, metadata_responses, row
from tests.test_unified_twitch_followers import checkpoint, queue_inputs
from tests.test_steam_publication import frontend, git, published_json

GID = "103582791429521531"


def unknown():
    item, details = steam()
    candidate, reason = canonical.build_candidate(123, proof(), item, details, None, None, NOW, set())
    assert reason == "accepted"
    return candidate


def collect(root, *, surface=canonical, caches=(), previous=None, master=None):
    client = Mock()
    client.get.side_effect = list(metadata_responses(123))
    batch = surface.collect(root, SHA, master or {"games": []}, previous or {}, session=client,
        now=NOW, caches=list(caches), blocked=set(), sleep=lambda _: None)
    return batch, client


@pytest.mark.parametrize("surface", [canonical, legacy])
def test_missing_group_directly_accepts_nullable_record_with_no_community_request(tmp_path, surface):
    fake_frontend(tmp_path)
    batch, client = collect(tmp_path, surface=surface)
    accepted, = batch["records"]
    assert accepted == unknown() and is_twitch_qualified(accepted)
    assert has_unavailable_group_followers(accepted)
    assert accepted["followers"] is None and accepted["follower_checked_at"] is None
    assert accepted["follower_source"] is None and accepted["official_ge5000"] is False
    assert accepted["follower_unavailable_at"] == "2026-10-02T09:00:00Z"
    assert batch["follower_candidates"] == []
    assert batch["state_updates"]["123"]["reason"] == "twitch_verified_without_group"
    assert client.get.call_count == 2
    assert all("steamcommunity" not in call.args[0] for call in client.get.call_args_list)


@pytest.mark.parametrize("field", ["games", "verified", "official_results", "official_growth_observations", "pending_candidates", "unresolved_candidates"])
@pytest.mark.parametrize("identity", [{"group_id64": GID}, {"official_group_id64": GID}, {"group_short_id": 123}])
def test_known_group_in_any_checkpoint_keeps_waiting_for_true_count(tmp_path, field, identity):
    fake_frontend(tmp_path)
    batch, _ = collect(tmp_path, caches=[{field: {"123": {"appid": 123, **identity}}}])
    assert not batch["records"]
    candidate, = batch["follower_candidates"]
    assert candidate["group_id64"] == GID
    assert "followers" not in candidate["steam_candidate"]
    assert batch["state_updates"]["123"]["reason"] == "queued_official_followers"


@pytest.mark.parametrize("nested", ["follower_candidate", "normal_candidate", "steam_candidate"])
def test_previously_queued_nested_group_is_not_lost(tmp_path, nested):
    fake_frontend(tmp_path)
    batch, _ = collect(tmp_path, previous={"games": {"123": {nested: {"appid": 123, "group_id64": GID}}}})
    assert batch["records"] == [] and batch["follower_candidates"][0]["group_id64"] == GID


@pytest.mark.parametrize("count", [0, 88])
def test_real_cache_count_even_zero_remains_numeric(tmp_path, count):
    fake_frontend(tmp_path)
    batch, _ = collect(tmp_path, caches=[{"official_growth_observations": {"123": {
        "followers": count, "follower_checked_at": "2026-10-02T08:10:00Z"}}}])
    accepted, = batch["records"]
    assert accepted["followers"] == count and accepted["follower_source"] is not None
    assert "follower_status" not in accepted and "follower_unavailable_at" not in accepted


@pytest.mark.parametrize("mutation", ["threshold", "expiry", "mapped_appid", "igdb", "proof_missing"])
def test_unqualified_or_mismatched_twitch_evidence_never_admits_unknown(tmp_path, mutation):
    fake_frontend(tmp_path)
    discovery_path = tmp_path / "data/twitch_steam_discovery.json"
    tracking_path = tmp_path / "data/twitch_tracking.json"
    discovery, tracking = json.loads(discovery_path.read_text()), json.loads(tracking_path.read_text())
    source = tracking["games"]["22"]["tracking_sources"]["twitch_new"]
    if mutation == "threshold":
        source["enrollment"]["viewer_count"] = 6999
        discovery["games"]["22"]["twitch_enrollment"]["viewer_count"] = 6999
    elif mutation == "expiry": source["expires_at"] = NOW.isoformat()
    elif mutation == "mapped_appid": discovery["games"]["22"]["links"][0]["steam_appid"] = "124"
    elif mutation == "igdb": discovery["games"]["22"]["links"][0]["game"] = "44"
    else: source.pop("enrollment")
    discovery_path.write_text(json.dumps(discovery)); tracking_path.write_text(json.dumps(tracking))
    batch, client = collect(tmp_path)
    assert batch["records"] == [] and not client.get.called


@pytest.mark.parametrize("mutation", ["adult", "dlc", "date", "wrong_identity", "no_descriptors"])
def test_unknown_count_keeps_official_steam_gates(mutation):
    item, details = steam()
    if mutation == "adult": details["content_descriptors"]["ids"] = [3]
    elif mutation == "dlc": details["type"] = "dlc"
    elif mutation == "date": details["release_date"]["date"] = "2026"
    elif mutation == "wrong_identity": details["steam_appid"] = 124
    else: details.pop("content_descriptors")
    accepted, _ = canonical.build_candidate(123, proof(), item, details, None, None, NOW, set())
    assert accepted is None


def test_success_clears_only_its_normal_and_parked_queue_then_does_not_reappear(tmp_path):
    fake_frontend(tmp_path)
    normal = {"appid": 123, "name": "Same title", "release_date": "2026-10-02", "group_id64": None, "queue_source": "normal"}
    unrelated = {"appid": 124, "name": "Other", "release_date": "2026-10-02", "group_id64": GID, "queue_source": "normal"}
    cp = checkpoint([normal, unrelated]); cp["unresolved_candidates"] = {"123": {**normal, "status": "missing_group"}}
    original = deepcopy(cp)
    prior = {"games": {"123": {"status": "pending", "reason": "queued_official_followers",
        "retry_at": (NOW + timedelta(days=1)).isoformat(), "twitch_admission": proof()}}}
    batch, _ = collect(tmp_path, caches=[cp], previous=prior)
    master, state, saved = canonical.apply_queue_batch({"games": []}, prior, cp, batch)
    assert cp == original and saved["pending_candidates"] == {"124": unrelated}
    assert "123" not in saved["unresolved_candidates"]
    assert saved["twitch_admissions"]["123"] == unknown()
    assert saved["official_results"] == {} and saved["attempt_events"] == []
    frozen, legacy_cp, groups = queue_inputs()
    from radar_backend.domain.official_queue import make_queue
    queue, _ = make_queue(saved, frozen, legacy_cp, groups,
        {"screened_at": NOW.isoformat(), "games": [{"appid": 123, "name": "Same title", "release_start": "2026-10-02",
        "release_precision": "day", "release_display_precision": "date_full", "sexual_content_screened": True}]},
        {"updated_at": NOW.isoformat(), "complete": True, "games": {"123": {"third_party_followers": None}}}, {}, {},
        now=NOW, is_twitch_queue_candidate=canonical.is_twitch_queue_candidate, cached_follower=canonical.cached_follower)
    assert [r["appid"] for r in queue] == [124]
    repeat, client = collect(tmp_path, caches=[saved], previous=state, master=master)
    assert repeat["records"] == [] and not client.get.called
    assert canonical.apply_queue_batch(master, state, saved, repeat) == (master, state, saved)


def test_concurrent_adult_exclusion_does_not_complete_unknown_fallback_queue():
    game = unknown()
    current = deepcopy(game); current["content_descriptorids"] = [3]
    normal = {"appid": 123, "group_id64": None, "queue_source": "normal"}
    cp = checkpoint([normal])
    batch = {"schema_version": 1, "generated_at": NOW.isoformat(), "records": [game], "follower_candidates": [],
        "state_updates": {"123": {"status": "accepted", "updated_at": NOW.isoformat(), "twitch_admission": proof()}}}
    master, _state, saved = canonical.apply_queue_batch({"games": [current]}, {}, cp, batch)
    assert master["games"] == [current]
    assert saved["pending_candidates"] == cp["pending_candidates"] and "twitch_admissions" not in saved


@pytest.mark.parametrize("count", [0, 6000])
def test_new_numeric_observation_recovers_unknown_and_clears_marker(tmp_path, count):
    fake_frontend(tmp_path)
    old = unknown()
    batch, client = collect(tmp_path, master={"games": [old]}, previous={"games": {"123": {"status": "accepted"}}},
        caches=[{"official_results": {"123": {"official_followers": count, "official_checked_at_taipei": NOW.isoformat(), "group_id64": GID}}}])
    master, _ = canonical.apply_batch({"games": [old]}, {}, batch)
    recovered, = master["games"]
    assert recovered["followers"] == count and recovered["group_id64"] == GID
    assert recovered["follower_source"] == "Steam Community XML memberCount"
    assert "follower_status" not in recovered and "follower_unavailable_at" not in recovered
    assert is_twitch_qualified(recovered) and client.get.call_count == 2


@pytest.mark.parametrize("count", [0, 88])
def test_unknown_frozen_batch_cannot_erase_prior_true_count_or_concurrent_newer_count(count):
    current = row(count); current["group_id64"] = GID
    old = unknown()
    batch = {"schema_version": 1, "generated_at": NOW.isoformat(), "records": [old], "state_updates": {}}
    master, _ = canonical.apply_batch({"games": [current]}, {}, batch)
    accepted, = master["games"]
    for field in ("followers", "follower_checked_at", "follower_source", "official_ge5000", "group_id64"):
        assert accepted[field] == current[field]
    assert "follower_status" not in accepted and is_twitch_qualified(accepted)
    baseline = {"games": []}
    merged = merge_master({"games": [current]}, baseline, {"games": [old]})
    assert merged["games"] == [current]


def test_dispatch_unknown_count_has_explicit_marker_and_normal_receipt_deduplication():
    game = unknown(); state = {"games": {"123": {"status": "accepted"}}}
    client = Mock(); client.post.return_value = Mock(status_code=204)
    batch = canonical.dispatch({"games": [game]}, state, token="test", target="owner/content", session=client, now=NOW)
    payload = client.post.call_args.kwargs["json"]["client_payload"]
    assert payload["official_followers"] is None and payload["official_checked_at_taipei"] is None
    for field in ("follower_status", "follower_unavailable_at", "follower_source", "group_id64", "official_ge5000", "twitch_admission"):
        assert payload[field] == game[field]
    master, saved = canonical.apply_batch({"games": [game]}, state, batch)
    client.reset_mock()
    again = canonical.dispatch(master, saved, token="test", target="owner/content", session=client, now=NOW)
    assert again["state_updates"] == {} and not client.post.called


def test_real_cli_applies_unknown_record_and_completes_queue_without_network(tmp_path):
    game = unknown()
    cp_path = tmp_path / canonical.CHECKPOINT
    write_json(cp_path, checkpoint([{"appid": 123, "group_id64": None, "queue_source": "normal"}]))
    write_json(tmp_path / canonical.MASTER, {"games": []}); write_json(tmp_path / canonical.STATE, {})
    batch = {"schema_version": 1, "generated_at": NOW.isoformat(), "records": [game], "follower_candidates": [],
        "state_updates": {"123": {"status": "accepted", "updated_at": NOW.isoformat(), "twitch_admission": proof()}}}
    batch_path = tmp_path / "frozen.json"; write_json(batch_path, batch)
    result = subprocess.run([os.sys.executable, "-B", "-m", "radar_backend.jobs.reconcile_twitch_official_queue",
        "--phase", "apply", "--batch", str(batch_path)], cwd=tmp_path, text=True, capture_output=True,
        env={**os.environ, "PYTHONPATH": os.pathsep.join([str(Path(__file__).resolve().parents[1]),
            *(str(Path(p).resolve()) for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p)])})
    assert result.returncode == 0, result.stderr
    assert read_json(tmp_path / canonical.MASTER)["games"] == [game]
    assert read_json(cp_path)["pending_candidates"] == {} and read_json(cp_path)["official_results"] == {}


def test_bare_git_unknown_projection_and_numeric_race_preserve_receipt_and_noop(tmp_path):
    remote, root, source = frontend(tmp_path)
    game = unknown(); source["games"].append(game)
    source["generated_at"] = NOW.isoformat()
    receipt = publish_catalog(root, source, input_revision="frozen-twitch", now=NOW)
    for path in ("data/games/123.json", "data/catalog.json", "data/steam_upcoming.json"):
        payload = published_json(remote, path)
        published = payload if "games" not in payload else next(r for r in payload["games"] if r["appid"] == 123)
        assert has_unavailable_group_followers(published) and is_twitch_qualified(published)
    assert receipt.payload_revision == snapshot_revision({"steam_master": source})
    assert publish_catalog(root, source, input_revision="frozen-twitch", now=NOW).changed is False
    writer = tmp_path / "numeric-writer"
    subprocess.run(["git", "clone", str(remote), str(writer)], check=True, capture_output=True)
    git(writer, "config", "user.name", "Test"); git(writer, "config", "user.email", "test@example.com")
    class NumericRace(SubprocessGitRepository):
        raced = False
        def _run(self, *args, push=False):
            if push and not self.raced:
                self.raced = True
                current = published_json(remote, "data/games/123.json")
                current.update(followers=0, follower_checked_at=(NOW + timedelta(minutes=5)).isoformat(),
                    follower_source="Steam Community XML memberCount", official_ge5000=False, group_id64=GID,
                    name_zh_tw="並行內容更新")
                current.pop("follower_status"); current.pop("follower_unavailable_at")
                write_json(writer / "data/games/123.json", current)
                git(writer, "add", "data/games/123.json"); git(writer, "commit", "-m", "official numeric recovery")
                git(writer, "push", "origin", "HEAD:main")
            return super()._run(*args, push=push)
    source["games"][0]["followers"] = 5600
    receipt = publish_catalog(root, source, input_revision="same-frozen-twitch", now=NOW,
        repository=NumericRace(root, disposable_checkout=True))
    assert receipt.attempts == 2 and receipt.published_revision == git(remote, "rev-parse", "main")
    published = published_json(remote, "data/games/123.json")
    assert published["followers"] == 0 and published["group_id64"] == GID
    assert published["name_zh_tw"] == "並行內容更新" and "follower_status" not in published
    assert is_twitch_qualified(published)
    assert publish_catalog(root, source, input_revision="same-frozen-twitch", now=NOW).changed is False


def test_daily_master_gate_keeps_nullable_admission_and_rejects_bare_null():
    from scripts.steam_master_date_gate import filter_confirmed_master_games
    game = unknown()
    bare = deepcopy(game); bare.pop("twitch_admission")
    assert filter_confirmed_master_games([game, bare], [], today=NOW.date()) == [game]


def test_refresh_selection_dispatches_null_and_complete_fallback_bundle(monkeypatch):
    from scripts import dispatch_content_refresh_events as refresh
    game = unknown()
    bare = deepcopy(game); bare.pop("twitch_admission"); bare["appid"] = 124
    selected = refresh.select_games({"games": [bare, game]}, None)
    assert len(selected) == 1 and selected[0]["followers"] is None
    class Response:
        status = 204
        def __enter__(self): return self
        def __exit__(self, *args): return False
    send = Mock(return_value=Response()); monkeypatch.setattr(refresh.urllib.request, "urlopen", send)
    refresh.dispatch("offline-token", selected[0], "releasing-soon")
    payload = json.loads(send.call_args.args[0].data)["client_payload"]
    assert payload["official_followers"] is None and payload["official_checked_at_taipei"] is None
    for field in ("follower_status", "follower_unavailable_at", "follower_source", "group_id64", "official_ge5000", "twitch_admission"):
        assert payload[field] == game[field]


@pytest.mark.parametrize("count", [None, 0])
def test_actual_title_refresh_keeps_unknown_or_concurrent_true_zero_in_all_projections(tmp_path, count):
    from scripts.refresh_published_chinese_titles import refresh
    data = tmp_path / "data"; game = unknown(); full = deepcopy(game)
    if count is not None:
        full.update(followers=count, follower_checked_at=(NOW + timedelta(minutes=5)).isoformat(),
                    follower_source="Steam Community XML memberCount", official_ge5000=False, group_id64=GID)
        full.pop("follower_status"); full.pop("follower_unavailable_at")
    write_json(data / "index.json", {"version": 2, "months": ["2026-10"]})
    write_json(data / "calendar/2026-10.json", {"count": 1, "games": [game]})
    write_json(data / "games/123.json", full)
    client = Mock()
    response = Mock(status_code=200); response.json.return_value = {"response": {"store_items": [{"appid": 123, "name": "新作繁中名稱"}]}}
    client.get.return_value = response
    result = refresh(data, session=client, interval=0)
    assert result["published_qualified"] == 1 and result["changed_appids"] == [123]
    for path in ("games/123.json", "catalog.json", "steam_upcoming.json", "calendar/2026-10.json"):
        doc = read_json(data / path); published = doc if "games" not in doc else doc["games"][0]
        assert published["followers"] == count and is_twitch_qualified(published)
        if count is None: assert has_unavailable_group_followers(published)
        else:
            assert published["follower_checked_at"] == full["follower_checked_at"]
            assert "follower_status" not in published and published["group_id64"] == GID
    assert refresh(data, session=client, interval=0)["changed_games"] == 0


def test_one_shot_master_repair_never_discards_unknown_or_counts_it_as_official(tmp_path, monkeypatch):
    from scripts import repair_steam_master_store_dates as repair
    master, eligible, report = (tmp_path / filename for filename in ("master.json", "eligible.json", "report.json"))
    game = unknown()
    regular = [{"appid": 1000 + i, "followers": 6000, "release_start": "2026-09-01"} for i in range(80)]
    write_json(master, {"games": [game, *regular]}); write_json(eligible, {"games": []})
    monkeypatch.setattr(os.sys, "argv", ["repair", "--master", str(master), "--eligible", str(eligible), "--report", str(report)])
    monkeypatch.setattr(repair, "taiwan_today", lambda: NOW.date())
    fetch = Mock(return_value={123: {"exact": False, "status": "unavailable"}})
    monkeypatch.setattr(repair, "fetch_store_release_details", fetch)
    repair.main()
    assert read_json(master)["games"][0] == game
    assert read_json(report)["official_ge5000_checked"] == 80
    assert read_json(report)["twitch_admission_checked"] == 1
    assert 123 in fetch.call_args.args[1]


def test_public_date_audit_keeps_qualified_unknown_in_upcoming_and_released_lists(tmp_path):
    from scripts.audit_published_release_dates import audit
    game = unknown(); future = deepcopy(game)
    future["appid"] = 124; future["twitch_admission"]["appid"] = 124
    future_day = (NOW + timedelta(days=3)).date().isoformat()
    future.update(release_start=future_day, release_end=future_day, release_store_date=future_day,
                  release_time_utc=future_day + "T07:00:00Z", release_timestamp_taipei_date=future_day,
                  store_url="https://store.steampowered.com/app/124/")
    data = tmp_path / "data"
    write_json(data / "index.json", {"version": 2, "months": ["2026-10"]})
    write_json(data / "calendar/2026-10.json", {"count": 2, "games": [game, future]})
    for row in (game, future): write_json(data / f"games/{row['appid']}.json", row)
    response = Mock(status_code=200); response.json.return_value = {"response": {"store_items": [{"appid": 124,
        "release": {"coming_soon_display": "date_full", "steam_release_date": int((NOW + timedelta(days=3)).replace(hour=7).timestamp())}}]}}
    session = Mock(); session.get.return_value = response
    audit(data, session=session, interval=0, today=(NOW + timedelta(days=1)).date())
    assert read_json(data / "lists/upcoming.json")["appids"] == [124]
    assert read_json(data / "lists/released.json")["appids"] == [123]
    assert all(is_twitch_qualified(r) for r in read_json(data / "catalog.json")["games"])


@pytest.mark.parametrize("newer_layer", ["detail", "calendar"])
def test_title_refresh_keeps_newer_unknown_observation_coherent_with_its_proof(tmp_path, newer_layer):
    from scripts.refresh_published_chinese_titles import refresh
    from radar_core.domain.twitch_admission import UNKNOWN_FOLLOWER_FIELDS
    older = unknown(); newer = deepcopy(older)
    newer["twitch_admission"]["checked_at"] = (NOW + timedelta(minutes=5)).isoformat()
    newer["follower_unavailable_at"] = (NOW + timedelta(minutes=6)).isoformat()
    assert is_twitch_qualified(older) and is_twitch_qualified(newer)
    calendar, detail = (older, newer) if newer_layer == "detail" else (newer, older)
    data = tmp_path / "data"
    write_json(data / "index.json", {"version": 2, "months": ["2026-10"]})
    write_json(data / "calendar/2026-10.json", {"count": 1, "games": [calendar]})
    write_json(data / "games/123.json", detail)
    client = Mock()
    response = Mock(status_code=200)
    response.json.return_value = {"response": {"store_items": [{"appid": 123, "name": "新作繁中名稱"}]}}
    client.get.return_value = response
    stats = refresh(data, session=client, interval=0)
    assert stats["changed_appids"] == [123]
    for path in ("games/123.json", "catalog.json", "steam_upcoming.json", "calendar/2026-10.json"):
        doc = read_json(data / path); published = doc if "games" not in doc else doc["games"][0]
        assert is_twitch_qualified(published) and has_unavailable_group_followers(published)
        assert published["twitch_admission"] == newer["twitch_admission"]
        assert all(published[field] == newer[field] for field in UNKNOWN_FOLLOWER_FIELDS)
    before = {path.relative_to(data): path.read_bytes() for path in data.rglob("*.json")}
    assert refresh(data, session=client, interval=0)["changed_games"] == 0
    assert (data / "games/123.json").read_bytes() == before[Path("games/123.json")]
    assert (data / "calendar/2026-10.json").read_bytes() == before[Path("calendar/2026-10.json")]
