"""Slicer with perimeters, sparse/solid infill, overhang detection, and
auto-generated supports.

For each layer Z we:
  1. Intersect every triangle that spans Z with the Z plane -> a line segment.
  2. Chain segments head-to-tail into closed polygons (the layer contours).
  3. Rasterize each mesh's per-layer polygon to a shared grid (see surfaces.py),
     so top/bottom/overhang detection becomes set algebra on cells.
  4. Emit G-code: N perimeters (+1 on overhang-heavy layers), infill at
     100% spacing on solid regions (top/bottom/overhang), sparse spacing
     elsewhere, and sparse support columns below unreachable overhangs.

Each emitted Move is tagged with a `role` ("perimeter", "infill_solid",
"infill_sparse", "support", "travel") so the viewer can color the toolpath
by function and the user can actually see shape instead of one orange blob.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .stl_loader import Mesh, Triangle
from .surfaces import (
    SUPPORT_RESOLUTION,
    bottom_surface_cells,
    compute_support_cells,
    overhang_cells,
    rasterize_polygons,
    top_surface_cells,
)


MoveKind = Literal["travel", "extrude"]
MoveRole = Literal[
    "travel",
    "perimeter",
    "overhang_perimeter",
    "infill_solid",
    "infill_sparse",
    "support",
    "bottom",
    "top",
]


@dataclass
class Move:
    kind: MoveKind
    x: float
    y: float
    z: float
    e: float = 0.0  # cumulative extruder position
    # Functional role of the move. Travel moves are always "travel"; extrudes
    # carry the richer tag so the frontend can color by role to make depth
    # and overhang legible instead of rendering one orange blob.
    role: MoveRole = "travel"


@dataclass
class LayerPaths:
    z: float
    contours: list[list[tuple[float, float]]] = field(default_factory=list)


@dataclass
class SliceResult:
    layer_height: float
    perimeters: int
    moves: list[Move]
    layers: list[LayerPaths]
    gcode: str
    infill_density: float = 0.0
    top_layers: int = 0
    bottom_layers: int = 0
    support_density: float = 0.0
    support_cell_count: int = 0

    def summary(self) -> dict:
        return {
            "layer_count": len(self.layers),
            "move_count": len(self.moves),
            "layer_height": self.layer_height,
            "perimeters": self.perimeters,
            "infill_density": self.infill_density,
            "top_layers": self.top_layers,
            "bottom_layers": self.bottom_layers,
            "support_density": self.support_density,
            "support_cell_count": self.support_cell_count,
            "total_extrusion": self.moves[-1].e if self.moves else 0.0,
        }


EPS = 1e-9


def _segment_at_z(t: Triangle, z: float) -> tuple[tuple[float, float], tuple[float, float]] | None:
    """Return the 2D segment where triangle t crosses plane z, or None."""
    pts = [t.v0, t.v1, t.v2]
    above = [p[2] > z + EPS for p in pts]
    below = [p[2] < z - EPS for p in pts]
    if all(above) or all(below):
        return None

    crossings: list[tuple[float, float]] = []
    for i in range(3):
        a = pts[i]
        b = pts[(i + 1) % 3]
        za, zb = a[2], b[2]
        if (za <= z + EPS and zb >= z - EPS) or (zb <= z + EPS and za >= z - EPS):
            if abs(zb - za) < EPS:
                continue
            t_param = (z - za) / (zb - za)
            if t_param < -EPS or t_param > 1 + EPS:
                continue
            x = a[0] + t_param * (b[0] - a[0])
            y = a[1] + t_param * (b[1] - a[1])
            crossings.append((x, y))

    unique: list[tuple[float, float]] = []
    for c in crossings:
        if not any(abs(c[0] - u[0]) < 1e-6 and abs(c[1] - u[1]) < 1e-6 for u in unique):
            unique.append(c)

    if len(unique) < 2:
        return None
    return unique[0], unique[1]


def _chain_segments(
    segs: list[tuple[tuple[float, float], tuple[float, float]]],
    tol: float = 1e-3,
) -> list[list[tuple[float, float]]]:
    """Chain a soup of 2D segments into polylines (preferring closed loops)."""
    def key(p: tuple[float, float]) -> tuple[int, int]:
        return (round(p[0] / tol), round(p[1] / tol))

    remaining: dict[int, tuple[tuple[float, float], tuple[float, float]]] = {
        i: s for i, s in enumerate(segs)
    }
    by_key: dict[tuple[int, int], list[int]] = {}
    for i, (a, b) in remaining.items():
        by_key.setdefault(key(a), []).append(i)
        by_key.setdefault(key(b), []).append(i)

    def pop(i: int) -> tuple[tuple[float, float], tuple[float, float]]:
        seg = remaining.pop(i)
        for p in seg:
            lst = by_key.get(key(p), [])
            if i in lst:
                lst.remove(i)
        return seg

    polylines: list[list[tuple[float, float]]] = []

    while remaining:
        start_i = next(iter(remaining))
        a, b = pop(start_i)
        poly: list[tuple[float, float]] = [a, b]
        while True:
            end = poly[-1]
            cands = by_key.get(key(end), [])
            cands = [c for c in cands if c in remaining]
            if not cands:
                break
            ci = cands[0]
            sa, sb = pop(ci)
            nxt = sb if key(sa) == key(end) else sa
            if key(nxt) == key(poly[0]):
                poly.append(poly[0])
                break
            poly.append(nxt)
        while True:
            start = poly[0]
            cands = by_key.get(key(start), [])
            cands = [c for c in cands if c in remaining]
            if not cands:
                break
            ci = cands[0]
            sa, sb = pop(ci)
            nxt = sb if key(sa) == key(start) else sa
            if key(nxt) == key(poly[-1]):
                poly.insert(0, poly[-1])
                break
            poly.insert(0, nxt)

        polylines.append(poly)

    return polylines


def _polyline_length(poly: list[tuple[float, float]]) -> float:
    total = 0.0
    for i in range(len(poly) - 1):
        dx = poly[i + 1][0] - poly[i][0]
        dy = poly[i + 1][1] - poly[i][1]
        total += (dx * dx + dy * dy) ** 0.5
    return total


def slice_meshes(
    meshes: list[Mesh],
    layer_height: float = 0.4,
    perimeters: int = 1,
    infill_density: float = 0.2,
    top_layers: int = 3,
    bottom_layers: int = 3,
    nozzle_width: float = 0.4,
    support_density: float = 0.25,
    extrusion_per_mm: float = 0.04,
    travel_speed: float = 120.0,
    print_speed: float = 40.0,
) -> SliceResult:
    if layer_height <= 0:
        raise ValueError("layer_height must be > 0")
    if perimeters < 1:
        raise ValueError("perimeters must be >= 1")
    if not (0.0 <= infill_density <= 1.0):
        raise ValueError("infill_density must be in [0, 1]")
    if top_layers < 0 or bottom_layers < 0:
        raise ValueError("top_layers and bottom_layers must be >= 0")
    if nozzle_width <= 0:
        raise ValueError("nozzle_width must be > 0")
    if not (0.0 <= support_density <= 1.0):
        raise ValueError("support_density must be in [0, 1]")
    if not meshes:
        return SliceResult(
            layer_height=layer_height,
            perimeters=perimeters,
            moves=[],
            layers=[],
            gcode="; empty\n",
            infill_density=infill_density,
            top_layers=top_layers,
            bottom_layers=bottom_layers,
            support_density=support_density,
            support_cell_count=0,
        )

    min_z = min(m.min_xyz[2] for m in meshes)
    max_z = max(m.max_xyz[2] for m in meshes)

    zs: list[float] = []
    z = min_z + layer_height / 2
    while z < max_z + layer_height / 2 and len(zs) < 10000:
        zs.append(z)
        z += layer_height

    moves: list[Move] = []
    layers: list[LayerPaths] = []
    cumulative_e = 0.0

    moves.append(Move(kind="travel", x=0.0, y=0.0, z=5.0, e=0.0, role="travel"))

    def emit_travel_to(x: float, y: float, z_target: float) -> None:
        moves.append(
            Move(kind="travel", x=x, y=y, z=z_target, e=cumulative_e, role="travel")
        )

    def emit_extrude_to(x: float, y: float, z_target: float, role: MoveRole) -> None:
        nonlocal cumulative_e
        prev = moves[-1]
        dx = x - prev.x
        dy = y - prev.y
        dist = (dx * dx + dy * dy) ** 0.5
        cumulative_e += dist * extrusion_per_mm
        moves.append(
            Move(kind="extrude", x=x, y=y, z=z_target, e=cumulative_e, role=role)
        )

    # Pre-pass 1: slice every mesh at every layer and rasterize the resulting
    # polygons. We need the full mask stack to decide top/bottom/overhang and
    # to route supports, which means slicing can't happen purely in a single
    # top-to-bottom loop anymore.
    mesh_layer_polys: list[list[list[list[tuple[float, float]]]]] = []
    mesh_layer_masks: list[list[frozenset]] = []
    for mesh in meshes:
        poly_by_layer: list[list[list[tuple[float, float]]]] = []
        masks: list[frozenset] = []
        for z_target in zs:
            if z_target < mesh.min_xyz[2] - EPS or z_target > mesh.max_xyz[2] + EPS:
                poly_by_layer.append([])
                masks.append(frozenset())
                continue
            segs: list[tuple[tuple[float, float], tuple[float, float]]] = []
            for tri in mesh.triangles:
                s = _segment_at_z(tri, z_target)
                if s is not None:
                    segs.append(s)
            polylines = _chain_segments(segs)
            poly_by_layer.append(polylines)
            masks.append(rasterize_polygons(polylines, SUPPORT_RESOLUTION))
        mesh_layer_polys.append(poly_by_layer)
        mesh_layer_masks.append(masks)

    # Pre-pass 2: per-mesh support cells. Computed from the mask stack alone,
    # so concave parts (cups, brackets) and real overhangs (letter T, fidget
    # spinner arms) both get proper columns.
    mesh_support: list[list[set]] = [
        compute_support_cells(masks) for masks in mesh_layer_masks
    ]

    total_support_cells = sum(sum(len(s) for s in layers_) for layers_ in mesh_support)

    # Emission pass: per layer, iterate meshes and write perimeters, infill,
    # and the support row for that layer.
    for layer_index, z_target in enumerate(zs):
        combined_polylines: list[list[tuple[float, float]]] = []
        for mesh_index in range(len(meshes)):
            combined_polylines.extend(mesh_layer_polys[mesh_index][layer_index])
        layer = LayerPaths(z=z_target, contours=combined_polylines)
        layers.append(layer)

        for mesh_index in range(len(meshes)):
            polylines = mesh_layer_polys[mesh_index][layer_index]
            if not polylines:
                continue

            masks = mesh_layer_masks[mesh_index]
            here = masks[layer_index]
            below = masks[layer_index - 1] if layer_index > 0 else frozenset()

            # Overhang = cells of this layer that weren't solid on the layer
            # below. We treat overhangs as bottom surfaces (the filament there
            # is being bridged/extruded into air) which means:
            #   - solid infill spacing over the overhang region,
            #   - an extra perimeter pass for a cleaner bottom edge.
            overhangs = overhang_cells(here, below)
            top_cells = top_surface_cells(masks, layer_index, top_layers)
            bot_cells = bottom_surface_cells(masks, layer_index, bottom_layers)
            solid_cells = top_cells | bot_cells | overhangs

            has_overhang = len(overhangs) > 0
            # One extra perimeter around overhanging regions so the unsupported
            # edge has more material to bridge with. Matches what PrusaSlicer
            # does with "extra perimeters on overhangs".
            effective_perimeters = perimeters + (1 if has_overhang else 0)

            # Perimeters.
            for poly in polylines:
                if len(poly) < 2:
                    continue
                for p_idx in range(effective_perimeters):
                    start = poly[0]
                    emit_travel_to(start[0], start[1], z_target)
                    role: MoveRole = (
                        "overhang_perimeter"
                        if has_overhang and p_idx == effective_perimeters - 1
                        else "perimeter"
                    )
                    for pt in poly[1:]:
                        emit_extrude_to(pt[0], pt[1], z_target, role=role)

            # Infill: solid vs sparse decision is now per-layer but with a
            # richer trigger set. "Solid" fires when there's any top/bottom
            # surface or overhang — a cup's inner floor at mid-height gets
            # treated correctly, not just the first/last N layers of the part.
            layer_is_solid = len(solid_cells) > 0 and (
                len(solid_cells) >= max(1, len(here) // 3)
                or len(overhangs) > 0
            )

            horizontal = (layer_index % 2 == 0)

            if layer_is_solid:
                spacing = nozzle_width
                fill_role: MoveRole = (
                    "bottom" if len(overhangs) > 0 or len(bot_cells) > len(top_cells)
                    else "top"
                )
            elif infill_density > 0:
                spacing = nozzle_width / infill_density
                fill_role = "infill_sparse"
            else:
                spacing = None
                fill_role = "infill_sparse"

            if spacing is not None:
                infill_segments = _rectilinear_infill(polylines, spacing, horizontal=horizontal)
                for (a, b) in infill_segments:
                    emit_travel_to(a[0], a[1], z_target)
                    emit_extrude_to(b[0], b[1], z_target, role=fill_role)

        # Support emission: per-mesh, per-layer, emit a sparse rectilinear
        # pattern over the support cells at this layer. We generate simple
        # short stubs through each support cell — not the prettiest pattern,
        # but enough for the viewer to show where supports would land and
        # enough extrusion in the G-code to be physically meaningful.
        if support_density > 0:
            stub_spacing_rows = max(1, int(round(1.0 / support_density)))
            for mesh_index in range(len(meshes)):
                cells = mesh_support[mesh_index][layer_index]
                if not cells:
                    continue
                # Sub-sample rows based on density so low support_density
                # leaves visible gaps between stubs.
                for (col, row) in cells:
                    if (row + col) % stub_spacing_rows != 0:
                        continue
                    cx = (col + 0.5) * SUPPORT_RESOLUTION
                    cy = (row + 0.5) * SUPPORT_RESOLUTION
                    half = SUPPORT_RESOLUTION / 2
                    if (row % 2) == 0:
                        a = (cx - half, cy)
                        b = (cx + half, cy)
                    else:
                        a = (cx, cy - half)
                        b = (cx, cy + half)
                    emit_travel_to(a[0], a[1], z_target)
                    emit_extrude_to(b[0], b[1], z_target, role="support")

    gcode = _render_gcode(moves, travel_speed, print_speed)

    return SliceResult(
        layer_height=layer_height,
        perimeters=perimeters,
        moves=moves,
        layers=layers,
        gcode=gcode,
        infill_density=infill_density,
        top_layers=top_layers,
        bottom_layers=bottom_layers,
        support_density=support_density,
        support_cell_count=total_support_cells,
    )


def _rectilinear_infill(
    contours: list[list[tuple[float, float]]],
    spacing: float,
    horizontal: bool,
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """Scan-line rectilinear infill clipped to (closed) contour polygons."""
    closed: list[list[tuple[float, float]]] = []
    for poly in contours:
        if len(poly) < 3:
            continue
        if abs(poly[0][0] - poly[-1][0]) < 1e-6 and abs(poly[0][1] - poly[-1][1]) < 1e-6:
            closed.append(poly)
    if not closed or spacing <= 0:
        return []

    xs = [p[0] for poly in closed for p in poly]
    ys = [p[1] for poly in closed for p in poly]
    if not xs:
        return []
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)

    segments: list[tuple[tuple[float, float], tuple[float, float]]] = []

    if horizontal:
        y = ymin + spacing / 2
        row_index = 0
        while y < ymax:
            crossings = _scanline_crossings(closed, y, axis="y")
            if row_index % 2 == 1:
                pairs = list(zip(crossings[0::2], crossings[1::2]))
                pairs.reverse()
                for a, b in pairs:
                    segments.append(((b, y), (a, y)))
            else:
                for i in range(0, len(crossings) - 1, 2):
                    segments.append(((crossings[i], y), (crossings[i + 1], y)))
            y += spacing
            row_index += 1
    else:
        x = xmin + spacing / 2
        col_index = 0
        while x < xmax:
            crossings = _scanline_crossings(closed, x, axis="x")
            if col_index % 2 == 1:
                pairs = list(zip(crossings[0::2], crossings[1::2]))
                pairs.reverse()
                for a, b in pairs:
                    segments.append(((x, b), (x, a)))
            else:
                for i in range(0, len(crossings) - 1, 2):
                    segments.append(((x, crossings[i]), (x, crossings[i + 1])))
            x += spacing
            col_index += 1

    return segments


def _scanline_crossings(
    closed: list[list[tuple[float, float]]],
    value: float,
    axis: str,
) -> list[float]:
    """Sorted coordinates along the orthogonal axis where a horizontal
    (axis='y') or vertical (axis='x') scan line crosses the polygon boundary.
    """
    out: list[float] = []
    for poly in closed:
        for i in range(len(poly) - 1):
            a = poly[i]
            b = poly[i + 1]
            if axis == "y":
                ya, yb = a[1], b[1]
                if ya == yb:
                    continue
                if (ya <= value < yb) or (yb <= value < ya):
                    t = (value - ya) / (yb - ya)
                    out.append(a[0] + t * (b[0] - a[0]))
            else:  # axis == "x"
                xa, xb = a[0], b[0]
                if xa == xb:
                    continue
                if (xa <= value < xb) or (xb <= value < xa):
                    t = (value - xa) / (xb - xa)
                    out.append(a[1] + t * (b[1] - a[1]))
    out.sort()
    deduped: list[float] = []
    for v in out:
        if not deduped or abs(v - deduped[-1]) > 1e-6:
            deduped.append(v)
    return deduped


def _render_gcode(moves: list[Move], travel_speed: float, print_speed: float) -> str:
    lines: list[str] = [
        "; 3dprintsim generated toolpath",
        "G21 ; mm units",
        "G90 ; absolute",
        "M82 ; extruder absolute",
        "G28 ; home",
    ]
    prev: Move | None = None
    f_travel = int(travel_speed * 60)
    f_print = int(print_speed * 60)
    for m in moves:
        if prev is None or prev.kind != m.kind:
            feed = f" F{f_travel}" if m.kind == "travel" else f" F{f_print}"
        else:
            feed = ""
        if m.kind == "travel":
            lines.append(f"G0 X{m.x:.3f} Y{m.y:.3f} Z{m.z:.3f}{feed}")
        else:
            lines.append(f"G1 X{m.x:.3f} Y{m.y:.3f} Z{m.z:.3f} E{m.e:.4f}{feed}")
        prev = m
    lines.append("; end")
    return "\n".join(lines) + "\n"
