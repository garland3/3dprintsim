# 3D Print Sim

Virtual FDM printer simulator. Upload STLs, auto-arrange on the bed, slice into
perimeter toolpaths, and watch the simulated print head build each layer.

Two ways to drive it:

- **Humans**: React + Three.js UI at `http://localhost:5173`.
- **AI agents**: Model Context Protocol server exposed over the same FastAPI
  backend at `http://localhost:8000/mcp` via `fastmcp`.

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
