# Steam growth identity and daily completion

The 2026-10-11 Taiwan 07:15 production run (38094424285) persisted its
checkpoint and published its measurements, but failed the final coverage gate.
Its 136 accepted games had 123 dated measurements and 13 unresolved identities.
All 13 had older official Followers in `data/steam_followers_cache.json`; those
legacy rows had no GroupID. They were also absent from the identity queues.
The normal admission resolver intentionally skips games with a measured count,
so retrying the growth workflow could never resolve these identities.

The original unresolved cohort was:

| AppID | Game | Retained historical official Followers |
| --- | --- | ---: |
| 3510740 | Normal Golf Game | 1,731 |
| 4739040 | DISCIPLINE SIMULATOR | 17,796 |
| 3328490 | Airport Control 27 | 5,207 |
| 4694270 | Backrooms: Code Yellow | 1,413 |
| 2710040 | Delverium | 6,735 |
| 4019220 | Dressmaker | 12,629 |
| 4705510 | Happy Wheels | 10,789 |
| 3255500 | Ved: Recure | 5,572 |
| 3699590 | Well Dweller | 5,046 |
| 4072240 | Woman Simulator | 5,242 |
| 4080220 | EA SPORTS FC™ 27 | 21,415 |
| 3832490 | Antivirus Survivors 2003 Professional | 2,395 |
| 3917700 | Dicevaders | 1,513 |

These historical values are not new daily measurements. Their identities must
be resolved or carry actual official unavailability evidence; the existing
Twitch admission policy keeps qualified games eligible, including counts below
the ordinary Steam threshold.

Growth now invokes the existing bounded official ResolveVanityURL resolver for
accepted games that lack both a current Taiwan-day measurement and a GroupID.
`resolve_for_growth=True` permits identity repair independently of a historical
Followers count. Normal admission and Twitch fallback rules are unchanged.
The workflow passes the existing `STEAM_WEB_API_KEY` secret. API requests retain
the resolver's limit of 20 requests, 120 seconds and at least one-second pacing;
Community XML retains its separate 120-request budget and 30-second pacing.

## Evidence and completion

| Outcome | Evidence | Daily handling |
| --- | --- | --- |
| Measured | Actual same-Taiwan-day Community count and source timestamp | Reuse or retain the real measurement |
| Retryable | Unknown mapping, missing key, authentication failure, rate limit, network failure, malformed response or expired absence receipt | Keep pending; do not suppress the next six-hour check |
| Unavailable | Attempted official identity request, HTTP 200, integer EResult 42, no SteamID, source/checked time and future retry deadline | Record separately from measurements; recheck after expiry or when a GroupID becomes known |

`coverage_schema_version=2` records `eligible_appids`,
`collection_date_taipei`, `measurements`, `unavailable` and `pending`.
`measurement_complete` means every accepted game was actually measured.
`collection_complete` means the entire accepted cohort was accounted for by
real measurements or verified availability outcomes. It must not be described
as complete numeric measurement coverage when `unavailable` is nonempty.
An unavailable outcome never supplies Followers, a measurement timestamp or a
history point, and never replaces historical official Followers with zero.

Identity receipts live in `official_growth_group_resolutions`, separate from
admission queues and official observations. Checkpoint publication merges only
newer identity/API-cooldown receipts and retains concurrently resolved GroupIDs,
observations and unrelated queue updates. Progress is saved before XML collection.

`coverage_checked_at` records the actual publication attempt clock. Each Git
retry rechecks availability against its current delivery time, while the frozen
collection clock and real history timestamps remain unchanged. The frontend
uses this bounded clock for coverage, so a newer durable receipt is neither
misclassified as future evidence nor trusted after expiry.

Public publication rebuilds against the latest accepted catalog. It records
`published_eligible_appids` and `published_resolvable_appids`; a changed cohort
or newly resolved formerly-unavailable game invalidates daily completion while
still allowing genuine collected history to be published.

## Retry and verification

The final step remains named **Require complete growth coverage**. The existing
Cloudflare Worker recognizes daily success only when both the Actions run and
this step succeed. It checks at Taiwan 01:15, 07:15, 13:15 and 19:15, skips after
one validated success that day, and retries unsuccessful collection/delivery.
An active official or shared API cooldown is still respected; the six-hour
check does not shorten an existing server deadline or authentication backoff.
The report is stamped successful only after durable checkpoint and public Git
publication acknowledgements are both true.

For a recovery run, inspect `steam-public-growth-result`:

1. The exact accepted AppIDs must equal measured plus unavailable AppIDs, with
   no duplicates or pending/error entries.
2. Missing identities must have actual official resolver evidence; authentication
   or network failures are not absence evidence.
3. Both publication receipts must point to their committed backend/frontend
   revisions, and the stamped JobResult must pass the retained coverage gate.
4. Public `growth.json` coverage metadata must distinguish measured, unavailable
   and retryable outcomes; `insights-state.json` must retain dated real history.

The separately prepared Twitch GroupID fallback remains a coordinated admission
change. Growth consumes its shared `is_twitch_qualified` policy and does not
reimplement Twitch qualification or synthesize Steam metrics. Consumer tests
also exercise Core 0.3.0 while production retains its immutable Core pin.
