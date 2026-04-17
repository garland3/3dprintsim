# Frontend

React + Three.js, bundled by Vite. Two main files:

- [`frontend/src/App.jsx`](../frontend/src/App.jsx) — UI, state, API calls.
- [`frontend/src/PrinterScene.js`](../frontend/src/PrinterScene.js) — the
  only place that imports `three`. Owns the renderer, orbit camera, and
  scene graph.

## Split of responsibilities

`App.jsx` never touches Three.js. It:

1. Fetches printer state from the backend.
2. Renders the sidebar (bed config, dropzone, parts list, slicer, sim
   controls).
3. Hands raw JSON data to `PrinterScene` via setters:
   `setBed(x, y, z)`, `setParts(parts, geometryById)`,
   `setToolpath(moves)`, `setCursor(cursor)`.

The scene rebuilds its buffers from scratch inside those setters. React
never observes Three.js state and vice versa — which avoids the usual
React-Three coupling headaches.

## Scene graph

```
Scene
├── AmbientLight + DirectionalLight
├── bedGroup        — plate + grid + wireframe build volume
├── partsGroup      — one Mesh per loaded part
├── toolpathGroup   — dim-blue LineSegments ghost of the full toolpath
├── printedGroup    — bright-orange LineSegments of moves extruded so far
└── head            — orange cone; position follows cursor
```

`printedGroup` reuses a single `Float32BufferAttribute` whose
`setDrawRange` is updated in `setCursor`. That keeps scrubbing cheap — we
never allocate new buffers per frame.

## Orbit camera

There's no OrbitControls dependency; [`_attachInput`](../frontend/src/PrinterScene.js)
implements minimal drag-to-rotate / wheel-to-zoom / shift-drag-to-pan
against a spherical `{azimuth, polar, radius, target}` state. `setBed`
re-centers `target` on the new build volume.

## Coordinate conversion

Backend sends `(x, y, z)` with **Z up**. Three.js uses **Y up** by default.
The scene re-maps on ingest only — anything pushed into a buffer becomes
`(x, z, y)`:

```js
positions.push(v[0], v[2], v[1]);       // parts
positions.push(a.x, a.z, a.y, b.x, b.z, b.y);  // toolpath
this.head.position.set(head.x, head.z + 3, head.y);
```

Make sure any new code that consumes backend geometry does the same swap.

## Test hooks

`window.__printerScene` is set on mount, exposing:

- `stats()` — `{bed, parts, hasToolpath, printedVerts, head}` used by the
  Playwright tests to assert what the renderer actually produced.
- `_orbit` — mutable camera state; the docs screenshot script pokes it to
  frame close-ups.

These are debug conveniences; don't rely on them from application code.

## Styling

All styles live in [`frontend/src/styles.css`](../frontend/src/styles.css).
Dark background, single-column sidebar, no CSS framework.
