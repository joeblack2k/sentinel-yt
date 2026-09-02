# Dashboard Functions

The Guardian dashboard is the parent control surface for SubTube Kids. It
contains only catalog, filtering, schedule, settings, and watch-history
controls.

## Kids

Shows configured channels and playlists, catalog items, resolver backlog, and
the Kids watch log.

### Configure
- Add a channel or playlist source.
- Revoke a source or catalog item.
- Review source checks, resolver quality, expiry, and ingest state.

### Example Use
- Add a trusted educational channel and wait for the ingest worker to publish
  its eligible videos.

## History

Shows the latest playback events received from the Kids app.

### Configure
- No separate history source is required.
- Events are written to the shared SQLite database.

### Example Use
- Review selections, starts, completions, positions, and startup timings when
  adjusting the Kids catalog.

## Blocklist

The blocklist is the source of truth for content that must not appear in the
Kids catalog or start playback.

### Configure
- Add manual video or channel rules.
- Enable policy flags for categories such as brainrot, horror, violence,
  weapons, dangerous challenges, and clickbait.
- Import external TXT blocklist sources.
- Maintain local blocklist entries and comments.

### Example Use
- Add `channel:UC...` or `video:dQw4w9WgXcQ` and reload the blocklist.
  Matching catalog sources and items are removed from the Kids feed.

## Schedule

Controls when the Kids dataplane is available. The schedule always uses
blocklist enforcement.

### Configure
- Add, update, or remove schedule windows.
- Set timezone, start time, end time, and enabled state.

### Example Use
- Close the Kids feed during school or bedtime hours.

## Settings

Controls the Guardian runtime.

### Configure
- Enable or disable monitoring.
- Inspect the configured OpenCodex classifier endpoint and model.
- Configure the failure webhook.
- Inspect database statistics and purge watch history.

## Readiness

`/readyz` reports whether OpenCodex is reachable, ingest is fresh, and the
resolver has enough fresh playable items. A stale ingest or unavailable
classifier keeps the Kids dataplane closed until the pipeline recovers.
