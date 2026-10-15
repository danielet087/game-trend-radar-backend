# Turso primary / Cloudflare D1 standby rollout

The complete source-document ingestion, D1 replication, and daily portable SQL
backup workflow is implemented in `source-storage-sync.yml`. See
[database-storage.md](database-storage.md) for current coverage, recovery and
operating instructions. Direct collector-primary support is implemented behind
the explicit `RADAR_DATABASE_PRIMARY` flag. Deployment starts in `prepare` mode;
only a successful reviewed cutover report establishes that production writers
have switched. See the cutover procedure in that document.

## Stage 1: connection validation

Configure these **repository Actions secrets** in `game-trend-radar-backend`:

| Secret | Value |
| --- | --- |
| `TURSO_DATABASE_URL` | The empty libSQL database's `libsql://…turso.io` URL |
| `TURSO_AUTH_TOKEN` | A database-scoped read/write token, not a Turso account-management token |
| `CLOUDFLARE_ACCOUNT_ID` | The Cloudflare account containing the standby database |
| `CLOUDFLARE_D1_DATABASE_ID` | The UUID of `game-trend-radar-backup` |
| `CLOUDFLARE_D1_API_TOKEN` | An API token with Account → D1 → Edit (D1 Write), scoped to that account |

Run **Verify Turso database connection** and **Verify Cloudflare D1 database
connection** from Actions. Each also runs when its own workflow, diagnostic or
tests change on `main`. Neither has a recurring schedule. Offline safeguards run
before the respective provider's secrets are injected into the live probe.

Each probe creates `_radar_connection_checks`, commits one uniquely identified
test row, verifies a separate read, deletes only that row and verifies cleanup.
The value includes Chinese text and an exact GroupID64 string. An empty
diagnostic table remains in each provider. Game data and collection workflows
are not read or changed. Requests have bounded retries/timeouts and reject redirects.
The main probe has a 120-second request budget, with a separate 30-second
cleanup budget; the Actions job also has a five-minute hard timeout.
Logs, job summary and the 7-day report artifact contain status only, never the
URL, account/database identifiers, token or provider error body. A failed cleanup
is a failed check, not a successful connection claim. An interrupted run can leave its diagnostic row;
do not confuse this with game data or delete another active run's row.

Successful connectivity does **not** mean that production data has migrated or
that D1 failover is ready. The JSON storage/publication path remains active until
the import and standby gates below pass.

## Stage 2: pinned-source inventory and lossless import

Inventory all source repositories at fixed commits, including frontend growth
history and Twitch history. Never label a backend-only import as a complete
system migration. At backend commit
`6785ee523ea9305a1844e9705949d53e92f65eb5`, the 19 JSON documents under `data/`
and `experiments/` total 23,272,992 bytes; this is only a baseline snapshot.

Preserve unknown fields and source provenance while building normalized tables.
Keep GroupID64 and external identifiers as strings end to end. Retain per-source
collections, repeated attempts, cooldowns, publication receipts and original
date evidence. Derive executable queues using the existing domain rules: a
`pending_candidates` dictionary is not an authoritative runnable-job list.
Do not exclude accepted master records that no longer appear in candidates.

Validate per-source counts, checksums and reconstructability before switching
writers. Large original documents belong in versioned backup files or bounded
chunks: D1's 2 MB row limit rules out storing a 12 MB source JSON in one row.
Measure database size including indexes and replication bookkeeping, not only
raw JSON size. Initial imports and catch-up writes must respect daily quotas.

## Stage 3: D1 standby and portability

Validate the empty D1 database using the Stage 1 check and its three repository
secrets before implementing synchronization. If synchronization later moves to
another repository, provision its secrets explicitly there as well.

Confirm actual account plan and shared quotas. The current Free limits are
500 MB/database, 10 databases/account, 5 GB total and 100,000 rows written/day;
index updates also contribute. These are limits, not reserved capacity. Set a
capacity warning before 400 MB and validate any partitioning before depending
on it for complete standby coverage.

Use SQLite-compatible schemas with separately tested Turso and D1 access
adapters; replacing the connection URL alone is not sufficient. Write normal
business data only to the selected primary. Record changes transactionally on
that primary, replicate them idempotently to D1, and advance checkpoints only
after a complete batch is confirmed. Show the last replicated batch, lag and
pending count. Standby lag represents possible data loss if the primary is
permanently lost.

Keep the last successful public JSON during a primary outage. Persist pending
collection payloads outside ephemeral Actions runner storage. For a prolonged
outage, stop/fence old writers, verify standby coverage and explicitly promote
D1 before allowing new writes. On recovery, reconcile D1's new writes into
Turso before switching all writers back. Do not automatically run two primaries.

Retain versioned SQL exports outside the primary provider and test restoration;
a mirrored deletion is still a deletion. Future self-hosted deployment is out
of scope for this rollout.

## References

- https://docs.turso.tech/sdk/http/reference
- https://docs.turso.tech/cli/db/shell
- https://developers.cloudflare.com/d1/platform/limits/
- https://developers.cloudflare.com/api/resources/d1/subresources/database/methods/query/
- https://developers.cloudflare.com/d1/platform/pricing/
- https://developers.cloudflare.com/d1/best-practices/import-export-data/
