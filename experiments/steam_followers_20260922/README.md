# 2026-09-22 fresh Steam year — Followers experiments

This folder is **experiment-only**. None of the scripts/workflows here reads or overwrites the production Steam follower cache, candidate synchronization cursor, main public game list, or site data. No recurring workflow trigger is enabled.

## Frozen independent cohort

- Fresh Steam Store upcoming search (2026-09-22 … 2027-09-22): 3,469 exact-date candidates.
- Adult descriptor exclusions (official Steam descriptors 3/4): 231.
- Remaining games including demos: **3,238**.
- First third-party steam-groups.com pass: 1,880 measured, 92 at/above 4,000, 1,358 unavailable.
- Source first-pass artifact (7-day original retention): [run 35702182234](https://github.com/danielet087/game-trend-radar-backend/actions/runs/35702182234).

## Durable checkpoints

- [Games Popularity](../games_popularity_20260922/checkpoint.json) — data for **this experiment only**, keyed by Steam AppID; each result includes source, status, observed time if provided and Followers if present.
- [Official Steam Community](../steam_official_followers_20260922/checkpoint.json) — restores the 90 actually verified records from the earlier run; never treats two pending games as below 5,000.
- [Games Popularity authenticated resumable workflow](../../.github/workflows/games-popularity-resumable-once.yml) — manual dispatch after quota/service access recovers.
- [Bounded anonymous salvage](../../.github/workflows/games-popularity-anonymous-salvage-once.yml) — **do not repeatedly dispatch**; the service's anonymous allowance is limited.
- [Official Steam resumable workflow](../../.github/workflows/steam-official-followers-resumable-once.yml) — manually dispatch only when Steam Community XML is no longer 429.

## Verified 2026-09-23 10:20 Taipei results

- Games Popularity initial authenticated full scan: **cancelled after 30 minutes**; last progress log was 1,000 requests, and it wrote no JSON artifact. Do not count the unsaved results as recoverable.
- Authenticated resumable diagnostic: 25/25 initial batch requests returned a throttling response and produced **zero measured records**. A later isolated diagnostic returned **429 with key**, **200 without key**. No Retry-After was supplied. Do not assert the key is invalid: provider-side key/account throttling or quota is not disambiguated.
- Bounded anonymous pilot: **60 final observations persisted**, 54 measured, 6 without followers, **7 extra >=4,000 third-party candidates**; two-source priority total 99 at that checkpoint. These 7 are **not officially verified**.
- Steam official first-source shortlist: **90/92 verified**, 89 >=5,000, 1 <5,000, 2 pending. XML was 429 again on 2026-09-23 so the optimized verifier stopped after one request and saved the 90 prior outcomes.

**Never label a checkpoint's successful GitHub run as a complete scan unless its report status is `complete` and pending is 0.** Unknown/missing third-party data remains unresolved. Do not present unverified third-party counts as official Followers.

## Run links

- [Anonymous salvage](https://github.com/danielet087/game-trend-radar-backend/actions/runs/35809844306)
- [Authenticated 429 comparison](https://github.com/danielet087/game-trend-radar-backend/actions/runs/35809491959)
- [Official checkpoint replay](https://github.com/danielet087/game-trend-radar-backend/actions/runs/35809710469)

