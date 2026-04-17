// PrinterScene: owns the Three.js renderer, camera, controls-less orbit, and
// the objects that represent the bed, the parts, and the running toolpath.
// The React layer only hands it data — this class is the sole Three.js user.

import * as THREE from 'three';
import { LineSegments2 } from 'three/addons/lines/LineSegments2.js';
import { LineSegmentsGeometry } from 'three/addons/lines/LineSegmentsGeometry.js';
import { LineMaterial } from 'three/addons/lines/LineMaterial.js';

// Number of most-recent extrude segments that get the "hot / freshly extruded"
// color ramp. Picked by eye — big enough to read, small enough to stay local.
const GLOW_SEGMENTS = 40;

export class PrinterScene {
  constructor(canvas) {
    this.canvas = canvas;

    this.renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
    this.renderer.setPixelRatio(window.devicePixelRatio || 1);
    this.renderer.setClearColor(0x0b0d10, 1);

    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(0x0b0d10);

    this.camera = new THREE.PerspectiveCamera(50, 1, 1, 5000);
    this.camera.position.set(400, 300, 400);
    this.camera.lookAt(0, 0, 0);

    this.scene.add(new THREE.AmbientLight(0xffffff, 0.5));
    const dir = new THREE.DirectionalLight(0xffffff, 0.7);
    dir.position.set(200, 400, 200);
    this.scene.add(dir);

    this.bedGroup = new THREE.Group();
    this.partsGroup = new THREE.Group();
    this.toolpathGroup = new THREE.Group();
    this.printedGroup = new THREE.Group();
    this.scene.add(this.bedGroup, this.partsGroup, this.toolpathGroup, this.printedGroup);

    // Hide the blue ghost toolpath by default — users asked to keep it off
    // unless they opt in.
    this.toolpathGroup.visible = false;

    this.head = this.makeHead();
    this.scene.add(this.head);
    this.head.visible = false;

    // simple orbit: LMB=rotate, wheel=zoom, shift+LMB or RMB=pan.
    // Must be initialized before setBed(), which reads _orbit.target.
    this._orbit = { azimuth: Math.PI / 4, polar: Math.PI / 3, radius: 500, target: new THREE.Vector3() };

    this.bedSize = [250, 210, 210];
    this.setBed(...this.bedSize);

    this._resize();
    window.addEventListener('resize', this._resize);

    this._applyOrbit();
    this._attachInput();

    this._raf = null;
    this._animate();
  }

  dispose() {
    cancelAnimationFrame(this._raf);
    window.removeEventListener('resize', this._resize);
    this._disposeGroup(this.toolpathGroup);
    this._disposeGroup(this.printedGroup);
    this.renderer.dispose();
  }

  _disposeGroup(group) {
    for (const obj of group.children) {
      if (obj.geometry && typeof obj.geometry.dispose === 'function') {
        obj.geometry.dispose();
      }
      if (obj.material && typeof obj.material.dispose === 'function') {
        obj.material.dispose();
      }
    }
  }

  _resize = () => {
    const rect = this.canvas.getBoundingClientRect();
    const w = Math.max(1, Math.floor(rect.width));
    const h = Math.max(1, Math.floor(rect.height));
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    // LineMaterial needs the viewport resolution to compute pixel line widths.
    if (this._ghostMat) this._ghostMat.resolution.set(w, h);
    if (this._printedMat) this._printedMat.resolution.set(w, h);
  };

  _animate = () => {
    this.renderer.render(this.scene, this.camera);
    this._raf = requestAnimationFrame(this._animate);
  };

  _applyOrbit() {
    const { azimuth, polar, radius, target } = this._orbit;
    const x = target.x + radius * Math.sin(polar) * Math.cos(azimuth);
    const z = target.z + radius * Math.sin(polar) * Math.sin(azimuth);
    const y = target.y + radius * Math.cos(polar);
    this.camera.position.set(x, y, z);
    this.camera.lookAt(target);
  }

