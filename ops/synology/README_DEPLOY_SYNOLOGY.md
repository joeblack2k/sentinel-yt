# Sentinel Synology Deploy Runbook (Bundle)

## Prerequisites
- User has permission to deploy Docker stacks on Synology.
- Docker/Container Manager available.
- Network access to YouTube lounge devices from Synology host.

## Install
1. Copy this folder to Synology.
2. Run `./scripts/install_synology.sh` (or manually place files under target stack directory).
3. Edit `.env` and set `GEMINI_API_KEY`.
4. Run `docker compose --env-file .env up -d --build` in stack directory.

## Verify
- `./scripts/verify_synology.sh`
- `curl http://127.0.0.1:8090/healthz`
- `curl http://127.0.0.1:8090/api/status`

## Rollback
- `./scripts/rollback_synology.sh`
- Restore previous stack backup if needed.
