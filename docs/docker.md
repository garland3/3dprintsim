# Docker

3dprintsim ships with a multi-stage Dockerfile that produces a single image
containing both the FastAPI backend (with the MCP server mounted at `/mcp`)
and the built React UI. The base image is
**Red Hat UBI 9** (RHEL 9 compatible).

## Image layout

```
Dockerfile                     # two stages: Node build → UBI 9 runtime
docker/entrypoint.sh           # runs both services, exits on first failure
.dockerignore                  # keeps node_modules, caches, and .git out
```

### Stage 1 — frontend build

Base: `registry.access.redhat.com/ubi9/nodejs-20`. Installs exact npm deps
from `package-lock.json`, then runs `npm run build` to produce
`frontend/dist/`.

### Stage 2 — runtime

Base: `registry.access.redhat.com/ubi9/ubi`. Installs Python 3.11 and
Node.js from the UBI repos, installs backend requirements, copies the
backend source and the built frontend bundle, and runs as non-root user
`printsim` (UID 1001).

A healthcheck polls `/api/health` every 15 s.

## Build

```bash
docker build -t 3dprintsim .
```

The build pulls from `registry.access.redhat.com`, which is free and does
not require a Red Hat subscription.

## Run

```bash
docker run --rm -it \
  -p 8000:8000 \
  -p 5173:5173 \
  --name 3dprintsim \
  3dprintsim
```

Then:

- UI: http://localhost:5173
- HTTP API: http://localhost:8000
- MCP (streamable HTTP): http://localhost:8000/mcp/

The container runs both services under `entrypoint.sh`; if either process
exits, the container exits so an orchestrator can restart it.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `HOST` | `0.0.0.0` | Bind host for both services. |
| `BACKEND_PORT` | `8000` | FastAPI / MCP port. |
| `FRONTEND_PORT` | `5173` | Vite preview port. |

Example: run on different ports internally:

```bash
docker run --rm -p 9000:9000 -p 4173:4173 \
  -e BACKEND_PORT=9000 -e FRONTEND_PORT=4173 \
  3dprintsim
```

## Rebuilding after source changes

The build is layered so that dependency installs are cached across runs:

1. Only `package.json` / `requirements.txt` changed → only the dep-install
   layers rebuild.
2. Only `frontend/src` changed → only the Vite build re-runs.
3. Only `backend/app` changed → only the final COPY layer re-runs.

## Troubleshooting

- **`curl` not found for healthcheck.** The image intentionally keeps
  userland minimal. If you need `curl` inside the container, add it with:
  ```dockerfile
  RUN dnf install -y curl && dnf clean all
  ```
- **Port already in use.** Change the left side of `-p` (host:container) or
  the `BACKEND_PORT` / `FRONTEND_PORT` env vars.
- **MCP client can't connect.** The MCP endpoint is `/mcp/` (with trailing
  slash). `fastmcp` clients error without it.
