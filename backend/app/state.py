"""Shared in-memory printer state plus a façade for operations.

Both the HTTP API and the MCP server talk to the same PrinterService singleton
so an AI agent and a human share one virtual printer.
"""

from __future__ import annotations

import base64
import threading
import uuid
from dataclasses import asdict, dataclass, field

from .arrange import ArrangeError, ArrangeInput, Placement, arrange
from .slicer import SliceResult, slice_meshes
from .stl_loader import Mesh, parse_stl, scale_mesh, translate


# Prusa i3 MK3S+ default build volume.
DEFAULT_BED = (250.0, 210.0, 210.0)


@dataclass
class Part:
    id: str
    name: str
    mesh: Mesh  # original, unplaced, unscaled mesh from the STL file
    scale: float = 1.0  # user-applied scale factor (e.g. 25.4 for inch → mm)
    placement: Placement | None = None

    def scaled_bounds(self) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
        """AABB of the mesh after `scale` is applied, without touching triangles.

        This is the read-heavy path (state fetches, arrange, bed-fit checks),
        so we skip the full triangle rescale whenever only the bounds matter.
        """
        s = self.scale
        mn = self.mesh.min_xyz
        mx = self.mesh.max_xyz
        if s == 1.0:
            return mn, mx
        return (mn[0] * s, mn[1] * s, mn[2] * s), (mx[0] * s, mx[1] * s, mx[2] * s)

    def scaled_size(self) -> tuple[float, float, float]:
        mn, mx = self.scaled_bounds()
        return (mx[0] - mn[0], mx[1] - mn[1], mx[2] - mn[2])

    def scaled_mesh(self) -> Mesh:
        """Return the mesh with the user's scale factor applied.

        Rescales every triangle — call only when the full geometry is needed
        (slicing, client-side rendering). For bounds/size prefer `scaled_bounds()`.
        """
        if self.scale == 1.0:
            return self.mesh
        return scale_mesh(self.mesh, self.scale)

    def placed_mesh(self) -> Mesh:
        """Return mesh scaled, then translated so its min corner sits at placement x,y and Z=0."""
        base = self.scaled_mesh()
        mn = base.min_xyz
        dx = -mn[0]
        dy = -mn[1]
        dz = -mn[2]  # drop to bed
        if self.placement is not None:
            dx += self.placement.x
            dy += self.placement.y
        return translate(base, dx, dy, dz)

    def to_public(self) -> dict:
        size = self.scaled_size()
        return {
            "id": self.id,
            "name": self.name,
            "size": list(size),
            "scale": self.scale,
            "triangle_count": len(self.mesh.triangles),
            "placement": (
                {"x": self.placement.x, "y": self.placement.y, "rotation_deg": self.placement.rotation_deg}
                if self.placement
                else None
            ),
        }


@dataclass
class Simulation:
    running: bool = False
    cursor: int = 0  # index into moves list
    speed: float = 1.0  # moves advanced per step

    def to_public(self, total: int) -> dict:
        return {
            "running": self.running,
            "cursor": self.cursor,
            "total_moves": total,
            "speed": self.speed,
        }


