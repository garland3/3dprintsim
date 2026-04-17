# HTTP API

Base URL: `http://127.0.0.1:8000` (set in
[`frontend/src/api.js`](../frontend/src/api.js) and the Playwright config).

All routes live in [`backend/app/main.py`](../backend/app/main.py). Every
mutating call goes through the same `PrinterService` singleton used by the
MCP server, so an API client and an MCP agent share state.

## Printer state

### `GET /api/health`

Liveness check.

```bash
curl http://127.0.0.1:8000/api/health
# {"ok": true, "service": "3dprintsim"}
```

### `GET /api/state`

Full snapshot: bed size, parts, slice summary, simulation cursor.

```json
{
  "bed_size": [250.0, 210.0, 210.0],
  "parts": [
    {
      "id": "a1b2c3d4",
      "name": "cube20.stl",
      "size": [20.0, 20.0, 20.0],
      "triangle_count": 12,
      "placement": {"x": 5.0, "y": 5.0, "rotation_deg": 0.0}
    }
  ],
  "slice": {
    "layer_count": 20,
    "move_count": 181,
    "layer_height": 1.0,
    "perimeters": 1,
    "total_extrusion": 64.0
  },
  "simulation": {"running": false, "cursor": 0, "total_moves": 181, "speed": 1.0}
}
```

### `POST /api/reset`

Wipe everything — bed size is reset to default, all parts and slice results
are cleared. Used by Playwright tests and the screenshot pipeline.

## Bed

### `POST /api/bed`

```bash
curl -X POST http://127.0.0.1:8000/api/bed \
  -H 'content-type: application/json' \
  -d '{"x":180,"y":180,"z":180}'
```

Returns the same body as `/api/state`. Resizing the bed invalidates the
slice.

## Parts

### `POST /api/parts/upload` (multipart)

```bash
curl -X POST http://127.0.0.1:8000/api/parts/upload \
  -F "file=@tests/fixtures/cube20.stl"
```

Returns the new part's metadata (same shape as entries in
`/api/state.parts`). Invalid STLs produce a `400`.

### `GET /api/parts`

List all parts with their placements.

### `GET /api/parts/{id}/geometry`

Returns the *placed* mesh (i.e. translated into bed coordinates) as a list of
triangles plus its AABB. The UI calls this to draw each part.

```json
{
  "id": "a1b2c3d4",
  "triangles": [ [[x,y,z],[x,y,z],[x,y,z]], ... ],
  "min": [5.0, 5.0, 0.0],
  "max": [25.0, 25.0, 20.0]
}
```

### `DELETE /api/parts/{id}` · `POST /api/parts/clear`

Remove one part or all parts. Both invalidate the slice.

## Pipeline

### `POST /api/arrange`

Auto-arrange all loaded parts using the shelf packer. Returns the list of
new placements. Fails with `409` if a part doesn't fit.

### `POST /api/slice`

```bash
curl -X POST http://127.0.0.1:8000/api/slice \
  -H 'content-type: application/json' \
  -d '{"layer_height":0.4,"perimeters":1}'
```

Returns a slice summary:

```json
{"layer_count": 50, "move_count": 402, "layer_height": 0.4,
 "perimeters": 1, "total_extrusion": 160.0}
```

### `GET /api/slice`

Returns the full slice payload: summary, per-layer polygon contours, and the
flat list of `Move` objects used by the simulator.

### `GET /api/gcode`

Plain-text G-code for the latest slice. 400 if you haven't sliced.

```bash
curl http://127.0.0.1:8000/api/gcode | head -20
```

## Simulation

### `POST /api/simulation/start`

```bash
curl -X POST http://127.0.0.1:8000/api/simulation/start \
  -H 'content-type: application/json' -d '{"speed":1.0}'
```

Resets the cursor to 0 and sets `running=true`. 400 if you haven't sliced.

### `POST /api/simulation/step`

Advance the cursor by `steps` moves. Returns `{running, cursor, speed}`.
When the cursor reaches `total_moves`, `running` flips to `false`.

### `POST /api/simulation/cursor`

Jump the cursor to an explicit index.

### `GET /api/simulation/frame`

Returns the current head position and the slice of moves already extruded —
this is the one the frontend polls during playback.

```json
{
  "ready": true,
  "cursor": 107,
  "total_moves": 181,
  "running": true,
  "speed": 1.0,
  "head": {"kind": "extrude", "x": 25.0, "y": 5.0, "z": 10.5, "e": 38.2},
  "extruded_moves": [ {"kind": "extrude", "x": ..., ...}, ... ]
}
```

## Errors

| Status | When |
|---|---|
| 400 | Bad input (empty STL, negative layer height, slicing without parts, G-code before slice) |
| 404 | Unknown part id |
| 409 | `auto_arrange` cannot fit a part onto the bed |

Error bodies are FastAPI's default `{"detail": "..."}`.
