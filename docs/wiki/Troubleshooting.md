# Troubleshooting

## `/readyz` reports stale ingest

Check the ingest service and the existing persistent Chromium session. The
worker stops safely when YouTube Kids requires parent setup or the page is no
longer a valid Kids page. Complete the one-time setup in the existing profile,
then let the next scheduled ingest run.

## No new sources or videos appear

1. Confirm the source is a YouTube Kids channel or playlist reference.
2. Check the `/kids` page for ingest and resolver errors.
3. Check `/blocklist` for a matching channel, video, or policy rule.
4. Confirm OpenCodex is reachable and `/readyz` reports it as ready.

Unknown or uncertain content stays hidden. There is no general YouTube
fallback.

## A blocked item is still visible

Reload the blocklist and check the catalog revision. Matching sources and
items are reconciled out of the Kids feed. A revoked or stale playback lease
cannot authorize the item again.

## Playback is unavailable

Confirm that monitoring is active, the current schedule is open, and the
resolver has a fresh candidate at the configured 720p or 1080p minimum.
Playback authorization is checked again for every session.

## Watch history is missing

Open `/history` or `GET /api/kids/watch-events`. The Kids app must send an
event with the correct catalog item and profile. Database counts are available
from `GET /api/db/stats`.

## Data persistence

SQLite data is stored in the configured data directory. Keep the data volume
when recreating the service or the catalog, resolver backlog, and watch
history will be lost.
