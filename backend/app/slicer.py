"""Slicer with offset perimeters, sparse/solid/bridge infill, clustered
supports, adaptive layer heights, and a richer G-code dialect.

For each layer Z we:
  1. Intersect every triangle that spans Z with the Z plane -> a line segment.
  2. Chain segments head-to-tail into closed polygons (the layer contours).
  3. Rasterize each mesh's per-layer polygon to a shared grid (see surfaces.py),
     so top/bottom/overhang detection becomes set algebra on cells.
  4. Emit G-code: N inset perimeters (+1 on overhang-heavy layers), brim
     loops on layer 0, infill clipped to solid cell regions (100% at solid
     spacing, sparse at density-based spacing elsewhere), bridge-tagged fill
     on unsupported infill lines, and clustered-pillar supports below
     unreachable overhangs.

Each emitted Move is tagged with a `role` ("perimeter",
"overhang_perimeter", "bottom", "top", "infill_sparse", "support",
"bridge", "brim", "travel") so the viewer can color the toolpath by function
and the user can actually see shape instead of one orange blob.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

from .geometry import (
    offset_polygon,
    pick_seam_index,
    polygon_area,
    polygon_is_closed,
    rotate_polygon_start,
)
from .stl_loader import Mesh, Triangle
from .surfaces import (
    SUPPORT_RESOLUTION,
    Cell,
    bottom_surface_cells,
    cell_of,
    cells_to_boundary,
    cluster_cells,
    compute_support_cells,
    overhang_cells,
    rasterize_polygons,
    top_surface_cells,
)


MoveKind = Literal["travel", "extrude"]
# Solid fills are tagged "top" or "bottom" depending on which surface they
# serve; there's no bare "infill_solid" role on the wire because the
# top/bottom split is what the viewer wants for coloring.
MoveRole = Literal[
    "travel",
    "perimeter",
    "overhang_perimeter",
    "infill_sparse",
    "support",
    "bottom",
    "top",
    "bridge",
    "brim",
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
    # Richer counters for the new features so the UI and MCP clients can
    # surface what the slicer actually did, not just the nominal settings.
    bridge_moves: int = 0
    brim_loops: int = 0
    adaptive_layers: bool = False

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
            "bridge_moves": self.bridge_moves,
            "brim_loops": self.brim_loops,
            "adaptive_layers": self.adaptive_layers,
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


def _tri_normal_z(tri: Triangle) -> float:
    """|nz| / ||n|| for triangle t — 1 for a horizontal face, 0 for a wall."""
    ax, ay, az = tri.v0
    bx, by, bz = tri.v1
    cx, cy, cz = tri.v2
    ux, uy, uz = bx - ax, by - ay, bz - az
    vx, vy, vz = cx - ax, cy - ay, cz - az
    nx = uy * vz - uz * vy
    ny = uz * vx - ux * vz
    nz = ux * vy - uy * vx
    length = math.sqrt(nx * nx + ny * ny + nz * nz)
    if length < EPS:
        return 0.0
    return abs(nz) / length


def _compute_layer_zs(
    meshes: list[Mesh],
    min_z: float,
    max_z: float,
    layer_height: float,
    *,
    first_layer_height: float | None,
    adaptive: bool,
    h_min: float,
    h_max: float,
) -> list[float]:
    """Produce the list of layer center-z values for slicing.

    Non-adaptive path: classic fixed-`layer_height` stepping, with an
    optional thicker/thinner `first_layer_height` applied to layer 0 for
    bed adhesion.

    Adaptive path: inspect mesh triangle normals near each candidate z; use
    smaller `dz` near sloped faces (which stair-step visibly at coarse
    layers) and larger `dz` through vertical walls or flat regions where
    layer thickness is invisible. Stays within [h_min, h_max].
    """
    fl_h = first_layer_height if first_layer_height else layer_height
    zs: list[float] = [min_z + fl_h / 2.0]
    z_top = min_z + fl_h  # top of layer 0

    if not adaptive:
        # Classic fixed spacing after layer 0.
        next_center = z_top + layer_height / 2.0
        while next_center < max_z + layer_height / 2.0 and len(zs) < 10000:
            zs.append(next_center)
            next_center += layer_height
        return zs

    # Adaptive: pre-bucket triangles by z-range so picking the local max-slope
    # doesn't re-scan the whole mesh per layer.
    all_tris: list[tuple[float, float, float]] = []  # (tmin, tmax, slope_cost)
    for m in meshes:
        for t in m.triangles:
            tmin = min(t.v0[2], t.v1[2], t.v2[2])
            tmax = max(t.v0[2], t.v1[2], t.v2[2])
            nz_abs = _tri_normal_z(t)
            # slope cost peaks at 45° (nz_abs ≈ 0.707) — those are the faces
            # where layer banding is visible. Vertical walls (nz_abs≈0) and
            # horizontal tops (nz_abs≈1) don't benefit from thinner layers.
            slope_cost = 4.0 * nz_abs * (1.0 - nz_abs)
            all_tris.append((tmin, tmax, slope_cost))

    next_center = z_top + h_max / 2.0
    while next_center < max_z + h_max / 2.0 and len(zs) < 10000:
        window_top = next_center + h_max / 2.0
        window_bot = next_center - h_max / 2.0
        local = 0.0
        for tmin, tmax, cost in all_tris:
            if tmax < window_bot or tmin > window_top:
                continue
            if cost > local:
                local = cost
                if local > 0.99:
                    break
        # Blend h_max → h_min as slope cost rises.
        dz = h_max - (h_max - h_min) * local
        dz = max(h_min, min(h_max, dz))
        zs.append(next_center)
        next_center += dz
    return zs


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
    # --- G-code niceties ---
    retract_mm: float = 1.0,
    retract_speed: float = 40.0,
    hotend_temp: float = 200.0,
    bed_temp: float = 60.0,
    fan_speed: int = 255,
    first_layer_fan: int = 0,
    bridge_fan: int = 255,
    bridge_speed_factor: float = 0.5,
    # --- Toolpath quality ---
    seam_position: str = "auto",
    first_layer_height: float | None = None,
    first_layer_speed: float | None = None,
    brim_loops: int = 0,
    adaptive_layers: bool = False,
    layer_height_min: float | None = None,
    layer_height_max: float | None = None,
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
    if retract_mm < 0:
        raise ValueError("retract_mm must be >= 0")
    if brim_loops < 0:
        raise ValueError("brim_loops must be >= 0")
    if not (0.0 < bridge_speed_factor <= 1.0):
        raise ValueError("bridge_speed_factor must be in (0, 1]")
    if first_layer_height is not None and first_layer_height <= 0:
        raise ValueError("first_layer_height must be > 0")
    h_min = layer_height_min if layer_height_min is not None else layer_height * 0.5
    h_max = layer_height_max if layer_height_max is not None else layer_height * 1.5
    if h_min <= 0 or h_max <= 0 or h_min > h_max:
        raise ValueError("layer_height_min/_max must be > 0 and min <= max")

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
            adaptive_layers=adaptive_layers,
        )

    min_z = min(m.min_xyz[2] for m in meshes)
    max_z = max(m.max_xyz[2] for m in meshes)

    zs = _compute_layer_zs(
        meshes,
        min_z,
        max_z,
        layer_height,
        first_layer_height=first_layer_height,
        adaptive=adaptive_layers,
        h_min=h_min,
        h_max=h_max,
    )

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

    def emit_polyline(poly: list[tuple[float, float]], z_target: float, role: MoveRole) -> None:
        if len(poly) < 2:
            return
        start = poly[0]
        emit_travel_to(start[0], start[1], z_target)
        for pt in poly[1:]:
            emit_extrude_to(pt[0], pt[1], z_target, role=role)

    # Pre-pass 1: slice every mesh at every layer and rasterize the resulting
    # polygons. We need the full mask stack to decide top/bottom/overhang and
    # to route supports.
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

    # Pre-pass 2: per-mesh support cells.
    if support_density > 0:
        mesh_support: list[list[set]] = [
            compute_support_cells(masks) for masks in mesh_layer_masks
        ]
        total_support_cells = sum(
            sum(len(s) for s in layers_) for layers_ in mesh_support
        )
    else:
        mesh_support = [[set() for _ in masks] for masks in mesh_layer_masks]
        total_support_cells = 0

    bridge_move_count = 0

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

            # Layer 0 sits on the bed, not in the air — it must never be
            # classified as an overhang even though `mask[L-1]` is empty by
            # convention.
            if layer_index > 0:
                overhangs = overhang_cells(here, below)
            else:
                overhangs = frozenset()
            top_cells = top_surface_cells(masks, layer_index, top_layers)
            bot_cells = bottom_surface_cells(masks, layer_index, bottom_layers)
            solid_cells = top_cells | bot_cells | overhangs

            has_overhang = len(overhangs) > 0
            effective_perimeters = perimeters + (1 if has_overhang else 0)

            # --- Brim (layer 0 only) ---
            if layer_index == 0 and brim_loops > 0:
                for poly in polylines:
                    if not polygon_is_closed(poly):
                        continue
                    for b in range(1, brim_loops + 1):
                        # Brim rings are outward offsets of the contour at
                        # successive nozzle-width steps. offset_polygon(poly,
                        # -distance) reverses the inward-offset convention.
                        brim_poly = offset_polygon(poly, -nozzle_width * b)
                        if not brim_poly:
                            break
                        emit_polyline(brim_poly, z_target, role="brim")

            # --- Perimeters (actual insets, not retraces) ---
            prev_head = (moves[-1].x, moves[-1].y) if moves else None
            for poly in polylines:
                if len(poly) < 2:
                    continue
                for p_idx in range(effective_perimeters):
                    # p_idx=0 is the outer wall (the original contour). Each
                    # inner wall is offset by one extrusion width so the
                    # shells stack instead of retracing.
                    if p_idx == 0:
                        current = poly
                    else:
                        current = offset_polygon(poly, nozzle_width * p_idx)
                        if not current:
                            break  # collapsed — stop insetting
                    if not polygon_is_closed(current):
                        # Partial chaining (open polyline). Still emit but
                        # don't try to rotate the seam.
                        emit_polyline(current, z_target, role="perimeter")
                        continue
                    idx = pick_seam_index(current, seam_position, ref=prev_head)
                    current = rotate_polygon_start(current, idx)
                    role: MoveRole = (
                        "overhang_perimeter"
                        if has_overhang and p_idx == effective_perimeters - 1
                        else "perimeter"
                    )
                    emit_polyline(current, z_target, role=role)
                    prev_head = (current[-1][0], current[-1][1])

            # --- Infill: per-segment classification into solid/sparse/bridge. ---
            # Generate at the solid spacing (one line per nozzle_width). For
            # sparse regions we drop all but every Nth line so the density
            # matches infill_density. A single scan-line pass gives us both
            # solid and sparse at their correct spacings without generating
            # two separate infill runs.
            horizontal = (layer_index % 2 == 0)
            solid_spacing = nozzle_width
            # Sparse reuses the same grid but keeps only every `sparse_step`th
            # row, so sparse spacing == nozzle_width * sparse_step.
            if infill_density > 0:
                sparse_step = max(1, int(round(1.0 / infill_density)))
            else:
                sparse_step = 0  # 0 means "never emit sparse"

            infill_segments = _rectilinear_infill_rows(polylines, solid_spacing, horizontal=horizontal)
            for row_idx, (a, b) in infill_segments:
                # Classify by midpoint: if it lies on a solid cell, this is
                # solid fill; if also the cell below has no material, it's a
                # bridge. Otherwise (sparse region) emit only every
                # sparse_step'th row.
                mx, my = (a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0
                c = cell_of(mx, my, SUPPORT_RESOLUTION)
                in_solid = c in solid_cells
                if in_solid:
                    if layer_index > 0 and c not in below:
                        role_fill: MoveRole = "bridge"
                        bridge_move_count += 1
                    elif c in overhangs or c in bot_cells:
                        role_fill = "bottom"
                    else:
                        role_fill = "top"
                else:
                    if sparse_step == 0 or (row_idx % sparse_step) != 0:
                        continue
                    role_fill = "infill_sparse"
                emit_travel_to(a[0], a[1], z_target)
                emit_extrude_to(b[0], b[1], z_target, role=role_fill)

        # --- Supports: clustered pillars, not isolated stubs. ---
        if support_density > 0:
            for mesh_index in range(len(meshes)):
                cells = mesh_support[mesh_index][layer_index]
                if not cells:
                    continue
                clusters = cluster_cells(cells)
                for cluster in clusters:
                    _emit_support_cluster(
                        cluster,
                        SUPPORT_RESOLUTION,
                        z_target,
                        support_density,
                        nozzle_width,
                        emit_polyline,
                        emit_travel_to,
                        emit_extrude_to,
                        (moves[-1].x, moves[-1].y),
                    )

    gcode = _render_gcode(
        moves,
        travel_speed=travel_speed,
        print_speed=print_speed,
        retract_mm=retract_mm,
        retract_speed=retract_speed,
        hotend_temp=hotend_temp,
        bed_temp=bed_temp,
        fan_speed=fan_speed,
        first_layer_fan=first_layer_fan,
        bridge_fan=bridge_fan,
        bridge_speed_factor=bridge_speed_factor,
        first_layer_speed=first_layer_speed,
        first_layer_z=zs[0] if zs else 0.0,
    )

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
        bridge_moves=bridge_move_count,
        brim_loops=brim_loops,
        adaptive_layers=adaptive_layers,
    )


def _emit_support_cluster(
    cluster: set[Cell],
    res: float,
    z_target: float,
    support_density: float,
    nozzle_width: float,
    emit_polyline,
    emit_travel_to,
    emit_extrude_to,
    prev_head: tuple[float, float],
) -> None:
    """Emit a support pillar for one connected cell cluster.

    Draws a single perimeter around the cluster's boundary, then lays
    sparse rectilinear fill inside at `support_density`. Compared to the
    earlier "one stub per cell" approach this produces support structures
    that actually look like walls in the viewer and that transfer load
    along the pillar rather than relying on dozens of point stubs.
    """
    if not cluster:
        return
    edges = cells_to_boundary(cluster, res)
    boundary_polys = _chain_segments(edges, tol=res / 8.0)

    # Perimeter.
    for poly in boundary_polys:
        emit_polyline(poly, z_target, role="support")

    # Sparse rectilinear infill at support_density. We walk the cluster's
    # bounding box on a nozzle-width grid and only emit strokes whose
    # midpoint falls in a cluster cell.
    cols = [c for (c, _) in cluster]
    rows = [r for (_, r) in cluster]
    x0 = min(cols) * res
    x1 = (max(cols) + 1) * res
    y0 = min(rows) * res
    y1 = (max(rows) + 1) * res
    step = max(nozzle_width, nozzle_width / max(support_density, 0.05))
    y = y0 + step / 2.0
    while y < y1:
        # Scan left→right, emit short strokes while inside the cluster.
        x = x0 + nozzle_width / 2.0
        run_start: float | None = None
        while x <= x1:
            col = int(x // res)
            row = int(y // res)
            if (col, row) in cluster:
                if run_start is None:
                    run_start = x
            else:
                if run_start is not None:
                    emit_travel_to(run_start, y, z_target)
                    emit_extrude_to(x - nozzle_width, y, z_target, role="support")
                    run_start = None
            x += nozzle_width
        if run_start is not None:
            emit_travel_to(run_start, y, z_target)
            emit_extrude_to(x1, y, z_target, role="support")
        y += step


def _rectilinear_infill_rows(
    contours: list[list[tuple[float, float]]],
    spacing: float,
    horizontal: bool,
) -> list[tuple[int, tuple[tuple[float, float], tuple[float, float]]]]:
    """Scan-line rectilinear fill, returning (row_index, segment) pairs.

    The row_index lets callers sample every Nth row for sparse fill without
    regenerating the scan. Alternating rows are reversed so the infill
    zigzags — that saves a travel between passes in the naïve case, and
    more importantly matches the pattern real slicers produce.
    """
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

    out: list[tuple[int, tuple[tuple[float, float], tuple[float, float]]]] = []

    if horizontal:
        y = ymin + spacing / 2
        row_index = 0
        while y < ymax:
            crossings = _scanline_crossings(closed, y, axis="y")
            if row_index % 2 == 1:
                pairs = list(zip(crossings[0::2], crossings[1::2]))
                pairs.reverse()
                for a, b in pairs:
                    out.append((row_index, ((b, y), (a, y))))
            else:
                for i in range(0, len(crossings) - 1, 2):
                    out.append((row_index, ((crossings[i], y), (crossings[i + 1], y))))
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
                    out.append((col_index, ((x, b), (x, a))))
            else:
                for i in range(0, len(crossings) - 1, 2):
                    out.append((col_index, ((x, crossings[i]), (x, crossings[i + 1]))))
            x += spacing
            col_index += 1

    return out


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


# Roles that should fire the "high fan / low speed" bridge cooling profile.
_BRIDGE_ROLES: set[str] = {"bridge", "overhang_perimeter"}


def _render_gcode(
    moves: list[Move],
    *,
    travel_speed: float,
    print_speed: float,
    retract_mm: float,
    retract_speed: float,
    hotend_temp: float,
    bed_temp: float,
    fan_speed: int,
    first_layer_fan: int,
    bridge_fan: int,
    bridge_speed_factor: float,
    first_layer_speed: float | None,
    first_layer_z: float,
) -> str:
    """Emit G-code with a proper thermal preamble, retraction between extrude
    runs, and per-layer fan control.

    Travel moves carry the E coordinate as "retracted" (E_cum - retract_mm)
    and the next extrude move un-retracts first, so each hop gets a clean
    G1 E- / G0 travel / G1 E+ / extrude sequence instead of oozing over the
    whole travel.
    """
    retract_mm = max(0.0, retract_mm)
    f_retract = max(1, int(retract_speed * 60))

    # Preamble: heat bed+hotend, home, set modes. M190/M109 waits before the
    # first move so we don't start printing into a cold nozzle.
    lines: list[str] = [
        "; 3dprintsim generated toolpath",
        "G21 ; mm units",
        "G90 ; absolute",
        "M82 ; extruder absolute",
        f"M140 S{int(bed_temp)} ; set bed",
        f"M104 S{int(hotend_temp)} ; set hotend",
        f"M190 S{int(bed_temp)} ; wait for bed",
        f"M109 S{int(hotend_temp)} ; wait for hotend",
        "G28 ; home",
        "G92 E0 ; reset extruder",
    ]

    fl_print_speed = first_layer_speed if first_layer_speed is not None else print_speed
    f_travel = max(1, int(travel_speed * 60))
    f_print_default = max(1, int(print_speed * 60))
    f_print_first = max(1, int(fl_print_speed * 60))
    f_print_bridge = max(1, int(print_speed * bridge_speed_factor * 60))

    current_fan: int | None = None
    # Treat anything within a layer-height of the first layer's z as "still
    # layer 0" for cooling purposes — the first layer benefits from low fan
    # regardless of the nominal layer height.
    first_layer_threshold = first_layer_z + 1e-3

    retracted = False
    prev: Move | None = None

    def set_fan(target: int) -> None:
        nonlocal current_fan
        if target == current_fan:
            return
        if target <= 0:
            lines.append("M107 ; fan off")
        else:
            lines.append(f"M106 S{max(0, min(255, int(target)))}")
        current_fan = target

    # Explicit fan off at the top (M107) is baked into set_fan's first call.
    set_fan(first_layer_fan)

    for m in moves:
        is_first_layer = m.z <= first_layer_threshold

        # Decide fan target before emitting the move.
        if m.kind == "extrude":
            if is_first_layer:
                target_fan = first_layer_fan
            elif m.role in _BRIDGE_ROLES:
                target_fan = bridge_fan
            else:
                target_fan = fan_speed
            set_fan(target_fan)

        if m.kind == "travel":
            # Retract before the travel hop so the nozzle isn't oozing over it.
            if retract_mm > 0 and not retracted and prev is not None and prev.kind == "extrude":
                lines.append(f"G1 E{prev.e - retract_mm:.4f} F{f_retract} ; retract")
                retracted = True
            feed = f" F{f_travel}"
            lines.append(f"G0 X{m.x:.3f} Y{m.y:.3f} Z{m.z:.3f}{feed}")
        else:
            # Un-retract before resuming extrusion.
            if retract_mm > 0 and retracted:
                # After un-retract the extruder sits back at the last
                # cumulative_e it was at before the retract (i.e. the prev
                # extrude's e value). The next G1 then advances to m.e.
                last_e = prev.e if prev is not None else m.e
                lines.append(f"G1 E{last_e:.4f} F{f_retract} ; un-retract")
                retracted = False
            # Pick feedrate: bridge < first-layer override < default.
            if m.role == "bridge":
                f_print = f_print_bridge
            elif is_first_layer:
                f_print = f_print_first
            else:
                f_print = f_print_default
            feed_change = prev is None or prev.kind != m.kind or prev.role != m.role
            feed = f" F{f_print}" if feed_change else ""
            lines.append(f"G1 X{m.x:.3f} Y{m.y:.3f} Z{m.z:.3f} E{m.e:.4f}{feed}")
        prev = m

    # Tail: park the nozzle-adjacent heaters off so the printer doesn't idle
    # hot after the job. M107 ensures fan is off for the cooldown indicator
    # some firmwares watch for.
    lines.append("M107 ; fan off")
    lines.append("M104 S0 ; hotend off")
    lines.append("M140 S0 ; bed off")
    lines.append("; end")
    return "\n".join(lines) + "\n"
