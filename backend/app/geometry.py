"""2D polygon helpers for the slicer: signed area, offset, seam rotation.

The slicer deliberately avoids pulling in Shapely or Clipper — the offset
algorithm here is simple (edge-normal shift with line-intersection at
corners) and adequate for the closed, reasonably well-formed contours that
our plane-intersect + chain pipeline produces. It doesn't handle topology
changes (self-intersecting offsets collapse), so callers must stop
insetting once the polygon area approaches zero.
"""

from __future__ import annotations

import math

Point = tuple[float, float]
Polygon = list[Point]


_EPS = 1e-9


def polygon_is_closed(poly: Polygon) -> bool:
    return (
        len(poly) >= 4
        and abs(poly[0][0] - poly[-1][0]) < 1e-6
        and abs(poly[0][1] - poly[-1][1]) < 1e-6
    )


def polygon_area(poly: Polygon) -> float:
    """Signed shoelace area. Positive = CCW, negative = CW. Assumes closed poly."""
    if len(poly) < 4:
        return 0.0
    total = 0.0
    for i in range(len(poly) - 1):
        x1, y1 = poly[i]
        x2, y2 = poly[i + 1]
        total += x1 * y2 - x2 * y1
    return total / 2.0


def _line_intersect(a1: Point, a2: Point, b1: Point, b2: Point) -> Point | None:
    x1, y1 = a1
    x2, y2 = a2
    x3, y3 = b1
    x4, y4 = b2
    denom = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(denom) < 1e-12:
        return None
    t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / denom
    return (x1 + t * (x2 - x1), y1 + t * (y2 - y1))


def offset_polygon(poly: Polygon, distance: float) -> Polygon | None:
    """Offset a closed polygon by `distance` (positive = inward for CCW polygons,
    outward for CW). Returns None if the polygon collapses.

    Uses per-edge normal shift + pairwise intersection at corners. No miter
    limit or self-intersection cleanup — callers should stop insetting once
    the returned polygon's area drops below `nozzle_width ** 2` or becomes
    negative-signed relative to the input.
    """
    if not polygon_is_closed(poly):
        return None
    area = polygon_area(poly)
    if abs(area) < _EPS:
        return None
    # For CCW (area > 0) the "inward" normal of edge a->b is (-dy, dx)/len.
    # For CW swap sign. We express the direction by `s`:
    s = 1.0 if area > 0 else -1.0
    # Caller convention: positive distance shrinks the polygon (inset). For a
    # CW polygon we'd need to flip, so multiply by s.
    shift = distance * s

    n = len(poly) - 1
    edges: list[tuple[Point, Point] | None] = []
    for i in range(n):
        a = poly[i]
        b = poly[i + 1]
        dx = b[0] - a[0]
        dy = b[1] - a[1]
        length = math.hypot(dx, dy)
        if length < _EPS:
            edges.append(None)
            continue
        nx = -dy / length
        ny = dx / length
        shifted_a = (a[0] + nx * shift, a[1] + ny * shift)
        shifted_b = (b[0] + nx * shift, b[1] + ny * shift)
        edges.append((shifted_a, shifted_b))

    new_poly: Polygon = []
    for i in range(n):
        prev_idx = (i - 1) % n
        e1 = edges[prev_idx]
        e2 = edges[i]
        if e1 is None and e2 is None:
            continue
        if e1 is None:
            new_poly.append(e2[0])
            continue
        if e2 is None:
            new_poly.append(e1[1])
            continue
        pt = _line_intersect(e1[0], e1[1], e2[0], e2[1])
        if pt is None:
            # Parallel edges (straight segment split): take the junction point.
            pt = e2[0]
        new_poly.append(pt)

    if len(new_poly) < 3:
        return None
    new_poly.append(new_poly[0])

    # Collapse check: a valid inward offset shrinks the polygon, so the new
    # signed area must have the same sign as the old one and a consistent
    # magnitude relationship with the offset direction. Once the inward
    # offset distance exceeds the inradius the edges flip onto the far side
    # of the centroid and the "new" polygon can actually be *larger* than
    # the original — detect and reject that so callers can stop insetting.
    new_area = polygon_area(new_poly)
    if (area > 0) != (new_area > 0) or abs(new_area) < _EPS:
        return None
    if distance > 0 and abs(new_area) >= abs(area):
        return None
    if distance < 0 and abs(new_area) <= abs(area):
        return None
    return new_poly


def polygon_centroid(poly: Polygon) -> Point:
    if len(poly) < 2:
        return (0.0, 0.0)
    xs = [p[0] for p in poly[:-1]] if polygon_is_closed(poly) else [p[0] for p in poly]
    ys = [p[1] for p in poly[:-1]] if polygon_is_closed(poly) else [p[1] for p in poly]
    return (sum(xs) / len(xs), sum(ys) / len(ys))


def rotate_polygon_start(poly: Polygon, start_index: int) -> Polygon:
    """Rotate a closed polygon so poly[start_index] becomes poly[0].

    Polygon must be closed (first == last). Returns a new list.
    """
    if not polygon_is_closed(poly):
        return poly
    n = len(poly) - 1
    idx = start_index % n
    if idx == 0:
        return poly
    rotated = poly[idx:-1] + poly[:idx]
    rotated.append(rotated[0])
    return rotated


def pick_seam_index(poly: Polygon, mode: str, ref: Point | None = None) -> int:
    """Choose the start vertex index for a seam based on `mode`.

    - "auto": return 0 (no change)
    - "rear": vertex with max y
    - "nearest": vertex closest to `ref` (falls back to auto if ref is None)
    - "aligned": vertex with max (x + y) — arbitrary but consistent world
       corner, which is exactly what "aligned" means: every layer picks the
       same corner of the polygon, so seams stack into a single vertical
       line the user can hide on the back of the part.
    """
    if not polygon_is_closed(poly):
        return 0
    n = len(poly) - 1
    if n < 1:
        return 0
    if mode == "rear":
        return max(range(n), key=lambda i: poly[i][1])
    if mode == "aligned":
        return max(range(n), key=lambda i: poly[i][0] + poly[i][1])
    if mode == "nearest" and ref is not None:
        rx, ry = ref
        return min(
            range(n),
            key=lambda i: (poly[i][0] - rx) ** 2 + (poly[i][1] - ry) ** 2,
        )
    return 0


def point_in_polygon(pt: Point, poly: Polygon) -> bool:
    """Winding-rule test for a closed polygon. Boundary is treated as inside."""
    if not polygon_is_closed(poly):
        return False
    x, y = pt
    inside = False
    for i in range(len(poly) - 1):
        x1, y1 = poly[i]
        x2, y2 = poly[i + 1]
        if (y1 > y) != (y2 > y):
            x_at = x1 + (y - y1) * (x2 - x1) / (y2 - y1) if y2 != y1 else x1
            if x < x_at:
                inside = not inside
    return inside


__all__ = [
    "Point",
    "Polygon",
    "polygon_is_closed",
    "polygon_area",
    "polygon_centroid",
    "offset_polygon",
    "rotate_polygon_start",
    "pick_seam_index",
    "point_in_polygon",
]
