#!/usr/bin/env bash
set -euo pipefail

STACK_DIR=${STACK_DIR:-/volume1/docker/sentinel}
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
mkdir -p "$STACK_DIR"
cp "$ROOT_DIR/compose.yaml" "$STACK_DIR/compose.yaml"
cp "$ROOT_DIR/.env.template" "$STACK_DIR/.env"
mkdir -p "$STACK_DIR/data"
echo "Stack prepared at $STACK_DIR"
echo "Next: cd $STACK_DIR && docker compose --env-file .env up -d --build"