  _attachInput() {
    const c = this.canvas;
    let dragging = false;
    let panning = false;
    let lastX = 0;
    let lastY = 0;
    c.addEventListener('mousedown', (e) => {
      // Right mouse button OR shift+left = pan; plain left = rotate.
      if (e.button === 2 || (e.button === 0 && e.shiftKey)) {
        panning = true;
        dragging = false;
      } else if (e.button === 0) {
        dragging = true;
        panning = false;
      } else {
        return;
      }
      lastX = e.clientX;
      lastY = e.clientY;
      e.preventDefault();
    });
    // Suppress the browser context menu so RMB pan feels natural.
    c.addEventListener('contextmenu', (e) => e.preventDefault());
    window.addEventListener('mouseup', () => { dragging = false; panning = false; });
    window.addEventListener('mousemove', (e) => {
      if (!dragging && !panning) return;
      const dx = e.clientX - lastX;
      const dy = e.clientY - lastY;
      lastX = e.clientX;
      lastY = e.clientY;
      if (panning) {
        // Pan in the camera's own X/Y plane so dragging feels 1:1 regardless
        // of the current orbit angle.
        const panScale = this._orbit.radius * 0.0015;
        const right = new THREE.Vector3();
        const up = new THREE.Vector3();
        this.camera.matrixWorld.extractBasis(right, up, new THREE.Vector3());
        this._orbit.target.addScaledVector(right, -dx * panScale);
        this._orbit.target.addScaledVector(up, dy * panScale);
      } else {
        // CAD-style: dragging the cursor RIGHT rotates the view RIGHT (camera
        // orbits in the same direction the user drags).
        this._orbit.azimuth -= dx * 0.008;
        this._orbit.polar = Math.max(0.1, Math.min(Math.PI - 0.1, this._orbit.polar + dy * 0.008));
      }
      this._applyOrbit();
    });
    c.addEventListener('wheel', (e) => {
      e.preventDefault();
      const scale = e.deltaY > 0 ? 1.1 : 1 / 1.1;
      this._orbit.radius = Math.max(50, Math.min(3000, this._orbit.radius * scale));
      this._applyOrbit();
    }, { passive: false });
  }

  makeHead() {
    // Cone with its apex (nozzle tip) at local origin pointing down, body
    // extending +Y so the mesh's world position is the nozzle tip itself.
    const geo = new THREE.ConeGeometry(4, 10, 16);
    geo.rotateX(Math.PI);
    geo.translate(0, 5, 0);
    const mat = new THREE.MeshStandardMaterial({ color: 0xff5522, emissive: 0x441100 });
    const mesh = new THREE.Mesh(geo, mat);
    mesh.name = 'head';
    return mesh;
  }

  setBed(x, y, z) {
    this.bedSize = [x, y, z];
    this.bedGroup.clear();

    // Bed plate at Y=0, centered on origin, spanning X × Z (mm).
    const plate = new THREE.Mesh(
      new THREE.BoxGeometry(x, 1, y),
      new THREE.MeshStandardMaterial({ color: 0x1a1f25, roughness: 0.9 }),
    );
    plate.position.set(x / 2, -0.5, y / 2);
    this.bedGroup.add(plate);

    const grid = new THREE.GridHelper(Math.max(x, y), Math.max(x, y) / 10, 0x2a313a, 0x1a1f25);
    grid.position.set(x / 2, 0.01, y / 2);
    this.bedGroup.add(grid);

    // Volume outline as a box wireframe
    const boxGeo = new THREE.BoxGeometry(x, z, y);
    const edges = new THREE.EdgesGeometry(boxGeo);
    const line = new THREE.LineSegments(edges, new THREE.LineBasicMaterial({ color: 0x2a82e4, transparent: true, opacity: 0.35 }));
    line.position.set(x / 2, z / 2, y / 2);
    this.bedGroup.add(line);

    // Recenter camera target on the bed
    this._orbit.target.set(x / 2, z / 4, y / 2);
    this._orbit.radius = Math.max(x, y) * 2;
    this._applyOrbit();
  }

  // parts: [{id, name, size, placement}], geometry dict keyed by id -> triangle list
  setParts(parts, geometryById) {
    this.partsGroup.clear();
    for (const part of parts) {
      const geomData = geometryById[part.id];
      if (!geomData) continue;
      const positions = [];
      for (const tri of geomData.triangles) {
        for (const v of tri) {
          // Backend uses (x, y, z) where z is up. Three.js default is y up,
          // so map (x, z, y) when we push into the buffer.
          positions.push(v[0], v[2], v[1]);
        }
      }
      const geom = new THREE.BufferGeometry();
      geom.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
      geom.computeVertexNormals();
      const mat = new THREE.MeshStandardMaterial({
        color: 0x6fb7ff,
        transparent: true,
        opacity: 0.65,
        side: THREE.DoubleSide,
      });
      const mesh = new THREE.Mesh(geom, mat);
      mesh.userData.partId = part.id;
      this.partsGroup.add(mesh);
    }
  }

