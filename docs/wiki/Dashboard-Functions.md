# Dashboard Functions

## Home
Shows totals, trends, and block/allow analytics.

### Configure
- No setup required.
- Data comes from `video_decisions` and DB stats.

### Example Use
- Check if block rate is rising after enabling strict policy toggles.

## Live
Real-time stream of current video decisions and interventions.

### Configure
- Auto-updates through SSE (`/api/live/events`).

### Example Use
- Confirm a blocked video triggers `intervention_play_safe`.

## History
Paginated decision log (50 per page, max 500 retained for UI paging).

### Configure
- Add manual rules directly from rows.

### Example Use
- Review a false ALLOW and blacklist that video/channel.

## Blocklist
Manage what must be blocked.

### Configure
- Manual rules: `video:<id>` or `channel:<id>`
- Policy toggles: strict filters (brainrot/horror/violence/etc)
- Remote source lists: import raw TXT files (GitHub raw URLs)
- Local file editor: maintain custom block entries with comments

### Example Use
- Add `video:dQw4w9WgXcQ | Example | https://www.youtube.com/watch?v=dQw4w9WgXcQ`

## Allowlist
Manage what is explicitly allowed.

### Configure
- Manual allow rules for video/channel
- Allow-policy toggles (cartoons, educational, Disney, etc)
- Remote source lists and local allowlist file

### Example Use
- Enforce whitelist-only schedule mode, then allow only trusted channels.

## Schedule
Create multiple schedule windows with individual enforcement mode.

### Configure
- Add/remove windows
- Per window:
  - timezone
  - start/end
  - mode: `blocklist` or `whitelist`

### Example Use
- School hours = `whitelist`, evening = `blocklist`.

## SponsorBlock
Independent sponsor segment skipper.

### Configure
- Enabled state toggle
- Own schedule + timezone
- Segment categories
- Minimum segment length
- Temporary remote release (minutes)

### Example Use
- Disable interventions for 15 minutes during Shorts:
  - set release minutes to `15`

## Devices
Find, pair, and monitor TV connection status.

### Configure
- Scan network
- Pair per discovered row (preferred)
- Manual code-only pairing fallback

### Example Use
- Re-pair after token expiry and confirm device status changes to connected.

## Settings
Global Sentinel behavior and AI configuration.

### Configure
- Active state
- Gemini key runtime override + enable/disable
- Custom prompt editor
- Locked JSON output contract suffix (always appended)
- Failure webhook URL
- DB stats and purge controls

### Example Use
- Disable Gemini and run with local block/allow rules only.

## Automation
Shows webhook/API payloads for Home Assistant or external automation.

### Configure
- Use control endpoints in scripts/automations.

### Example Use
- HA automation pauses Sentinel at bedtime with `POST /api/webhook/control`.

