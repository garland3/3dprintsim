"""MCP tools exposing the virtual printer to AI agents via fastmcp.

The tools intentionally cover the full pipeline — upload, arrange, slice,
simulate, introspect — so an agent can operate the printer end to end without
ever opening the web UI.
"""

from __future__ import annotations

import os
from urllib.parse import urljoin, urlparse

import httpx
from fastmcp import FastMCP

from .state import get_service


# Atlas file-upload handoff: when an Atlas host injects a file into an MCP call
# it rewrites the user-facing filename into a signed download URL. That URL may
# arrive either as an absolute URL or as a relative path the tool is expected
# to resolve against the backend's public base.
_BACKEND_PUBLIC_URL = os.getenv("BACKEND_PUBLIC_URL", "http://localhost:8000")
_ATLAS_FETCH_TIMEOUT = 30.0
# Cap the byte stream we're willing to ingest. Keeps a hostile or accidental
# multi-GB STL from pinning the event loop and the in-memory triangle list.
_ATLAS_MAX_BYTES = 200 * 1024 * 1024  # 200 MiB


def _normalize_atlas_url(filename: str) -> str:
    """Resolve an Atlas `filename` handoff to an absolute URL we can GET."""
    if filename.startswith("/"):
        return urljoin(_BACKEND_PUBLIC_URL, filename)
    return filename


def _atlas_display_name(url: str, fallback: str = "atlas.stl") -> str:
    """Best-effort filename for display — Atlas URLs expose the original name
    in the path segment (e.g. /mcp/files/download/<id>/my-model.stl) but many
    handoffs strip it, so default to something sensible rather than "".
    """
    try:
        path = urlparse(url).path
    except ValueError:
        return fallback
    tail = path.rsplit("/", 1)[-1]
    if tail and tail.lower().endswith(".stl"):
        return tail
    return fallback