  // moves: full list from the slice endpoint (kind, x, y, z, e)
  setToolpath(moves) {
    // Dispose GPU resources from the previous slice before clearing the
    // Three.js groups; LineSegmentsGeometry/LineMaterial don't release their
    // InstancedInterleavedBuffer / shader program otherwise.
    this._disposeGroup(this.toolpathGroup);
    this._disposeGroup(this.printedGroup);
    this.toolpathGroup.clear();
    this.printedGroup.clear();
    this._moves = moves || [];
    this._lastCursor = 0;

    if (!moves || moves.length === 0) {
      this.head.visible = false;
      // Clearing / reloading after a sim ran needs to undo the "hide the source
      // mesh during printing" flag that setCursor sets — otherwise newly
      // uploaded parts render invisible until the user scrubs back to 0.
      this.partsGroup.visible = true;
      this._ghostMat = null;
      this._printedMat = null;
      this._printedPositions = null;
      this._printedColors = null;
      this._extrudeSegments = null;
      this._printedVertCount = 0;
      return;
    }

    // Pre-compute every extrude segment once. The move index is retained so
    // setCursor can map a simulation cursor back to a segment count.
    const segs = [];
    for (let i = 0; i < moves.length - 1; i++) {
      const a = moves[i];
      const b = moves[i + 1];
      if (b.kind !== 'extrude') continue;
      // Three.js Y is up; backend (x,y,z) uses z as up, so swap here.
      segs.push({
        moveIndex: i + 1,
        ax: a.x, ay: a.z, az: a.y,
        bx: b.x, by: b.z, bz: b.y,
      });
    }
    this._extrudeSegments = segs;

    const rect = this.canvas.getBoundingClientRect();
    const resW = Math.max(1, Math.floor(rect.width));
    const resH = Math.max(1, Math.floor(rect.height));

    // --- ghost: the full toolpath drawn as a dim, thick-ish line ---
    const ghostPositions = new Float32Array(segs.length * 6);
    for (let i = 0; i < segs.length; i++) {
      const s = segs[i];
      const o = i * 6;
      ghostPositions[o] = s.ax; ghostPositions[o + 1] = s.ay; ghostPositions[o + 2] = s.az;
      ghostPositions[o + 3] = s.bx; ghostPositions[o + 4] = s.by; ghostPositions[o + 5] = s.bz;
    }
    const ghostGeom = new LineSegmentsGeometry();
    ghostGeom.setPositions(ghostPositions);
    this._ghostMat = new LineMaterial({
      color: 0x2a82e4,
      linewidth: 1.2,
      transparent: true,
      opacity: 0.22,
      depthTest: true,
    });
    this._ghostMat.resolution.set(resW, resH);
    const ghostLines = new LineSegments2(ghostGeom, this._ghostMat);
    ghostLines.computeLineDistances();
    this.toolpathGroup.add(ghostLines);

    // --- printed: the deposited filament. We pre-allocate the scratch arrays
    // but DON'T create a LineSegments2 yet — setCursor rebuilds it from scratch
    // each call because calling setPositions() on an existing
    // LineSegmentsGeometry leaves Three.js's instanced-buffer state out of
    // sync in r0.162 (the underlying buffer is swapped but the renderer keeps
    // drawing the first frame's attribute). Rebuilding is cheap at our sizes.
    this._printedPositions = new Float32Array(segs.length * 6);
    this._printedColors = new Float32Array(segs.length * 6);
    this._printedMat = new LineMaterial({
      linewidth: 3.5,
      vertexColors: true,
      transparent: false,
      depthTest: true,
    });
    this._printedMat.resolution.set(resW, resH);
    this._printedVertCount = 0;

    this.head.visible = true;
    this.setCursor(0);
  }

