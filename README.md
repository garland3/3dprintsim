# 3D Print Sim

![A virtual FDM print mid-simulation — thick glowing orange filament fills the lower layers, sparse rectilinear infill is visible through the top, and the print head hovers over the next layer](docs/screenshots/10-hero-infill-closeup.png)

Virtual FDM printer simulator. Upload STLs, auto-arrange on the bed, slice into
perimeters + infill + solid top/bottom layers, and watch the simulated print
head build each layer as thick, glowing filament.

Two ways to drive it:

- **Humans**: React + Three.js UI at `http://localhost:5173`.
- **AI agents**: Model Context Protocol server exposed over the same FastAPI
  backend at `http://localhost:8000/mcp` via `fastmcp`.

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

## Quickstart

```bash
# Backend
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000

# Frontend (separate terminal)
cd frontend
npm install
npm run dev

# Open http://localhost:5173
```

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

## Testing

```bash
# Backend unit tests
cd backend && pytest

# Playwright E2E (starts backend + frontend via config)
cd tests && npm install && npx playwright test
```

## Docker

A RHEL 9 (UBI 9) image builds both services into one container:

```bash
docker build -t 3dprintsim .
docker run --rm -p 8000:8000 -p 5173:5173 3dprintsim
```

See [`docs/docker.md`](docs/docker.md) for details.

## Documentation

Full walkthrough with screenshots lives under [`docs/`](docs/README.md) —
architecture, HTTP API, MCP tools, slicer internals, frontend layout, and
the screenshot pipeline.