def build_mcp() -> FastMCP:
    mcp = FastMCP(
        name="3dprintsim",
        instructions=(
            "Tools for driving a virtual FDM 3D printer. Typical flow: "
            "set_bed_size → upload_stl (one or more) → auto_arrange → "
            "slice_all → start_simulation → step_simulation until finished. "
            "Use get_printer_state at any time to inspect."
        ),
    )

    @mcp.tool
    def get_printer_state() -> dict:
        """Return bed size, loaded parts, slice summary, and simulation cursor."""
        return get_service().get_state()

    @mcp.tool
    def set_bed_size(x_mm: float, y_mm: float, z_mm: float) -> dict:
        """Resize the virtual print bed. Defaults to Prusa i3-style 250x210x210mm."""
        return get_service().set_bed_size(x_mm, y_mm, z_mm)

    @mcp.tool
    def atlas_upload(
        filename: str,
        name: str = "",
        scale: float = 1.0,
    ) -> dict:
        """Upload an STL that the Atlas host has already stashed in its file
        vault. `filename` is the secure download URL supplied by Atlas — it
        may be an absolute URL or a relative path (e.g.
        `/mcp/files/download/abc123?token=xyz`); either is resolved against
        the backend's public base URL before the GET.

        Use this when the user drops an STL into the chat rather than asking
        the model to base64-encode the file inline (which blows up both the
        tool-call size and the context window for real-world parts).

        `name` overrides the display name; otherwise it's derived from the
        URL path. `scale` works the same as upload_stl (25.4 for inches,
        0.001 for metres, etc).
        """
        if not filename:
            raise ValueError("filename (Atlas download URL) is required")

        url = _normalize_atlas_url(filename)

        try:
            with httpx.Client(timeout=_ATLAS_FETCH_TIMEOUT, follow_redirects=True) as client:
                with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in resp.iter_bytes():
                        total += len(chunk)
                        if total > _ATLAS_MAX_BYTES:
                            raise ValueError(
                                f"Atlas file exceeds {_ATLAS_MAX_BYTES // (1024 * 1024)} MiB cap"
                            )
                        chunks.append(chunk)
                    data = b"".join(chunks)
        except httpx.HTTPStatusError as exc:
            raise ValueError(
                f"Atlas download failed: HTTP {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            raise ValueError(f"Atlas download failed: {exc}") from exc

        display_name = name.strip() if name and name.strip() else _atlas_display_name(url)
        part = get_service().add_part_from_bytes(display_name, data, scale=scale)
        return part.to_public()

    @mcp.tool
    def upload_stl(name: str, stl_base64: str, scale: float = 1.0) -> dict:
        """Upload an STL file as base64 bytes. Returns the new part's metadata.

        `scale` is a linear multiplier applied to every vertex at import — pass
        25.4 for an STL authored in inches, 0.001 for metres, etc.
        """
        part = get_service().add_part_from_base64(name, stl_base64, scale=scale)
        return part.to_public()

    @mcp.tool
    def set_part_scale(part_id: str, scale: float) -> dict:
        """Resize a loaded part by a linear scale factor (e.g. 2.0 doubles every dimension)."""
        part = get_service().set_part_scale(part_id, scale)
        return part.to_public()

    @mcp.tool
    def list_parts() -> list[dict]:
        """List all loaded parts."""
        return [p.to_public() for p in get_service().parts.values()]

    @mcp.tool
    def remove_part(part_id: str) -> dict:
        """Remove a part by id."""
        get_service().remove_part(part_id)
        return {"ok": True, "removed": part_id}

    @mcp.tool
    def clear_parts() -> dict:
        """Remove all parts from the bed."""
        get_service().clear_parts()
        return {"ok": True}

    @mcp.tool
    def auto_arrange() -> list[dict]:
        """Pack all loaded parts onto the bed using a shelf packer. Returns placements."""
        placements = get_service().auto_arrange()
        return [
            {"part_id": p.part_id, "x": p.x, "y": p.y, "rotation_deg": p.rotation_deg}
            for p in placements
        ]

    @mcp.tool
    def slice_all(
        layer_height_mm: float = 0.4,
        perimeters: int = 1,
        infill_density: float = 0.2,
        top_layers: int = 3,
        bottom_layers: int = 3,
        nozzle_width_mm: float = 0.4,
        support_density: float = 0.25,
    ) -> dict:
        """Slice every loaded part and return a summary.

        infill_density is 0..1 (fraction of the layer filled with sparse infill).
        Top/bottom/overhang detection is raster-based: the first bottom_layers
        and last top_layers of each part get solid infill, and any intermediate
        layer with an overhang or hollow above/below also becomes solid.
        support_density (0..1) controls how dense the auto-generated support
        columns are — set to 0 to disable supports entirely.
        """
        result = get_service().slice_all(
            layer_height=layer_height_mm,
            perimeters=perimeters,
            infill_density=infill_density,
            top_layers=top_layers,
            bottom_layers=bottom_layers,
            nozzle_width=nozzle_width_mm,
            support_density=support_density,
        )
        return result.summary()

    @mcp.tool
    def get_gcode() -> str:
        """Return the latest slice's G-code as a string."""
        svc = get_service()
        if svc.slice_result is None:
            raise ValueError("slice first")
        return svc.slice_result.gcode

    @mcp.tool
    def start_simulation(speed: float = 1.0) -> dict:
        """Start/reset the simulation cursor at 0. Returns simulation state."""
        sim = get_service().start_simulation(speed=speed)
        return {
            "running": sim.running,
            "cursor": sim.cursor,
            "speed": sim.speed,
        }

    @mcp.tool
    def step_simulation(steps: int = 1) -> dict:
        """Advance the simulation cursor by N moves."""
        sim = get_service().step_simulation(steps=steps)
        svc = get_service()
        total = len(svc.slice_result.moves) if svc.slice_result else 0
        return {
            "running": sim.running,
            "cursor": sim.cursor,
            "total_moves": total,
        }

    @mcp.tool
    def set_simulation_cursor(cursor: int) -> dict:
        """Jump the simulation cursor to a specific move index."""
        sim = get_service().set_simulation_cursor(cursor)
        svc = get_service()
        total = len(svc.slice_result.moves) if svc.slice_result else 0
        return {
            "running": sim.running,
            "cursor": sim.cursor,
            "total_moves": total,
        }

    @mcp.tool
    def get_simulation_frame() -> dict:
        """Return the current head position and extruded moves-so-far."""
        return get_service().get_simulation_frame()

    @mcp.tool
    def focus_viewer() -> dict:
        """Ask the browser UI to reframe its camera so the loaded parts fill
        ~90% of the viewport. The browser polls for this request, so the
        effect is visible in a running UI session within a couple of seconds.
        """
        return {"focus_request": get_service().request_focus()}

    return mcp
