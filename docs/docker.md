# Container image (podman)

3dprintsim ships with a multi-stage Containerfile (`Dockerfile`) that
produces a single image containing both the FastAPI backend — with the
MCP server mounted at `/mcp/` — and the prebuilt React UI served as
static files from the same process. One process, one port.

The base image is **Red Hat UBI 9** (RHEL 9 compatible). All commands
below use `podman`; swap `podman` for `docker` if you prefer Docker —
the Containerfile is compatible with both.

## Image layout

```
Dockerfile                     # two stages: Node build → UBI 9 runtime
docker/entrypoint.sh           # runs the single backend process
.dockerignore                  # keeps node_modules, caches, and .git out
```

### Stage 1 — frontend build

Base: `registry.access.redhat.com/ubi9/nodejs-20`. Installs exact npm
deps from `package-lock.json`, then runs `npm run build` to produce
`frontend/dist/`.

### Stage 2 — runtime

Base: `registry.access.redhat.com/ubi9/ubi`. Installs Python 3.11 and
`uv` (via pip) from the UBI repos; `uv sync --frozen` resolves the
backend `.venv` from the committed `uv.lock`, the backend source and
built frontend bundle are copied in, and the image runs as non-root user
`printsim` (UID 1001). No Node.js at runtime — FastAPI serves the
prebuilt bundle directly via `StaticFiles`.

A healthcheck polls `/api/health` every 15 s.

## Build

```bash
podman build -t 3dprintsim .
```

The build pulls from `registry.access.redhat.com`, which is free and
does not require a Red Hat subscription.

## Run

```bash
# Uses the ports from your repo-root .env (copy .env.example if you
# haven't yet). Only BACKEND_PORT is published — the frontend shares it.
podman run --rm -it \
  --env-file .env \
  -p "${BACKEND_PORT:-8000}:${BACKEND_PORT:-8000}" \
  --name 3dprintsim \
  3dprintsim
```

Then, on `http://localhost:${BACKEND_PORT}`:

- UI: `/`
- HTTP API: `/api/...`
- MCP (streamable HTTP): `/mcp/`

If you want to pick a port ad-hoc without touching `.env`:

```bash
podman run --rm -p 9000:9000 -e BACKEND_PORT=9000 3dprintsim
```

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `HOST` | `0.0.0.0` | Bind host for the backend. |
| `BACKEND_PORT` | `8000` | FastAPI / MCP / UI port. |
| `FRONTEND_DIST` | `/app/frontend/dist` | Path to the built SPA the backend serves. Override only if you're mounting a different bundle in. |
| `FRAME_ANCESTORS` | `*` | CSP `frame-ancestors` for the Atlas embed iframe. |

`FRONTEND_PORT` from `.env` is intentionally unused by the container —
there is no second server inside the image. It only matters for local
`npm run dev`.

## Rebuilding after source changes

The build is layered so that dependency installs are cached across runs:

1. Only `package.json` / `pyproject.toml` / `uv.lock` changed → only the
   dep-install layers rebuild.
2. Only `frontend/src` changed → only the Vite build re-runs.
3. Only `backend/app` changed → only the final COPY layer re-runs.

## Troubleshooting

- **`curl` not found for healthcheck.** The image intentionally keeps
  userland minimal. If you need `curl` inside the container, add it
  with:
  ```dockerfile
  RUN dnf install -y curl && dnf clean all
  ```
- **Port already in use.** Change the left side of `-p` (host:container)
  or set `BACKEND_PORT` to a free value on both sides.
- **MCP client can't connect.** The MCP endpoint is `/mcp/` (with
  trailing slash). `fastmcp` clients error without it.
- **Rootless podman and low ports.** `podman run -p 80:8000` needs
  `sysctl net.ipv4.ip_unprivileged_port_start=80` (or a `sudo podman`
  invocation). Picking a port ≥ 1024 avoids the problem.