  setCursor(cursor) {
    if (!this._moves || this._moves.length === 0) return;
    const moves = this._moves;
    cursor = Math.max(0, Math.min(moves.length, cursor));
    const segs = this._extrudeSegments || [];

    // How many extrude segments have completed by this cursor. `moveIndex` is
    // the index of the move that *terminates* the segment; the backend's
    // cursor is the next move to execute, so a segment is complete only once
    // cursor has advanced strictly past it (matches the original i<cursor-1).
    let visibleSegs = 0;
    for (let i = 0; i < segs.length; i++) {
      if (segs[i].moveIndex < cursor) visibleSegs = i + 1;
      else break;
    }

    // Fill positions + color ramp; the last GLOW_SEGMENTS get a hot-deposit
    // color that fades into the settled filament tone. We write into our
    // reusable scratch arrays, then build a fresh LineSegmentsGeometry whose
    // size matches the visible prefix.
    const pos = this._printedPositions;
    const col = this._printedColors;
    const deposited = [0.95, 0.42, 0.18]; // settled filament: orange
    const hot = [1.0, 0.95, 0.55];        // just-deposited: near-white yellow
    for (let i = 0; i < visibleSegs; i++) {
      const s = segs[i];
      const o = i * 6;
      pos[o] = s.ax; pos[o + 1] = s.ay; pos[o + 2] = s.az;
      pos[o + 3] = s.bx; pos[o + 4] = s.by; pos[o + 5] = s.bz;

      const fromEnd = visibleSegs - 1 - i;
      const t = fromEnd < GLOW_SEGMENTS ? 1 - fromEnd / GLOW_SEGMENTS : 0;
      const r = deposited[0] + (hot[0] - deposited[0]) * t;
      const g = deposited[1] + (hot[1] - deposited[1]) * t;
      const b = deposited[2] + (hot[2] - deposited[2]) * t;
      col[o] = r; col[o + 1] = g; col[o + 2] = b;
      col[o + 3] = r; col[o + 4] = g; col[o + 5] = b;
    }

    // During simulation hide the translucent source mesh so the fresh filament
    // is the only thing on screen; when reset to cursor 0 the mesh reappears.
    this.partsGroup.visible = visibleSegs === 0;

    this._disposeGroup(this.printedGroup);
    this.printedGroup.clear();
    if (visibleSegs > 0 && this._printedMat) {
      // Fresh allocations so Three.js builds a clean InstancedInterleavedBuffer
      // for this frame; attempting to swap buffers on an existing
      // LineSegmentsGeometry produced stale renders in r0.162.
      const posView = new Float32Array(pos.buffer, pos.byteOffset, visibleSegs * 6);
      const colView = new Float32Array(col.buffer, col.byteOffset, visibleSegs * 6);
      const geom = new LineSegmentsGeometry();
      geom.setPositions(posView);
      geom.setColors(colView);
      const lines = new LineSegments2(geom, this._printedMat);
      this.printedGroup.add(lines);
    }
    this._printedVertCount = visibleSegs * 2;

    const head = moves[Math.min(cursor, moves.length - 1)];
    if (head) {
      // Apex sits 0.5mm above the current layer z so the nozzle is visibly
      // poised just over the last deposited segment instead of buried in it.
      this.head.position.set(head.x, head.z + 0.5, head.y);
    }
    this._lastCursor = cursor;
  }

  setToolpathVisible(visible) {
    this.toolpathGroup.visible = !!visible;
  }

  // Frame the camera on the loaded parts (or bed, if none) so they fill ~90%
  // of the viewport. Keeps the current azimuth/polar, only adjusts target and
  // radius so the user's orientation isn't disturbed.
  focus() {
    const bbox = new THREE.Box3();
    if (this.partsGroup.children.length > 0) {
      bbox.makeEmpty();
      for (const child of this.partsGroup.children) {
        const childBox = new THREE.Box3().setFromObject(child);
        bbox.union(childBox);
      }
    }
    if (bbox.isEmpty()) {
      // Fall back to the bed volume so the button is never a no-op.
      const [bx, by, bz] = this.bedSize;
      bbox.set(
        new THREE.Vector3(0, 0, 0),
        new THREE.Vector3(bx, bz, by),
      );
    }

    const center = bbox.getCenter(new THREE.Vector3());
    const size = bbox.getSize(new THREE.Vector3());

    // Pick a radius that places the largest dimension at ~90% of the smaller
    // viewport axis. Using the vertical half-fov as the limiting factor keeps
    // the fit correct even when the viewer is tall.
    const aspect = this.camera.aspect || 1;
    const vFov = (this.camera.fov * Math.PI) / 180;
    const hFov = 2 * Math.atan(Math.tan(vFov / 2) * aspect);
    const halfV = Math.max(size.x, size.y, size.z) / 2;
    const halfH = halfV;
    const distV = halfV / Math.tan(vFov / 2);
    const distH = halfH / Math.tan(hFov / 2);
    const fillRatio = 0.9;
    const radius = Math.max(distV, distH) / fillRatio;

    this._orbit.target.copy(center);
    this._orbit.radius = Math.max(50, Math.min(3000, radius));
    this._applyOrbit();
  }

  // Accessor for tests to verify what got rendered.
  stats() {
    return {
      bed: [...this.bedSize],
      parts: this.partsGroup.children.length,
      hasToolpath: this.toolpathGroup.children.length > 0,
      printedVerts: this._printedVertCount || 0,
      head: this.head.visible
        ? [this.head.position.x, this.head.position.y, this.head.position.z]
        : null,
    };
  }
}
