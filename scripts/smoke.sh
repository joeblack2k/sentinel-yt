#!/usr/bin/env bash
set -euo pipefail

BASE_URL=${BASE_URL:-http://127.0.0.1:8090}

echo "[smoke] healthz"
curl -fsS "${BASE_URL}/healthz" | jq .

echo "[smoke] status"
curl -fsS "${BASE_URL}/api/status" | jq .

echo "[smoke] control toggle false"
curl -fsS -X POST "${BASE_URL}/api/control/state" \
  -H 'Content-Type: application/json' \
  -d '{"active":false}' | jq .

echo "[smoke] control toggle true"
curl -fsS -X POST "${BASE_URL}/api/control/state" \
  -H 'Content-Type: application/json' \
  -d '{"active":true}' | jq .

echo "[smoke] sponsorblock toggle true"
curl -fsS -X POST "${BASE_URL}/api/sponsorblock/state" \
  -H 'Content-Type: application/json' \
  -d '{"active":true}' | jq .

echo "[smoke] sponsorblock release 2m"
curl -fsS -X POST "${BASE_URL}/api/sponsorblock/release" \
  -H 'Content-Type: application/json' \
  -d '{"minutes":2,"source":"smoke","reason":"validation"}' | jq .

echo "[smoke] sponsorblock release clear"
curl -fsS -X POST "${BASE_URL}/api/sponsorblock/release" \
  -H 'Content-Type: application/json' \
  -d '{"minutes":0,"source":"smoke","reason":"clear"}' | jq .

echo "[smoke] sponsorblock toggle false"
curl -fsS -X POST "${BASE_URL}/api/sponsorblock/state" \
  -H 'Content-Type: application/json' \
  -d '{"active":false}' | jq .

echo "[smoke] db stats"
curl -fsS "${BASE_URL}/api/db/stats" | jq '{total_bytes, video_decisions, analysis_cache, rules, sponsorblock_actions}'

echo "[smoke] done"
