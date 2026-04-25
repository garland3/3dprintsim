# Multi-stage build on RHEL 9 (Red Hat Universal Base Image 9).
#   stage 1: build the Vite bundle with Node.js
#   stage 2: install the Python backend and copy the built frontend in
# The final image runs a single FastAPI process that serves both the API
# (and MCP at /mcp) and the static frontend bundle from one port.

ARG UBI_VERSION=9.4
# UBI application stream images (nodejs-20, etc.) tag by stream major, not RHEL
# minor — keep this separate from UBI_VERSION.
ARG UBI_NODEJS_TAG=1
# Pin uv so resolver/CLI changes in a future release can't silently break the
# backend install step; bump deliberately when rolling the lockfile.
ARG UV_VERSION=0.9.26

# ─── Stage 1 — build the React/Vite frontend ──────────────────────────────────
FROM registry.access.redhat.com/ubi9/nodejs-20:${UBI_NODEJS_TAG} AS frontend-build

USER 0
WORKDIR /build/frontend

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY frontend/ ./
RUN npm run build

# ─── Stage 2 — Python runtime + built frontend ────────────────────────────────
FROM registry.access.redhat.com/ubi9/ubi:${UBI_VERSION} AS runtime

# ARGs declared before the first FROM need re-declaration to be visible in
# a stage (Docker scoping rule).
ARG UV_VERSION

# uv manages Python itself, and uv's standalone CPython builds work fine in
# rootless podman. Bootstrap uv via the official installer (no system Python
# needed at runtime; uv downloads 3.11 on `uv sync` below). We still skip
# shadow-utils for the same reason as before: it carries
# file capabilities that setcap can't apply under rootless build.
# curl-minimal + ca-certificates are already in the UBI9 base — installing
# the full `curl` package conflicts with curl-minimal under dnf, so we just
# use what's there to fetch the uv installer.
RUN curl -LsSf "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-installer.sh" \
        | env UV_INSTALL_DIR=/usr/local/bin sh

# Create an unprivileged user to run the service.
RUN useradd --system --create-home --uid 1001 printsim

WORKDIR /app

# Install backend deps with uv into a project-local .venv. --frozen uses the
# committed lockfile for reproducible builds; --no-install-project skips
# installing the backend itself (it's a non-package app, run from source).
ENV UV_PROJECT_ENVIRONMENT=/app/backend/.venv \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_INSTALL_DIR=/opt/uv-python \
    UV_PYTHON=3.11
COPY backend/pyproject.toml backend/uv.lock /app/backend/
WORKDIR /app/backend
RUN uv python install 3.11 \
    && uv sync --frozen --no-install-project

COPY backend/ /app/backend/

# Pull in the built Vite bundle. FastAPI mounts this dir at / so the UI is
# served from the same port as /api and /mcp.
COPY --from=frontend-build /build/frontend/dist /app/frontend/dist

WORKDIR /app
COPY docs/ /app/docs/
COPY README.md /app/README.md

RUN chown -R printsim:printsim /app
USER printsim

ENV PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    BACKEND_PORT=8000 \
    FRONTEND_DIST=/app/frontend/dist \
    PATH=/app/backend/.venv/bin:$PATH

EXPOSE 8000

# --chmod bakes mode 0755 into the COPY so we don't need a separate RUN chmod
# after switching to USER printsim — rootless podman's user-namespace mapping
# blocks non-root chmod on files with root-mapped attrs.
COPY --chown=printsim:printsim --chmod=0755 docker/entrypoint.sh /app/entrypoint.sh

HEALTHCHECK --interval=15s --timeout=5s --retries=5 \
  CMD curl -fsS http://127.0.0.1:${BACKEND_PORT}/api/health || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
