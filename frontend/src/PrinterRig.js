// One "printer rig" — all the Three.js state for a single printer: its bed,
// parts, toolpath + printed filament lines, and nozzle head. Factored out of
// PrinterScene so the factory 3D view can mount N rigs on a shared shelf
// without duplicating the renderer/camera/lights that the single-printer view
// already owns.
//
// A rig lives inside a THREE.Group parent (the "rigRoot") so the host can
// translate the whole printer to a shelf position by setting
// `rigRoot.position`. Geometry inside the rig stays in local bed coordinates
// (0..bedSize on each axis), which means all the setBed/setParts/setToolpath
// math carries over unchanged.

import * as THREE from 'three';
import { LineSegments2 } from 'three/addons/lines/LineSegments2.js';
import { LineSegmentsGeometry } from 'three/addons/lines/LineSegmentsGeometry.js';
import { LineMaterial } from 'three/addons/lines/LineMaterial.js';

const GLOW_SEGMENTS = 40;

const ROLE_BASE = {
  perimeter:          [0.18, 0.62, 0.95],
  overhang_perimeter: [1.00, 0.38, 0.18],
  infill_sparse:      [0.55, 0.55, 0.62],
  bottom:             [0.95, 0.55, 0.20],
  top:                [0.85, 0.90, 0.45],
  support:            [0.35, 0.80, 0.55],
};
const ROLE_DEFAULT = [0.95, 0.42, 0.18];
const HOT_COLOR = [1.0, 0.98, 0.75];

const DEPTH_LIGHTEN = 0.35;
const DEPTH_DARKEN = 0.30;

export class PrinterRig {
  // host: the owning PrinterScene — supplies canvas rect for LineMaterial
  //       resolution and a scene node to parent the rig group into.
  // id:   string identifier, used by the host to route setBed(id, ...) etc.
  // position: { x, z } in world mm — where on the shelf this rig sits.
  //           Y is always 0 (all beds rest on the shelf plane).
  constructor(host, id, { x = 0, z = 0 } = {}) {
    this.host = host;
    this.id = id;

    this.rigRoot = new THREE.Group();
    this.rigRoot.name = `rig-${id}`;
    this.rigRoot.position.set(x, 0, z);
    host.scene.add(this.rigRoot);

    this.bedGroup = new THREE.Group();
    this.partsGroup = new THREE.Group();
    this.toolpathGroup = new THREE.Group();
    this.printedGroup = new THREE.Group();
    this.rigRoot.add(this.bedGroup, this.partsGroup, this.toolpathGroup, this.printedGroup);

    this.toolpathGroup.visible = false;
    this._partsSimVisible = false;

    this.head = this._makeHead();
    this.rigRoot.add(this.head);
    this.head.visible = false;

    this.bedSize = [250, 210, 210];
    this.setBed(...this.bedSize);

    // Per-rig visual budget. When false, setCursor only moves the head
    // (skipping the printed-filament rebuild). Cheaper for rigs the user
    // isn't focused on in the factory shelf.
    this._renderFullDetail = true;

    // Optional name sprite floating above the bed — useful on the factory
    // shelf so users can tell printer 1-1 from 3-2 at a glance.
    this._labelSprite = null;
    this._statusSprite = null;
  }

