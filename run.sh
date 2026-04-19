#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
set -a; source "$SCRIPT_DIR/.env"; set +a

echo "Using port: ${BACKEND_PORT}"

podman build -t 3dprintsim "$SCRIPT_DIR" && \
  podman run --rm --env-file "$SCRIPT_DIR/.env" \
    --network=host 3dprintsim
