# Getting Started

## Prerequisites

- Docker and Docker Compose, or the local Python environment.
- LAN access to the Sentinel host.
- A persistent Chromium profile already signed in to YouTube Kids for ingest.
- A reachable OpenCodex endpoint for classification.

## 1) Clone and Configure

```bash
git clone https://github.com/<your-account>/<your-repo>.git
cd <your-repo>
cp .env.example .env
```

Example `.env` values:

```env
SENTINEL_PORT=8090
SENTINEL_DB_PATH=/data/sentinel.db
SENTINEL_BUILD_VERSION=v1
OPENCODEX_BASE_URL=http://127.0.0.1:10100/v1
OPENCODEX_MODEL=google-antigravity/gemini-3.7-flash
KIDS_BROWSER_CDP_URL=http://127.0.0.1:9223
TZ=Europe/Amsterdam
```

## 2) Start Sentinel

```bash
docker compose --env-file .env up -d --build
```

Open the Guardian at `http://<host-ip>:8090/kids`.

## 3) Verify the Pipeline

- `/healthz` returns `{"status":"ok"}`.
- `/readyz` reports OpenCodex, ingest freshness, and resolver readiness.
- The `/blocklist` page contains the rules used for Kids filtering.
- The `/kids` page lists the configured sources and catalog backlog.

## 4) Add Kids Sources

1. Open the `/kids` page.
2. Add a YouTube Kids channel or playlist reference.
3. Let the ingest worker inspect the source and populate the catalog.
4. Confirm that the source and its items pass the blocklist and classifier
   checks before they appear in the Kids feed.

The persistent browser session is reused by the ingest worker. Do not replace
it with a newly created profile while the worker is running.

## 5) Development

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
