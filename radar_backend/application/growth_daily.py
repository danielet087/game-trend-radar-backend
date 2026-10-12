"""Resume real daily growth progress within one bounded runner allocation.

The existing collector owns every measurement and identity outcome. This use
case only budgets its attempts and waits for an acknowledged shared cooldown;
it neither clears server deadlines nor acknowledges checkpoint/public delivery.
"""
from __future__ import annotations

from copy import deepcopy
import math

from radar_core.jobs import JobResult, JobStatus
from radar_backend.application.growth import collect_observations
from radar_backend.domain.growth import TAIPEI, growth_collection_complete


def _seconds(value, label, *, allow_zero=False):
    if (type(value) not in {int, float} or not math.isfinite(value)
            or value < 0 or (not allow_zero and value == 0)):
        raise ValueError(f"{label} must be finite {'nonnegative' if allow_zero else 'positive'} seconds")


class _AttemptClock:
    def __init__(self, client, monotonic):
        self.client = client
        self.monotonic = monotonic
        self.last_start = None

    def fetch(self, group_id):
        started = self.monotonic()
        outcome = self.client.fetch(group_id)
        if outcome.http is not None or outcome.error_type is not None:
            self.last_start = started
        return outcome


def collect_daily_observations(rows, history, checkpoint, *, client, cache, cooldown,
                               clock, sleep, monotonic, persist, max_requests=250,
                               max_seconds=9000, max_cooldown_wait=3600,
                               interval=30, state_saved=False, group_resolver=None,
                               collector=collect_observations, on_wait=None):
    """Wait/resume only evidenced cooldowns, charging all work to one budget.

    Long or absent deadlines, identity failures and malformed source responses
    stay partial for the existing six-hour retry. Successful earlier attempts
    remain in the same durable cache and retain their actual source timestamps.
    """
    if type(max_requests) is not int or not 0 <= max_requests <= 250:
        raise ValueError("Daily growth needs 0..250 official requests")
    _seconds(max_seconds, "Daily growth budget")
    _seconds(max_cooldown_wait, "Cooldown wait budget", allow_zero=True)
    _seconds(interval, "Official pacing interval")
    if interval < 30:
        raise ValueError("Keep at least 30 seconds between official requests")
    first_clock = clock()
    if first_clock.tzinfo is None or first_clock.utcoffset() is None:
        raise ValueError("Growth collection needs an aware clock")
    collection_day = first_clock.astimezone(TAIPEI).date()
    started = monotonic()
    if type(started) not in {int, float} or not math.isfinite(started):
        raise ValueError("Growth collection needs a finite monotonic clock")
    last_tick = started
    requests = group_requests = passes = 0
    waited = 0.0
    events, group_batches = [], []
    collected_ids = set()
    tracked = _AttemptClock(client, monotonic)
    stop = None

    def elapsed():
        nonlocal last_tick
        tick = monotonic()
        if type(tick) not in {int, float} or not math.isfinite(tick) or tick < last_tick:
            raise ValueError("Growth monotonic clock must be finite and cannot move backwards")
        last_tick = tick
        return tick - started

    def wait_step(seconds):
        nonlocal waited, stop
        before = elapsed()
        sleep(seconds)
        spent = elapsed() - before
        if spent <= 0:
            raise ValueError("Growth cooldown sleep must advance the monotonic clock")
        waited += spent
        if waited > max_cooldown_wait:
            stop = "cooldown_exceeds_wait_budget"
            return False
        if elapsed() >= max_seconds:
            stop = "cooldown_exceeds_runner_budget"
            return False
        return True

    def wait_until_ready():
        nonlocal waited, stop
        # Re-read the deadline on every wake. A concurrently extended deadline
        # must not be shortened to the original planned wait.
        while cooldown.blocked(clock()):
            deadline = cooldown.deadline()
            if deadline is None:
                stop = "cooldown_without_deadline"
                return False
            delay = max(0, (deadline - clock()).total_seconds())
            if tracked.last_start is not None:
                delay = max(delay, interval - (monotonic() - tracked.last_start))
            if delay > max_cooldown_wait - waited:
                stop = "cooldown_exceeds_wait_budget"
                return False
            if delay >= max_seconds - elapsed():
                stop = "cooldown_exceeds_runner_budget"
                return False
            if on_wait is not None:
                on_wait(deadline, delay)
            step = min(60, delay)
            if step <= 0:
                if not cooldown.blocked(clock()):
                    break
                stop = "cooldown_clock_not_ready"
                return False
            if not wait_step(step):
                return False
        # Preserve pacing across collector passes even for a short Retry-After.
        spacing = (max(0, interval - (monotonic() - tracked.last_start))
                   if tracked.last_start is not None else 0)
        if spacing:
            if spacing > max_cooldown_wait - waited or spacing >= max_seconds - elapsed():
                stop = "retry_spacing_exceeds_budget"
                return False
            if not wait_step(spacing):
                return False
        if clock().astimezone(TAIPEI).date() != collection_day:
            stop = "collection_day_changed"
            return False
        return True

    while True:
        remaining = max_seconds - elapsed()
        # The first pass can still reuse all known real points at zero requests.
        # Later passes are never allowed to reset the total elapsed/request cap.
        if passes and (requests >= max_requests or remaining <= 0):
            stop = "daily_budget_exhausted"
            break
        result = collector(rows, history, checkpoint, client=tracked, cache=cache,
                           cooldown=cooldown, clock=clock, sleep=sleep,
                           monotonic=monotonic, persist=persist,
                           max_requests=max_requests - requests,
                           max_seconds=max(remaining, 0.000001), interval=interval,
                           state_saved=state_saved, group_resolver=group_resolver)
        passes += 1
        requests += result["requests"]
        group_requests += result.get("group_resolution_requests", 0)
        events.extend(result.get("events", []))
        collected_ids.update(event["appid"] for event in result.get("events", [])
                             if event.get("status") == "ok")
        if result.get("group_resolution_batch") is not None:
            group_batches.append(deepcopy(result["group_resolution_batch"]))
        if result["collection_complete"]:
            stop = "completed"
            break
        if result.get("reason") != "rate_limited":
            stop = result.get("reason", "source_unavailable")
            break
        deadline = cooldown.deadline()
        if deadline is None or deadline <= clock():
            stop = "cooldown_without_future_deadline"
            break
        if requests >= max_requests or elapsed() >= max_seconds:
            stop = "daily_budget_exhausted"
            break
        if not wait_until_ready():
            break

    result = deepcopy(result)
    finished = clock()
    result.update(generated_at=finished.isoformat(), requests=requests,
                  reused=sum(point["appid"] not in collected_ids for point in result["measurements"]),
                  events=events, group_resolution_requests=group_requests,
                  group_resolution_batches=group_batches,
                  max_requests=max_requests, max_seconds=max_seconds,
                  elapsed_seconds=elapsed(),
                  daily_budget={"max_requests": max_requests, "max_seconds": max_seconds,
                                "max_cooldown_wait": max_cooldown_wait,
                                "cooldown_wait_seconds": waited,
                                "elapsed_seconds": elapsed(), "passes": passes,
                                "stop_reason": stop})
    # Revalidate at the final actual clock; a day change or expired absence can
    # never inherit an earlier in-memory completion result.
    result.pop("job_result", None)
    result["collection_complete"] = growth_collection_complete(result, finished)
    result["job_result"] = JobResult(
        job="public-growth", status=(JobStatus.COOLING_DOWN if result["reason"] == "rate_limited" else JobStatus.PARTIAL),
        reason=result["reason"], collection_complete=result["collection_complete"],
        state_persisted=False, published=False, requires_publication=True,
        target_slot=collection_day.isoformat(),
    ).to_dict()
    persist(checkpoint, result)
    return result
