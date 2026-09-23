# Steam official Followers — near-release, small-batch experiment

Frozen cohort: `steam_fresh_20260922_post_adult_1317_near_release`. It is
**not** an automatically refreshed daily Steam candidate scan.

## Proven initial results

- [2026-09-23 initial run 35814756620](https://github.com/danielet087/game-trend-radar-backend/actions/runs/35814756620): 3/3 official `memberCount` results (HTTP 200), zero 429, 28-second spacing.
- [2026-09-23 second run 35815045289](https://github.com/danielet087/game-trend-radar-backend/actions/runs/35815045289): another 3/3 HTTP 200, zero 429.
- **6/1317 have actual official count; 0 above 5000 in those six; 1311 remain.** Never infer the remaining games' Followers from this early-date pilot.

## Queue and limits

- Sort by Taiwan release date: today/future first, oldest release dates last; within a day by AppID. The five titles already released 2026-09-22 remain in the cohort and are **not** silently removed.
- Max **3 official XML requests per run**, **28 seconds** between requests, serialized, no simultaneous fanout.
- Scheduled every **2 hours** at UTC minute 37 (Taiwan minute 37, even local hours), subject to normal GitHub Actions scheduling delays. A scheduled run is not a promise that Steam will serve responses.
- On the first HTTP 429, stop immediately; wait at least 48 hours before any further official request. Repeated 429 escalates cooldown (up to 168 hours). HTTP 401/403, 5xx and malformed group data also halt the batch. Missing data is never treated as zero.
- Each successful numeric result is saved immediately, and the checkpoint and immutable input lists are committed to this **experiment-only** folder.
- Results are based on official Steam Community XML `memberCount`, not Steam Store `appdetails`, third-party followers or wishlists.
- The workflow attempts to disable its own recurrence when all 1,317 have official results.
- **This separate scheduled GitHub Actions task does use GitHub Actions minutes.** Production sync, frontend, previous follower cache and their workflows were not changed.

## Files

- `source_queue.json` — immutable 1,317-candidate original dated queue
- `source_unresolved.json` — original 1,358 third-party unresolved, supplying short Group IDs (one of 1,317 is missing ID, for which a valid official AppID game-group URL is used).
- `checkpoint.json` — cumulative official counts, response statuses and a 429 cooldown timestamp.

## Links

- [Workflow](../../.github/workflows/steam-official-nearfirst-batch-once.yml)
- [Checkpoint](checkpoint.json)
