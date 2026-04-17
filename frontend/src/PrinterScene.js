// PrinterScene: owns the Three.js renderer, camera, controls-less orbit, and
// the objects that represent the bed, the parts, and the running toolpath.
// The React layer only hands it data — this class is the sole Three.js user.

import * as THREE from 'three';

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

    this.head = this.makeHead();
    this.scene.add(this.head);
    this.head.visible = false;

    // simple orbit: drag=rotate, wheel=zoom, shift+drag=pan.
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
    this.renderer.dispose();
  }

  _resize = () => {
    const rect = this.canvas.getBoundingClientRect();
    const w = Math.max(1, Math.floor(rect.width));
    const h = Math.max(1, Math.floor(rect.height));
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
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
    let shiftPan = false;
    let lastX = 0;
    let lastY = 0;
    c.addEventListener('mousedown', (e) => {
      dragging = true;
      shiftPan = e.shiftKey;
      lastX = e.clientX;
      lastY = e.clientY;
    });
    window.addEventListener('mouseup', () => { dragging = false; });
    window.addEventListener('mousemove', (e) => {
      if (!dragging) return;
      const dx = e.clientX - lastX;
      const dy = e.clientY - lastY;
      lastX = e.clientX;
      lastY = e.clientY;
      if (shiftPan) {
        const panScale = this._orbit.radius * 0.002;
        this._orbit.target.x -= dx * panScale;
        this._orbit.target.z += dy * panScale;
      } else {
        this._orbit.azimuth -= dx * 0.008;
        this._orbit.polar = Math.max(0.1, Math.min(Math.PI - 0.1, this._orbit.polar - dy * 0.008));
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
    const geo = new THREE.ConeGeometry(4, 10, 16);
    geo.rotateX(Math.PI); // point down
    geo.translate(0, -5, 0);
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
    this.toolpathGroup.clear();
    this.printedGroup.clear();
    this._moves = moves || [];
    this._lastCursor = 0;

    if (!moves || moves.length === 0) {
      this.head.visible = false;
      return;
    }

    // Ghost of the full toolpath, drawn dim.
    const positions = [];
    for (let i = 0; i < moves.length - 1; i++) {
      const a = moves[i];
      const b = moves[i + 1];
      if (b.kind !== 'extrude') continue;
      positions.push(a.x, a.z, a.y, b.x, b.z, b.y);
    }
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
    const mat = new THREE.LineBasicMaterial({ color: 0x2a82e4, transparent: true, opacity: 0.25 });
    const lines = new THREE.LineSegments(geo, mat);
    this.toolpathGroup.add(lines);

    // Pre-build a "printed so far" line segments buffer updated in setCursor.
    this._printedGeom = new THREE.BufferGeometry();
    const maxVerts = positions.length;
    this._printedPositions = new Float32Array(maxVerts);
    this._printedGeom.setAttribute('position', new THREE.Float32BufferAttribute(this._printedPositions, 3));
    this._printedGeom.setDrawRange(0, 0);
    const printedMat = new THREE.LineBasicMaterial({ color: 0xff9955 });
    const printedLines = new THREE.LineSegments(this._printedGeom, printedMat);
    this.printedGroup.add(printedLines);
    this._printedVertCount = 0;

    this.head.visible = true;
    this.setCursor(0);
  }

  setCursor(cursor) {
    if (!this._moves || this._moves.length === 0) return;
    const moves = this._moves;
    cursor = Math.max(0, Math.min(moves.length, cursor));

    // Rebuild printed segments from 0..cursor (simpler and correct after scrubbing).
    const pos = this._printedPositions;
    let n = 0;
    for (let i = 0; i < cursor - 1 && i < moves.length - 1; i++) {
      const a = moves[i];
      const b = moves[i + 1];
      if (b.kind !== 'extrude') continue;
      pos[n++] = a.x; pos[n++] = a.z; pos[n++] = a.y;
      pos[n++] = b.x; pos[n++] = b.z; pos[n++] = b.y;
    }
    this._printedGeom.attributes.position.needsUpdate = true;
    this._printedGeom.setDrawRange(0, n / 3);
    this._printedVertCount = n / 3;

    const head = moves[Math.min(cursor, moves.length - 1)];
    if (head) {
      this.head.position.set(head.x, head.z + 3, head.y);
    }
    this._lastCursor = cursor;
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
