#!/usr/bin/env bash
set -euo pipefail

BASE_URL=${BASE_URL:-http://127.0.0.1:8090}

echo "[smoke] healthz"
curl -fsS "${BASE_URL}/healthz" | jq .

echo "[smoke] status"
curl -fsS "${BASE_URL}/api/status" | jq .

echo "[smoke] kids status"
curl -fsS "${BASE_URL}/api/kids/status" | jq .

echo "[smoke] guardian pages"
for path in /kids /sources /resolve /history /blocklist /schedule /settings; do
  curl -fsS "${BASE_URL}${path}" >/dev/null
done

echo "[smoke] kids profiles"
test "$(curl -fsS "${BASE_URL}/api/kids/profiles" | jq '.profiles | length')" -ge 2

echo "[smoke] removed legacy pages"
for path in /live /allowlist /devices /automation /mqtt /sponsorblock /rules; do
  test "$(curl -s -o /dev/null -w '%{http_code}' "${BASE_URL}${path}")" = 404
done

echo "[smoke] db stats"
curl -fsS "${BASE_URL}/api/db/stats" | jq '{total_bytes, kids_watch_events, rules}'

echo "[smoke] done"
