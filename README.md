# Sentinel Kids Guardian

Sentinel is the LAN-first Guardian control plane for SubTube Kids. It manages
the Kids catalog, blocklist policy, YouTube Kids ingest, resolver backlog,
playback relay, schedules, and watch history in one service and SQLite database.

## Quick Start

```bash
cp .env.example .env
docker compose --env-file .env up -d --build
```

Open `http://localhost:8090/kids`, or replace `localhost` with the host LAN
address.
The data directory is mounted at `/data` and the database is
`/data/sentinel.db`.

## Endpoints

General service:

- `GET /healthz`
- `GET /readyz`
- `GET /api/status`
- `GET /api/history`

Kids dataplane:

- `GET /v1/kids/feed`
- `GET /v1/kids/thumbnails/{asset_id}`
- `POST /v1/kids/playback-sessions`
- `GET /v1/kids/playback-sessions/{lease_id}/manifest`
- `GET /v1/kids/playback-sessions/{lease_id}/{video|audio}`
- `POST /v1/kids/events`
- `GET /api/kids/status`
- `GET /api/kids/readyz`

Guardian control:

- `GET /kids`
- `GET /history`
- `GET /blocklist`
- `GET /schedule`
- `GET /settings`

The Kids client receives opaque asset IDs and thumbnails only. Playback is
revalidated against the current catalog, schedule, blocklist, and resolver
lease. There is no general YouTube fallback.

Detailed API, dashboard, setup, and troubleshooting notes are in
[`docs/wiki`](docs/wiki).

## Configuration

The main settings are:

- `SENTINEL_PORT`, default `8090`
- `SENTINEL_DB_PATH`, default `/data/sentinel.db`
- `SENTINEL_BUILD_VERSION`, shown in status output
- `OPENCODEX_BASE_URL`, the local classifier gateway
- `OPENCODEX_MODEL`, the classifier model ID
- `KIDS_BROWSER_CDP_URL`, the existing persistent Kids Chromium CDP endpoint
- `KIDS_RESOLVER_MIN_QUALITY_HEIGHT`, `720` or `1080`; 4K is not supported

The `/blocklist` page and its policy flags are the source of truth for Kids
catalog filtering. The Kids ingest worker never approves a source from AI
output alone, and uncertain classification remains hidden.

## Deployment

For the integrated LAN service, deploy Sentinel on port `8090` and run the
Kids ingest and resolver timers from `deploy/`. Every deployment should record
the exact Git SHA in `SENTINEL_BUILD_VERSION`.

The Home Assistant add-on files are in `addon/sentinel-yt`. Synology deployment
uses the source files under `ops/synology`; generated archives are built during
deployment and are not committed.

## Development

Install the repository hooks once:

```bash
scripts/install-hooks
```

Run the test suite:

```bash
python -m pytest -q
```

Before every commit and push, the configured hooks run `/ponytail-review` on
the exact staged or outgoing changes.
