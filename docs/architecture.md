# Architecture

3dprintsim is three layers sharing one in-memory printer.

```
┌───────────────────────┐     ┌────────────────────────┐
│  React + Three.js UI  │     │  AI agent (fastmcp)    │
│  frontend/src         │     │  backend/app/mcp_...   │
└──────────┬────────────┘     └──────────┬─────────────┘
           │ HTTP /api/*                 │ MCP /mcp
           ▼                             ▼
          ┌───────────────────────────────┐
          │   FastAPI app                  │
          │   backend/app/main.py          │
          └──────────────┬─────────────────┘
                         │ calls methods on
                         ▼
          ┌───────────────────────────────┐
          │   PrinterService (singleton)   │
          │   backend/app/state.py         │
          │   • bed size                   │
          │   • parts[]                    │
          │   • slice_result               │
          │   • simulation cursor          │
          └───────────────────────────────┘
```

Both the UI and the MCP client hit the same `PrinterService` singleton, so a
human and an agent always see the same virtual printer.

## Backend modules

| File | Responsibility |
|---|---|
| [`backend/app/main.py`](../backend/app/main.py) | FastAPI application, HTTP routes, CORS, mounts `/mcp`. |
| [`backend/app/mcp_server.py`](../backend/app/mcp_server.py) | `fastmcp` tools that wrap `PrinterService`. |
| [`backend/app/state.py`](../backend/app/state.py) | In-memory state + `PrinterService` façade (thread-safe via `RLock`). |
| [`backend/app/stl_loader.py`](../backend/app/stl_loader.py) | ASCII + binary STL parser → typed triangles + AABB. |
| [`backend/app/arrange.py`](../backend/app/arrange.py) | Deterministic shelf packer for part footprints. |
| [`backend/app/slicer.py`](../backend/app/slicer.py) | Z-plane slicer, polyline chaining, G-code emitter. |

## Frontend modules

| File | Responsibility |
|---|---|
| [`frontend/src/App.jsx`](../frontend/src/App.jsx) | UI composition: sidebar, dropzone, slicer/sim controls. Owns state fetched from the backend. |
| [`frontend/src/PrinterScene.js`](../frontend/src/PrinterScene.js) | Sole Three.js user. Manages renderer, orbit camera, bed/parts/toolpath groups, print-head mesh. |
| [`frontend/src/api.js`](../frontend/src/api.js) | Thin fetch wrapper around every `/api/*` route. |

The React layer hands the scene raw data (part geometry, toolpath moves); the
scene rebuilds buffers itself. This isolation keeps Three.js out of React's
render cycle.

## Shared state model

The `PrinterService` singleton is the ground truth:

```python
class PrinterService:
    bed_size: tuple[float, float, float]
    parts: dict[str, Part]
    slice_result: SliceResult | None
    simulation: Simulation  # { running, cursor, speed }
```

Every mutating call takes the service's `RLock` and invalidates the slice
when something earlier in the pipeline changes (new part, bed resize, part
removal). Re-sliced toolpaths reset the simulation cursor to 0.

## End-to-end flow

The typical request order matches what both the UI and the MCP agent do:

1. `set_bed_size` (optional; defaults are Prusa i3-style).
2. `upload_stl` one or more parts.
3. `auto_arrange` to lay parts out on the bed. (Called automatically by
   `slice_all` if any part has no placement.)
4. `slice_all` with a layer height and perimeter count → produces the ghost
   toolpath and an initial G-code string.
5. `start_simulation` then `step_simulation` (or `set_simulation_cursor`) to
   advance the print head along the toolpath.
6. `get_simulation_frame` returns the current head position plus the list of
   moves extruded so far — this is what the UI polls to redraw.

See [`api.md`](./api.md) for HTTP examples and [`mcp.md`](./mcp.md) for the
equivalent tool calls.

## Coordinate conventions

- Backend / G-code: right-handed, **Z up**, origin at bed front-left corner.
  Triangles, toolpath moves, G-code — all in these coords.
- Three.js scene: default **Y up**. `PrinterScene` re-maps `(x, y, z) →
  (x, z, y)` when it pushes vertices into buffers. If you write new code
  that feeds geometry into the scene, do the same swap.

## Why one service, not two

Running the UI and the MCP server against the same in-process state means:

- An agent can set up the print and a human can hit "Start" to watch it.
- E2E tests can upload via the HTTP API and then poke the scene directly
  (`window.__printerScene.stats()`), no cross-process coordination.
- There is exactly one source of truth; no sync logic between REST and MCP.

The trade-off is that the service is not multi-tenant — one running instance
is one virtual printer.
