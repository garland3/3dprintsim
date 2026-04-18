#!/usr/bin/env bash
# Launches the FastAPI backend, which also serves the prebuilt Vite bundle.
# One process, one port — the container dies with the service and the
# orchestrator can restart it.
set -euo pipefail

BACKEND_PORT="${BACKEND_PORT:-8000}"
HOST="${HOST:-0.0.0.0}"

echo "[entrypoint] starting 3dprintsim on ${HOST}:${BACKEND_PORT}"
cd /app/backend
exec uv run --frozen --no-sync uvicorn app.main:app --host "${HOST}" --port "${BACKEND_PORT}"
