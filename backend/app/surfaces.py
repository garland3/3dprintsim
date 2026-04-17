"""Raster-mask helpers for surface classification and support generation.

Per-layer 2D polygons are rasterized onto a shared integer grid so top/bottom
surfaces, overhangs, and support columns can be expressed as plain set
operations. The grid resolution trades accuracy for memory: 2 mm catches the
overhangs a hobbyist printer cares about while keeping O(layers × cells)
storage comfortable for parts up to roughly one build volume in size.
"""

from __future__ import annotations

from typing import Iterable


Cell = tuple[int, int]


# Default support grid resolution (mm). Matches typical sparse-infill spacing
# on a 0.4 mm nozzle at ~20% density — fine enough to catch real overhangs
# without exploding memory on tall parts.
SUPPORT_RESOLUTION = 2.0


def _is_closed(poly: list[tuple[float, float]]) -> bool:
    return (
        len(poly) >= 4
        and abs(poly[0][0] - poly[-1][0]) < 1e-6
        and abs(poly[0][1] - poly[-1][1]) < 1e-6
    )


def _scanline_crossings_y(
    polys: list[list[tuple[float, float]]],
    y: float,
) -> list[float]:
    """Sorted X-coordinates where a horizontal scan line at `y` crosses the
    boundary of any polygon in `polys`. Uses the half-open vertex rule to
    avoid double-counting corners that lie exactly on the scan line.
    """
    out: list[float] = []
    for poly in polys:
        for i in range(len(poly) - 1):
            ya, yb = poly[i][1], poly[i + 1][1]
            if ya == yb:
                continue
            if (ya <= y < yb) or (yb <= y < ya):
                t = (y - ya) / (yb - ya)
                out.append(poly[i][0] + t * (poly[i + 1][0] - poly[i][0]))
    out.sort()
    deduped: list[float] = []
    for v in out:
        if not deduped or abs(v - deduped[-1]) > 1e-6:
            deduped.append(v)
    return deduped


def rasterize_polygons(
    polylines: Iterable[list[tuple[float, float]]],
    res: float = SUPPORT_RESOLUTION,
) -> frozenset[Cell]:
    """Return the set of grid cells whose center lies inside any closed
    polygon in `polylines`. Cells are indexed by `floor(center/res)` so masks
    from different layers can be subtracted directly.
    """
    closed = [p for p in polylines if _is_closed(p)]
    if not closed:
        return frozenset()
    ys = [pt[1] for poly in closed for pt in poly]
    ymin, ymax = min(ys), max(ys)

    mask: set[Cell] = set()
    row_lo = int(ymin // res)
    row_hi = int(ymax // res) + 1
    for row in range(row_lo, row_hi + 1):
        y = (row + 0.5) * res
        if y < ymin or y > ymax:
            continue
        crossings = _scanline_crossings_y(closed, y)
        for i in range(0, len(crossings) - 1, 2):
            x0, x1 = crossings[i], crossings[i + 1]
            col_lo = int(x0 // res)
            col_hi = int(x1 // res) + 1
            for col in range(col_lo, col_hi + 1):
                cx = (col + 0.5) * res
                if x0 <= cx <= x1:
                    mask.add((col, row))
    return frozenset(mask)


def overhang_cells(current: frozenset[Cell], previous: frozenset[Cell]) -> frozenset[Cell]:
    """Cells present in `current` but absent in `previous` — i.e., newly
    floating material that has nothing directly below from the prior layer.
    """
    return current - previous


def top_surface_cells(
    layer_masks: list[frozenset[Cell]],
    layer_index: int,
    top_layers: int,
) -> frozenset[Cell]:
    """Cells in layer L whose column becomes void within `top_layers` layers
    above L — i.e., that material is part of a ceiling.

    Computed as mask[L] minus the intersection of the next `top_layers`
    masks; if any of those masks is missing (we're near the top of the
    model) the whole layer counts as a top surface.
    """
    if top_layers <= 0 or layer_index >= len(layer_masks):
        return frozenset()
    here = layer_masks[layer_index]
    above: frozenset[Cell] | None = None
    for k in range(1, top_layers + 1):
        idx = layer_index + k
        if idx >= len(layer_masks):
            return here
        m = layer_masks[idx]
        above = m if above is None else above & m
    return here - (above or frozenset())


def bottom_surface_cells(
    layer_masks: list[frozenset[Cell]],
    layer_index: int,
    bottom_layers: int,
) -> frozenset[Cell]:
    """Cells in layer L with void within `bottom_layers` layers below —
    either the bed (layer_index < bottom_layers) or an overhang in a
    concave part."""
    if bottom_layers <= 0 or layer_index < 0:
        return frozenset()
    here = layer_masks[layer_index]
    below: frozenset[Cell] | None = None
    for k in range(1, bottom_layers + 1):
        idx = layer_index - k
        if idx < 0:
            return here
        m = layer_masks[idx]
        below = m if below is None else below & m
    return here - (below or frozenset())


def compute_support_cells(
    layer_masks: list[frozenset[Cell]],
    min_overhang_cells: int = 1,
) -> list[set[Cell]]:
    """Per-layer support cell sets for the given part masks.

    For every overhang cell at layer L (a cell in L but not L-1), walk
    downward marking each empty layer as needing a support column there,
    until the walk hits a layer that already has material at that cell
    (supports always rest on the layer below).

    `min_overhang_cells` ignores isolated single-cell overhangs that tend
    to be numerical chatter at the triangle edges rather than real features.
    """
    layer_count = len(layer_masks)
    out: list[set[Cell]] = [set() for _ in range(layer_count)]
    if layer_count < 2:
        return out

    for L in range(1, layer_count):
        new = layer_masks[L] - layer_masks[L - 1]
        if len(new) < min_overhang_cells:
            continue
        for cell in new:
            # Walk down until we find a supporting solid or hit the bed.
            for z in range(L - 1, -1, -1):
                if cell in layer_masks[z]:
                    break
                out[z].add(cell)
    return out


__all__ = [
    "Cell",
    "SUPPORT_RESOLUTION",
    "rasterize_polygons",
    "overhang_cells",
    "top_surface_cells",
    "bottom_surface_cells",
    "compute_support_cells",
]
