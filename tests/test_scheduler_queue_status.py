"""Offline checks for the dashboard's projection of the official queue."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts import experiment_official_daily_catchup_250 as worker
from scripts import export_scheduler_queue_status as exporter
from tests.test_twitch_steam_admission import NOW
from tests.test_unified_twitch_followers import (
    checkpoint, current_daily, ordinary_candidate, queue_inputs, run_worker, twitch_candidate,
)


def inputs(cp, eligible=None, prefilter=None, cache=None):
    frozen, legacy, groups = queue_inputs()
    return (cp, frozen, legacy, groups, eligible or {"games": []},
            prefilter or {}, cache or {"games": {}}, {"verified": {}})


def html_attempt(appid, at="2026-10-01T09:00:00+08:00"):
    return {"appid": appid, "when_taipei": at, "http": 200,
            "status": "request_or_parse_error", "error_type": "ParseError",
            "content_type": "text/html"}


def test_projection_matches_official_order_without_mutating_source_inputs():
    cp = checkpoint([twitch_candidate(123), twitch_candidate(124),
                     twitch_candidate(126), ordinary_candidate(127)])
    cp["attempt_events"] = [
        {"appid": 123, "queue_source": "twitch_steam_discovery",
         "when_taipei": NOW.isoformat(), "http": 429, "status": "rate_limited"},
        {"appid": 126, "queue_source": "twitch_steam_discovery",
         "when_taipei": (NOW - timedelta(hours=2)).isoformat(),
         "http": 429, "status": "rate_limited"},
    ]
    eligible, prefilter = current_daily(123, 128)
    source = inputs(cp, eligible, prefilter)
    original = deepcopy(source)
    official, _ = worker.make_queue(*deepcopy(source), now=NOW)

    status = exporter.build_status(*source, now=NOW)

    assert status["generated_at"] == "2026-10-02T09:00:00Z"
    assert [row["appid"] for row in status["queue"]] == [row["appid"] for row in official]
    assert [row["appid"] for row in status["queue"]] == [124, 126, 123, 127, 128]
    assert [row["position"] for row in status["queue"]] == [1, 2, 3, 4, 5]
    assert all(row["state"] == "waiting" for row in status["queue"])
    assert source == original


def test_daily_counts_and_cooldown_change_with_actual_official_results():
    cp = checkpoint([twitch_candidate(123), twitch_candidate(124),
                     twitch_candidate(126), ordinary_candidate(999)])
    cp["attempt_events"] = [html_attempt(999), *[
        {"appid": aid, "queue_source": "twitch_steam_discovery",
         "when_taipei": (NOW - timedelta(minutes=3 - index)).isoformat(),
         "http": 429, "status": "rate_limited"}
        for index, aid in enumerate((123, 124, 126))
    ]]
    cp["next_request_after_taipei"] = (NOW + timedelta(hours=2)).isoformat()
    eligible, prefilter = current_daily(*range(200, 273))
    source = inputs(cp, eligible, prefilter)

    status = exporter.build_status(*source, now=NOW)

    assert status["summary"] == {
        "normal_pending": 73, "twitch_priority_pending": 3, "parked": 1,
        "ready_pending": 76, "total_pending": 77,
        "today_attempts": 3, "today_successes": 0, "today_429": 3,
    }
    assert status["cooldown"]["active"] is True
    assert status["cooldown"]["reason"] == "steam_http_429"
    assert status["cooldown"]["next_eligible_slot"] == "2026-10-02T11:00:00Z"
    assert all(row["state"] == "cooldown" for row in status["queue"])
    assert status["parked"][0]["reason"] == "official_xml_fallback_returned_html"

    # A true zero count still completes official lookup; it must not stay queued.
    cp["official_results"]["123"] = {"official_followers": 0,
                                      "official_checked_at_taipei": NOW.isoformat()}
    source[6]["games"]["200"] = {"followers": 0, "checked_at": NOW.isoformat()}
    updated = exporter.build_status(*source, now=NOW)
    assert updated["summary"]["normal_pending"] == 72
    assert updated["summary"]["twitch_priority_pending"] == 2
    assert updated["summary"]["total_pending"] == 75
    assert {123, 200}.isdisjoint(row["appid"] for row in updated["queue"])


def test_parked_rows_reenter_after_group_repair_and_resolved_or_expired_rows_disappear():
    expired = twitch_candidate(126, "2026-09-04")
    cp = checkpoint([ordinary_candidate(999), ordinary_candidate(888), expired])
    cp["attempt_events"] = [html_attempt(aid) for aid in (999, 888, 126)]
    cp["unresolved_candidates"] = {
        aid: {**deepcopy(row), "status": "official_xml_fallback_returned_html"}
        for aid, row in cp["pending_candidates"].items()
    }
    cp["official_results"]["888"] = {"official_followers": 12,
                                      "official_checked_at_taipei": NOW.isoformat()}
    source = inputs(cp)
    after_twitch_window = NOW + timedelta(days=3)

    before = exporter.build_status(*source, now=after_twitch_window)
    assert [row["appid"] for row in before["parked"]] == [999]
    assert before["summary"]["total_pending"] == 1

    cp["pending_candidates"]["999"]["group_id64"] = worker.group_to_gid(999)
    repaired = exporter.build_status(*source, now=after_twitch_window)
    assert [row["appid"] for row in repaired["queue"]] == [999]
    assert repaired["parked"] == []
    assert repaired["summary"]["total_pending"] == 1
    assert repaired["summary"]["parked"] == 0
    # Exporting does not delete historical checkpoint evidence itself.
    assert set(cp["unresolved_candidates"]) == {"999", "888", "126"}


def test_daily_freshness_uses_taiwan_date_and_does_not_import_yesterday_rows():
    now = datetime(2026, 10, 3, 0, tzinfo=timezone.utc)  # Taiwan 08:00.
    eligible, prefilter = current_daily(200)
    eligible["screened_at"] = prefilter["updated_at"] = "2026-10-02T15:59:59Z"
    source = inputs(checkpoint(), eligible, prefilter)

    stale = exporter.build_status(*source, now=now)
    assert stale["today_taipei"] == "2026-10-03"
    assert stale["source_status"]["status"] == "no_current_day_prefilter"
    assert stale["summary"]["total_pending"] == 0

    # One second later is a new Taiwan date, although the UTC date is unchanged.
    eligible["screened_at"] = prefilter["updated_at"] = "2026-10-02T16:00:00Z"
    fresh = exporter.build_status(*source, now=now)
    assert fresh["source_status"]["status"] == "current_day_prefilter_complete"
    assert [row["appid"] for row in fresh["queue"]] == [200]


def test_export_writes_only_destination_without_requests_or_git(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    eligible, prefilter = current_daily(200)
    cp, frozen, legacy, groups, eligible, prefilter, cache, other = inputs(
        checkpoint([twitch_candidate()]), eligible, prefilter)
    documents = {
        worker.CHECKPOINT: cp,
        worker.FROZEN / "source_queue.json": frozen,
        worker.FROZEN / "checkpoint.json": legacy,
        worker.FROZEN / "source_unresolved.json": groups,
        worker.ELIGIBLE: eligible, worker.PREFILTER: prefilter,
        worker.OFFICIAL_CACHE: cache, worker.ORIGINAL_OFFICIAL: other,
        Path("data/steam_candidate_state.json"): {"updated_at": NOW.isoformat()},
        Path("data/twitch_steam_import_state.json"): {"updated_at": NOW.isoformat()},
    }
    for path, value in documents.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
    before = {path: sha256(path.read_bytes()).digest() for path in documents}
    forbidden = Mock(side_effect=AssertionError("Snapshot export must be read-only"))
    monkeypatch.setattr(worker.requests, "get", forbidden)
    monkeypatch.setattr(worker.requests, "post", forbidden)
    monkeypatch.setattr(worker.requests, "Session", forbidden)
    monkeypatch.setattr(worker.subprocess, "run", forbidden)
    monkeypatch.setattr(worker, "git_push", forbidden)
    destination = tmp_path / "snapshot.json"

    result = exporter.export_status(output=destination, now=NOW)

    assert json.loads(destination.read_text(encoding="utf-8")) == result
    assert result["source"]["candidate_state_updated_at"] == "2026-10-02T09:00:00Z"
    assert result["source"]["twitch_import_updated_at"] == "2026-10-02T09:00:00Z"
    assert result["summary"]["total_pending"] == 2
    assert {path: sha256(path.read_bytes()).digest() for path in documents} == before
    assert {path.relative_to(tmp_path) for path in tmp_path.rglob("*") if path.is_file()} == {
        *documents, Path("snapshot.json"),
    }
    forbidden.assert_not_called()


def test_batch_reports_last_actual_attempt_without_claiming_current_processing(monkeypatch, tmp_path):
    first, later = twitch_candidate(), ordinary_candidate()
    make_queue = worker.make_queue
    monkeypatch.setenv("GITHUB_RUN_ID", "123456")
    cp, _, client, *_ = run_worker(
        monkeypatch, tmp_path, [first, later],
        [SimpleNamespace(status_code=429, headers={})],
    )
    assert client.get.call_count == 1
    assert cp["scheduler_batch"]["last_appid"] == first["appid"]
    assert cp["scheduler_batch"]["last_name"] == first["name"]
    assert cp["scheduler_batch"]["status"] == "finished"
    assert cp["scheduler_batch"]["stop_reason"] == "first_http_429"
    assert "current_appid" not in cp["scheduler_batch"]
    assert "current_name" not in cp["scheduler_batch"]

    monkeypatch.setattr(worker, "make_queue", make_queue)
    status = exporter.build_status(*inputs(cp), now=NOW)
    assert status["batch"]["last_appid"] == first["appid"]
    assert status["batch"]["last_name"] == first["name"]
    assert status["batch"]["run_url"].endswith("/actions/runs/123456")
    assert "current_appid" not in status["batch"]
    assert "current_name" not in status["batch"]


def git(repository, *args):
    return subprocess.run(["git", "-C", str(repository), *args], check=True,
                          text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


def diverged_local_repositories(tmp_path, *, conflict_in_checkpoint=False):
    """Two local writers; no external Git host or authentication is involved."""
    remote, seed, collector, producer = [tmp_path / name for name in
                                         ("remote.git", "seed", "collector", "producer")]
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(remote)],
                   check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    seed.mkdir()
    git(seed, "init", "--initial-branch=main")
    git(seed, "config", "user.name", "Queue test")
    git(seed, "config", "user.email", "queue-test@example.invalid")
    for path, value in ((worker.CHECKPOINT, {"official_results": {}}),
                        (exporter.OUTPUT, {"writer": "base"})):
        target = seed / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(value) + "\n", encoding="utf-8")
    git(seed, "add", ".")
    git(seed, "commit", "-m", "base queue")
    git(seed, "remote", "add", "origin", str(remote))
    git(seed, "push", "origin", "main")
    for checkout in (collector, producer):
        subprocess.run(["git", "clone", str(remote), str(checkout)], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        git(checkout, "config", "user.name", "Queue test")
        git(checkout, "config", "user.email", "queue-test@example.invalid")

    local_checkpoint = {"official_results": {"123": {"official_followers": 0}}}
    (collector / worker.CHECKPOINT).write_text(json.dumps(local_checkpoint) + "\n", encoding="utf-8")
    (collector / exporter.OUTPUT).write_text('{"writer": "collector"}\n', encoding="utf-8")
    git(collector, "add", ".")
    git(collector, "commit", "-m", "collector durable result")

    (producer / exporter.OUTPUT).write_text('{"writer": "daily"}\n', encoding="utf-8")
    (producer / "daily-progress.txt").write_text("remote daily work preserved\n", encoding="utf-8")
    if conflict_in_checkpoint:
        (producer / worker.CHECKPOINT).write_text(
            json.dumps({"official_results": {"456": {"official_followers": 88}}}) + "\n",
            encoding="utf-8",
        )
    git(producer, "add", ".")
    git(producer, "commit", "-m", "daily status update")
    git(producer, "push", "origin", "main")
    return collector, local_checkpoint


def test_dashboard_only_git_conflict_is_recomputed_without_losing_collector_progress(monkeypatch, tmp_path):
    collector, local_checkpoint = diverged_local_repositories(tmp_path)
    monkeypatch.chdir(collector)

    def regenerate():
        cp = json.loads(worker.CHECKPOINT.read_text(encoding="utf-8"))
        assert cp == local_checkpoint
        exporter.OUTPUT.write_text(json.dumps({"recomputed_official_results": len(cp["official_results"])})
                                   + "\n", encoding="utf-8")

    export = Mock(side_effect=regenerate)
    monkeypatch.setattr(exporter, "export_status", export)

    worker.rebase_checkpoint()

    export.assert_called_once_with()
    assert json.loads(worker.CHECKPOINT.read_text(encoding="utf-8")) == local_checkpoint
    assert json.loads(exporter.OUTPUT.read_text(encoding="utf-8")) == {"recomputed_official_results": 1}
    assert (collector / "daily-progress.txt").read_text(encoding="utf-8") == "remote daily work preserved\n"
    assert git(collector, "diff", "--name-only", "--diff-filter=U") == ""
    assert git(collector, "status", "--porcelain") == ""
    git(collector, "merge-base", "--is-ancestor", "origin/main", "HEAD")


def test_git_conflict_in_checkpoint_and_dashboard_stays_unresolved(monkeypatch, tmp_path):
    collector, _ = diverged_local_repositories(tmp_path, conflict_in_checkpoint=True)
    monkeypatch.chdir(collector)
    export = Mock(side_effect=AssertionError("A checkpoint conflict must never be auto-resolved"))
    monkeypatch.setattr(exporter, "export_status", export)

    with pytest.raises(subprocess.CalledProcessError):
        worker.rebase_checkpoint()

    export.assert_not_called()
    assert set(git(collector, "diff", "--name-only", "--diff-filter=U").splitlines()) == {
        str(worker.CHECKPOINT), str(exporter.OUTPUT),
    }
    conflict = worker.CHECKPOINT.read_text(encoding="utf-8")
    assert "<<<<<<<" in conflict
    assert '"123"' in conflict and '"456"' in conflict
