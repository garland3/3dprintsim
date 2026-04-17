import React, { useCallback, useEffect, useRef, useState } from 'react';
import { api } from './api.js';
import { PrinterScene } from './PrinterScene.js';

// 1 inch in mm — used when the user picks "inches" from the unit dropdown.
const MM_PER_INCH = 25.4;

// Tiny inline spinner that inherits the surrounding text color. Rendered in
// place of a button label (or part icon) while an in-flight request resolves.
function Spinner({ className = '' }) {
  return <span className={`spinner ${className}`.trim()} aria-label="loading" role="status" />;
}

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
  const [uploadUnit, setUploadUnit] = useState('mm'); // 'mm' | 'in'
  const [scaleDraft, setScaleDraft] = useState({}); // per-part in-progress scale input
  const [error, setError] = useState('');
  const [dragging, setDragging] = useState(false);
  const [pending, setPending] = useState({});
  const [showToolpath, setShowToolpath] = useState(false);
  // Synchronous mirror of `pending`. React's setPending is async/batched, so a
  // guard that reads the state-updater closure can miss a rapid re-entrant
  // click. The ref gives us an immediate lockout before we kick off `fn`.
  const pendingRef = useRef({});

  // Wrap an async handler so the corresponding `pending[key]` flag is set for
  // the duration of the request. Callers gate their button on `isPending(key)`
  // to both disable re-entry and swap the label for a spinner.
  const isPending = useCallback((key) => !!pending[key], [pending]);
  const run = useCallback((key, fn) => async (...args) => {
    if (pendingRef.current[key]) return;
    pendingRef.current[key] = true;
    setPending({ ...pendingRef.current });
    setError('');
    try {
      return await fn(...args);
    } catch (e) {
      setError(String(e));
    } finally {
      delete pendingRef.current[key];
      setPending({ ...pendingRef.current });
    }
  }, []);

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

  const focusView = useCallback(() => {
    if (sceneRef.current) sceneRef.current.focus();
  }, []);

  // 'f' hotkey — ignore when the user is typing into a form field so it
  // doesn't swallow a keystroke inside a number input.
  useEffect(() => {
    const onKey = (e) => {
      if (e.key !== 'f' && e.key !== 'F') return;
      const t = e.target;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return;
      e.preventDefault();
      focusView();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [focusView]);

  // Poll the backend for one-shot viewer requests (e.g. MCP-initiated focus).
  // Two-second cadence is fine: this is a developer tool and the polls are
  // tiny. Only the delta (counter increasing) triggers the local action.
  useEffect(() => {
    let cancelled = false;
    let lastFocus = null;
    const tick = async () => {
      try {
        const r = await api.viewerRequests();
        if (cancelled) return;
        if (lastFocus !== null && r.focus_request > lastFocus) {
          focusView();
        }
        lastFocus = r.focus_request;
      } catch {
        // intentional swallow: backend hiccups shouldn't crash the UI
      }
    };
    tick();
    const id = setInterval(tick, 2000);
    return () => { cancelled = true; clearInterval(id); };
  }, [focusView]);

  // Keep the scene's toolpath visibility in sync with the sidebar toggle.
  useEffect(() => {
    if (sceneRef.current) sceneRef.current.setToolpathVisible(showToolpath);
  }, [showToolpath]);

  const refreshState = useCallback(async () => {
    try {
      const st = await api.state();
      setBed({ x: st.bed_size[0], y: st.bed_size[1], z: st.bed_size[2] });
      setParts(st.parts);
      setSliceSummary(st.slice);
      setSim((s) => ({
        ...s,
        // Don't let a backend refresh clobber a client-side simulation in
        // flight; the local RAF loop is authoritative while running.
        running: s.running ? s.running : st.simulation.running,
        cursor: s.running ? s.cursor : st.simulation.cursor,
        total: st.simulation.total_moves,
        speed: st.simulation.speed,
      }));
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

  const handleUpload = run('upload', async (file) => {
    if (!file) return;
    const scale = uploadUnit === 'in' ? MM_PER_INCH : 1;
    await api.upload(file, scale);
    await refreshState();
  });

  const handleArrange = run('arrange', async () => {
    await api.arrange();
    await refreshState();
  });

  const handleSlice = run('slice', async () => {
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
  });

  const handleStartSim = run('start-sim', async () => {
    // Make sure the scene has a toolpath loaded — the simulation is purely
    // client-side from here on, so the moves array is authoritative.
    const slicePayload = await api.getSlice();
    if (!slicePayload.ready) return;
    if (sceneRef.current) sceneRef.current.setToolpath(slicePayload.moves);
    const total = slicePayload.moves.length;
    // Tell the backend the sim is running at 0 so its state matches if an MCP
    // agent inspects it. We don't call step_sim per frame anymore — the UI
    // ticks the cursor locally and only pushes on scrub/reset/finish.
    await api.startSim(1.0);
    if (sceneRef.current) sceneRef.current.setCursor(0);
    setSim({ running: true, cursor: 0, total, speed: 1 });
  });

  // Client-side simulation: advance the cursor with requestAnimationFrame so
  // the Three.js scene repaints every frame without waiting on the network.
  // The backend cursor is only synced when the user scrubs, resets, jumps to
  // end, or restarts — all user-initiated, low-frequency events.
  useEffect(() => {
    if (!sim.running || sim.total === 0) return;
    const total = sim.total;
    const start = performance.now();
    const startCursor = sim.cursor;
    // Target a ~10s full pass at speed=1; scale down on very small toolpaths
    // so a tiny print doesn't finish in a single frame.
    const movesPerSec = Math.max(50, total / 10) * (sim.speed || 1);
    let rafId;
    const step = (now) => {
      const elapsed = (now - start) / 1000;
      const next = Math.min(total, Math.floor(startCursor + movesPerSec * elapsed));
      if (sceneRef.current) sceneRef.current.setCursor(next);
      if (next >= total) {
        setSim((s) => ({ ...s, cursor: total, running: false }));
        // Sync final cursor back so the backend reflects "done".
        api.setCursor(total).catch(() => {});
        return;
      }
      setSim((s) => ({ ...s, cursor: next }));
      rafId = requestAnimationFrame(step);
    };
    rafId = requestAnimationFrame(step);
    return () => cancelAnimationFrame(rafId);
    // sim.cursor is intentionally left out: it changes every frame via the
    // loop itself and including it would restart the effect on every tick.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sim.running, sim.total, sim.speed]);

  const handleCursor = run('cursor', async (v) => {
    const cursor = Number(v);
    // Update scene + local state first so scrubbing feels instant, then push
    // the authoritative value to the backend. Pausing during scrub matches the
    // prior behavior — the user is taking control of the timeline.
    if (sceneRef.current) sceneRef.current.setCursor(cursor);
    setSim((s) => ({ ...s, cursor, running: false }));
    await api.setCursor(cursor);
  });

  const handleBedChange = run('apply-bed', async () => {
    await api.setBed(Number(bed.x), Number(bed.y), Number(bed.z));
    await refreshState();
  });

  const removePart = (id) => run(`remove:${id}`, async () => {
    await api.removePart(id);
    await refreshState();
  })();

  const applyPartScale = (id) => run(`scale:${id}`, async () => {
    const raw = scaleDraft[id];
    const scale = raw === undefined || raw === '' ? 1 : Number(raw);
    if (!(scale > 0)) {
      setError('scale must be a positive number');
      return;
    }
    await api.setPartScale(id, scale);
    setScaleDraft((d) => {
      const n = { ...d };
      delete n[id];
      return n;
    });
    await refreshState();
  })();

  const clearAll = run('clear', async () => {
    await api.clearParts();
    if (sceneRef.current) sceneRef.current.setToolpath([]);
    await refreshState();
  });

  const onDrop = (e) => {
    e.preventDefault();
    setDragging(false);
    const f = e.dataTransfer.files[0];
    if (f) handleUpload(f);
  };

  const uploadBusy = isPending('upload');

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
        <button
          className="secondary"
          onClick={handleBedChange}
          disabled={isPending('apply-bed')}
          data-testid="apply-bed"
        >
          {isPending('apply-bed') ? <Spinner /> : 'Apply bed size'}
        </button>

        <h2>Upload STL</h2>
        <div className="row">
          <label>Units</label>
          <select
            value={uploadUnit}
            onChange={(e) => setUploadUnit(e.target.value)}
            data-testid="upload-unit"
            disabled={uploadBusy}
          >
            <option value="mm">mm</option>
            <option value="in">inches</option>
          </select>
        </div>
        <label
          className={`dropzone ${dragging ? 'dragging' : ''} ${uploadBusy ? 'busy' : ''}`}
          onDragOver={(e) => { if (!uploadBusy) { e.preventDefault(); setDragging(true); } }}
          onDragLeave={() => setDragging(false)}
          onDrop={uploadBusy ? (e) => e.preventDefault() : onDrop}
          data-testid="dropzone"
          aria-busy={uploadBusy || undefined}
        >
          {uploadBusy ? (
            <><Spinner /> <span>Uploading…</span></>
          ) : (
            <>Drop .stl here or click to browse ({uploadUnit})</>
          )}
          <input
            type="file"
            accept=".stl"
            style={{ display: 'none' }}
            disabled={uploadBusy}
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
          {parts.map((p) => {
            const scaleBusy = isPending(`scale:${p.id}`);
            const removeBusy = isPending(`remove:${p.id}`);
            const draft = scaleDraft[p.id];
            const shownScale = draft !== undefined ? draft : (p.scale ?? 1);
            return (
              <li key={p.id} data-testid={`part-${p.id}`}>
                <div className="part-main">
                  <div>{p.name}</div>
                  <div className="meta">
                    {p.size.map((s) => s.toFixed(1)).join(' × ')} mm · {p.triangle_count} tris
                  </div>
                  <div className="part-scale">
                    <label>scale</label>
                    <input
                      type="number"
                      step="0.1"
                      min="0.01"
                      value={shownScale}
                      onChange={(e) => setScaleDraft({ ...scaleDraft, [p.id]: e.target.value })}
                      disabled={scaleBusy}
                      data-testid={`part-scale-${p.id}`}
                    />
                    <button
                      className="secondary"
                      onClick={() => applyPartScale(p.id)}
                      disabled={scaleBusy}
                      data-testid={`apply-scale-${p.id}`}
                    >
                      {scaleBusy ? <Spinner /> : 'Apply'}
                    </button>
                  </div>
                </div>
                <button
                  className="remove"
                  onClick={() => removePart(p.id)}
                  aria-label="remove"
                  disabled={removeBusy}
                >
                  {removeBusy ? <Spinner /> : '×'}
                </button>
              </li>
            );
          })}
        </ul>
        {parts.length > 0 && (
          <div className="button-row">
            <button
              className="secondary"
              onClick={handleArrange}
              disabled={isPending('arrange')}
              data-testid="arrange"
            >
              {isPending('arrange') ? <Spinner /> : 'Auto-arrange'}
            </button>
            <button
              className="danger"
              onClick={clearAll}
              disabled={isPending('clear')}
              data-testid="clear"
            >
              {isPending('clear') ? <Spinner /> : 'Clear'}
            </button>
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
        <button
          onClick={handleSlice}
          disabled={parts.length === 0 || isPending('slice')}
          data-testid="slice"
        >
          {isPending('slice') ? <Spinner /> : 'Slice'}
        </button>

        {sliceSummary && (
          <div className="status" data-testid="slice-summary">
            <div>{sliceSummary.layer_count} layers</div>
            <div>{sliceSummary.move_count} moves</div>
            <div>{sliceSummary.total_extrusion?.toFixed(2) ?? '0.00'} mm extruded</div>
          </div>
        )}

        <h2>Simulation</h2>
        <div className="button-row">
          <button
            onClick={handleStartSim}
            disabled={!sliceSummary || isPending('start-sim')}
            data-testid="start-sim"
          >
            {isPending('start-sim') ? <Spinner /> : 'Start'}
          </button>
          <button
            className="secondary"
            onClick={() => handleCursor(0)}
            disabled={!sliceSummary || isPending('cursor')}
            data-testid="reset-sim"
          >
            Reset
          </button>
          <button
            className="secondary"
            onClick={() => handleCursor(sim.total)}
            disabled={!sliceSummary || isPending('cursor')}
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
        <label className="checkbox-row">
          <input
            type="checkbox"
            checked={showToolpath}
            onChange={(e) => setShowToolpath(e.target.checked)}
            data-testid="toggle-toolpath"
          />
          Show toolpath lines
        </label>
      </aside>

      <main className="viewer">
        <canvas ref={canvasRef} data-testid="viewer-canvas" />
        <div className="viewer-toolbar">
          <button
            className="secondary"
            onClick={focusView}
            title="Focus on parts (f)"
            data-testid="focus-view"
          >
            Focus (f)
          </button>
        </div>
        <div className="overlay" data-testid="overlay">
          bed {bed.x}×{bed.y}×{bed.z} · parts {parts.length}
          {sliceSummary ? ` · ${sliceSummary.layer_count} layers` : ''}
          {sim.total ? ` · ${sim.cursor}/${sim.total}` : ''}
        </div>
      </main>
    </div>
  );
}
