# 3D Print Sim

![A virtual FDM print mid-simulation — thick glowing orange filament fills the lower layers, sparse rectilinear infill is visible through the top, and the print head hovers over the next layer](docs/screenshots/10-hero-infill-closeup.png)

Virtual FDM printer simulator. Upload STLs, auto-arrange on the bed, slice into
perimeters + infill + solid top/bottom layers, and watch the simulated print
head build each layer as thick, glowing filament.

Three ways to drive it:

- **Humans**: React + Three.js UI at `http://localhost:5173`.
- **AI agents**: stateful Model Context Protocol server (fastmcp 3.2+) at
  `http://localhost:8000/mcp`. Each conversation gets its own virtual printer.
- **Atlas UI**: the `open_viewer` MCP tool returns a canvas iframe envelope
  so Sandia's [Atlas UI 3](https://github.com/sandialabs/atlas-ui-3) shows the
  live 3D view next to the chat. See [`docs/mcp.md`](docs/mcp.md#live-viewer-in-atlas-iframe).

## Features

- **Real infill.** Rectilinear scan-line infill clipped to each layer's
  contours, configurable density, alternating direction + zig-zag routing.
- **Solid top/bottom layers.** First `bottom_layers` and last `top_layers` of
  each part print at 100% infill so surfaces look closed. Per-part — a short
  model next to a tall one still gets its own solid top.
- **Thick glowing toolpath render.** Printed filament is drawn as thick
  screen-space lines with a hot-yellow → settled-orange color ramp over the
  most recently deposited segments.
- **Auto-centered bed placement.** Uploaded parts land on the middle of the
  plate; `auto_arrange` centers the packed block instead of hugging the corner.
  Oversize parts stay unplaced and surface a 409 from `/api/slice` rather than
  silently producing out-of-bounds toolpaths.
- **Factory-as-a-service** (behind `FACTORY_ENABLED=1`). A grid of simulated
  printers with a FIFO job queue, a pick-and-place robot, and real-time
  progress tracking. One `factory_submit_job(...)` MCP call (or
  `POST /api/factory/jobs/upload`) covers upload + slice + queue + route.
  Tracks filament + cost per printer. See [`docs/factory.md`](docs/factory.md).

## Quickstart

```bash
# One-time: copy the example env file so backend and frontend share ports.
cp .env.example .env

# Backend (uses uv — https://docs.astral.sh/uv/)
cd backend
uv sync
set -a && source ../.env && set +a
uv run uvicorn app.main:app --reload --port "${BACKEND_PORT}"

# Frontend (separate terminal — Vite reads ../.env automatically)
cd frontend
npm install
npm run dev

# Open http://localhost:5173 (or whatever FRONTEND_PORT you set).
```

Both ports are driven by the repo-root `.env`:

| Variable | Default | Purpose |
|---|---|---|
| `HOST` | `0.0.0.0` | Bind host for the backend (and Vite in dev). |
| `BACKEND_PORT` | `8000` | FastAPI + MCP port. |
| `FRONTEND_PORT` | `5173` | Vite dev-server port (dev only — the container doesn't run Vite). |

## Architecture

- `backend/app/state.py` — in-memory printer state: bed size, parts,
  arrangement, slice results, simulation cursor.
- `backend/app/stl_loader.py` — parse ASCII/binary STL into triangles and an
  axis-aligned bounding box.
- `backend/app/arrange.py` — shelf-packer that places each part's XY footprint
  on the bed.
- `backend/app/slicer.py` — Z-plane slicer that intersects triangles per layer,
  chains segments into closed loops, and writes a G-code-ish toolpath.
- `backend/app/main.py` — FastAPI routes for upload/list/arrange/slice/simulate.
- `backend/app/mcp_server.py` — `fastmcp` tools that wrap the same operations so
  an AI agent can run the full pipeline.
- `frontend/src/` — Three.js scene with bed, parts, toolpath preview, and a
  simulation scrubber that steps through the G-code.
- `tests/e2e/` — Playwright tests exercising both the browser UI and the
  backend/MCP surface.

## MCP Tools

Served at `/mcp`. See `backend/app/mcp_server.py` for definitions.

- `get_printer_state`
- `set_bed_size(x_mm, y_mm, z_mm)`
- `upload_stl(name, stl_base64)`
- `list_parts`
- `remove_part(part_id)`
- `auto_arrange`
- `slice_all(layer_height_mm, perimeters)`
- `start_simulation`
- `step_simulation(steps)`
- `get_simulation_frame`
- `open_viewer(title?)` — opens the live 3D canvas in Atlas via the v2
  `display.type = "iframe"` envelope.

## Testing

```bash
# Backend unit tests
cd backend && uv run pytest

# Playwright E2E (starts backend + frontend via config)
cd tests && npm install && npx playwright test
```

## Podman

A RHEL 9 (UBI 9) image builds the backend and the prebuilt frontend into
one container that serves everything from a single port:

```bash
podman build -t 3dprintsim .
podman run --rm --env-file .env -p 8000:8000 3dprintsim
```

Then open `http://localhost:8000` — UI, `/api`, and `/mcp/` are all on the
same port. To run on a different port, pass both the publish spec and the
env override: `podman run --rm -e BACKEND_PORT=9000 -p 9000:9000 3dprintsim`.
See [`docs/docker.md`](docs/docker.md) for details.

## Documentation

Full walkthrough with screenshots lives under [`docs/`](docs/README.md) —
architecture, HTTP API, MCP tools, slicer internals, frontend layout, and
the screenshot pipeline.
