"""In-memory printer state plus a façade for operations.

Each MCP session (and each browser tab) gets its own PrinterService via the
SessionRegistry, so an AI agent and the user's UI in the SAME session share a
single virtual printer, while different users never see each other's parts.

For tests and dev calls without a session id, a sentinel "default" session is
used — that keeps the pre-multiuser code paths working.
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

from .arrange import ArrangeError, ArrangeInput, Placement, arrange
from .mesh_loader import parse_mesh
from .slicer import SliceResult, slice_meshes
from .stl_loader import (
    IDENTITY_ROTATION,
    Matrix3,
    Mesh,
    axis_rotation_matrix,
    multiply_matrix,
    parse_stl,
    rotate_mesh,
    scale_mesh,
    translate,
    validate_mesh,
)


RotationMatrix = tuple[tuple[float, float, float], ...]


def _identity() -> RotationMatrix:
    return IDENTITY_ROTATION


def _is_identity(m: Matrix3) -> bool:
    # Tight tolerance — this is exact unless the user composes many non-90°
    # rotations. Float drift at that point doesn't materially change what the
    # renderer sees, so the short-circuit is still safe.
    for i in range(3):
        for j in range(3):
            target = 1.0 if i == j else 0.0
            if abs(m[i][j] - target) > 1e-9:
                return False
    return True


# Prusa i3 MK3S+ default build volume.
DEFAULT_BED = (250.0, 210.0, 210.0)


# Printer technology being simulated. `FDM` (default) shows a hot-end depositing
# filament from the bed up; `LPBF` shows a laser fusing powder while the build
# plate descends. Slicing is shared — only the visualization differs today.
VALID_PRINTER_TYPES = ("FDM", "LPBF")


def _resolve_printer_type() -> str:
    raw = os.getenv("PRINTER_TYPE", "FDM")
    normalized = (raw or "").strip().upper()
    if normalized in VALID_PRINTER_TYPES:
        return normalized
    # Anything unrecognized (typo, blank) falls back to FDM rather than
    # surfacing a hard error — operators should be able to typo their .env
    # without bricking the server.
    return "FDM"


@dataclass
class Part:
    id: str
    name: str
    mesh: Mesh  # original, unplaced, unscaled mesh from the STL file
    scale: float = 1.0  # user-applied scale factor (e.g. 25.4 for inch → mm)
    # World-axis rotation applied after scale, before translation to the bed.
    # Stored as a 3x3 matrix so composing successive 90° clicks doesn't drift
    # into gimbal-lock weirdness the way accumulated Euler angles would.
    rotation: RotationMatrix = field(default_factory=_identity)
    placement: Placement | None = None
    # Advisory warnings from the STL validator (degenerate triangles,
    # non-manifold edges). The slicer still runs on parts with warnings,
    # but the UI can surface them so a user knows why their output looks wrong.
    warnings: list[str] = field(default_factory=list)

    def has_rotation(self) -> bool:
        return not _is_identity(self.rotation)

    def scaled_bounds(self) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
        """AABB after scale + rotation have been applied.

        Used by arrange/bed-fit; once the part is rotated its bed footprint
        is the post-rotation AABB, not the raw scaled AABB. Slow path only
        when a rotation is set — the pure-scale case stays bounds-only.
        """
        if self.has_rotation():
            transformed = self.transformed_mesh()
            return transformed.min_xyz, transformed.max_xyz
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
        """Return the mesh with the user's scale factor applied (no rotation).

        Rescales every triangle — call only when the full geometry is needed.
        For bounds/size prefer `scaled_bounds()`.
        """
        if self.scale == 1.0:
            return self.mesh
        return scale_mesh(self.mesh, self.scale)

    def transformed_mesh(self) -> Mesh:
        """Scale + rotation applied, still in object frame (not translated)."""
        mesh = self.scaled_mesh()
        if not self.has_rotation():
            return mesh
        return rotate_mesh(mesh, self.rotation)

    def placed_mesh(self) -> Mesh:
        """Mesh scaled, rotated, then translated so min-corner is at (placement.x, placement.y, 0)."""
        base = self.transformed_mesh()
        mn = base.min_xyz
        dx = -mn[0]
        dy = -mn[1]
        dz = -mn[2]  # drop to bed
        if self.placement is not None:
            dx += self.placement.x
            dy += self.placement.y
        return translate(base, dx, dy, dz)

    def shape_fingerprint(self) -> str:
        """Stable string identifying the part's geometry sans placement.

        The frontend uses this to decide whether to refetch a part's vertex
        buffer. Anything that changes the actual rotated/scaled geometry —
        scale, rotation matrix, the underlying triangle data — flips the
        fingerprint. Pure XY placement does not, so dragging a part around
        the bed never triggers a full geometry round-trip.
        """
        rot_key = ",".join(f"{v:.10g}" for row in self.rotation for v in row)
        return (
            f"{self.id}|tris={self.mesh.triangle_count()}|"
            f"scale={self.scale:.10g}|rot={rot_key}"
        )

    def to_public(self) -> dict:
        # Coerce every numeric to a builtin float so the dict is guaranteed
        # JSON-safe. Without this, an upstream Pydantic model (e.g. a stray
        # mcp.types.Root from a client `roots/list_changed` round-trip) or a
        # numpy scalar from a future trimesh-backed mesh path can leak through
        # and trip stdlib json.dumps with "Object of type X is not JSON
        # serializable" inside FastMCP's serialization layer.
        size = self.scaled_size()
        return {
            "id": self.id,
            "name": self.name,
            "size": [float(v) for v in size],
            "scale": float(self.scale),
            "rotation": [[float(v) for v in row] for row in self.rotation],
            "triangle_count": self.mesh.triangle_count(),
            "warnings": list(self.warnings),
            "shape_fingerprint": self.shape_fingerprint(),
            "placement": (
                {
                    "x": float(self.placement.x),
                    "y": float(self.placement.y),
                    "rotation_deg": float(self.placement.rotation_deg),
                }
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
    def __init__(self, session_id: str | None = None) -> None:
        self._lock = threading.RLock()
        self.session_id = session_id or DEFAULT_SESSION_ID
        self.bed_size: tuple[float, float, float] = DEFAULT_BED
        # Printer technology — read at service construction so an env override
        # applied between sessions takes effect on the next session, but the
        # answer stays stable for the lifetime of any one PrinterService.
        self.printer_type: str = _resolve_printer_type()
        self.parts: dict[str, Part] = {}
        self.slice_result: SliceResult | None = None
        self.simulation = Simulation()
        # Monotonic counter the browser polls (or subscribes to via SSE) to
        # trigger a one-shot viewer action (currently: camera focus). The
        # counter is preserved across slice invalidation on purpose — it's
        # a UI signal, not print state.
        self.focus_request: int = 0
        # Monotonic counter bumped on every mutation. The frontend subscribes
        # to /api/events (SSE); a `state` event is emitted on every increment,
        # so MCP-driven changes (LLM uploads a part, slices, steps the
        # simulation) appear in the UI in real time without polling.
        # /api/viewer/requests remains as a polling fallback for clients that
        # can't keep an SSE connection open.
        self.state_revision: int = 0

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
        # Multi-format dispatch — STL, 3MF, and (optionally) STEP all land
        # here; the loader sniffs by magic bytes first, falls back to the
        # filename extension, and raises ValueError for unsupported formats.
        mesh = parse_mesh(data, filename=name)
        warnings = validate_mesh(mesh)
        part_id = uuid.uuid4().hex[:8]
        part = Part(
            id=part_id, name=name, mesh=mesh, scale=float(scale), warnings=warnings
        )
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

    def rotate_part(
        self, part_id: str, axis: str, degrees: float, *, reset: bool = False
    ) -> Part:
        """Apply a world-axis rotation to a part.

        Passing `reset=True` clears the rotation back to identity and ignores
        `axis`/`degrees`. Otherwise the named world axis is rotated by
        `degrees` and pre-multiplied onto the existing orientation so
        successive clicks accumulate in the bed frame (not the part frame —
        the user wants "press +Z again to keep spinning around bed-up").
        """
        with self._lock:
            if part_id not in self.parts:
                raise KeyError(f"unknown part {part_id}")
            part = self.parts[part_id]
            if reset:
                part.rotation = IDENTITY_ROTATION
            else:
                rot = axis_rotation_matrix(axis, float(degrees))
                part.rotation = multiply_matrix(rot, part.rotation)
            # Footprint just changed — re-pack the bed (or re-center if this
            # is the only part). Fall back to "unplaced" if the rotation
            # makes the part no longer fit.
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

    def set_part_position(self, part_id: str, x: float, y: float) -> Part:
        """Manually place `part_id` so its min-corner sits at bed (x, y).

        Clamped to `[0, bed - size]` so a user can't drag a part off the
        plate. Margin is intentionally NOT enforced here — we prefer to let
        the user get close to the edge on purpose and surface any slice-time
        failure through the existing 409 path.
        """
        with self._lock:
            if part_id not in self.parts:
                raise KeyError(f"unknown part {part_id}")
            part = self.parts[part_id]
            w, d, _ = part.scaled_size()
            bx, by, _ = self.bed_size
            if w > bx or d > by:
                raise ValueError(
                    f"part {part_id} ({w:.1f}x{d:.1f}) larger than bed "
                    f"{bx:.0f}x{by:.0f}"
                )
            clamped_x = max(0.0, min(bx - w, float(x)))
            clamped_y = max(0.0, min(by - d, float(y)))
            prev_rotation = (
                part.placement.rotation_deg if part.placement is not None else 0.0
            )
            part.placement = Placement(
                part_id=part_id,
                x=clamped_x,
                y=clamped_y,
                rotation_deg=prev_rotation,
            )
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
        *,
        # Optional quality/print params (all default to the slicer's own
        # defaults so existing callers don't need to change).
        retract_mm: float | None = None,
        retract_speed: float | None = None,
        hotend_temp: float | None = None,
        bed_temp: float | None = None,
        fan_speed: int | None = None,
        first_layer_fan: int | None = None,
        bridge_fan: int | None = None,
        bridge_speed_factor: float | None = None,
        seam_position: str | None = None,
        first_layer_height: float | None = None,
        first_layer_speed: float | None = None,
        brim_loops: int | None = None,
        adaptive_layers: bool | None = None,
        layer_height_min: float | None = None,
        layer_height_max: float | None = None,
    ) -> SliceResult:
        with self._lock:
            if not self.parts:
                raise ValueError("no parts loaded")
            unplaced = [p.id for p in self.parts.values() if p.placement is None]
            if unplaced:
                # Auto-arrange first so slicing always works against real bed positions.
                self.auto_arrange()
            meshes = [p.placed_mesh() for p in self.parts.values()]
            kwargs: dict = {
                "layer_height": layer_height,
                "perimeters": perimeters,
                "infill_density": infill_density,
                "top_layers": top_layers,
                "bottom_layers": bottom_layers,
                "nozzle_width": nozzle_width,
                "support_density": support_density,
            }
            # Only forward non-None optionals so the slicer's defaults apply
            # for the common case and unit tests don't need a giant kwargs dict.
            opt_map: dict[str, object] = {
                "retract_mm": retract_mm,
                "retract_speed": retract_speed,
                "hotend_temp": hotend_temp,
                "bed_temp": bed_temp,
                "fan_speed": fan_speed,
                "first_layer_fan": first_layer_fan,
                "bridge_fan": bridge_fan,
                "bridge_speed_factor": bridge_speed_factor,
                "seam_position": seam_position,
                "first_layer_height": first_layer_height,
                "first_layer_speed": first_layer_speed,
                "brim_loops": brim_loops,
                "adaptive_layers": adaptive_layers,
                "layer_height_min": layer_height_min,
                "layer_height_max": layer_height_max,
            }
            for k, v in opt_map.items():
                if v is not None:
                    kwargs[k] = v
            result = slice_meshes(meshes, **kwargs)
            self.slice_result = result
            # Park the cursor at the end of the toolpath so the viewer shows
            # the finished print by default after slicing — matches the local
            # UI's post-slice behavior (handleSlice manually setCursor(total))
            # and gives MCP-driven slices a sensible "what will it look like"
            # preview without the caller needing a follow-up step_simulation.
            self.simulation = Simulation(
                running=False,
                cursor=len(result.moves),
                speed=self.simulation.speed,
            )
            self._bump_revision()
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
            self._bump_revision()
            return self.simulation

    def step_simulation(self, steps: int = 1) -> Simulation:
        with self._lock:
            if self.slice_result is None:
                raise ValueError("must slice before simulating")
            total = len(self.slice_result.moves)
            self.simulation.cursor = min(total, self.simulation.cursor + max(1, int(steps)))
            if self.simulation.cursor >= total:
                self.simulation.running = False
            self._bump_revision()
            return self.simulation

    def set_simulation_cursor(self, cursor: int) -> Simulation:
        with self._lock:
            if self.slice_result is None:
                raise ValueError("must slice before simulating")
            total = len(self.slice_result.moves)
            self.simulation.cursor = max(0, min(total, int(cursor)))
            self.simulation.running = self.simulation.cursor < total
            self._bump_revision()
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
                "printer_type": self.printer_type,
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
            counter = self.focus_request
        # Publish outside the lock: the broker's delivery is best-effort and
        # we don't want a slow subscriber to hold the printer's mutation lock.
        _broker().publish(
            self.session_id,
            {"type": "focus", "focus_request": counter},
        )
        return counter

    def get_viewer_requests(self) -> dict:
        with self._lock:
            return {
                "focus_request": self.focus_request,
                "state_revision": self.state_revision,
            }

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

    def get_part_geometry_buffer(self, part_id: str) -> tuple[bytes, dict]:
        """Return the placed mesh as a packed float32 byte buffer.

        Vertices are emitted in **object space** with the min corner pinned
        at (0, 0, 0) and in Three.js Y-up order. Placement (the part's bed
        XY translation) is shipped separately in the metadata so the
        frontend can apply it via ``mesh.position`` and translate the part
        cheaply on the GPU when the user drags it. That means a 1M-triangle
        mesh only has to cross the wire once per shape change — repositioning
        becomes a single Vector3 assignment.

        The legacy JSON endpoint still ships world-space (placed) vertices,
        so callers that haven't migrated keep working.
        """
        with self._lock:
            if part_id not in self.parts:
                raise KeyError(f"unknown part {part_id}")
            part = self.parts[part_id]
            # Object-space mesh: scale + rotation applied, dropped to bed
            # so min Z = 0, BUT without the placement.x/y translation. We
            # inline that math here so we don't churn an extra Mesh copy.
            base = part.transformed_mesh()
            verts = base.vertices  # (N, 3, 3) float64, object frame
            n_tris = int(verts.shape[0])
            if n_tris == 0:
                buffer = b""
                obj_min = (0.0, 0.0, 0.0)
                obj_max = (0.0, 0.0, 0.0)
            else:
                mn = base.min_xyz
                # Translate so min corner is at (0, 0, 0); equivalent to
                # placed_mesh() with placement.x = placement.y = 0.
                shifted = verts - np.array([mn[0], mn[1], mn[2]], dtype=np.float64)
                flat = shifted.reshape(-1, 3)
                # Bed (x, y, z) -> Three.js (x, z, y)
                yup = np.empty_like(flat, dtype=np.float32)
                yup[:, 0] = flat[:, 0]
                yup[:, 1] = flat[:, 2]
                yup[:, 2] = flat[:, 1]
                buffer = np.ascontiguousarray(yup).tobytes()
                obj_min = (0.0, 0.0, 0.0)
                obj_max = (
                    base.max_xyz[0] - mn[0],
                    base.max_xyz[1] - mn[1],
                    base.max_xyz[2] - mn[2],
                )
            placement = part.placement
            meta = {
                "id": part_id,
                "triangle_count": n_tris,
                "min": list(obj_min),
                "max": list(obj_max),
                "placement_x": float(placement.x) if placement is not None else 0.0,
                "placement_y": float(placement.y) if placement is not None else 0.0,
                "has_placement": placement is not None,
                "shape_fingerprint": part.shape_fingerprint(),
            }
            return buffer, meta

    # --- internal ---

    def _invalidate_slice(self) -> None:
        self.slice_result = None
        self.simulation = Simulation(running=False, cursor=0, speed=self.simulation.speed)
        self._bump_revision()

    def _bump_revision(self) -> None:
        # Caller must already hold self._lock — every mutation goes through
        # a `with self._lock:` block, so bumping inside that critical section
        # keeps the counter and the state it advertises in lockstep.
        self.state_revision += 1
        # Fan out to any SSE subscribers for this session. `publish` is
        # non-blocking (drops on a full queue) and thread-safe — mutations
        # routinely happen from FastAPI's threadpool worker, while
        # subscribers live on the main asyncio loop.
        _broker().publish(
            self.session_id,
            {"type": "state", "state_revision": self.state_revision},
        )


DEFAULT_SESSION_ID = "default"


# --- Event broker (SSE fan-out) --------------------------------------------


# Bounded per-subscriber queue. If a subscriber stalls (slow client, dead
# TCP connection) we drop newer events rather than block the printer service.
# 64 is comfortable headroom — a typical MCP call bursts ~5 revisions
# (upload → arrange → slice → sim start → a few steps).
_SSE_QUEUE_MAXSIZE = 64


class _Subscriber:
    """One active SSE client. Queue is asyncio-bound; publishers are either
    on the same loop (async handlers) or on a threadpool worker (FastAPI
    sync endpoints and fastmcp tool calls), so the publish path goes
    through `loop.call_soon_threadsafe` to stay race-free.
    """

    __slots__ = ("queue", "loop")

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=_SSE_QUEUE_MAXSIZE
        )
        self.loop = loop


class EventBroker:
    """Session-scoped pub/sub for SSE event fan-out.

    Subscribers (one per open `/api/events` connection) register an
    asyncio.Queue and drain it from the SSE streaming handler. Publishers
    (PrinterService mutations) push events keyed by session id; each queue
    in that session receives a copy. Other sessions never see the event, so
    an LLM driving Alice's printer doesn't leak into Bob's browser.

    Thread-safe: the subscriber set is guarded by a threading.RLock, and
    enqueues cross the thread → asyncio boundary via `call_soon_threadsafe`.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subs: dict[str, set[_Subscriber]] = {}

    def subscribe(
        self, session_id: str, loop: asyncio.AbstractEventLoop
    ) -> _Subscriber:
        sub = _Subscriber(loop)
        with self._lock:
            self._subs.setdefault(session_id, set()).add(sub)
        return sub

    def unsubscribe(self, session_id: str, sub: _Subscriber) -> None:
        with self._lock:
            bucket = self._subs.get(session_id)
            if not bucket:
                return
            bucket.discard(sub)
            if not bucket:
                self._subs.pop(session_id, None)

    def publish(self, session_id: str, event: dict[str, Any]) -> None:
        """Push `event` to every subscriber in `session_id`. Non-blocking —
        if a subscriber's queue is full we drop the event for that
        subscriber rather than stall the caller. Safe to call from any
        thread."""
        with self._lock:
            bucket = self._subs.get(session_id)
            subs = list(bucket) if bucket else []
        for sub in subs:
            # Hop onto the subscriber's loop so Queue.put_nowait is called
            # from the thread that owns it; otherwise asyncio will raise
            # RuntimeError from a wrong-thread mutation.
            try:
                sub.loop.call_soon_threadsafe(self._enqueue, sub.queue, event)
            except RuntimeError:
                # Loop is already closed — skip; the SSE handler will have
                # already unsubscribed on disconnect.
                pass

    @staticmethod
    def _enqueue(queue: asyncio.Queue[dict[str, Any]], event: dict[str, Any]) -> None:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            # Drop the event on a full queue rather than block — clients that
            # can't keep up will simply see fewer updates. The next mutation
            # carries a fresh revision number so the UI still converges.
            pass

    def active_sessions(self) -> list[str]:
        with self._lock:
            return list(self._subs.keys())


