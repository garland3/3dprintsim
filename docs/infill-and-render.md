# Infill, solid top/bottom, and the glowing toolpath render

This page covers three changes that make the simulator feel like a real FDM
printer instead of a perimeter-only wireframe: rectilinear infill, per-part
solid top/bottom layers, and a thick screen-space toolpath render with a
hot-deposit color ramp. Plus the UX tweak that makes them visible: parts now
auto-center on the bed.

## Auto-centering

![auto-centered upload](./screenshots/13-auto-centered-upload.png)

Uploading an STL used to drop the part at `(0, 0)` until you clicked
**Auto-arrange**. Now:

- A single upload gets a centered `Placement` immediately. A 20 mm cube on a
  250 × 210 bed lands with its min-corner at `(115, 95)` — exactly centered.
- Multi-part uploads re-run `arrange()` and the packed block is shifted so
  its bbox is centered on the plate (you don't end up hugging the origin).
- If the part doesn't fit the current bed (width or depth exceeds
  `bed − 2 × margin`), `placement` stays `None`. The part still shows up in
  the sidebar so the user sees it, but `POST /api/slice` returns HTTP 409 with
  a clear "does not fit" message instead of silently emitting an out-of-bounds
  toolpath.

## Infill + solid top/bottom

![slice summary](./screenshots/14-slice-with-infill-stats.png)

The slicer takes four new parameters (`infill_density`, `top_layers`,
`bottom_layers`, `nozzle_width`), all exposed on `POST /api/slice` and the
MCP `slice_all` tool.

For each layer we already had the contour polylines. On top of that:

1. **Solid vs sparse** decision, per mesh, per layer. Each mesh's own layer
   range is tracked, so a 4 mm part printed next to a 20 mm part still gets
   its own solid top — without this fix the global `layer_index` meant the
   short part's top window never triggered.
2. **Scan-line rectilinear fill.** A horizontal (even layers) or vertical
   (odd) scan line sweeps through the layer at nozzle-width spacing. For each
   scan line we compute the X/Y crossings of every closed contour, sort them,
   and emit segments between pairs (even-odd fill rule).
3. **Zig-zag routing.** Every other row reverses direction, so the head
   doesn't jump back to X-min between rows — this is what real slicers do
   and it cuts travel time.
4. **Spacing.** Solid layers use `nozzle_width` spacing (≈ 100% fill);
   sparse layers use `nozzle_width / infill_density`, e.g. 0.4 / 0.2 = 2 mm.

Move-count delta for a 20 mm cube at 0.4 mm layers with the default
`infill_density=0.2`, `top_layers=3`, `bottom_layers=3`:

| mode | layers | moves | extrusion |
|---|---|---|---|
| perimeters only (old default) | 51 | 451 | 160 mm |
| perimeters + 20% infill + 3 solid top/bottom | 51 | 1851 | 720 mm |

![finished print showing dense solid top layers](./screenshots/12-finished-print.png)

## Thick glowing toolpath render

![mid-print glow](./screenshots/11-mid-print-glow.png)

The toolpath render switched from 1-pixel `LineSegments` to
`LineSegments2` + `LineMaterial` (Three.js examples/jsm `lines`), which draws
screen-space thick lines via a custom shader.

Two layers are drawn:

- **Ghost toolpath** — every extrude segment in the slice, as a dim 1.2 px
  blue line at 0.22 opacity. Shows where the head will go.
- **Printed filament** — everything the cursor has passed, as a 3.5 px line
  with per-vertex colors. The color ramp is a simple linear interpolation
  between `(0.95, 0.42, 0.18)` ("settled filament") and `(1.0, 0.95, 0.55)`
  ("just deposited") over the last `GLOW_SEGMENTS` (40) segments. That gives
  the visible hot-yellow front that fades to orange behind the nozzle.

### An r0.162 gotcha

`LineSegmentsGeometry.setPositions()` swaps out its underlying
`InstancedInterleavedBuffer`, but calling it on an already-rendered geometry
leaves the Three.js renderer drawing the first frame's attribute and the new
data never shows up. The fix: rebuild the `LineSegments2` from scratch each
`setCursor`, disposing the previous geometry. At ~1 k segments it's cheap
and it's correct.

See `frontend/src/PrinterScene.js` — the comment above the `setCursor`
rebuild loop flags the bug.
