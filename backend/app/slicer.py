"""Basic perimeter-only slicer.

For each layer Z we:
  1. Intersect every triangle that spans Z with the Z plane -> a line segment.
  2. Chain segments head-to-tail into closed polygons (the layer contours).
  3. Emit a G-code toolpath that traces each contour once, with travel moves
     between contours and between layers.

This is "perimeter only" — no infill, no shells beyond 1. It is intentionally
small so the simulator has something to visualize, not a production slicer.

The toolpath is returned as a structured list so the frontend can animate it
without re-parsing G-code; the G-code string is also produced for completeness.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .stl_loader import Mesh, Triangle


MoveKind = Literal["travel", "extrude"]


@dataclass
class Move:
    kind: MoveKind
    x: float
    y: float
    z: float
    e: float = 0.0  # cumulative extruder position


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

    def summary(self) -> dict:
        return {
            "layer_count": len(self.layers),
            "move_count": len(self.moves),
            "layer_height": self.layer_height,
            "perimeters": self.perimeters,
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

    # Collect intersection points on each edge that crosses z.
    crossings: list[tuple[float, float]] = []
    for i in range(3):
        a = pts[i]
        b = pts[(i + 1) % 3]
        za, zb = a[2], b[2]
        if (za <= z + EPS and zb >= z - EPS) or (zb <= z + EPS and za >= z - EPS):
            if abs(zb - za) < EPS:
                # coplanar edge — skip; another edge will give us a proper crossing
                continue
            t_param = (z - za) / (zb - za)
            if t_param < -EPS or t_param > 1 + EPS:
                continue
            x = a[0] + t_param * (b[0] - a[0])
            y = a[1] + t_param * (b[1] - a[1])
            crossings.append((x, y))

    # Deduplicate near-coincident points (triangle touching plane at a vertex)
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
    # Build a lookup keyed by quantized endpoints.
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
        # Extend forward
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
        # Extend backward
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
    extrusion_per_mm: float = 0.04,
    travel_speed: float = 120.0,
    print_speed: float = 40.0,
) -> SliceResult:
    if layer_height <= 0:
        raise ValueError("layer_height must be > 0")
    if perimeters < 1:
        raise ValueError("perimeters must be >= 1")
    if not meshes:
        return SliceResult(
            layer_height=layer_height,
            perimeters=perimeters,
            moves=[],
            layers=[],
            gcode="; empty\n",
        )

    min_z = min(m.min_xyz[2] for m in meshes)
    max_z = max(m.max_xyz[2] for m in meshes)

    # Layer Zs: center of each layer, from first-layer top to top of model.
    zs: list[float] = []
    z = min_z + layer_height / 2
    # Guard against floating-point drift producing a zero-layer model.
    while z < max_z + layer_height / 2 and len(zs) < 10000:
        zs.append(z)
        z += layer_height

    moves: list[Move] = []
    layers: list[LayerPaths] = []
    cumulative_e = 0.0

    # Start somewhere safe
    moves.append(Move(kind="travel", x=0.0, y=0.0, z=5.0, e=0.0))

    for z_target in zs:
        segs: list[tuple[tuple[float, float], tuple[float, float]]] = []
        for mesh in meshes:
            if z_target < mesh.min_xyz[2] or z_target > mesh.max_xyz[2]:
                continue
            for tri in mesh.triangles:
                s = _segment_at_z(tri, z_target)
                if s is not None:
                    segs.append(s)

        polylines = _chain_segments(segs)
        # For "perimeter only" we trace each polyline once per requested perimeter count.
        # Additional perimeters are emitted as repeated traces (simplification — no offsetting).
        layer = LayerPaths(z=z_target, contours=polylines)
        layers.append(layer)

        for poly in polylines:
            if len(poly) < 2:
                continue
            for _ in range(perimeters):
                start = poly[0]
                moves.append(Move(kind="travel", x=start[0], y=start[1], z=z_target, e=cumulative_e))
                for pt in poly[1:]:
                    prev = moves[-1]
                    dx = pt[0] - prev.x
                    dy = pt[1] - prev.y
                    dist = (dx * dx + dy * dy) ** 0.5
                    cumulative_e += dist * extrusion_per_mm
                    moves.append(
                        Move(
                            kind="extrude",
                            x=pt[0],
                            y=pt[1],
                            z=z_target,
                            e=cumulative_e,
                        )
                    )

    gcode = _render_gcode(moves, travel_speed, print_speed)

    return SliceResult(
        layer_height=layer_height,
        perimeters=perimeters,
        moves=moves,
        layers=layers,
        gcode=gcode,
    )


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
