# Game Trend Radar Content Backend

Event-driven Steam metadata enrichment backend.

## Responsibility

This backend does **not** discover games and does **not** verify Followers.
It only accepts a game after Backend A has verified Steam Community
`memberCount >= 5000`.

For each accepted AppID it refreshes official/public Steam content:

- Taiwan release timestamp/date
- English / Traditional Chinese / Simplified Chinese Store names
- game language support
- official header / main capsule artwork
- genres
- public Steam Store tags when available
- short description
- official Followers value supplied by Backend A

The enriched record is then upserted into
`game-trend-radar/data/steam_upcoming.json`.

The operation is idempotent through
`content_enrichment_signature = appid:followers:release_date`.

## Event contract

`repository_dispatch` event type: `steam_game_qualified`

Required payload:

```json
{
  "appid": 4115450,
  "official_followers": 5000,
  "release_date": "2026-10-29",
  "official_checked_at_taipei": "2026-09-23T18:00:00+08:00"
}
```

A future physical split into a dedicated repository only requires moving this
directory and its workflow, then changing Backend A's dispatch target.
