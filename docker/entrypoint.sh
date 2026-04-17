#!/usr/bin/env bash
# Launches the FastAPI backend and the Vite preview server together.
# Exits as soon as either process exits, so the container dies with the
# first unhealthy service and the orchestrator can restart it.
set -euo pipefail

BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
HOST="${HOST:-0.0.0.0}"

echo "[entrypoint] starting backend on ${HOST}:${BACKEND_PORT}"
cd /app/backend
uv run --frozen --no-sync uvicorn app.main:app --host "${HOST}" --port "${BACKEND_PORT}" &
BACKEND_PID=$!

echo "[entrypoint] starting frontend preview on ${HOST}:${FRONTEND_PORT}"
cd /app/frontend
npx --yes vite preview --host "${HOST}" --port "${FRONTEND_PORT}" &
FRONTEND_PID=$!

trap 'echo "[entrypoint] shutting down"; kill -TERM "${BACKEND_PID}" "${FRONTEND_PID}" 2>/dev/null || true' INT TERM

# Exit with whichever process dies first.
wait -n "${BACKEND_PID}" "${FRONTEND_PID}"
EXIT_CODE=$?
kill -TERM "${BACKEND_PID}" "${FRONTEND_PID}" 2>/dev/null || true
wait 2>/dev/null || true
exit "${EXIT_CODE}"