_EVENT_BROKER = EventBroker()


def _broker() -> EventBroker:
    return _EVENT_BROKER


def get_broker() -> EventBroker:
    return _EVENT_BROKER


# Session ids land in dict keys, so we lock the charset down to keep an
# attacker from filling the registry with NULs, control chars, very long
# strings, or other weirdness. The format is wide enough for a UUID, an MCP
# session token, the literal "default" sentinel, or an Atlas conversation id.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._\-:]{1,128}$")


def _validate_session_id(sid: str) -> str:
    if not _SESSION_ID_RE.match(sid):
        raise ValueError(
            f"invalid session id (must match {_SESSION_ID_RE.pattern})"
        )
    return sid


class SessionRegistry:
    """Per-session PrinterService store with lazy creation + idle TTL cleanup.

    Every MCP session (one per chat conversation per user, keyed on the
    `mcp-session-id` header FastMCP's streamable-HTTP transport mints) and
    every browser tab (keyed on the `X-Session-Id` header the frontend sends)
    gets its own printer. Sessions idle for longer than `ttl_seconds` are
    evicted when any other session touches the registry, so a long-running
    server doesn't accumulate state from closed browser tabs.

    Because the session id is user-controlled (HTTP header / query param), we
    also enforce a hard cap on the number of concurrently live sessions and a
    charset whitelist on the id itself — together that prevents a hostile
    client from spinning up unbounded PrinterServices to OOM the process.
    """

    def __init__(
        self,
        ttl_seconds: float = 3600.0,
        max_sessions: int = 1000,
    ) -> None:
        self._lock = threading.RLock()
        self._services: dict[str, PrinterService] = {}
        self._last_seen: dict[str, float] = {}
        self.ttl_seconds = ttl_seconds
        self.max_sessions = max_sessions

    def get(self, session_id: str | None) -> PrinterService:
        """Return (and create-on-miss) the PrinterService for `session_id`.

        Raises ValueError on a malformed session id or when the live-session
        cap would be exceeded by a fresh allocation.
        """
        sid = _validate_session_id(session_id or DEFAULT_SESSION_ID)
        now = time.monotonic()
        with self._lock:
            self._evict_idle_locked(now)
            svc = self._services.get(sid)
            if svc is None:
                if len(self._services) >= self.max_sessions:
                    # Already pruned idle sessions above; if we're still at the
                    # cap, the live session count is genuinely too high. Refuse
                    # rather than silently evicting a live user's state.
                    raise ValueError(
                        f"session cap reached ({self.max_sessions}); "
                        "refusing to allocate a new PrinterService"
                    )
                svc = PrinterService(session_id=sid)
                self._services[sid] = svc
            self._last_seen[sid] = now
            return svc

    def drop(self, session_id: str) -> None:
        """Explicit eviction — used by `reset_service(session_id)`."""
        with self._lock:
            self._services.pop(session_id, None)
            self._last_seen.pop(session_id, None)

    def reset(self) -> None:
        """Wipe the entire registry. Used by tests and the global `/api/reset`."""
        with self._lock:
            self._services.clear()
            self._last_seen.clear()

    def _evict_idle_locked(self, now: float) -> None:
        if self.ttl_seconds <= 0:
            return
        cutoff = now - self.ttl_seconds
        stale = [sid for sid, ts in self._last_seen.items() if ts < cutoff]
        for sid in stale:
            self._services.pop(sid, None)
            self._last_seen.pop(sid, None)

    def active_sessions(self) -> list[str]:
        with self._lock:
            return list(self._services.keys())


