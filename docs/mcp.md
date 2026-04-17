# MCP server

Served at `http://127.0.0.1:8000/mcp/` (streamable-HTTP). Built with
[`fastmcp`](https://gofastmcp.com/). Definitions live in
[`backend/app/mcp_server.py`](../backend/app/mcp_server.py).

The server exposes the same `PrinterService` the HTTP API uses, so an agent
and a human share one virtual printer.

## Tools

| Tool | Args | Returns |
|---|---|---|
| `get_printer_state` | — | Full printer snapshot (same as `GET /api/state`). |
| `set_bed_size` | `x_mm`, `y_mm`, `z_mm` | Printer state after resize. |
| `upload_stl` | `name`, `stl_base64` | New part metadata (base64-in-tool-call upload). |
| `atlas_upload` | `filename`, `name?`, `scale=1.0` | New part metadata (downloads an Atlas-hosted STL by URL — preferred for real-world parts). |
| `list_parts` | — | All parts. |
| `remove_part` | `part_id` | `{ok: true, removed}` |
| `clear_parts` | — | `{ok: true}` |
| `auto_arrange` | — | List of placements. |
| `slice_all` | `layer_height_mm=0.4`, `perimeters=1`, `infill_density=0.2`, `top_layers=3`, `bottom_layers=3`, `support_density=0.25` | Slice summary (includes `support_cell_count`). |
| `get_gcode` | — | Latest G-code as a string. |
| `start_simulation` | `speed=1.0` | `{running, cursor, speed}` |
| `step_simulation` | `steps=1` | `{running, cursor, total_moves}` |
| `set_simulation_cursor` | `cursor` | `{running, cursor, total_moves}` |
| `get_simulation_frame` | — | `{cursor, total_moves, running, head, extruded_moves}` |

Server instructions (returned by `list_tools` metadata):

> Typical flow: `set_bed_size` → `upload_stl` (one or more) → `auto_arrange`
> → `slice_all` → `start_simulation` → `step_simulation` until finished.
> Use `get_printer_state` at any time to inspect.

## Example client

```python
import asyncio, base64, json
from pathlib import Path
from fastmcp import Client

async def main():
    stl_b64 = base64.b64encode(Path("cube20.stl").read_bytes()).decode()

    async with Client("http://127.0.0.1:8000/mcp/") as c:
        await c.call_tool("set_bed_size", {"x_mm": 220, "y_mm": 220, "z_mm": 210})
        await c.call_tool("upload_stl", {"name": "cube.stl", "stl_base64": stl_b64})
        await c.call_tool("auto_arrange", {})

        res = await c.call_tool("slice_all", {"layer_height_mm": 0.4, "perimeters": 1})
        print("slice:", res.structured_content)

        await c.call_tool("start_simulation", {"speed": 1.0})
        for _ in range(20):
            await c.call_tool("step_simulation", {"steps": 10})

        frame = await c.call_tool("get_simulation_frame", {})
        f = frame.structured_content
        print(f"head at move {f['cursor']}/{f['total_moves']}: {f['head']}")

asyncio.run(main())
```

## Sharing state with the UI

If you keep the web UI open in a browser while running the client above, you
will see every action reflected live:

- `set_bed_size` resizes the wireframe build volume.
- `upload_stl` → `auto_arrange` drops a new blue mesh onto the bed.
- `slice_all` lights up the dim blue toolpath ghost.
- `step_simulation` advances the orange print head along the toolpath.

See the E2E test at
[`tests/e2e/mcp.spec.js`](../tests/e2e/mcp.spec.js) for a passing example
that asserts this mirrored-state behaviour.

## Atlas file uploads

Real-world STLs run 1–50 MB; base64-encoding one into a tool call blows up
the prompt and the model's context window. Atlas-compatible hosts solve
this by stashing uploaded files in a signed vault and handing a download
URL to the tool as the `filename` argument.

The `atlas_upload` tool fetches that URL, feeds the bytes into the same
`add_part_from_bytes()` path `upload_stl` uses, and returns the new part's
metadata. The URL may be absolute (`https://atlas.example.com/mcp/files/...`)
or a relative path (`/mcp/files/download/abc123?token=xyz`); relative paths
are resolved against the `BACKEND_PUBLIC_URL` env var (defaults to
`http://localhost:8000`).

```python
await client.call_tool("atlas_upload", {
    "filename": "/mcp/files/download/abc123?token=xyz",
    "name": "bracket.stl",   # optional override; otherwise derived from URL
    "scale": 1.0,            # 25.4 for inches, 0.001 for metres
})
```

Failures are surfaced as clean `ValueError`s — a 404 from Atlas, a
truncated stream, or a file over the 200 MiB cap all raise without leaving
a half-loaded part in the state. Coverage lives in
[`backend/tests/test_atlas_upload.py`](../backend/tests/test_atlas_upload.py).