class PrinterService:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.bed_size: tuple[float, float, float] = DEFAULT_BED
        self.parts: dict[str, Part] = {}
        self.slice_result: SliceResult | None = None
        self.simulation = Simulation()
        # Monotonic counter the browser polls to trigger a one-shot viewer
        # action (currently: camera focus). The counter is preserved across
        # slice invalidation on purpose — it's a UI signal, not print state.
        self.focus_request: int = 0

    # --- printer config ---

    def set_bed_size(self, x: float, y: float, z: float) -> dict:
        if x <= 0 or y <= 0 or z <= 0:
            raise ValueError("bed dimensions must be positive")
        with self._lock:
            self.bed_size = (float(x), float(y), float(z))
            self._invalidate_slice()
            return self.get_state()

    # --- parts ---

    def add_part_from_bytes(self, name: str, data: bytes, scale: float = 1.0) -> Part:
        if scale <= 0:
            raise ValueError(f"scale must be positive, got {scale}")
        mesh = parse_stl(data)
        part_id = uuid.uuid4().hex[:8]
        part = Part(id=part_id, name=name, mesh=mesh, scale=float(scale))
        with self._lock:
            self.parts[part_id] = part
            # Place the new part on the bed immediately so it's visible without a
            # second click. Single-part case → centered; multi-part → re-pack.
            # If the bed can't hold the new set, leave the new part unplaced so
            # the next slice/arrange call fails loudly rather than silently
            # emitting an overlapping or out-of-bounds toolpath.
            if len(self.parts) == 1:
                try:
                    part.placement = self._center_placement(part)
                except ArrangeError:
                    part.placement = None
            else:
                try:
                    self._auto_arrange_locked()
                except ArrangeError:
                    part.placement = None
            self._invalidate_slice()
        return part

    def _center_placement(self, part: Part) -> Placement:
        """Return a centered Placement if the part fits, else raise ArrangeError.

        Uses the same margin rule `arrange()` uses so single-part and multi-part
        uploads agree on what "fits on this bed" means.
        """
        from .arrange import DEFAULT_MARGIN

        w, d, _ = part.scaled_size()
        bx, by, _ = self.bed_size
        if w + 2 * DEFAULT_MARGIN > bx or d + 2 * DEFAULT_MARGIN > by:
            raise ArrangeError(
                f"part {part.id} ({w:.1f}x{d:.1f}) does not fit on "
                f"{bx:.0f}x{by:.0f} bed"
            )
        x = (bx - w) / 2
        y = (by - d) / 2
        return Placement(part_id=part.id, x=x, y=y, rotation_deg=0.0)

    def add_part_from_base64(self, name: str, b64: str, scale: float = 1.0) -> Part:
        try:
            data = base64.b64decode(b64, validate=False)
        except Exception as exc:
            raise ValueError(f"invalid base64 STL: {exc}") from exc
        return self.add_part_from_bytes(name, data, scale=scale)

    def set_part_scale(self, part_id: str, scale: float) -> Part:
        """Apply a new scale factor to a part and re-place it on the bed."""
        if scale <= 0:
            raise ValueError(f"scale must be positive, got {scale}")
        with self._lock:
            if part_id not in self.parts:
                raise KeyError(f"unknown part {part_id}")
            part = self.parts[part_id]
            part.scale = float(scale)
            # Re-place: the footprint just changed, so the previous placement
            # may no longer fit or be centered.
            if len(self.parts) == 1:
                try:
                    part.placement = self._center_placement(part)
                except ArrangeError:
                    part.placement = None
            else:
                try:
                    self._auto_arrange_locked()
                except ArrangeError:
                    part.placement = None
            self._invalidate_slice()
            return part

    def remove_part(self, part_id: str) -> None:
        with self._lock:
            if part_id not in self.parts:
                raise KeyError(f"unknown part {part_id}")
            del self.parts[part_id]
            self._invalidate_slice()

    def clear_parts(self) -> None:
        with self._lock:
            self.parts.clear()
            self._invalidate_slice()

    # --- arrange ---

    def auto_arrange(self) -> list[Placement]:
        with self._lock:
            placements = self._auto_arrange_locked()
            self._invalidate_slice()
            return placements

    def _auto_arrange_locked(self) -> list[Placement]:
        inputs = []
        for part in self.parts.values():
            w, d, _ = part.scaled_size()
            inputs.append(ArrangeInput(part_id=part.id, width=w, depth=d))
        bx, by, _ = self.bed_size
        placements = arrange(inputs, bx, by)
        for p in placements:
            self.parts[p.part_id].placement = p
        return placements

    # --- slicing ---

    def slice_all(
        self,
        layer_height: float = 0.4,
        perimeters: int = 1,
        infill_density: float = 0.2,
        top_layers: int = 3,
        bottom_layers: int = 3,
        nozzle_width: float = 0.4,
        support_density: float = 0.25,
    ) -> SliceResult:
        with self._lock:
            if not self.parts:
                raise ValueError("no parts loaded")
            unplaced = [p.id for p in self.parts.values() if p.placement is None]
            if unplaced:
                # Auto-arrange first so slicing always works against real bed positions.
                self.auto_arrange()
            meshes = [p.placed_mesh() for p in self.parts.values()]
            result = slice_meshes(
                meshes,
                layer_height=layer_height,
                perimeters=perimeters,
                infill_density=infill_density,
                top_layers=top_layers,
                bottom_layers=bottom_layers,
                nozzle_width=nozzle_width,
                support_density=support_density,
            )
            self.slice_result = result
            self.simulation = Simulation(running=False, cursor=0, speed=self.simulation.speed)
            return result

    # --- simulation ---

    def start_simulation(self, speed: float | None = None) -> Simulation:
        with self._lock:
            if self.slice_result is None:
                raise ValueError("must slice before simulating")
            self.simulation = Simulation(
                running=True,
                cursor=0,
                speed=float(speed) if speed is not None else self.simulation.speed,
            )
            return self.simulation

    def step_simulation(self, steps: int = 1) -> Simulation:
        with self._lock:
            if self.slice_result is None:
                raise ValueError("must slice before simulating")
            total = len(self.slice_result.moves)
            self.simulation.cursor = min(total, self.simulation.cursor + max(1, int(steps)))
            if self.simulation.cursor >= total:
                self.simulation.running = False
            return self.simulation

    def set_simulation_cursor(self, cursor: int) -> Simulation:
        with self._lock:
            if self.slice_result is None:
                raise ValueError("must slice before simulating")
            total = len(self.slice_result.moves)
            self.simulation.cursor = max(0, min(total, int(cursor)))
            self.simulation.running = self.simulation.cursor < total
            return self.simulation

    def get_simulation_frame(self) -> dict:
        with self._lock:
            if self.slice_result is None:
                return {"ready": False}
            total = len(self.slice_result.moves)
            cursor = self.simulation.cursor
            head = self.slice_result.moves[min(cursor, total - 1)] if total else None
            extruded = [asdict(m) for m in self.slice_result.moves[:cursor] if m.kind == "extrude"]
            return {
                "ready": True,
                "cursor": cursor,
                "total_moves": total,
                "running": self.simulation.running,
                "speed": self.simulation.speed,
                "head": asdict(head) if head is not None else None,
                "extruded_moves": extruded,
            }

    # --- introspection ---

    def get_state(self) -> dict:
        with self._lock:
            return {
                "bed_size": list(self.bed_size),
                "parts": [p.to_public() for p in self.parts.values()],
                "slice": self.slice_result.summary() if self.slice_result else None,
                "simulation": self.simulation.to_public(
                    len(self.slice_result.moves) if self.slice_result else 0
                ),
            }

    def get_slice_payload(self) -> dict:
        with self._lock:
            if self.slice_result is None:
                return {"ready": False}
            return {
                "ready": True,
                "summary": self.slice_result.summary(),
                "layers": [
                    {"z": layer.z, "contours": [[list(pt) for pt in c] for c in layer.contours]}
                    for layer in self.slice_result.layers
                ],
                "moves": [asdict(m) for m in self.slice_result.moves],
            }

    def request_focus(self) -> int:
        with self._lock:
            self.focus_request += 1
            return self.focus_request

    def get_viewer_requests(self) -> dict:
        with self._lock:
            return {"focus_request": self.focus_request}

    def get_part_geometry(self, part_id: str) -> dict:
        with self._lock:
            if part_id not in self.parts:
                raise KeyError(f"unknown part {part_id}")
            placed = self.parts[part_id].placed_mesh()
            return {
                "id": part_id,
                "triangles": [t.as_list() for t in placed.triangles],
                "min": list(placed.min_xyz),
                "max": list(placed.max_xyz),
            }

    # --- internal ---

    def _invalidate_slice(self) -> None:
        self.slice_result = None
        self.simulation = Simulation(running=False, cursor=0, speed=self.simulation.speed)


_service: PrinterService | None = None


def get_service() -> PrinterService:
    global _service
    if _service is None:
        _service = PrinterService()
    return _service


def reset_service() -> None:
    """Test hook."""
    global _service
    _service = PrinterService()


__all__ = [
    "ArrangeError",
    "PrinterService",
    "Part",
    "get_service",
    "reset_service",
]