def _env_float(name: str, default: float) -> float:
    """Parse a float-valued env var without crashing the import on garbage.

    A misconfigured `SESSION_TTL_SECONDS=forever` shouldn't take the whole
    service down — fall back to the default and let the operator notice via
    the warning.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        import logging
        logging.getLogger(__name__).warning(
            "ignoring %s=%r (not a float); falling back to %s",
            name,
            raw,
            default,
        )
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        import logging
        logging.getLogger(__name__).warning(
            "ignoring %s=%r (not an int); falling back to %s",
            name,
            raw,
            default,
        )
        return default


_registry: SessionRegistry = SessionRegistry(
    ttl_seconds=_env_float("SESSION_TTL_SECONDS", 3600.0),
    max_sessions=_env_int("SESSION_MAX_COUNT", 1000),
)


def get_registry() -> SessionRegistry:
    return _registry


def get_service(session_id: str | None = None) -> PrinterService:
    """Return the PrinterService bound to `session_id`.

    Called with no argument it returns the "default" session's service, which
    matches the pre-multiuser behaviour — handy for dev clients, tests, and
    any non-session-aware HTTP call.
    """
    return _registry.get(session_id)


def reset_service(session_id: str | None = None) -> None:
    """Test hook. Without args, wipes every session's state."""
    if session_id is None:
        _registry.reset()
    else:
        _registry.drop(session_id)


__all__ = [
    "ArrangeError",
    "DEFAULT_SESSION_ID",
    "EventBroker",
    "PrinterService",
    "Part",
    "SessionRegistry",
    "get_broker",
    "get_registry",
    "get_service",
    "reset_service",
]
