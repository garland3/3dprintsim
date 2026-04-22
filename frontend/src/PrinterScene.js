// PrinterScene: owns the Three.js renderer, camera, orbit input, and a
// registry of PrinterRig objects. A default rig is created at the origin so
// the single-printer UI keeps working without any id plumbing. The factory
// 3D view calls addRig() to lay N printers out on a shelf.
//
// All per-printer state (bed, parts, toolpath, printed filament, head) lives
// on PrinterRig — see PrinterRig.js. This module only owns the globals
// (renderer, camera, lights, RAF, input) and the shelf layout.

import * as THREE from 'three';
import { PrinterRig } from './PrinterRig.js';

const DEFAULT_RIG = 'default';

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

    this._orbit = {
      azimuth: Math.PI / 4,
      polar: Math.PI / 3,
      radius: 500,
      target: new THREE.Vector3(),
    };

    // Rig registry. The single-printer UI only ever uses the default rig; the
    // factory view adds/removes rigs as the grid config changes.
    this.rigs = new Map();
    this._focusedRigId = DEFAULT_RIG;
    this.addRig(DEFAULT_RIG, { x: 0, z: 0 });

    this._resize();
    window.addEventListener('resize', this._resize);

    this._applyOrbit();
    this._attachInput();

    this._raf = null;
    this._animate();
  }

  // ---------- rig management ----------

  addRig(id, position = { x: 0, z: 0 }) {
    if (this.rigs.has(id)) return this.rigs.get(id);
    const rig = new PrinterRig(this, id, position);
    this.rigs.set(id, rig);
    return rig;
  }

  removeRig(id) {
    const rig = this.rigs.get(id);
    if (!rig) return;
    rig.dispose();
    this.rigs.delete(id);
    if (this._focusedRigId === id) {
      this._focusedRigId = this.rigs.keys().next().value || DEFAULT_RIG;
    }
  }

  // Drop every rig except the default one (or all rigs if `keepDefault`
  // is false). Used by the factory view when the grid config changes.
  clearRigs({ keepDefault = true } = {}) {
    for (const id of [...this.rigs.keys()]) {
      if (keepDefault && id === DEFAULT_RIG) continue;
      this.removeRig(id);
    }
  }

  rig(id = DEFAULT_RIG) {
    return this.rigs.get(id);
  }

  setFocusedRig(id) {
    if (!this.rigs.has(id)) return;
    this._focusedRigId = id;
  }

  // Called by rigs that need the current viewport size for LineMaterial
  // resolution (they can't read the canvas directly without coupling to
  // host internals).
  viewportSize() {
    const rect = this.canvas.getBoundingClientRect();
    return {
      w: Math.max(1, Math.floor(rect.width)),
      h: Math.max(1, Math.floor(rect.height)),
    };
  }

  // ---------- per-rig facade (keeps single-printer API intact) ----------

  setBed(x, y, z, id = DEFAULT_RIG) {
    this.rig(id)?.setBed(x, y, z);
    // Original single-printer behavior: changing the bed recenters the orbit
    // on the bed so the user sees the new volume framed. Only do this for the
    // default rig — the factory shelf places rigs on a grid and its view
    // is managed by the FactoryView.
    if (id === DEFAULT_RIG && this.rigs.size === 1) {
      this._orbit.target.set(x / 2, z / 4, y / 2);
      this._orbit.radius = Math.max(x, y) * 2;
      this._applyOrbit();
    }
  }
  setParts(parts, geometryById, id = DEFAULT_RIG) { this.rig(id)?.setParts(parts, geometryById); }
  setToolpath(moves, id = DEFAULT_RIG) { this.rig(id)?.setToolpath(moves); }
  setCursor(cursor, id = DEFAULT_RIG) { this.rig(id)?.setCursor(cursor); }
  setToolpathVisible(v, id = DEFAULT_RIG) { this.rig(id)?.setToolpathVisible(v); }
  setPartsSimVisible(v, id = DEFAULT_RIG) { this.rig(id)?.setPartsSimVisible(v); }

  stats(id = DEFAULT_RIG) {
    const rig = this.rig(id);
    if (!rig) return null;
    const s = rig.stats();
    // Historical shape: single-rig callers don't know about `id`, so we drop
    // it for backward compatibility.
    const { id: _, ...rest } = s;
    return rest;
  }

  // ---------- camera / scene globals ----------

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
    for (const rig of this.rigs.values()) rig.dispose();
    this.rigs.clear();
    this.renderer.dispose();
  }

  _resize = () => {
    const { w, h } = this.viewportSize();
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    for (const rig of this.rigs.values()) rig._updateLineResolution(w, h);
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

    const raycaster = new THREE.Raycaster();
    const ndc = new THREE.Vector2();
    const bedPlane = new THREE.Plane(new THREE.Vector3(0, 1, 0), 0);
    const hit = new THREE.Vector3();
    this._partDrag = null;

    // Flatten the currently-pickable part meshes across all rigs. Cheap: most
    // scenes have ≤1 part per rig and ≤9 rigs total.
    const pickablePartMeshes = () => {
      const out = [];
      for (const rig of this.rigs.values()) {
        for (const m of rig.partsGroup.children) out.push(m);
      }
      return out;
    };

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
      const hits = raycaster.intersectObjects(pickablePartMeshes(), false);
      for (const h of hits) {
        const pid = h.object?.userData?.partId;
        if (pid) {
          return {
            mesh: h.object,
            partId: pid,
            rigId: h.object.userData.rigId || DEFAULT_RIG,
          };
        }
      }
      return null;
    };

    const mousedown = (e) => {
      if (e.button === 2 || (e.button === 0 && e.shiftKey)) {
        panning = true;
        dragging = false;
      } else if (e.button === 0) {
        const picked = pickPart(e);
        const anchor = picked ? pointerToBed(e) : null;
        if (picked && anchor) {
          const mesh = picked.mesh;
          if (!mesh.geometry.boundingBox) mesh.geometry.computeBoundingBox();
          const bb = mesh.geometry.boundingBox;
          const rig = this.rig(picked.rigId);
          this._partDrag = {
            mesh,
            partId: picked.partId,
            rigId: picked.rigId,
            baseMinX: bb.min.x,
            baseMinZ: bb.min.z,
            width: bb.max.x - bb.min.x,
            depth: bb.max.z - bb.min.z,
            anchorX: anchor.x,
            anchorZ: anchor.z,
            rigOffsetX: rig ? rig.rigRoot.position.x : 0,
            rigOffsetZ: rig ? rig.rigRoot.position.z : 0,
            bedSize: rig ? rig.bedSize : [250, 210, 210],
          };
          this.setFocusedRig(picked.rigId);
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
        const newX = d.baseMinX + d.mesh.position.x;
        const newY = d.baseMinZ + d.mesh.position.z;
        if (typeof this.onPartDragEnd === 'function') {
          try {
            await this.onPartDragEnd(d.partId, newX, newY, d.rigId);
          } catch (_) {
            // surface the error through the caller
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
        // Pointer is in world coords; convert to rig-local by subtracting the
        // rig's shelf offset before clamping to the rig's bed.
        const localAnchorX = d.anchorX - d.rigOffsetX;
        const localAnchorZ = d.anchorZ - d.rigOffsetZ;
        const localPx = p.x - d.rigOffsetX;
        const localPz = p.z - d.rigOffsetZ;
        let dx = localPx - localAnchorX;
        let dz = localPz - localAnchorZ;
        const [bx, , by] = d.bedSize;
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

  // Snap the orbit to a canonical camera pose, framing either a specific rig
  // or (if id is null) the whole shelf.
  setView(name, id = null) {
    const bbox = this._frameBox(id);
    const size = new THREE.Vector3();
    bbox.getSize(size);
    const center = bbox.getCenter(new THREE.Vector3());
    this._orbit.target.copy(center);
    this._orbit.target.y = 0;
    this._orbit.radius = Math.max(size.x, size.z, 1) * 1.6;
    switch (name) {
      case 'top':
        this._orbit.polar = 0.02;
        this._orbit.azimuth = 0;
        break;
      case 'front':
        this._orbit.polar = Math.PI / 2;
        this._orbit.azimuth = Math.PI / 2;
        break;
      case 'right':
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

  // Frame the camera on a rig's parts, or (if id is null) the whole shelf.
  focus(id = null) {
    const bbox = id ? this.rig(id)?.boundingBox() : this._frameBox(null);
    if (!bbox || bbox.isEmpty()) return;

    const size = bbox.getSize(new THREE.Vector3());
    const center = bbox.getCenter(new THREE.Vector3());

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
    // Clamp is generous on the high end so a 10x10 factory shelf (~4m across)
    // can still be fully framed.
    this._orbit.radius = Math.max(50, Math.min(8000, radius));
    this._applyOrbit();
  }

  // World-space AABB covering every rig (shelf-level) or one rig.
  _frameBox(id) {
    if (id) {
      const r = this.rig(id);
      return r ? r.boundingBox() : new THREE.Box3();
    }
    const box = new THREE.Box3();
    box.makeEmpty();
    for (const rig of this.rigs.values()) box.union(rig.boundingBox());
    return box;
  }
}
