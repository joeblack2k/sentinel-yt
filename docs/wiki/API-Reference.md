# API Reference

All payloads are JSON unless noted.

## Health

### `GET /healthz`
Basic container health check.

Example:
```bash
curl -s http://localhost:8090/healthz
```

## Control and Status

### `POST /api/control/state`
Enable or disable Sentinel globally.

Payload:
```json
{"active": true}
```

### `GET /api/status`
Returns active state, schedule state, device counts, judge status, timezone, build version.

### `POST /api/webhook/control`
Webhook equivalent for Home Assistant.

Payload:
```json
{"active": false, "source": "home_assistant"}
```

## SponsorBlock APIs

### `POST /api/sponsorblock/state`
Enable or disable SponsorBlock module.

### `POST /api/webhook/sponsorblock/state`
Webhook version for external automation.

### `POST /api/sponsorblock/schedule`
Set SponsorBlock schedule.

Payload:
```json
{"enabled": true, "start": "07:00", "end": "21:00", "timezone": "Europe/Amsterdam"}
```

### `POST /api/sponsorblock/config`
Set categories and minimum skip segment length.

Payload:
```json
{"categories": ["sponsor", "selfpromo"], "min_length_seconds": 1.0}
```

### `POST /api/sponsorblock/release`
Temporarily release remote interventions for N minutes.

Payload:
```json
{"minutes": 15, "reason": "shorts", "source": "dashboard"}
```

### `POST /api/webhook/sponsorblock/release`
Webhook version of temporary release.

## Device APIs

### `POST /api/devices/scan`
Scans LAN for candidate YouTube Lounge devices.

### `POST /api/devices/pair`
Pair selected discovered device using TV code.

Payload:
```json
{"device_ref": "<from-scan>", "pairing_code": "123 456 789 012"}
```

### `POST /api/devices/pair/code`
Manual pairing fallback using only TV code.

Payload:
```json
{"pairing_code": "123 456 789 012"}
```

## Live Stream API

### `GET /api/live/events`
Server-Sent Events stream for live dashboard updates.

Example:
```bash
curl -N http://localhost:8090/api/live/events
```

## Rules APIs

### `POST /api/rules/whitelist`
Add manual allow rule.

### `POST /api/rules/blacklist`
Add manual block rule.

Payload for both:
```json
{
  "scope": "video",
  "video_id": "dQw4w9WgXcQ",
  "label": "Example",
  "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
}
```

### `DELETE /api/rules/{rule_id}`
Delete manual rule by id.

### `POST /api/blocklist/policies`
Set strict block policy flags.

### `POST /api/allowlist/policies`
Set allow policy flags.

### `POST /api/blocklist/sources`
Save external blocklist TXT source URLs.

### `POST /api/blocklist/reload`
Reload blocklists from local and remote sources.

### `POST /api/blocklist/local`
Update local blocklist TXT content.

### `POST /api/allowlist/sources`
Save external allowlist TXT source URLs.

### `POST /api/allowlist/reload`
Reload allowlists from local and remote sources.

### `POST /api/allowlist/local`
Update local allowlist TXT content.

## Prompt and Settings APIs

### `POST /api/settings/prompt`
Save custom prompt text (empty string resets to default behavior).

### `POST /api/settings/prompt/reset`
Force reset prompt to built-in default.

### `POST /api/settings/webhook`
Set failure webhook URL for fatal Gemini incidents.

### `POST /api/settings/gemini`
Update runtime Gemini key and optional enable/disable state.

Payload:
```json
{"api_key": "AIza...", "enabled": true}
```

## Schedule APIs

### `GET /api/schedules`
List all schedule windows.

### `POST /api/schedules/add`
Add schedule window.

### `POST /api/schedules/{schedule_id}/update`
Update schedule window.

### `DELETE /api/schedules/{schedule_id}`
Delete schedule window (at least one must remain).

### `POST /api/settings/schedule`
Legacy compatibility endpoint for single schedule setup.

## History and DB APIs

### `GET /api/history?page=<n>`
Paged history (50/page, max 500 total).

### `GET /api/db/stats`
Returns database size and table stats.

### `POST /api/admin/purge`
Purge selected data.

Payload:
```json
{"target":"analysis_cache"}
```
Allowed targets: `analysis_cache`, `history`, `all`.

