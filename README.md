# Sentinel v1 (YouTube AI Guardian)

Sentinel is a Docker-first parental-control gateway for YouTube on Apple TV (YouTube Lounge).  
It discovers TVs on LAN, pairs by TV code, monitors current/up-next videos, applies block/allow rules, optionally uses Gemini for AI decisions, and can actively force playback to a safe video.

## Quick Links
- Dashboard: `http://<host>:8090`
- Health: `GET /healthz`
- Live status API: `GET /api/status`
- Wiki pages (in this repo): `/docs/wiki/*.md`

## Tech Stack
- Backend: FastAPI + async workers
- UI: Jinja templates + SSE live updates
- Storage: SQLite (`/data/sentinel.db`)
- TV control: `pyytlounge`
- AI: Google Gemini (`google-genai`)
- Packaging: Docker + Docker Compose

## Run with Docker
```bash
cp .env.example .env
docker compose --env-file .env up -d --build
```

Open:
`http://localhost:8090` (or replace with your host IP)

## Environment Variables
| Variable | Default | Description |
|---|---|---|
| `SENTINEL_PORT` | `8090` | Web UI and API port |
| `SENTINEL_DB_PATH` | `/data/sentinel.db` | SQLite database path |
| `SENTINEL_BUILD_VERSION` | `v1` | Build/version label shown in UI |
| `GEMINI_API_KEY` | empty | Gemini API key (optional if running list-only mode) |
| `GEMINI_MODEL` | `gemini-2.0-flash` | Gemini model id |
| `TZ` | `UTC` | Container timezone |

## GitHub-Hosted Docker Image (GHCR)
This repo includes `.github/workflows/docker-publish.yml` to publish image tags to:

`ghcr.io/joeblack2k/sentinel-yt`

Pull latest:
```bash
docker pull ghcr.io/joeblack2k/sentinel-yt:latest
```

Example compose override using GHCR image:
```yaml
services:
  sentinel:
    image: ghcr.io/joeblack2k/sentinel-yt:latest
    network_mode: host
    volumes:
      - ./data:/data
    env_file:
      - .env
    restart: unless-stopped
```

## First-Time Setup (Pairing)
1. Open YouTube on Apple TV -> Settings -> Link with TV code.
2. In Sentinel, go to `Devices`.
3. Press `Scan Network`.
4. Click `Pair` on the matching TV row.
5. Enter the code shown on TV and submit.

Fallback:
- Use `POST /api/devices/pair/code` with only `pairing_code` when scan matching is not possible.

## Functional Reference (Each Function + Example)

### Function: Home Dashboard
Shows blocked/allowed totals, trend charts, source breakdown, and database size.

Example:
- Use this page to see if policy changes increased block rate.

### Function: Live Monitor
Streams real-time events (playing video, decision, intervention result, errors).

Example:
- Confirm a blocked video produced `intervention_play_safe`.

### Function: History
Displays paged history: 50 items per page, max 500.

Example:
- Review a bad ALLOW decision, then blacklist it directly.

### Function: Blocklist
Manual block rules + policy toggles + imported TXT sources + local editable list.

Example manual rule payload:
```json
{
  "scope": "video",
  "video_id": "dQw4w9WgXcQ",
  "label": "Never allow this",
  "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
}
```

### Function: Allowlist
Manual allow rules + positive-content policy toggles + imported/local TXT sources.

Example:
- Switch schedule mode to `whitelist` and only allow curated channels.

### Function: Schedule Windows
Supports multiple windows. Each window has its own mode:
- `blocklist`: normal allow + block on match
- `whitelist`: default deny unless allowlisted

Example:
- 07:00-17:00 whitelist mode, 17:00-20:00 blocklist mode.

### Function: SponsorBlock Module
Independent from Sentinel:
- own on/off
- own schedule
- own webhook controls
- temporary remote release timer

Example release request:
```json
{"minutes": 15, "source": "home_assistant", "reason": "shorts"}
```

### Function: Prompt Editor
Editable custom prompt with immutable JSON output contract appended automatically.

Example:
- Save custom guardrails, keep schema lock for strict parser compatibility.

### Function: Gemini Optional Mode
Gemini can be disabled. In that case Sentinel continues with local rules/lists only.

Example:
- Set `enabled=false` via `POST /api/settings/gemini`.

### Function: Fail-Open AI Handling
On fatal Gemini auth/quota/token failure:
- AI path degrades safely
- local block/allow lists still apply
- failure webhook can notify Home Assistant

### Function: Database Stats and Purge
View DB size and purge cache/history when needed.

Example purge payload:
```json
{"target":"analysis_cache"}
```

### Function: Automation/Webhooks
Control Sentinel and SponsorBlock from Home Assistant or any automation platform.

Examples:
- Pause Sentinel: `POST /api/webhook/control`
- Toggle SponsorBlock: `POST /api/webhook/sponsorblock/state`
- Temporary release: `POST /api/webhook/sponsorblock/release`

## API Overview

### Health and Status
- `GET /healthz`
- `GET /api/status`

### Control
- `POST /api/control/state`
- `POST /api/webhook/control`

### Devices
- `POST /api/devices/scan`
- `POST /api/devices/pair`
- `POST /api/devices/pair/code`

### Live
- `GET /api/live/events` (SSE)

### Rules
- `POST /api/rules/whitelist`
- `POST /api/rules/blacklist`
- `DELETE /api/rules/{rule_id}`
- `POST /api/blocklist/policies`
- `POST /api/allowlist/policies`

### Source Lists
- `POST /api/blocklist/sources`
- `POST /api/blocklist/reload`
- `POST /api/blocklist/local`
- `POST /api/allowlist/sources`
- `POST /api/allowlist/reload`
- `POST /api/allowlist/local`

### Prompt/Settings
- `POST /api/settings/prompt`
- `POST /api/settings/prompt/reset`
- `POST /api/settings/schedule`
- `POST /api/settings/webhook`
- `POST /api/settings/gemini`

### Schedules
- `GET /api/schedules`
- `POST /api/schedules/add`
- `POST /api/schedules/{schedule_id}/update`
- `DELETE /api/schedules/{schedule_id}`

### SponsorBlock
- `POST /api/sponsorblock/state`
- `POST /api/webhook/sponsorblock/state`
- `POST /api/sponsorblock/schedule`
- `POST /api/sponsorblock/config`
- `POST /api/sponsorblock/release`
- `POST /api/webhook/sponsorblock/release`

### Data Management
- `GET /api/history`
- `GET /api/db/stats`
- `POST /api/admin/purge`

## Blocklist / Allowlist TXT Format
Local files are adblock-style, supports comments and metadata:

```txt
# Block noisy toddler-targeted content
video:dQw4w9WgXcQ | Example title | https://www.youtube.com/watch?v=dQw4w9WgXcQ
channel:@ExampleChannel | Example channel | https://www.youtube.com/@ExampleChannel
```

## Deployment
```bash
docker compose --env-file .env up -d --build
BASE_URL=http://127.0.0.1:8090 ./scripts/smoke.sh
```

## Synology Deploy Bundle
See:
- `ops/synology/compose.yaml`
- `ops/synology/.env.template`
- `ops/synology/README_DEPLOY_SYNOLOGY.md`
- `ops/synology/deploy_bundle.tar.gz`

## Testing
```bash
pytest
```

CI workflow:
- `.github/workflows/ci.yml`

Docker publish workflow:
- `.github/workflows/docker-publish.yml`
