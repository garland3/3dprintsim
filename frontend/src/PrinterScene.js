// PrinterScene: owns the Three.js renderer, camera, controls-less orbit, and
// the objects that represent the bed, the parts, and the running toolpath.
// The React layer only hands it data — this class is the sole Three.js user.

import * as THREE from 'three';
import { LineSegments2 } from 'three/addons/lines/LineSegments2.js';
import { LineSegmentsGeometry } from 'three/addons/lines/LineSegmentsGeometry.js';
import { LineMaterial } from 'three/addons/lines/LineMaterial.js';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';

// Number of most-recent extrude segments that get the "hot / freshly extruded"
// color ramp. Picked by eye — big enough to read, small enough to stay local.
const GLOW_SEGMENTS = 40;

// LPBF spark physics — gravity (mm/s²) and the recoater's planar overhang
// past either edge of the bed (mm). Module-level so they're easy to retune
// without spelunking through the animation loop.
const SPARK_GRAVITY = 180;
const RECOATER_OVERHANG = 8;

// Off-screen sentinel for dead particles in pooled THREE.Points buffers.
// We move retired slots far below the scene rather than relying on a black
// vertex color, because PointsMaterial still rasterizes a (0,0,0) point as a
// faint dot against the dark background.
const PARTICLE_SINK_Y = -1e5;

// LPBF heat-trail visualization. Each newly completed scan segment drops a
// heat point at its endpoint that fades white-yellow → orange → red → off.
const HEAT_TRAIL_CAPACITY = 768;
const HEAT_LIFE_MIN = 1.2;
const HEAT_LIFE_MAX = 2.4;

