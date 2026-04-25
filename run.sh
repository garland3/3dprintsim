#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
set -a; source "$SCRIPT_DIR/.env"; set +a

echo "Using port: ${BACKEND_PORT}"

# --format=docker: HEALTHCHECK is not supported by the default OCI format;
#   without this the directive in the Dockerfile is silently dropped.
# --cap-add=SETFCAP: needed for rootless podman so RPM scriptlets that set
#   file capabilities during dnf install can succeed.
podman build --format=docker --cap-add=SETFCAP -t 3dprintsim "$SCRIPT_DIR" && \
  podman run --rm --env-file "$SCRIPT_DIR/.env" \
    --network=host 3dprintsim
