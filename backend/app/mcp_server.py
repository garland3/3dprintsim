"""MCP tools exposing the virtual printer to AI agents via fastmcp.

The tools intentionally cover the full pipeline — upload, arrange, slice,
simulate, introspect — so an agent can operate the printer end to end without
ever opening the web UI.
"""

from __future__ import annotations

from fastmcp import FastMCP

from .state import get_service


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
    ) -> dict:
        """Slice every loaded part and return a summary.

        infill_density is 0..1 (fraction of the layer filled with sparse infill).
        The first bottom_layers and last top_layers of each part are filled with
        solid (100%) infill to form proper top/bottom surfaces.
        """
        result = get_service().slice_all(
            layer_height=layer_height_mm,
            perimeters=perimeters,
            infill_density=infill_density,
            top_layers=top_layers,
            bottom_layers=bottom_layers,
            nozzle_width=nozzle_width_mm,
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
