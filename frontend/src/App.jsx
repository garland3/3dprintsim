import React, { useCallback, useEffect, useRef, useState } from 'react';
import { api } from './api.js';
import { PrinterScene } from './PrinterScene.js';

export default function App() {
  const canvasRef = useRef(null);
  const sceneRef = useRef(null);

  const [bed, setBed] = useState({ x: 250, y: 210, z: 210 });
  const [parts, setParts] = useState([]);
  const [sliceSummary, setSliceSummary] = useState(null);
  const [sim, setSim] = useState({ running: false, cursor: 0, total: 0, speed: 1 });
  const [layerHeight, setLayerHeight] = useState(0.4);
  const [perimeters, setPerimeters] = useState(1);
  const [infillDensity, setInfillDensity] = useState(0.2);
  const [topLayers, setTopLayers] = useState(3);
  const [bottomLayers, setBottomLayers] = useState(3);
  const [error, setError] = useState('');
  const [dragging, setDragging] = useState(false);

  // Initialize Three.js scene once.
  useEffect(() => {
    if (!canvasRef.current) return;
    const scene = new PrinterScene(canvasRef.current);
    sceneRef.current = scene;
    // expose for Playwright:
    window.__printerScene = scene;
    return () => {
      scene.dispose();
      sceneRef.current = null;
      window.__printerScene = null;
    };
  }, []);

  const refreshState = useCallback(async () => {
    try {
      const st = await api.state();
      setBed({ x: st.bed_size[0], y: st.bed_size[1], z: st.bed_size[2] });
      setParts(st.parts);
      setSliceSummary(st.slice);
      setSim({
        running: st.simulation.running,
        cursor: st.simulation.cursor,
        total: st.simulation.total_moves,
        speed: st.simulation.speed,
      });
      if (sceneRef.current) sceneRef.current.setBed(st.bed_size[0], st.bed_size[1], st.bed_size[2]);
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => { refreshState(); }, [refreshState]);

  // Refresh part geometries whenever the parts list changes.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (!sceneRef.current) return;
      if (parts.length === 0) {
        sceneRef.current.setParts([], {});
        return;
      }
      const geomEntries = await Promise.all(
        parts.map(async (p) => [p.id, await api.partGeometry(p.id)]),
      );
      if (cancelled) return;
      const geomById = Object.fromEntries(geomEntries);
      sceneRef.current.setParts(parts, geomById);
    })();
    return () => { cancelled = true; };
  }, [parts]);

  const handleUpload = async (file) => {
    if (!file) return;
    setError('');
    try {
      // Upload: the backend now centers a single part and re-packs when there
      // are multiple, so we don't need a second /arrange round-trip from here.
      await api.upload(file);
      await refreshState();
    } catch (e) {
      setError(String(e));
    }
  };

  const handleArrange = async () => {
    setError('');
    try {
      await api.arrange();
      await refreshState();
    } catch (e) { setError(String(e)); }
  };

  const handleSlice = async () => {
    setError('');
    try {
      const summary = await api.slice({
        layer_height: Number(layerHeight),
        perimeters: Number(perimeters),
        infill_density: Number(infillDensity),
        top_layers: Number(topLayers),
        bottom_layers: Number(bottomLayers),
      });
      setSliceSummary(summary);
      const slicePayload = await api.getSlice();
      if (sceneRef.current && slicePayload.ready) {
        sceneRef.current.setToolpath(slicePayload.moves);
      }
      await refreshState();
    } catch (e) { setError(String(e)); }
  };

  const handleStartSim = async () => {
    setError('');
    try {
      // ensure we have a toolpath loaded into the scene after a fresh load
      const slicePayload = await api.getSlice();
      if (sceneRef.current && slicePayload.ready) {
        sceneRef.current.setToolpath(slicePayload.moves);
      }
      await api.startSim(1.0);
      await refreshState();
    } catch (e) { setError(String(e)); }
  };

  // Animation loop: when running, repeatedly step the cursor forward.
  useEffect(() => {
    if (!sim.running || sim.total === 0) return;
    let cancelled = false;
    const tick = async () => {
      if (cancelled) return;
      try {
        const stepSize = Math.max(1, Math.round(sim.total / 200));
        const res = await api.stepSim(stepSize);
        if (cancelled) return;
        setSim((s) => ({ ...s, cursor: res.cursor, running: res.cursor < s.total }));
        if (sceneRef.current) sceneRef.current.setCursor(res.cursor);
        if (res.cursor < sim.total) {
          setTimeout(tick, 50);
        }
      } catch (e) {
        setError(String(e));
      }
    };
    tick();
    return () => { cancelled = true; };
  }, [sim.running, sim.total]);

  const handleCursor = async (v) => {
    const cursor = Number(v);
    try {
      await api.setCursor(cursor);
      // Scrubbing should not auto-resume playback — pause while we scrub.
      setSim((s) => ({ ...s, cursor, running: false }));
      if (sceneRef.current) sceneRef.current.setCursor(cursor);
    } catch (e) { setError(String(e)); }
  };

  const handleBedChange = async () => {
    setError('');
    try {
      await api.setBed(Number(bed.x), Number(bed.y), Number(bed.z));
      await refreshState();
    } catch (e) { setError(String(e)); }
  };

  const removePart = async (id) => {
    try {
      await api.removePart(id);
      await refreshState();
    } catch (e) { setError(String(e)); }
  };

  const clearAll = async () => {
    try {
      await api.clearParts();
      if (sceneRef.current) sceneRef.current.setToolpath([]);
      await refreshState();
    } catch (e) { setError(String(e)); }
  };

  const onDrop = (e) => {
    e.preventDefault();
    setDragging(false);
    const f = e.dataTransfer.files[0];
    if (f) handleUpload(f);
  };

  return (
    <div className="app">
      <aside className="sidebar" data-testid="sidebar">
        <h1>3D Print Sim</h1>
        {error && <div className="error" data-testid="error">{error}</div>}

        <h2>Bed</h2>
        <div className="row">
          <label>X</label>
          <input type="number" value={bed.x} onChange={(e) => setBed({ ...bed, x: e.target.value })} data-testid="bed-x" />
          <label>Y</label>
          <input type="number" value={bed.y} onChange={(e) => setBed({ ...bed, y: e.target.value })} data-testid="bed-y" />
          <label>Z</label>
          <input type="number" value={bed.z} onChange={(e) => setBed({ ...bed, z: e.target.value })} data-testid="bed-z" />
        </div>
        <button className="secondary" onClick={handleBedChange} data-testid="apply-bed">Apply bed size</button>

        <h2>Upload STL</h2>
        <label
          className={`dropzone ${dragging ? 'dragging' : ''}`}
          onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
          data-testid="dropzone"
        >
          Drop .stl here or click to browse
          <input
            type="file"
            accept=".stl"
            style={{ display: 'none' }}
            onChange={(e) => {
              const f = e.target.files[0];
              e.target.value = ''; // allow re-uploading the same file
              handleUpload(f);
            }}
            data-testid="file-input"
          />
        </label>

        <h2>Parts ({parts.length})</h2>
        <ul className="parts-list" data-testid="parts-list">
          {parts.map((p) => (
            <li key={p.id} data-testid={`part-${p.id}`}>
              <div>
                <div>{p.name}</div>
                <div className="meta">
                  {p.size.map((s) => s.toFixed(1)).join(' × ')} mm · {p.triangle_count} tris
                </div>
              </div>
              <button className="remove" onClick={() => removePart(p.id)} aria-label="remove">×</button>
            </li>
          ))}
        </ul>
        {parts.length > 0 && (
          <div className="button-row">
            <button className="secondary" onClick={handleArrange} data-testid="arrange">Auto-arrange</button>
            <button className="danger" onClick={clearAll} data-testid="clear">Clear</button>
          </div>
        )}

        <h2>Slicer</h2>
        <div className="row">
          <label>Layer</label>
          <input type="number" step="0.1" value={layerHeight} onChange={(e) => setLayerHeight(e.target.value)} data-testid="layer-height" />
          <label>Peri.</label>
          <input type="number" step="1" value={perimeters} onChange={(e) => setPerimeters(e.target.value)} data-testid="perimeters" />
        </div>
        <div className="row">
          <label>Infill %</label>
          <input
            type="number"
            step="5"
            min="0"
            max="100"
            value={Math.round(infillDensity * 100)}
            onChange={(e) => setInfillDensity(Math.max(0, Math.min(100, Number(e.target.value))) / 100)}
            data-testid="infill-density"
          />
          <label>Top</label>
          <input type="number" step="1" min="0" value={topLayers} onChange={(e) => setTopLayers(e.target.value)} data-testid="top-layers" />
          <label>Bot.</label>
          <input type="number" step="1" min="0" value={bottomLayers} onChange={(e) => setBottomLayers(e.target.value)} data-testid="bottom-layers" />
        </div>
        <button onClick={handleSlice} disabled={parts.length === 0} data-testid="slice">Slice</button>

        {sliceSummary && (
          <div className="status" data-testid="slice-summary">
            <div>{sliceSummary.layer_count} layers</div>
            <div>{sliceSummary.move_count} moves</div>
            <div>{sliceSummary.total_extrusion?.toFixed(2) ?? '0.00'} mm extruded</div>
          </div>
        )}

        <h2>Simulation</h2>
        <div className="button-row">
          <button onClick={handleStartSim} disabled={!sliceSummary} data-testid="start-sim">Start</button>
          <button className="secondary" onClick={() => handleCursor(0)} disabled={!sliceSummary} data-testid="reset-sim">Reset</button>
          <button
            className="secondary"
            onClick={() => handleCursor(sim.total)}
            disabled={!sliceSummary}
            data-testid="finish-sim"
          >
            Jump to end
          </button>
        </div>
        {sliceSummary && (
          <div className="slider-row" style={{ marginTop: 8 }}>
            <input
              type="range"
              min="0"
              max={sim.total || 0}
              value={sim.cursor}
              onChange={(e) => handleCursor(e.target.value)}
              data-testid="sim-slider"
            />
            <span className="value" data-testid="sim-cursor">{sim.cursor} / {sim.total}</span>
          </div>
        )}
      </aside>

      <main className="viewer">
        <canvas ref={canvasRef} data-testid="viewer-canvas" />
        <div className="overlay" data-testid="overlay">
          bed {bed.x}×{bed.y}×{bed.z} · parts {parts.length}
          {sliceSummary ? ` · ${sliceSummary.layer_count} layers` : ''}
          {sim.total ? ` · ${sim.cursor}/${sim.total}` : ''}
        </div>
      </main>
    </div>
  );
}
