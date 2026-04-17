# Multi-stage build on RHEL 9 (Red Hat Universal Base Image 9).
#   stage 1: build the Vite bundle with Node.js
#   stage 2: install the Python backend and copy the built frontend in
# The final image runs both services on ports 8000 (API+MCP) and 5173 (UI).

ARG UBI_VERSION=9.4
# Pin uv so resolver/CLI changes in a future release can't silently break the
# backend install step; bump deliberately when rolling the lockfile.
ARG UV_VERSION=0.8.17

# ─── Stage 1 — build the React/Vite frontend ──────────────────────────────────
FROM registry.access.redhat.com/ubi9/nodejs-20:${UBI_VERSION} AS frontend-build

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

# Python 3.11 + uv for dependency management, plus Node (used only to serve the
# built frontend via vite preview). dnf clean keeps the layer small.
RUN dnf install -y --setopt=install_weak_deps=False \
        python3.11 python3.11-pip \
        nodejs npm \
        shadow-utils \
    && dnf clean all \
    && rm -rf /var/cache/dnf \
    && python3.11 -m pip install --no-cache-dir "uv==${UV_VERSION}"

# Create an unprivileged user to run the services.
RUN useradd --system --create-home --uid 1001 printsim

WORKDIR /app

# Install backend deps with uv into a project-local .venv. --frozen uses the
# committed lockfile for reproducible builds; --no-install-project skips
# installing the backend itself (it's a non-package app, run from source).
ENV UV_PROJECT_ENVIRONMENT=/app/backend/.venv \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1
COPY backend/pyproject.toml backend/uv.lock /app/backend/
WORKDIR /app/backend
RUN uv sync --frozen --no-install-project

COPY backend/ /app/backend/

# Pull in the built Vite bundle plus the config needed to serve it.
COPY --from=frontend-build /build/frontend/dist /app/frontend/dist
COPY frontend/package.json /app/frontend/package.json
COPY frontend/vite.config.js /app/frontend/vite.config.js
# `vite preview` needs vite installed; install prod-only in a tiny node layer.
WORKDIR /app/frontend
RUN npm install --omit=dev --silent

WORKDIR /app
COPY docs/ /app/docs/
COPY README.md /app/README.md

RUN chown -R printsim:printsim /app
USER printsim

ENV PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    BACKEND_PORT=8000 \
    FRONTEND_PORT=5173 \
    PATH=/app/backend/.venv/bin:$PATH

EXPOSE 8000 5173

# Start both services; if either exits the container exits.
COPY --chown=printsim:printsim docker/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

HEALTHCHECK --interval=15s --timeout=5s --retries=5 \
  CMD curl -fsS http://127.0.0.1:${BACKEND_PORT}/api/health || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
