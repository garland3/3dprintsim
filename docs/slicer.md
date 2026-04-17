# Slicer

Implementation: [`backend/app/slicer.py`](../backend/app/slicer.py).

The slicer is intentionally minimal: perimeter-only, no infill, no offsetting.
It has just enough output for the simulator to visualize something
believable.

## Inputs

```python
slice_meshes(
    meshes: list[Mesh],
    layer_height: float = 0.4,
    perimeters: int = 1,
    extrusion_per_mm: float = 0.04,
    travel_speed: float = 120.0,  # mm/s
    print_speed: float = 40.0,    # mm/s
) -> SliceResult
```

`meshes` are already placed (translated into bed coordinates) by
[`state.py`](../backend/app/state.py): each part's own min corner is at
its placement `(x, y)` and the part drops to `Z=0`.

## Algorithm

For each layer `Z = min_z + h/2, min_z + 3h/2, ...`:

1. **Plane intersection**: for every triangle where some vertex is above
   `Z` and some is below, compute the two points where the triangle's edges
   cross the plane. That produces a 2D segment per spanning triangle.
2. **Segment chaining**: quantize endpoints (`1e-3` tolerance) and greedily
   stitch segments head-to-tail into polylines. Loops close when the
   current polyline's head key matches its tail key. See
   [`_chain_segments`](../backend/app/slicer.py).
3. **Toolpath emission**: for each closed polyline, emit
   - one `travel` move to the start point,
   - one `extrude` move per subsequent vertex, accumulating `E` by
     `extrusion_per_mm × segment_length`.

   If `perimeters > 1`, the same polyline is traced that many times in
   place. (No perimeter offset — this is a simulator, not a production
   slicer.)

## Output

```python
@dataclass
class SliceResult:
    layer_height: float
    perimeters: int
    moves: list[Move]        # structured toolpath used by the viewer
    layers: list[LayerPaths] # per-layer polygons (for debugging/future UI)
    gcode: str               # rendered G-code string
```

Each `Move`:

```python
@dataclass
class Move:
    kind: Literal["travel", "extrude"]
    x: float
    y: float
    z: float
    e: float  # cumulative extruder position, mm
```

The frontend animates `moves` directly — no G-code re-parsing. The `gcode`
string is still available via `GET /api/gcode` for inspection.

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

- **No infill.** Parts are hollow perimeters only.
- **No overhang logic.** The slicer does not insert supports.
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

- **Infill**: intersect each layer's polygons with a rotating line pattern.
  Emit as additional `extrude` moves after the perimeters.
- **Shell offsetting**: `perimeters > 1` could inset each contour by the
  extrusion width (≈ 0.4 mm) rather than retracing.
- **Z-hop**: insert a quick travel move upward between contours.
- **Time estimation**: `travel_speed` and `print_speed` already exist;
  summing `distance / speed` per move gives a usable total print time.
