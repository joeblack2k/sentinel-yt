#!/usr/bin/env bash
set -euo pipefail

STACK_DIR=${STACK_DIR:-/volume1/docker/sentinel}
cd "$STACK_DIR"
docker compose down

echo "Rollback baseline command executed: docker compose down"
echo "Restore previous stack backup if available."