// Module-level scratch objects so the per-frame laser/gimbal math doesn't
// allocate on every aim update.
const _v0 = new THREE.Vector3();
const _v1 = new THREE.Vector3();
const _q0 = new THREE.Quaternion();

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

    // Bloom postprocessing — gives the laser, sparks, melt pool, and heat
    // trail an actual self-illumination glow instead of a flat sprite. The
    // composer is rebuilt-in-place on resize via _resize().
    // Threshold is intentionally loose (0.55) so the orange/amber palette
    // bleeds; strength + radius dialed by eye against the dark background.
    this.composer = new EffectComposer(this.renderer);
    this.composer.addPass(new RenderPass(this.scene, this.camera));
    this.bloomPass = new UnrealBloomPass(new THREE.Vector2(1, 1), 0.65, 0.6, 0.55);
    this.composer.addPass(this.bloomPass);

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

    // LPBF rigging — built lazily so FDM renders aren't paying the cost.
    // `_printerType` switches the renderer between FDM (default; build-up)
    // and LPBF (build-plate-descends, laser fuses powder, recoater sweeps).
    this._printerType = 'FDM';
    this._laser = null;          // overhead laser source + beam line
    this._sparks = null;         // THREE.Points used as transient sparks
    this._sparkState = null;     // per-particle velocity / lifetime arrays
    this._heatTrail = null;      // THREE.Points used for melt-pool heat fade
    this._heatState = null;      // per-point lifetime + initial color arrays
    this._meltPool = null;       // additive sprite at the active scan spot
    this._meltPhase = 0;         // pulsing phase (radians) for the melt-pool sprite
    this._activeHead = null;     // last-known scan-spot head; kept fresh by setCursor
    this._recoater = null;       // horizontal bar that sweeps each layer
    this._recoaterState = null;  // animation state for the recoater sweep
    this._lpbfClock = null;      // last-frame timestamp for time-step deltas
    this._sinkOffset = 0;        // current "build plate has descended" mm
    this._lastPrintedZ = -Infinity; // tracks the active layer for recoater triggers
    this._lastVisibleSegs = 0;   // last setCursor()'s visibleSegs — drives heat emit

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
    if (this.composer) this.composer.dispose();
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
    // EffectComposer keeps its own framebuffers — must be resized in lockstep
    // with the renderer or the bloom output stretches/blurs incorrectly.
    if (this.composer) this.composer.setSize(w, h);
    if (this.bloomPass) this.bloomPass.setSize(w, h);
  };

  _animate = () => {
    if (this._printerType === 'LPBF') {
      const now = performance.now();
      const dt = this._lpbfClock == null ? 0 : Math.min(0.1, (now - this._lpbfClock) / 1000);
      this._lpbfClock = now;
      if (dt > 0) {
        this._tickSparks(dt);
        this._tickHeat(dt);
        this._tickRecoater(dt);
      }
      // Melt pool pulses every frame, even when dt is zero (first frame),
      // so the sprite renders the moment LPBF mode starts.
      this._updateMeltPool(this._activeHead, dt);
    }
    if (this.composer) {
      this.composer.render();
    } else {
      this.renderer.render(this.scene, this.camera);
    }
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
    plate.name = 'bed-plate';
    this._bedPlate = plate;
    // In LPBF mode the plate would hide the descending build column, so it's
    // swapped for a chamber (walls + lip + dim floor) below.
    plate.visible = this._printerType !== 'LPBF';
    this.bedGroup.add(plate);

    // LPBF build chamber: an open well below the bed mouth so the descending
    // column of melted powder is visible. Pre-built and toggled with the
    // printer type so FDM doesn't pay the geometry cost while it's hidden.
    this._lpbfChamber = this._buildLpbfChamber(x, y, z);
    this._lpbfChamber.visible = this._printerType === 'LPBF';
    this.bedGroup.add(this._lpbfChamber);

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

    // The LPBF recoater spans the bed depth — rebuild it whenever the bed
    // resizes so it doesn't end up clipping or overshooting the new volume.
    if (this._recoater) {
      this.scene.remove(this._recoater);
      this._recoater.geometry.dispose();
      this._recoater.material.dispose();
      this._recoater = this._buildRecoater();
      this.scene.add(this._recoater);
      this._recoater.visible = false;
      this._recoaterState = null;
    }
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
      // Reset the LPBF descent so a fresh upload doesn't render under the bed
      // because the previous run had sunk the column.
      this.printedGroup.position.y = 0;
      this.partsGroup.position.y = 0;
      this._sinkOffset = 0;
      this._lastPrintedZ = -Infinity;
      this._lastVisibleSegs = 0;
      this._activeHead = null;
      this._hideLaser();
      this._hideMeltPool();
      this._stopRecoaterSweep();
      this._clearHeat();
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

    // FDM shows the nozzle cone; LPBF replaces it with the laser rig and
    // hides the cone (the active spot is rendered by the laser beam instead).
    this.head.visible = this._printerType !== 'LPBF';
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

    // LPBF visualization layer — plays on top of the same printed/parts
    // geometry the FDM render uses. We compute the topmost printed Z, drop
    // the build plate by that amount (so the active layer always sits at the
    // recoater level), aim the laser at the active spot, and trigger a
    // recoater sweep + spark burst whenever a new layer starts.
    if (this._printerType === 'LPBF') {
      // Topmost printed-layer Z (the "active build surface"). Falls back to 0
      // before any segments have been deposited so the part starts flush.
      let topZ = 0;
      for (let i = 0; i < visibleSegs; i++) {
        const z = segs[i].z;
        if (z > topZ) topZ = z;
      }
      this._sinkOffset = topZ;
      // Push the printed material and the source-mesh ghost down by the
      // current build-plate descent. The bedGroup stays put — that's the
      // machine frame; only the powder column sinks.
      this.printedGroup.position.y = -topZ;
      this.partsGroup.position.y = -topZ;

      this._updateLaser(head, topZ);

      // Detect a layer change to fire the recoater + a spark burst at the
      // start of the new layer's first segment.
      if (head && this._lastPrintedZ !== topZ) {
        if (topZ > this._lastPrintedZ && this._lastPrintedZ !== -Infinity) {
          this._startRecoaterSweep();
        }
        this._lastPrintedZ = topZ;
      }
      // Constant low-rate spark emission while a melt is active so the spot
      // reads as "doing something" even between layer changes.
      if (head && visibleSegs > 0) {
        this._emitSparks(head.x, head.y, 4);
      }
      // Heat trail: drop a fading point at every newly completed segment's
      // endpoint. The trail lives on the active surface (world Y=0), so old
      // marks naturally cool away before the next layer is recoated. Cap how
      // many we emit per setCursor in case the user scrubs the slider — we
      // don't want a 100k-segment scrub to enqueue 100k heat points.
      if (this._heatState && visibleSegs > this._lastVisibleSegs) {
        const start = this._lastVisibleSegs;
        const end = Math.min(visibleSegs, start + 64);
        for (let i = start; i < end; i++) {
          // segs[i].bx / bz are in three.js coords (X = bed X, Z = bed Y).
          this._emitHeat(segs[i].bx, segs[i].bz);
        }
      }
      // Keep the active scan spot fresh so the melt-pool sprite (driven from
      // _animate) tracks the laser. Suppress while the sim is at rest (no
      // segments visible) so we don't park a melt pool over an empty bed.
      this._activeHead = head && visibleSegs > 0 ? head : null;
      this._lastVisibleSegs = visibleSegs;
    } else {
      this._activeHead = null;
      this._lastVisibleSegs = visibleSegs;
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
      printerType: this._printerType,
      sinkOffset: this._sinkOffset || 0,
      laserVisible: !!(this._laser && this._laser.visible),
      recoaterActive: !!(this._recoaterState && this._recoaterState.active),
    };
  }

  // ---- LPBF visualization ----------------------------------------------

  // Switch the visualization to FDM (build-up) or LPBF (build-plate-descends).
  // Idempotent — calling with the current type is a no-op so React's
  // setState-driven calls don't churn the scene.
  setPrinterType(type) {
    const next = (type || 'FDM').toString().toUpperCase() === 'LPBF' ? 'LPBF' : 'FDM';
    if (next === this._printerType) return;
    this._printerType = next;
    if (next === 'LPBF') {
      this._ensureLpbfRig();
      // The FDM nozzle cone makes no sense over a powder bed.
      this.head.visible = false;
      // Hide the solid plate and expose the chamber so the descending column
      // of printed material is visible below the bed mouth.
      if (this._bedPlate) this._bedPlate.visible = false;
      if (this._lpbfChamber) this._lpbfChamber.visible = true;
    } else {
      // Reverting to FDM — undo any descent and put the laser/recoater away.
      this.printedGroup.position.y = 0;
      this.partsGroup.position.y = 0;
      this._sinkOffset = 0;
      this._lastPrintedZ = -Infinity;
      this._lastVisibleSegs = 0;
      this._activeHead = null;
      this._hideLaser();
      this._hideMeltPool();
      this._stopRecoaterSweep();
      this._clearSparks();
      this._clearHeat();
      if (this._bedPlate) this._bedPlate.visible = true;
      if (this._lpbfChamber) this._lpbfChamber.visible = false;
      // Restore the head if a toolpath is loaded so the FDM nozzle reappears.
      if (this._moves && this._moves.length > 0) this.head.visible = true;
    }
    // Replay the last cursor so the new visualization snaps into place
    // without waiting for the next sim tick.
    if (this._moves && this._moves.length > 0) {
      this.setCursor(this._lastCursor || 0);
    }
  }

  _ensureLpbfRig() {
    if (!this._laser) {
      this._laser = this._buildLaser();
      this.scene.add(this._laser);
    }
    if (!this._recoater) {
      this._recoater = this._buildRecoater();
      this.scene.add(this._recoater);
      this._recoater.visible = false;
    }
    if (!this._sparks) {
      this._sparks = this._buildSparks();
      this.scene.add(this._sparks);
    }
    if (!this._heatTrail) {
      this._heatTrail = this._buildHeatTrail();
      this.scene.add(this._heatTrail);
    }
    if (!this._meltPool) {
      this._meltPool = this._buildMeltPool();
      this.scene.add(this._meltPool);
    }
  }

  _buildLaser() {
    // Stationary scanner head: the housing sits at a fixed point above the bed
    // center, and only the lower gimbal rotates to aim the beam at the active
    // scan spot. This matches a real galvanometer laser system, where the body
    // is bolted to the chamber and only the mirror assembly tilts.
    //
    // Hierarchy:
    //   group (positioned over bed center)
    //   ├── housing (fixed metal box with stripe + indicator LED)
    //   ├── ringMount (decorative torus where the gimbal pivots)
    //   ├── gimbal (rotates so its local -Y aims at the spot)
    //   │   ├── lensHousing (cylinder)
    //   │   ├── lens (glowing aperture)
    //   │   └── beam (cylinder along local -Y, scaled to spot distance)
    //   └── flash (PointLight, repositioned each frame to the spot)
    const group = new THREE.Group();
    group.name = 'lpbf-laser';

    const housingMat = new THREE.MeshStandardMaterial({
      color: 0x1a1d22,
      metalness: 0.85,
      roughness: 0.35,
    });
    const housing = new THREE.Mesh(new THREE.BoxGeometry(44, 22, 32), housingMat);
    housing.name = 'laser-housing';
    group.add(housing);

    // Accent stripe so the housing reads as a real piece of equipment rather
    // than a featureless block.
    const accentMat = new THREE.MeshStandardMaterial({
      color: 0x9a2a2a,
      emissive: 0x5a1010,
      emissiveIntensity: 0.7,
      metalness: 0.7,
      roughness: 0.4,
    });
    const stripe = new THREE.Mesh(new THREE.BoxGeometry(44.4, 2.4, 6), accentMat);
    stripe.position.set(0, 4, 0);
    group.add(stripe);

    // Status LED — a tiny green sphere on the housing corner.
    const led = new THREE.Mesh(
      new THREE.SphereGeometry(0.9, 12, 8),
      new THREE.MeshBasicMaterial({ color: 0x55ff66 }),
    );
    led.position.set(17, 4, 14);
    group.add(led);

    // Mounting collar / cooling fins on the underside — purely cosmetic.
    const collarMat = new THREE.MeshStandardMaterial({
      color: 0x2a2e34,
      metalness: 0.9,
      roughness: 0.3,
    });
    const collar = new THREE.Mesh(new THREE.CylinderGeometry(9, 11, 4, 24), collarMat);
    collar.position.set(0, -13, 0);
    group.add(collar);

    // Decorative torus where the gimbal nests — sits at the gimbal's pivot.
    const ringMat = new THREE.MeshStandardMaterial({
      color: 0x444a52,
      metalness: 0.9,
      roughness: 0.25,
    });
    const ring = new THREE.Mesh(new THREE.TorusGeometry(7.5, 1.2, 10, 28), ringMat);
    ring.position.set(0, -16, 0);
    ring.rotation.x = Math.PI / 2;
    group.add(ring);

    // Inner gimbal — rotates so its local -Y points at the active spot. All
    // beam-aiming geometry lives under this group so we can drive the entire
    // assembly with a single quaternion.
    const gimbal = new THREE.Group();
    gimbal.name = 'laser-gimbal';
    gimbal.position.set(0, -16, 0);
    group.add(gimbal);

    // Lens housing — the conical scanner tube the beam emerges from.
    const lensHousing = new THREE.Mesh(
      new THREE.CylinderGeometry(5.5, 4.0, 8, 20),
      new THREE.MeshStandardMaterial({
        color: 0x2c3036,
        metalness: 0.9,
        roughness: 0.25,
      }),
    );
    lensHousing.position.set(0, -4, 0);
    gimbal.add(lensHousing);

    // Visible glowing aperture at the bottom of the lens housing.
    const lens = new THREE.Mesh(
      new THREE.CylinderGeometry(3.6, 3.6, 0.7, 24),
      new THREE.MeshStandardMaterial({
        color: 0xff6633,
        emissive: 0xff4422,
        emissiveIntensity: 1.4,
        metalness: 0.1,
        roughness: 0.2,
      }),
    );
    lens.position.set(0, -8, 0);
    gimbal.add(lens);

    // Volumetric beam: a thin bright core inside a wider soft halo. Both run
    // along beam-local -Y and are scaled in Y at runtime to span lens→spot.
    // Cylinder geometry runs along Y by default; translating so y∈[0,-1] keeps
    // the top anchored at beam.position (the lens) and lets a positive scaleY
    // extend it downward.
    //
    // Bloom does the heavy lifting on the glow — these meshes only need to
    // contribute the right shape; the postprocessing pass blooms them out.
    const coreMat = new THREE.MeshBasicMaterial({
      color: 0xfff2c0,
      transparent: true,
      opacity: 0.95,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
    });
    const beamCore = new THREE.Mesh(new THREE.CylinderGeometry(0.45, 0.18, 1, 12), coreMat);
    beamCore.name = 'laser-beam-core';
    beamCore.geometry.translate(0, -0.5, 0);
    beamCore.position.set(0, -8, 0);
    gimbal.add(beamCore);

    const haloMat = new THREE.MeshBasicMaterial({
      color: 0xff7a33,
      transparent: true,
      opacity: 0.22,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
    });
    const beamHalo = new THREE.Mesh(new THREE.CylinderGeometry(2.2, 0.9, 1, 14), haloMat);
    beamHalo.name = 'laser-beam-halo';
    beamHalo.geometry.translate(0, -0.5, 0);
    beamHalo.position.set(0, -8, 0);
    gimbal.add(beamHalo);

    group.userData.gimbal = gimbal;
    group.userData.lens = lens;
    // `beam` is consumed by _updateLaser to set length; we point it at the
    // core and have _updateLaser sync the halo's scale alongside it via
    // userData.beamHalo.
    group.userData.beam = beamCore;
    group.userData.beamHalo = beamHalo;
    // Cache the lens's local Y inside the gimbal so _updateLaser can compute
    // the gimbal-to-spot distance and subtract this offset to size the beam.
    group.userData.lensLocalY = -8;

    // Bright point light at the active spot, repositioned in the laser-local
    // frame each frame so it tracks the scan head.
    const flash = new THREE.PointLight(0xffaa44, 0.0, 80, 2.0);
    flash.name = 'laser-flash';
    group.add(flash);
    group.userData.flash = flash;

    group.visible = false;
    return group;
  }

  _buildLpbfChamber(x, y, z) {
    // Open well below the bed mouth: four metal walls, a thin frame lip at the
    // bed plane so the build extents stay readable, and a dim floor at the
    // bottom so the descent has a defined endpoint instead of opening onto the
    // background. Depth is sized to the build height plus margin so the deepest
    // descent never punches through the floor.
    const group = new THREE.Group();
    group.name = 'lpbf-chamber';
    const depth = z * 1.25 + 20;

    const wallMat = new THREE.MeshStandardMaterial({
      color: 0x23272d,
      roughness: 0.55,
      metalness: 0.4,
      side: THREE.DoubleSide,
    });
    const wallT = 1.5;
    const walls = [
      // front (z=0) and back (z=y)
      { sx: x + wallT * 2, sy: depth, sz: wallT, x: x / 2, z: -wallT / 2 },
      { sx: x + wallT * 2, sy: depth, sz: wallT, x: x / 2, z: y + wallT / 2 },
      // left (x=0) and right (x=x)
      { sx: wallT, sy: depth, sz: y, x: -wallT / 2, z: y / 2 },
      { sx: wallT, sy: depth, sz: y, x: x + wallT / 2, z: y / 2 },
    ];
    for (const w of walls) {
      const wall = new THREE.Mesh(new THREE.BoxGeometry(w.sx, w.sy, w.sz), wallMat);
      // Walls hang downward from Y=0; center is at -depth/2.
      wall.position.set(w.x, -depth / 2, w.z);
      group.add(wall);
    }

    // Frame lip at the bed plane — a slim metallic rim around the chamber
    // mouth so the bed extents stay visually crisp even with the plate hidden.
    const lipMat = new THREE.MeshStandardMaterial({
      color: 0x4d5460,
      metalness: 0.7,
      roughness: 0.45,
    });
    const lipH = 0.8;
    const lipW = 4;
    const lipDefs = [
      { sx: x + lipW * 2, sz: lipW, x: x / 2, z: -lipW / 2 },
      { sx: x + lipW * 2, sz: lipW, x: x / 2, z: y + lipW / 2 },
      { sx: lipW, sz: y, x: -lipW / 2, z: y / 2 },
      { sx: lipW, sz: y, x: x + lipW / 2, z: y / 2 },
    ];
    for (const l of lipDefs) {
      const lip = new THREE.Mesh(new THREE.BoxGeometry(l.sx, lipH, l.sz), lipMat);
      lip.position.set(l.x, lipH / 2, l.z);
      group.add(lip);
    }

    // Dim chamber floor — gives the descent a defined bottom rather than
    // opening into the background void.
    const floor = new THREE.Mesh(
      new THREE.BoxGeometry(x, 0.5, y),
      new THREE.MeshStandardMaterial({ color: 0x0a0c0f, roughness: 0.95 }),
    );
    floor.position.set(x / 2, -depth + 0.25, y / 2);
    group.add(floor);

    return group;
  }

  _buildRecoater() {
    // A flat metallic bar that spans the bed's depth (Y axis in the backend
    // frame, which is +Z in three.js). It rides at the build surface (Y=0
    // after the printedGroup sinks) and translates along +X across the bed.
    const [bx, , by] = this.bedSize;
    const mat = new THREE.MeshStandardMaterial({
      color: 0x9aa0aa,
      metalness: 0.85,
      roughness: 0.35,
      emissive: 0x111418,
    });
    const bar = new THREE.Mesh(new THREE.BoxGeometry(8, 6, by + RECOATER_OVERHANG * 1.5), mat);
    bar.name = 'lpbf-recoater';
    return bar;
  }

  _buildSparks() {
    // Capacity picked so a ~30Hz emission rate over a few seconds doesn't
    // wrap before the oldest particles have faded — small enough to keep
    // the overdraw negligible.
    const capacity = 256;
    const positions = new Float32Array(capacity * 3);
    const colors = new Float32Array(capacity * 3);
    // Sink every slot off-screen so idle particles never render at the bed
    // origin as faint black dots before they've ever been emitted.
    for (let i = 0; i < capacity; i++) positions[i * 3 + 1] = PARTICLE_SINK_Y;
    const geom = new THREE.BufferGeometry();
    geom.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    geom.setAttribute('color', new THREE.BufferAttribute(colors, 3));
    geom.setDrawRange(0, capacity);
    const mat = new THREE.PointsMaterial({
      size: 2.5,
      vertexColors: true,
      transparent: true,
      opacity: 0.95,
      depthWrite: false,
      sizeAttenuation: true,
    });
    const points = new THREE.Points(geom, mat);
    points.name = 'lpbf-sparks';
    points.frustumCulled = false;
    this._sparkState = {
      capacity,
      cursor: 0,
      live: 0,
      // Per-particle velocity (mm/s) and remaining lifetime (s).
      vx: new Float32Array(capacity),
      vy: new Float32Array(capacity),
      vz: new Float32Array(capacity),
      life: new Float32Array(capacity),
      maxLife: new Float32Array(capacity),
      // Initial RGB color recorded at emit time so the per-frame fade can
      // reproduce the ramp `color = initial * t` instead of compounding
      // multiplicative decay (which under-shoots into black quickly).
      r0: new Float32Array(capacity),
      g0: new Float32Array(capacity),
      b0: new Float32Array(capacity),
    };
    return points;
  }

  _hideLaser() {
    if (this._laser) {
      this._laser.visible = false;
      const flash = this._laser.userData.flash;
      if (flash) flash.intensity = 0;
    }
  }

  _updateLaser(headMove, topZ) {
    if (!this._laser) return;
    if (!headMove) {
      this._hideLaser();
      return;
    }
    // The housing is stationary above the bed center; only the gimbal pivots
    // to track the scan spot. The build surface sits at world Y=0 in LPBF mode
    // because the printedGroup is sunk by topZ.
    const [bx, , by] = this.bedSize;
    const cx = bx / 2;
    const cz = by / 2;
    const sourceY = Math.max(90, this.bedSize[2] * 0.7);

    this._laser.visible = true;
    this._laser.position.set(cx, sourceY, cz);

    const gimbal = this._laser.userData.gimbal;
    const beam = this._laser.userData.beam;
    const lensLocalY = this._laser.userData.lensLocalY ?? 0;
    if (gimbal) {
      // Gimbal world position = housing position + gimbal local offset.
      const gimbalWorldY = sourceY + gimbal.position.y;
      // Direction from gimbal pivot to active spot, in world coords. The spot
      // is at world (headMove.x, 0, headMove.y) — Y=0 because the printedGroup
      // is sunk so the active layer always sits there.
      const dx = headMove.x - cx;
      const dy = 0 - gimbalWorldY;
      const dz = headMove.y - cz;
      const dist = Math.hypot(dx, dy, dz);
      // Aim the gimbal so its local -Y axis points at the spot. The gimbal's
      // parent (the laser group) has no rotation, so a world-space quaternion
      // is also valid in the parent frame.
      _v0.set(0, -1, 0);
      _v1.set(dx / dist, dy / dist, dz / dist);
      _q0.setFromUnitVectors(_v0, _v1);
      gimbal.quaternion.copy(_q0);

      if (beam) {
        // Beam runs from the lens (at gimbal-local y=lensLocalY along the now-
        // rotated -Y axis) to the spot. The lens lies on the gimbal-to-spot
        // line at distance |lensLocalY|, so the remaining beam length is
        // dist - |lensLocalY|.
        const beamLen = Math.max(1, dist - Math.abs(lensLocalY));
        beam.scale.set(1, beamLen, 1);
        const halo = this._laser.userData.beamHalo;
        if (halo) halo.scale.set(1, beamLen, 1);
      }
    }

    const flash = this._laser.userData.flash;
    if (flash) {
      // Flash is a child of the laser group (origin at housing). Convert the
      // world spot to laser-local coords by subtracting the housing position.
      flash.position.set(headMove.x - cx, -sourceY, headMove.y - cz);
      flash.intensity = 1.6;
    }
  }

  _emitSparks(headX, headY, count) {
    const s = this._sparkState;
    if (!s || !this._sparks) return;
    const pos = this._sparks.geometry.attributes.position.array;
    const col = this._sparks.geometry.attributes.color.array;
    for (let n = 0; n < count; n++) {
      const i = s.cursor;
      s.cursor = (s.cursor + 1) % s.capacity;
      if (s.live < s.capacity) s.live += 1;
      // Spawn at the active spot in world coords. Build surface is at Y=0
      // because the printedGroup is sunk by topZ in LPBF mode.
      pos[i * 3 + 0] = headX;
      pos[i * 3 + 1] = 0;
      pos[i * 3 + 2] = headY;
      // Random upward cone — `polar` is the angle off the +Y axis, so
      // small polar = mostly straight up. Cap at ~60° from vertical so
      // particles never spray sideways or downward.
      const speed = 30 + Math.random() * 60;
      const az = Math.random() * Math.PI * 2;
      const polar = Math.random() * (Math.PI / 3); // 0..60° off vertical
      const horiz = Math.sin(polar) * speed;
      s.vx[i] = Math.cos(az) * horiz;
      s.vz[i] = Math.sin(az) * horiz;
      s.vy[i] = Math.cos(polar) * speed;
      // Hot-yellow → orange. Slight per-particle hue jitter keeps the
      // burst from looking like a single solid blob.
      const r0 = 1.0;
      const g0 = 0.6 + Math.random() * 0.4;
      const b0 = 0.15 + Math.random() * 0.2;
      s.r0[i] = r0;
      s.g0[i] = g0;
      s.b0[i] = b0;
      col[i * 3 + 0] = r0;
      col[i * 3 + 1] = g0;
      col[i * 3 + 2] = b0;
      const life = 0.3 + Math.random() * 0.5;
      s.life[i] = life;
      s.maxLife[i] = life;
    }
    this._sparks.geometry.setDrawRange(0, s.capacity);
    this._sparks.geometry.attributes.position.needsUpdate = true;
    this._sparks.geometry.attributes.color.needsUpdate = true;
  }

  _tickSparks(dt) {
    const s = this._sparkState;
    if (!s || !this._sparks) return;
    const pos = this._sparks.geometry.attributes.position.array;
    const col = this._sparks.geometry.attributes.color.array;
    for (let i = 0; i < s.capacity; i++) {
      if (s.life[i] <= 0) continue;
      s.vy[i] -= SPARK_GRAVITY * dt;
      pos[i * 3 + 0] += s.vx[i] * dt;
      pos[i * 3 + 1] += s.vy[i] * dt;
      pos[i * 3 + 2] += s.vz[i] * dt;
      s.life[i] -= dt;
      if (s.life[i] <= 0) {
        // Sink the slot far below the scene so PointsMaterial doesn't keep
        // rasterizing a faint dot at the death position. We can't rely on a
        // black vertex color — the dark scene background still tints toward
        // visible at the material's transparent opacity.
        pos[i * 3 + 0] = 0;
        pos[i * 3 + 1] = PARTICLE_SINK_Y;
        pos[i * 3 + 2] = 0;
        col[i * 3 + 0] = 0;
        col[i * 3 + 1] = 0;
        col[i * 3 + 2] = 0;
        continue;
      }
      // Linear fade: derive current color from the recorded initial color
      // each frame. Multiplying the existing color in-place compounds across
      // frames and crashes to black far faster than `life/maxLife` implies.
      const t = Math.max(0, s.life[i] / s.maxLife[i]);
      col[i * 3 + 0] = s.r0[i] * t;
      col[i * 3 + 1] = s.g0[i] * t;
      col[i * 3 + 2] = s.b0[i] * t;
    }
    this._sparks.geometry.attributes.position.needsUpdate = true;
    this._sparks.geometry.attributes.color.needsUpdate = true;
  }

  _clearSparks() {
    const s = this._sparkState;
    if (!s || !this._sparks) return;
    const pos = this._sparks.geometry.attributes.position.array;
    const col = this._sparks.geometry.attributes.color.array;
    pos.fill(0);
    col.fill(0);
    for (let i = 0; i < s.capacity; i++) pos[i * 3 + 1] = PARTICLE_SINK_Y;
    s.life.fill(0);
    s.live = 0;
    s.cursor = 0;
    this._sparks.geometry.attributes.position.needsUpdate = true;
    this._sparks.geometry.attributes.color.needsUpdate = true;
  }

  _buildHeatTrail() {
    // Heat residue points dropped at scan-segment endpoints. They sit at world
    // Y=0 (the active build surface — printedGroup is sunk by topZ in LPBF
    // mode) and fade white-yellow → orange → red → off so the user can see
    // where the laser has just passed. Additive blending makes overlapping
    // points read as a brighter melt pool rather than washed out gray.
    const capacity = HEAT_TRAIL_CAPACITY;
    const positions = new Float32Array(capacity * 3);
    const colors = new Float32Array(capacity * 3);
    for (let i = 0; i < capacity; i++) positions[i * 3 + 1] = PARTICLE_SINK_Y;
    const geom = new THREE.BufferGeometry();
    geom.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    geom.setAttribute('color', new THREE.BufferAttribute(colors, 3));
    geom.setDrawRange(0, capacity);
    const mat = new THREE.PointsMaterial({
      size: 4.0,
      vertexColors: true,
      transparent: true,
      opacity: 0.9,
      depthWrite: false,
      sizeAttenuation: true,
      blending: THREE.AdditiveBlending,
    });
    const points = new THREE.Points(geom, mat);
    points.name = 'lpbf-heat';
    points.frustumCulled = false;
    this._heatState = {
      capacity,
      cursor: 0,
      life: new Float32Array(capacity),
      maxLife: new Float32Array(capacity),
      r0: new Float32Array(capacity),
      g0: new Float32Array(capacity),
      b0: new Float32Array(capacity),
    };
    return points;
  }

  _emitHeat(x, z) {
    const s = this._heatState;
    if (!s || !this._heatTrail) return;
    const pos = this._heatTrail.geometry.attributes.position.array;
    const col = this._heatTrail.geometry.attributes.color.array;
    const i = s.cursor;
    s.cursor = (s.cursor + 1) % s.capacity;
    // Pin to the active build surface (world Y=0 in LPBF view). Spatial X/Z
    // come from the segment endpoint in three.js coords.
    pos[i * 3 + 0] = x;
    pos[i * 3 + 1] = 0;
    pos[i * 3 + 2] = z;
    // Initial hot color — slight per-emit jitter on green keeps a row of
    // adjacent melts from looking like a single solid stripe.
    s.r0[i] = 1.0;
    s.g0[i] = 0.85 + Math.random() * 0.12;
    s.b0[i] = 0.45;
    col[i * 3 + 0] = s.r0[i];
    col[i * 3 + 1] = s.g0[i];
    col[i * 3 + 2] = s.b0[i];
    const life = HEAT_LIFE_MIN + Math.random() * (HEAT_LIFE_MAX - HEAT_LIFE_MIN);
    s.life[i] = life;
    s.maxLife[i] = life;
    this._heatTrail.geometry.attributes.position.needsUpdate = true;
    this._heatTrail.geometry.attributes.color.needsUpdate = true;
  }

  _tickHeat(dt) {
    const s = this._heatState;
    if (!s || !this._heatTrail) return;
    const pos = this._heatTrail.geometry.attributes.position.array;
    const col = this._heatTrail.geometry.attributes.color.array;
    let dirty = false;
    for (let i = 0; i < s.capacity; i++) {
      if (s.life[i] <= 0) continue;
      s.life[i] -= dt;
      dirty = true;
      if (s.life[i] <= 0) {
        // Sink the slot off-screen and zero its color so it can't contribute
        // a stray pixel after death.
        pos[i * 3 + 0] = 0;
        pos[i * 3 + 1] = PARTICLE_SINK_Y;
        pos[i * 3 + 2] = 0;
        col[i * 3 + 0] = 0;
        col[i * 3 + 1] = 0;
        col[i * 3 + 2] = 0;
        continue;
      }
      // Color cooling: red channel fades slowly so the trail goes through a
      // visible orange/red phase before dying out. Green and blue fade faster
      // so the white-yellow start cools to red rather than gray.
      const t = s.life[i] / s.maxLife[i];
      const tR = Math.sqrt(t);
      const tG = t * t;
      const tB = t * t * t;
      col[i * 3 + 0] = s.r0[i] * tR;
      col[i * 3 + 1] = s.g0[i] * tG;
      col[i * 3 + 2] = s.b0[i] * tB;
    }
    if (dirty) {
      this._heatTrail.geometry.attributes.position.needsUpdate = true;
      this._heatTrail.geometry.attributes.color.needsUpdate = true;
    }
  }

  _clearHeat() {
    const s = this._heatState;
    if (!s || !this._heatTrail) return;
    const pos = this._heatTrail.geometry.attributes.position.array;
    const col = this._heatTrail.geometry.attributes.color.array;
    pos.fill(0);
    col.fill(0);
    for (let i = 0; i < s.capacity; i++) pos[i * 3 + 1] = PARTICLE_SINK_Y;
    s.life.fill(0);
    s.cursor = 0;
    this._heatTrail.geometry.attributes.position.needsUpdate = true;
    this._heatTrail.geometry.attributes.color.needsUpdate = true;
  }

  _buildMeltPool() {
    // Radial-gradient billboard sprite that sits at the active scan spot. The
    // sprite is screen-aligned (Sprite, not Mesh) so it always faces the
    // camera, and it uses additive blending so bloom can grab it. The texture
    // is painted once on a 64×64 canvas — the gradient does the work.
    const canvas = document.createElement('canvas');
    canvas.width = 64;
    canvas.height = 64;
    const ctx = canvas.getContext('2d');
    const grad = ctx.createRadialGradient(32, 32, 0, 32, 32, 32);
    grad.addColorStop(0.00, 'rgba(255, 255, 240, 1.0)');
    grad.addColorStop(0.18, 'rgba(255, 220, 140, 0.95)');
    grad.addColorStop(0.45, 'rgba(255, 130, 50, 0.6)');
    grad.addColorStop(0.75, 'rgba(180, 40, 10, 0.18)');
    grad.addColorStop(1.00, 'rgba(0, 0, 0, 0)');
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, 64, 64);
    const tex = new THREE.CanvasTexture(canvas);
    tex.anisotropy = 4;
    const mat = new THREE.SpriteMaterial({
      map: tex,
      transparent: true,
      depthWrite: false,
      depthTest: false,
      blending: THREE.AdditiveBlending,
    });
    const sprite = new THREE.Sprite(mat);
    sprite.name = 'lpbf-melt-pool';
    sprite.scale.set(8, 8, 1);
    // renderOrder so the sprite draws after the printed lines (depthTest is
    // off, but a high renderOrder also keeps it ordered correctly with the
    // heat-trail Points).
    sprite.renderOrder = 12;
    sprite.visible = false;
    return sprite;
  }

  _updateMeltPool(headMove, dt) {
    if (!this._meltPool) return;
    if (!headMove) {
      this._meltPool.visible = false;
      return;
    }
    // Sit just above the build surface (Y=0.1 in world) to avoid z-fighting
    // with the heat-trail Points, then pulse scale + opacity to suggest a
    // boiling melt pool. dt may be 0 on the first frame; default to a small
    // step so the phase advances.
    this._meltPhase += (dt > 0 ? dt : 0.016) * 9.0;
    const pulse = 0.5 + 0.5 * Math.sin(this._meltPhase);
    const scale = 7 + pulse * 3.5;
    this._meltPool.position.set(headMove.x, 0.1, headMove.y);
    this._meltPool.scale.set(scale, scale, 1);
    if (this._meltPool.material) {
      this._meltPool.material.opacity = 0.75 + pulse * 0.25;
    }
    this._meltPool.visible = true;
  }

  _hideMeltPool() {
    if (this._meltPool) this._meltPool.visible = false;
  }

  _startRecoaterSweep() {
    if (!this._recoater) return;
    const [bx] = this.bedSize;
    // Sweep left → right across the full bed in ~0.6s. Fast enough that the
    // user sees it on every layer transition without it dominating the sim.
    const startX = -RECOATER_OVERHANG;
    const endX = bx + RECOATER_OVERHANG;
    // Bar's depth axis is three.js Z (= backend bed Y). Center it on the bed
    // and ride 3mm above the build surface (which sits at world Y=0 in the
    // LPBF view because the printedGroup is sunk by topZ). The bedGroup never
    // sinks, so the recoater stays in the machine frame regardless of descent.
    this._recoater.position.set(startX, 3, this.bedSize[1] / 2);
    this._recoater.visible = true;
    this._recoaterState = {
      active: true,
      startX,
      endX,
      x: startX,
      speed: (endX - startX) / 0.6, // mm/s
    };
  }

  _tickRecoater(dt) {
    const r = this._recoaterState;
    if (!r || !r.active || !this._recoater) return;
    r.x += r.speed * dt;
    if (r.x >= r.endX) {
      this._stopRecoaterSweep();
      return;
    }
    this._recoater.position.x = r.x;
  }

  _stopRecoaterSweep() {
    if (this._recoater) this._recoater.visible = false;
    if (this._recoaterState) this._recoaterState.active = false;
  }
}
