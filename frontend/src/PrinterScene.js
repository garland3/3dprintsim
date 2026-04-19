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

// Palette for role-based coloring. The viewer's biggest readability problem is
// that a single-color toolpath turns into an orange blob — the eye can't parse
// depth or distinguish structural features. We color by role (perimeter / infill
// / support / overhang) and then blend a Z-height ramp on top so the shape is
// legible from any angle.
//
// Colors are linear RGB triples in [0, 1] because we feed them directly to a
// LineMaterial with vertexColors enabled.
const ROLE_BASE = {
  perimeter:          [0.18, 0.62, 0.95],   // cool cyan/blue: walls
  overhang_perimeter: [1.00, 0.38, 0.18],   // hot orange: unsupported edges pop
  infill_sparse:      [0.55, 0.55, 0.62],   // dim grey: interior lattice
  bottom:             [0.95, 0.55, 0.20],   // amber: bottom/overhang fills
  top:                [0.85, 0.90, 0.45],   // pale yellow-green: ceilings
  support:            [0.35, 0.80, 0.55],   // muted green: easy to subtract visually
};
const ROLE_DEFAULT = [0.95, 0.42, 0.18];   // fallback to the old orange
const HOT_COLOR = [1.0, 0.98, 0.75];       // just-extruded glow

