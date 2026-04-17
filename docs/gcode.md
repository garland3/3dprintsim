# G-code output

The slicer emits a small, readable G-code flavour suitable for the simulator.
It is produced by [`_render_gcode`](../backend/app/slicer.py) and available
via `GET /api/gcode` once you have sliced.

## Header

Every slice starts with the same prelude:

```
; 3dprintsim generated toolpath
G21 ; mm units
G90 ; absolute
M82 ; extruder absolute
G28 ; home
```

## Body

Each `Move` becomes one line:

| Kind | Line format |
|---|---|
| `travel` | `G0 X<x> Y<y> Z<z> F<feed>` |
| `extrude` | `G1 X<x> Y<y> Z<z> E<cum> F<feed>` |

Feed rates are only emitted when the move kind changes (travel ↔ extrude),
to cut noise. `E` is cumulative in millimetres, `extrusion_per_mm * distance`
per extrude segment.

## Example (first layer of a 20 mm cube, 1 mm layer height)

```
; 3dprintsim generated toolpath
G21 ; mm units
G90 ; absolute
M82 ; extruder absolute
G28 ; home
G0 X0.000 Y0.000 Z5.000 F7200
G0 X5.000 Y5.000 Z0.500 F7200
G1 X25.000 Y5.000 Z0.500 E0.8000 F2400
G1 X25.000 Y25.000 Z0.500 E1.6000
G1 X5.000 Y25.000 Z0.500 E2.4000
G1 X5.000 Y5.000 Z0.500 E3.2000
G0 X5.000 Y5.000 Z1.500 F7200
G1 X25.000 Y5.000 Z1.500 E4.0000 F2400
...
```

Reading it: the head hops up to `Z=5` at home, travels to the start of the
first layer at `Z=0.5`, traces the perimeter (four `G1`s at constant `Z`,
monotonically increasing `E`), then lifts to the next layer.

## Caveats

- This isn't a physically accurate printer dialect — no `M104`/`M109`
  temperature commands, no retraction, no fan settings.
- Feed rates come from `travel_speed` and `print_speed` constants in
  [`slicer.py`](../backend/app/slicer.py) (mm/s, converted to mm/min on
  emission).
- If `perimeters > 1`, the same polyline is retraced that many times. No
  offsetting is performed.