  dispose() {
    this._disposeGroup(this.toolpathGroup);
    this._disposeGroup(this.printedGroup);
    this._disposeGroup(this.partsGroup);
    this._disposeGroup(this.bedGroup);
    if (this.head.material) this.head.material.dispose();
    if (this.head.geometry) this.head.geometry.dispose();
    if (this.rigRoot.parent) this.rigRoot.parent.remove(this.rigRoot);
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

  // Set the rig's world position on the shelf.
  setShelfPosition(x, z) {
    this.rigRoot.position.set(x, 0, z);
  }

  // Full-detail vs budget rendering. Budget mode skips the printed-filament
  // rebuild in setCursor — useful when 10+ rigs animate at once. The ghost
  // toolpath is also hidden in budget mode so faraway beds stay clean.
  setRenderBudget(full) {
    this._renderFullDetail = !!full;
    if (!full) {
      this.toolpathGroup.visible = false;
      this._disposeGroup(this.printedGroup);
      this.printedGroup.clear();
      this._printedVertCount = 0;
    } else if (this._moves && this._moves.length > 0) {
      // Rebuild the printed-filament view at the last known cursor.
      this.setCursor(this._lastCursor || 0);
    }
  }

  _updateLineResolution(w, h) {
    if (this._ghostMat) this._ghostMat.resolution.set(w, h);
    if (this._printedMat) this._printedMat.resolution.set(w, h);
  }

  _makeHead() {
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
    this._disposeGroup(this.bedGroup);
    this.bedGroup.clear();

    const plate = new THREE.Mesh(
      new THREE.BoxGeometry(x, 1, y),
      new THREE.MeshStandardMaterial({ color: 0x1a1f25, roughness: 0.9 }),
    );
    plate.position.set(x / 2, -0.5, y / 2);
    this.bedGroup.add(plate);

    const grid = new THREE.GridHelper(
      Math.max(x, y),
      Math.max(x, y) / 10,
      0x2a313a,
      0x1a1f25,
    );
    grid.position.set(x / 2, 0.01, y / 2);
    this.bedGroup.add(grid);

    const boxGeo = new THREE.BoxGeometry(x, z, y);
    const edges = new THREE.EdgesGeometry(boxGeo);
    const line = new THREE.LineSegments(
      edges,
      new THREE.LineBasicMaterial({ color: 0x2a82e4, transparent: true, opacity: 0.35 }),
    );
    line.position.set(x / 2, z / 2, y / 2);
    this.bedGroup.add(line);

    this.bedGroup.add(this._buildAxesGizmo(
      Math.max(20, Math.min(40, Math.min(x, y) * 0.12)),
    ));
  }

  _buildAxesGizmo(size) {
    const group = new THREE.Group();
    group.name = 'axes-gizmo';
    group.position.set(0, 0, 0);

    const head = size * 0.18;
    const headW = size * 0.08;
    const axes = [
      { label: 'X', color: 0xff5050, dir: new THREE.Vector3(1, 0, 0) },
      { label: 'Y', color: 0x50d060, dir: new THREE.Vector3(0, 0, 1) },
      { label: 'Z', color: 0x5094ff, dir: new THREE.Vector3(0, 1, 0) },
    ];
    for (const a of axes) {
      const arrow = new THREE.ArrowHelper(
        a.dir,
        new THREE.Vector3(0, 0, 0),
        size, a.color, head, headW,
      );
      if (arrow.line && arrow.line.material) arrow.line.material.linewidth = 2;
      group.add(arrow);
      const sprite = this._makeAxisLabel(a.label, a.color);
      sprite.position.copy(a.dir).multiplyScalar(size + head * 0.8);
      group.add(sprite);
    }
    return group;
  }

  _makeAxisLabel(text, color) {
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
    const material = new THREE.SpriteMaterial({
      map: texture,
      transparent: true,
      depthTest: false,
    });
    const sprite = new THREE.Sprite(material);
    sprite.scale.set(14, 14, 1);
    sprite.renderOrder = 10;
    return sprite;
  }

  setParts(parts, geometryById) {
    this._disposeGroup(this.partsGroup);
    this.partsGroup.clear();
    for (const part of parts) {
      const geomData = geometryById[part.id];
      if (!geomData) continue;
      const positions = [];
      for (const tri of geomData.triangles) {
        for (const v of tri) {
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
      mesh.userData.rigId = this.id;
      mesh.userData.placement = part.placement || null;
      geom.computeBoundingBox();
      this.partsGroup.add(mesh);
    }
  }

  setToolpath(moves) {
    this._disposeGroup(this.toolpathGroup);
    this._disposeGroup(this.printedGroup);
    this.toolpathGroup.clear();
    this.printedGroup.clear();
    this._moves = moves || [];
    this._lastCursor = 0;

    if (!moves || moves.length === 0) {
      this.head.visible = false;
      this.partsGroup.visible = true;
      this._ghostMat = null;
      this._printedMat = null;
      this._printedPositions = null;
      this._printedColors = null;
      this._extrudeSegments = null;
      this._printedVertCount = 0;
      return;
    }

    const segs = [];
    let zMin = Infinity;
    let zMax = -Infinity;
    for (let i = 0; i < moves.length - 1; i++) {
      const a = moves[i];
      const b = moves[i + 1];
      if (b.kind !== 'extrude') continue;
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

    const { w: resW, h: resH } = this.host.viewportSize();

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

    let visibleSegs = 0;
    for (let i = 0; i < segs.length; i++) {
      if (segs[i].moveIndex < cursor) visibleSegs = i + 1;
      else break;
    }

    const head = moves[Math.min(cursor, moves.length - 1)];
    if (head) {
      this.head.position.set(head.x, head.z + 0.5, head.y);
    }
    this._lastCursor = cursor;

    // Budget path: head position only, no printed-filament rebuild.
    if (!this._renderFullDetail) {
      this._printedVertCount = visibleSegs * 2;
      return;
    }

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
      const posView = new Float32Array(pos.buffer, pos.byteOffset, visibleSegs * 6);
      const colView = new Float32Array(col.buffer, col.byteOffset, visibleSegs * 6);
      const geom = new LineSegmentsGeometry();
      geom.setPositions(posView);
      geom.setColors(colView);
      const lines = new LineSegments2(geom, this._printedMat);
      this.printedGroup.add(lines);
    }
    this._printedVertCount = visibleSegs * 2;
  }

  setToolpathVisible(visible) {
    this.toolpathGroup.visible = !!visible && this._renderFullDetail;
  }

  setPartsSimVisible(visible) {
    this._partsSimVisible = !!visible;
    if (this._moves && this._moves.length > 0) {
      this.setCursor(this._lastCursor || 0);
    }
  }

  // Set a text label floating above the bed. Pass '' to clear.
  setLabel(text) {
    if (this._labelSprite) {
      this.rigRoot.remove(this._labelSprite);
      if (this._labelSprite.material?.map) this._labelSprite.material.map.dispose();
      this._labelSprite.material.dispose();
      this._labelSprite = null;
    }
    if (!text) return;
    const [bx, by, bz] = this.bedSize;
    this._labelSprite = this._makeTextSprite(text, '#e2e7ee', 48, 320);
    this._labelSprite.position.set(bx / 2, bz + 40, by / 2);
    this._labelSprite.scale.set(bx * 0.6, bx * 0.18, 1);
    this.rigRoot.add(this._labelSprite);
  }

  // Small colored status pill below the label (idle/printing/finished/...).
  setStatus(text, color = '#8b9299') {
    if (this._statusSprite) {
      this.rigRoot.remove(this._statusSprite);
      if (this._statusSprite.material?.map) this._statusSprite.material.map.dispose();
      this._statusSprite.material.dispose();
      this._statusSprite = null;
    }
    if (!text) return;
    const [bx, by, bz] = this.bedSize;
    this._statusSprite = this._makeTextSprite(text, color, 40, 260);
    this._statusSprite.position.set(bx / 2, bz + 20, by / 2);
    this._statusSprite.scale.set(bx * 0.45, bx * 0.12, 1);
    this.rigRoot.add(this._statusSprite);
  }

  _makeTextSprite(text, color, fontSize, pxWidth) {
    const canvas = document.createElement('canvas');
    canvas.width = pxWidth;
    canvas.height = Math.round(pxWidth * 0.3);
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = color;
    ctx.font = `bold ${fontSize}px system-ui, sans-serif`;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.shadowColor = 'rgba(0, 0, 0, 0.85)';
    ctx.shadowBlur = 6;
    ctx.fillText(text, canvas.width / 2, canvas.height / 2);
    const texture = new THREE.CanvasTexture(canvas);
    texture.anisotropy = 4;
    const material = new THREE.SpriteMaterial({
      map: texture,
      transparent: true,
      depthTest: false,
    });
    const sprite = new THREE.Sprite(material);
    sprite.renderOrder = 20;
    return sprite;
  }

  // World-space AABB of this rig's parts (or its bed if no parts).
  boundingBox() {
    const THREEModule = THREE;
    const bbox = new THREEModule.Box3();
    if (this.partsGroup.children.length > 0) {
      bbox.makeEmpty();
      for (const child of this.partsGroup.children) {
        bbox.union(new THREEModule.Box3().setFromObject(child));
      }
    }
    if (bbox.isEmpty()) {
      const [bx, by, bz] = this.bedSize;
      const origin = this.rigRoot.position;
      bbox.set(
        new THREEModule.Vector3(origin.x, 0, origin.z),
        new THREEModule.Vector3(origin.x + bx, bz, origin.z + by),
      );
    }
    return bbox;
  }

  stats() {
    return {
      id: this.id,
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