// Depth ramp: each segment's base color is biased brighter as Z grows so the
// eye reads height even on single-role prints. The shift is small (±20% per
// channel) to keep role identity intact.
const DEPTH_LIGHTEN = 0.35;
const DEPTH_DARKEN = 0.30;

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
    // Hide part meshes while simulation is progressing so printed filament
    // reads cleanly. Flipped by `setPartsSimVisible` from the React side.
    this._partsSimVisible = false;

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
    if (this._inputHandlers) {
      const { mousedown, contextmenu, wheel, mouseup, mousemove } = this._inputHandlers;
      this.canvas.removeEventListener('mousedown', mousedown);
      this.canvas.removeEventListener('contextmenu', contextmenu);
      this.canvas.removeEventListener('wheel', wheel);
      window.removeEventListener('mouseup', mouseup);
      window.removeEventListener('mousemove', mousemove);
      this._inputHandlers = null;
    }
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
    const panRight = new THREE.Vector3();
    const panUp = new THREE.Vector3();
    const panFwd = new THREE.Vector3();

    // Per-drag state for part-repositioning. When a plain-left click lands
    // on a part mesh we suppress orbit for that gesture and translate the
    // mesh along the bed plane instead.
    const raycaster = new THREE.Raycaster();
    const ndc = new THREE.Vector2();
    const bedPlane = new THREE.Plane(new THREE.Vector3(0, 1, 0), 0);
    const hit = new THREE.Vector3();
    this._partDrag = null; // { mesh, partId, baseX, baseZ, anchorX, anchorZ }

    const pointerToBed = (e) => {
      const rect = c.getBoundingClientRect();
      ndc.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
      ndc.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
      raycaster.setFromCamera(ndc, this.camera);
      return raycaster.ray.intersectPlane(bedPlane, hit) ? hit.clone() : null;
    };

    const pickPart = (e) => {
      const rect = c.getBoundingClientRect();
      ndc.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
      ndc.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
      raycaster.setFromCamera(ndc, this.camera);
      const hits = raycaster.intersectObjects(this.partsGroup.children, false);
      for (const h of hits) {
        const pid = h.object?.userData?.partId;
        if (pid) return { mesh: h.object, partId: pid };
      }
      return null;
    };

    const mousedown = (e) => {
      if (e.button === 2 || (e.button === 0 && e.shiftKey)) {
        panning = true;
        dragging = false;
      } else if (e.button === 0) {
        // Try to pick a part first — if the click lands on one, the gesture
        // repositions that part rather than orbiting the camera.
        const picked = pickPart(e);
        const anchor = picked ? pointerToBed(e) : null;
        if (picked && anchor) {
          const mesh = picked.mesh;
          if (!mesh.geometry.boundingBox) mesh.geometry.computeBoundingBox();
          const bb = mesh.geometry.boundingBox;
          this._partDrag = {
            mesh,
            partId: picked.partId,
            // Triangles are already in world/bed coords; mesh.position is
            // (0,0,0) at rest. On release we compute the new min-corner as
            // bb.min + current offset and hand it up for persistence.
            baseMinX: bb.min.x,
            baseMinZ: bb.min.z,
            width: bb.max.x - bb.min.x,
            depth: bb.max.z - bb.min.z,
            anchorX: anchor.x,
            anchorZ: anchor.z,
          };
          dragging = false;
          panning = false;
          e.preventDefault();
          return;
        }
        dragging = true;
        panning = false;
      } else {
        return;
      }
      lastX = e.clientX;
      lastY = e.clientY;
      e.preventDefault();
    };
    const contextmenu = (e) => e.preventDefault();
    const mouseup = async () => {
      if (this._partDrag) {
        const d = this._partDrag;
        this._partDrag = null;
        // Translate the mesh's current offset into a new min-corner on the
        // bed (baseMin + currentOffset) and hand it up to the app layer for
        // persistence. We don't wait for the round-trip — the refresh after
        // the API call rebuilds setParts with canonical geometry.
        const newX = d.baseMinX + d.mesh.position.x;
        const newY = d.baseMinZ + d.mesh.position.z;
        if (typeof this.onPartDragEnd === 'function') {
          try {
            await this.onPartDragEnd(d.partId, newX, newY);
          } catch (_) {
            // surface the error through the caller — we already cleared the
            // drag, so the mesh snaps back on the next refreshState().
          }
        }
      }
      dragging = false;
      panning = false;
    };
    const mousemove = (e) => {
      if (this._partDrag) {
        const p = pointerToBed(e);
        if (!p) return;
        const d = this._partDrag;
        // Keep the grabbed point on the bed under the cursor: mesh offset
        // shifts by (pointer - anchor) in bed-space coordinates.
        let dx = p.x - d.anchorX;
        let dz = p.z - d.anchorZ;
        // Clamp so the visual preview matches the server-side clamp. bed X
        // maps to Three.js X; bed Y maps to Three.js Z.
        const [bx, , by] = this.bedSize;
        dx = Math.max(-d.baseMinX, Math.min(bx - d.width - d.baseMinX, dx));
        dz = Math.max(-d.baseMinZ, Math.min(by - d.depth - d.baseMinZ, dz));
        d.mesh.position.set(dx, 0, dz);
        return;
      }
      if (!dragging && !panning) return;
      const dx = e.clientX - lastX;
      const dy = e.clientY - lastY;
      lastX = e.clientX;
      lastY = e.clientY;
      if (panning) {
        const panScale = this._orbit.radius * 0.0015;
        this.camera.updateMatrixWorld(true);
        this.camera.matrixWorld.extractBasis(panRight, panUp, panFwd);
        this._orbit.target.addScaledVector(panRight, -dx * panScale);
        this._orbit.target.addScaledVector(panUp, dy * panScale);
      } else {
        this._orbit.azimuth += dx * 0.008;
        this._orbit.polar = Math.max(0.1, Math.min(Math.PI - 0.1, this._orbit.polar - dy * 0.008));
      }
      this._applyOrbit();
    };
    const wheel = (e) => {
      e.preventDefault();
      const scale = e.deltaY > 0 ? 1.1 : 1 / 1.1;
      this._orbit.radius = Math.max(50, Math.min(3000, this._orbit.radius * scale));
      this._applyOrbit();
    };

    c.addEventListener('mousedown', mousedown);
    c.addEventListener('contextmenu', contextmenu);
    c.addEventListener('wheel', wheel, { passive: false });
    window.addEventListener('mouseup', mouseup);
    window.addEventListener('mousemove', mousemove);
    this._inputHandlers = { mousedown, contextmenu, wheel, mouseup, mousemove };
  }

  // Snap the orbit to a canonical camera pose. Users expect the same set of
  // presets in any CAD-adjacent tool; we keep the names terse so the toolbar
  // buttons fit in a row.
  setView(name) {
    const [x, , y] = this.bedSize;
    this._orbit.target.set(x / 2, 0, y / 2);
    this._orbit.radius = Math.max(x, y) * 1.6;
    switch (name) {
      case 'top':
        // Looking straight down — polar → 0 collapses the camera onto +Y.
        this._orbit.polar = 0.02;
        this._orbit.azimuth = 0;
        break;
      case 'front':
        // Looking along +Y (from the user's side of the bed toward the back).
        this._orbit.polar = Math.PI / 2;
        this._orbit.azimuth = Math.PI / 2;
        break;
      case 'right':
        // Looking along +X from the right side of the bed.
        this._orbit.polar = Math.PI / 2;
        this._orbit.azimuth = 0;
        break;
      case 'iso':
      default:
        this._orbit.polar = Math.PI / 3;
        this._orbit.azimuth = Math.PI / 4;
        break;
    }
    this._applyOrbit();
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

    // X/Y/Z reference gizmo at the (0,0,0) corner of the bed. Scaled to the
    // bed so it stays visible on both Prusa-sized and tiny custom volumes.
    // Colors map to the bed frame (red=X right, green=Y depth, blue=Z up).
    this.bedGroup.add(this._buildAxesGizmo(Math.max(20, Math.min(40, Math.min(x, y) * 0.12))));

    // Recenter camera target on the bed
    this._orbit.target.set(x / 2, z / 4, y / 2);
    this._orbit.radius = Math.max(x, y) * 2;
    this._applyOrbit();
  }

  _buildAxesGizmo(size) {
    const group = new THREE.Group();
    group.name = 'axes-gizmo';
    // Anchor at (0, 0, 0) in world — the front-left-bottom corner of the bed.
    group.position.set(0, 0, 0);

    // Directions use Three.js axes (y is up in this scene), but the labels and
    // colors follow the backend/print-bed frame: +X=right, +Y=depth, +Z=up.
    // So bed +Y maps to Three.js +Z, and bed +Z maps to Three.js +Y.
    const head = size * 0.18;
    const headW = size * 0.08;
    const axes = [
      { label: 'X', color: 0xff5050, dir: new THREE.Vector3(1, 0, 0) }, // bed X = three x
      { label: 'Y', color: 0x50d060, dir: new THREE.Vector3(0, 0, 1) }, // bed Y = three z
      { label: 'Z', color: 0x5094ff, dir: new THREE.Vector3(0, 1, 0) }, // bed Z = three y
    ];
    for (const a of axes) {
      const arrow = new THREE.ArrowHelper(a.dir, new THREE.Vector3(0, 0, 0), size, a.color, head, headW);
      // Thicker trunk than the default 1px.
      if (arrow.line && arrow.line.material) arrow.line.material.linewidth = 2;
      group.add(arrow);
      const sprite = this._makeAxisLabel(a.label, a.color);
      sprite.position.copy(a.dir).multiplyScalar(size + head * 0.8);
      group.add(sprite);
    }
    return group;
  }

  _makeAxisLabel(text, color) {
    // Canvas-texture sprite: cheap, stays screen-aligned, and scales naturally
    // with camera distance because the sprite's world size is tied to bed mm.
    const canvas = document.createElement('canvas');
    canvas.width = 64;
    canvas.height = 64;
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, 64, 64);
    ctx.fillStyle = '#' + color.toString(16).padStart(6, '0');
    ctx.font = 'bold 42px system-ui, sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.shadowColor = 'rgba(0, 0, 0, 0.75)';
    ctx.shadowBlur = 4;
    ctx.fillText(text, 32, 34);
    const texture = new THREE.CanvasTexture(canvas);
    texture.anisotropy = 4;
    const material = new THREE.SpriteMaterial({ map: texture, transparent: true, depthTest: false });
    const sprite = new THREE.Sprite(material);
    sprite.scale.set(14, 14, 1);
    // Draw on top of the arrow shaft so labels don't get occluded by the bed
    // frame when the camera is low.
    sprite.renderOrder = 10;
    return sprite;
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
        opacity: 0.35,
        side: THREE.DoubleSide,
        depthWrite: false,
      });
      const mesh = new THREE.Mesh(geom, mat);
      mesh.userData.partId = part.id;
      // Store placement + triangle-space AABB so the part-drag handler can
      // clamp to the bed without calling back into React state.
      mesh.userData.placement = part.placement || null;
      geom.computeBoundingBox();
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
    // setCursor can map a simulation cursor back to a segment count. We also
    // record role + z-height per segment so setCursor can do role+depth
    // coloring without re-reading the moves array every frame.
    const segs = [];
    let zMin = Infinity;
    let zMax = -Infinity;
    for (let i = 0; i < moves.length - 1; i++) {
      const a = moves[i];
      const b = moves[i + 1];
      if (b.kind !== 'extrude') continue;
      // Three.js Y is up; backend (x,y,z) uses z as up, so swap here.
      segs.push({
        moveIndex: i + 1,
        ax: a.x, ay: a.z, az: a.y,
        bx: b.x, by: b.z, bz: b.y,
        role: b.role || 'perimeter',
        z: b.z,
      });
      if (b.z < zMin) zMin = b.z;
      if (b.z > zMax) zMax = b.z;
    }
    this._extrudeSegments = segs;
    this._zMin = isFinite(zMin) ? zMin : 0;
    this._zRange = isFinite(zMax - zMin) && zMax > zMin ? (zMax - zMin) : 1;

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

    // Fill positions + a role-aware, depth-aware color ramp. The last
    // GLOW_SEGMENTS blend toward a near-white hot color so the active extrusion
    // stands out. Earlier segments get their role's base color biased by Z
    // height (lighter at the top, darker at the bottom) — this is what gives
    // the print visible shape instead of an orange blob.
    const pos = this._printedPositions;
    const col = this._printedColors;
    const zMin = this._zMin ?? 0;
    const zRange = this._zRange ?? 1;
    for (let i = 0; i < visibleSegs; i++) {
      const s = segs[i];
      const o = i * 6;
      pos[o] = s.ax; pos[o + 1] = s.ay; pos[o + 2] = s.az;
      pos[o + 3] = s.bx; pos[o + 4] = s.by; pos[o + 5] = s.bz;

      const base = ROLE_BASE[s.role] || ROLE_DEFAULT;
      // Normalize Z within the toolpath's own range; center at 0.5 so the
      // mid-layers are unshifted and only the ceiling/floor get the extreme
      // tints. shift ∈ [-0.5, +0.5].
      const zNorm = Math.max(0, Math.min(1, (s.z - zMin) / zRange));
      const shift = zNorm - 0.5;
      const liftAmt = shift > 0 ? shift * 2 * DEPTH_LIGHTEN : 0;
      const sinkAmt = shift < 0 ? -shift * 2 * DEPTH_DARKEN : 0;
      let r = base[0] * (1 - sinkAmt) + (1 - base[0]) * liftAmt;
      let g = base[1] * (1 - sinkAmt) + (1 - base[1]) * liftAmt;
      let b = base[2] * (1 - sinkAmt) + (1 - base[2]) * liftAmt;

      const fromEnd = visibleSegs - 1 - i;
      if (fromEnd < GLOW_SEGMENTS) {
        const t = 1 - fromEnd / GLOW_SEGMENTS;
        r = r + (HOT_COLOR[0] - r) * t;
        g = g + (HOT_COLOR[1] - g) * t;
        b = b + (HOT_COLOR[2] - b) * t;
      }
      col[o] = r; col[o + 1] = g; col[o + 2] = b;
      col[o + 3] = r; col[o + 4] = g; col[o + 5] = b;
    }

    // Before any simulation progress the source mesh is the scene — always
    // show it at the upload-time opacity. Once the print starts advancing we
    // honor the `partsSimVisible` flag (defaults off) so the printed filament
    // reads clearly instead of being smeared by the translucent mesh.
    const simActive = visibleSegs > 0;
    if (simActive) {
      this.partsGroup.visible = this._partsSimVisible !== false;
      if (this.partsGroup.visible) {
        const meshOpacity = 0.12;
        for (const child of this.partsGroup.children) {
          if (child.material && child.material.opacity !== meshOpacity) {
            child.material.opacity = meshOpacity;
            child.material.transparent = true;
            child.material.depthWrite = false;
          }
        }
      }
    } else {
      this.partsGroup.visible = true;
      const meshOpacity = 0.35;
      for (const child of this.partsGroup.children) {
        if (child.material && child.material.opacity !== meshOpacity) {
          child.material.opacity = meshOpacity;
          child.material.transparent = true;
          child.material.depthWrite = false;
        }
      }
    }

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

  // Show/hide the translucent source meshes while the simulation is printing.
  // Hidden by default so the printed filament isn't washed out; users can flip
  // the checkbox in the Simulation panel to see the source geometry as a
  // low-opacity ghost during the print.
  setPartsSimVisible(visible) {
    this._partsSimVisible = !!visible;
    // Replay setCursor with the last known cursor so the opacity/visibility
    // update lands immediately instead of waiting for the next sim tick.
    if (this._moves && this._moves.length > 0) {
      this.setCursor(this._lastCursor || 0);
    }
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
