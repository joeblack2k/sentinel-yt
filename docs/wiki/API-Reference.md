# API Reference

This document describes the Kids-focused Sentinel Guardian API. All payloads
are JSON unless noted.

## Health

### `GET /healthz`
Basic process health check.

### `GET /readyz`
Readiness check for the Kids pipeline. It verifies OpenCodex availability,
ingest freshness, and the resolver backlog. The equivalent namespaced route is
`GET /api/kids/readyz`.

## Control and Status

### `POST /api/control/state`
Enable or disable Guardian monitoring globally.

Payload:
```json
{"active": true}
```

### `GET /api/status`
Returns monitoring, schedule, judge, timezone, error, and build information.

### `POST /api/webhook/control`
Webhook equivalent for changing the global monitoring state.

Payload:
```json
{"active": false, "source": "home_assistant"}
```

## Blocklist

The blocklist is the source of truth for content that must not enter the Kids
catalog.

### `POST /api/blocklist/rules`
Add a block rule.

Payload:
```json
{
  "scope": "video",
  "video_id": "dQw4w9WgXcQ",
  "label": "Example",
  "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
}
```

### `DELETE /api/blocklist/rules/{rule_id}`
Delete a block rule by id.

### `POST /api/blocklist/policies`
Update the blocklist policy flags.

### `POST /api/blocklist/sources`
Save external blocklist TXT source URLs.

### `POST /api/blocklist/reload`
Reload local and external blocklist sources.

### `POST /api/blocklist/local`
Replace the local blocklist TXT content.

## Kids Catalog

### `GET /api/kids/catalog/revision`
Return the current catalog revision.

### `GET /api/kids/sources`
List configured channel and playlist sources.

### `POST /api/kids/sources`
Add a channel or playlist source for Guardian ingest.

### `PATCH /api/kids/sources/{source_id}/state`
Change a source state, including `revoked`.

### `GET /api/kids/catalog/items`
List catalog items visible to the Guardian UI.

### `GET /api/kids/catalog/items/{item_id}`
Return one catalog item.

### `GET /api/kids/catalog/items/by-video/{video_id}`
Return the catalog item for a video id.

### `POST /api/kids/catalog/items`
Add a catalog candidate.

### `PATCH /api/kids/catalog/items/{item_id}/state`
Change an item state, including `revoked`.

### `GET /api/kids/status`
Return kill-switch, schedule, catalog, and resolver status.

### `GET /api/kids/control/kill-switch`
Read the Kids kill-switch state.

### `POST /api/kids/control/kill-switch`
Set the Kids kill-switch state. Enabling it empties the Kids feed and stops
playback authorization.

## Kids Dataplane

The tvOS client uses only these minimal dataplane routes. It does not connect
to YouTube directly.

### `GET /v1/kids/feed?cursor=&limit=&profile=`
Return an opaque-cursor page of approved, currently playable thumbnails. The
response contains no titles, channel names, or raw YouTube URLs.

### `GET /v1/kids/thumbnails/{asset_id}`
Proxy one approved thumbnail.

### `POST /v1/kids/playback-sessions`
Create a short-lived playback lease after a fresh Guardian check.

### `GET /v1/kids/playback-sessions/{lease_id}/manifest`
Return the authorized video and audio relay endpoints.

### `GET /v1/kids/playback-sessions/{lease_id}/status`
Check whether a playback lease is still active.

### `DELETE /v1/kids/playback-sessions/{lease_id}`
Close a playback lease.

### `GET|HEAD /v1/kids/playback-sessions/{lease_id}/{video|audio}`
Relay the authorized media stream while the lease remains valid.

### `POST /v1/kids/events`
Record a dataplane event such as `selected`, `started`, `stopped`, or
`completed`.

## History and Audit

### `GET /api/kids/watch-events?limit=`
Return Kids playback events.

### `POST /api/kids/watch-events`
Record a Kids playback event from an authorized integration.

### `GET /api/kids/audit?limit=`
Return Guardian audit events for catalog and policy changes.

### `GET /api/history`
Return the latest Kids watch events for the Guardian history view.

### `GET /api/db/stats`
Return database size and table statistics.

### `POST /api/admin/purge`
Purge selected data.

Payload:
```json
{"target": "history"}
```

Allowed targets are `history` and `all`.

## Settings and Schedule

### `POST /api/settings/webhook`
Set the failure webhook URL.

### `GET /api/schedules`
List schedule windows.

### `POST /api/schedules/add`
Add a blocklist schedule window.

### `POST /api/schedules/{schedule_id}/update`
Update a schedule window.

### `DELETE /api/schedules/{schedule_id}`
Delete a schedule window. At least one window must remain.

### `POST /api/settings/schedule`
Update the single schedule compatibility endpoint.
