#!/usr/bin/env bash
set -euo pipefail

OPTIONS_FILE="/data/options.json"

if [[ -f "${OPTIONS_FILE}" ]]; then
  eval "$(
    python3 - <<'PY'
import json
import shlex
from pathlib import Path

path = Path("/data/options.json")
try:
    options = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    options = {}

def emit(k: str, v: str) -> None:
    print(f"export {k}={shlex.quote(v)}")

emit("TZ", str(options.get("timezone", "UTC")))
emit("GEMINI_API_KEY", str(options.get("gemini_api_key", "")))
emit("GEMINI_MODEL", str(options.get("gemini_model", "gemini-2.0-flash")))
emit("SENTINEL_BUILD_VERSION", str(options.get("build_version", "ha-addon")))
PY
  )"
fi

export SENTINEL_HOST="${SENTINEL_HOST:-0.0.0.0}"
export SENTINEL_PORT="${SENTINEL_PORT:-8090}"
export SENTINEL_DB_PATH="${SENTINEL_DB_PATH:-/data/sentinel.db}"

echo "[sentinel-addon] starting on port ${SENTINEL_PORT}"
exec uvicorn app.main:app --host "${SENTINEL_HOST}" --port "${SENTINEL_PORT}"

