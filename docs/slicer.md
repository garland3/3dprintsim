# Slicer

Implementation: [`backend/app/slicer.py`](../backend/app/slicer.py).

The slicer produces perimeters, sparse/solid infill, and auto-generated
supports with raster-based top/bottom/overhang detection. See
[`surfaces-and-supports.md`](./surfaces-and-supports.md) for the full
surface-classification pipeline and [`depth-visualization.md`](./depth-visualization.md)
for how emitted move roles feed the viewer's color ramp.

## Inputs

```python
slice_meshes(
    meshes: list[Mesh],
    layer_height: float = 0.4,
    perimeters: int = 1,
    infill_density: float = 0.2,     # 0..1 sparse fill fraction
    top_layers: int = 3,             # solid-surface lookup window above
    bottom_layers: int = 3,          # and below
    nozzle_width: float = 0.4,       # mm; spacing = nozzle_width / infill_density
    support_density: float = 0.25,   # 0 disables supports
    extrusion_per_mm: float = 0.04,
    travel_speed: float = 120.0,     # mm/s
    print_speed: float = 40.0,       # mm/s
) -> SliceResult
```

`meshes` are already placed (translated into bed coordinates) by
[`state.py`](../backend/app/state.py): each part's own min corner is at
its placement `(x, y)` and the part drops to `Z=0`.

## Algorithm

The slicer runs in two passes. Pass 1 slices and rasterizes every mesh at
every layer; pass 2 walks the layers in order and emits the toolpath using
the full mask stack.

### Pass 1: slice + rasterize

For each mesh at each layer `Z = min_z + h/2, min_z + 3h/2, ...`:

1. **Plane intersection**: for every triangle where some vertex is above
   `Z` and some is below, compute the two points where the triangle's edges
   cross the plane. That produces a 2D segment per spanning triangle.
2. **Segment chaining**: quantize endpoints (`1e-3` tolerance) and greedily
   stitch segments head-to-tail into polylines. Loops close when the
   current polyline's head key matches its tail key. See
   [`_chain_segments`](../backend/app/slicer.py).
3. **Rasterize**: the closed polylines are converted to a `frozenset[Cell]`
   at a 2 mm grid (see [`app/surfaces.py`](../backend/app/surfaces.py)).
   The full per-mesh mask stack drives every surface classification in
   the next pass.

### Pass 2: emission

For each layer, for each mesh:

1. **Surface classification**: compute `top_cells`, `bottom_cells`, and
   `overhang_cells` from the mask stack. Their union decides whether this
   layer's infill runs solid (100%) or sparse (`nozzle_width / infill_density`).
2. **Perimeters**: trace each polyline `perimeters` times (or `perimeters+1`
   on layers that contain overhang cells — a well-tested "extra perimeter
   on overhang" trick). Each extrude move is tagged `perimeter` or
   `overhang_perimeter`.
3. **Infill**: rectilinear scan-line fill clipped to the contours. The
   role on each extrude move is one of `infill_sparse`, `bottom`, or
   `top` based on the classification above (solid fills get `top` or
   `bottom` depending on which surface they serve).
4. **Supports**: every cell in the per-layer support set (computed once
   by `compute_support_cells()`) emits a short stub at the cell center.
   Stubs sub-sample by `support_density` — 0.25 → every 4th cell on the
   grid gets a stub.

## Output

```python
@dataclass
class SliceResult:
    layer_height: float
    perimeters: int
    moves: list[Move]              # structured toolpath used by the viewer
    layers: list[LayerPaths]       # per-layer polygons (for debugging/future UI)
    gcode: str                     # rendered G-code string
    infill_density: float
    top_layers: int
    bottom_layers: int
    support_density: float
    support_cell_count: int        # total cells with supports across all layers
```

Each `Move`:

```python
@dataclass
class Move:
    kind: Literal["travel", "extrude"]
    x: float
    y: float
    z: float
    e: float   # cumulative extruder position, mm
    role: str  # "perimeter" | "overhang_perimeter" | "infill_sparse" |
               # "bottom" | "top" | "support" | "travel"
```

The frontend animates `moves` directly — no G-code re-parsing. The role
field drives the viewer color ramp (see
[`depth-visualization.md`](./depth-visualization.md)). The `gcode` string
is still available via `GET /api/gcode` for inspection.

## G-code dialect

Minimal and readable. The header is the same for every slice:

```
; 3dprintsim generated toolpath
G21 ; mm units
G90 ; absolute
M82 ; extruder absolute
G28 ; home
```

Travel moves use `G0` and print moves use `G1 ... E<pos>`. Feed rates are
emitted on each move-kind transition (travel ↔ extrude). See
[`gcode.md`](./gcode.md) for an annotated example.

## Where it's lossy

- **Grid-based supports.** The support grid is 2 mm; sub-millimeter
  overhangs are not detected. Support stubs are also simple grid-aligned
  lines, not tree-support towers.
- **Solid infill is per-layer, not per-region.** If any meaningful fraction
  of a layer is flagged as top/bottom/overhang, the whole layer runs
  solid. That over-prints slightly (a middle cross-section with a single
  ceiling cell still gets full density) but avoids polygon Boolean ops.
- **Floating-point thresholds.** `EPS = 1e-9` for plane classification;
  `1e-6` for vertex-coincidence dedup when a triangle touches the plane at
  a vertex. Degenerate inputs (edges exactly in-plane) skip emitting a
  crossing and rely on a neighbouring edge to supply the point.
- **Segment chaining is greedy.** Self-intersecting contours or very thin
  shells can produce open polylines; those are traced as-is, not closed.

For the demo parts this is fine. If you swap in complex meshes, expect
occasional open loops — the simulator still renders them; they just won't
form a closed bead.

## Extending

Good entry points:

- **Shell offsetting**: `perimeters > 1` could inset each contour by the
  extrusion width (≈ 0.4 mm) rather than retracing.
- **Z-hop**: insert a quick travel move upward between contours.
- **Time estimation**: `travel_speed` and `print_speed` already exist;
  summing `distance / speed` per move gives a usable total print time.
- **Region-aware solid infill**: current logic is per-layer; with a
  polygon Boolean library you could split each layer into solid and
  sparse regions (intersection of solid cells with the contour).
- **Tree supports**: the mask stack already has everything you need —
  cluster support cells and emit tapered pillars instead of grid stubs.
