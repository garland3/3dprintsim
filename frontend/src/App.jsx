import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api } from './api.js';
import { PrinterScene } from './PrinterScene.js';

// 1 inch in mm — used when the user picks "inches" from the unit dropdown.
const MM_PER_INCH = 25.4;

// Common printer bed sizes (X, Y, Z in mm). Source: vendor specs; kept to
// popular hobbyist + "prosumer" boxes so most users hit one in the list.
const PRINTER_PRESETS = [
  { name: 'Prusa MK3S+', bed: [250, 210, 210] },
  { name: 'Prusa MK4', bed: [250, 210, 220] },
  { name: 'Prusa Mini+', bed: [180, 180, 180] },
  { name: 'Ender 3 / V2', bed: [235, 235, 250] },
  { name: 'Ender 3 S1 Pro', bed: [220, 220, 270] },
  { name: 'Bambu A1', bed: [256, 256, 256] },
  { name: 'Bambu X1 Carbon', bed: [256, 256, 256] },
  { name: 'Voron 2.4 (300)', bed: [300, 300, 300] },
  { name: 'Voron Trident (350)', bed: [350, 350, 350] },
  { name: 'Anycubic Kobra 2', bed: [220, 220, 250] },
];

const STORAGE_KEY = 'printsim.prefs.v1';

// Everything that should survive a reload. Parts are omitted on purpose —
// they're authoritative on the server and large enough to bloat localStorage.
const DEFAULT_PREFS = {
  bed: { x: 250, y: 210, z: 210 },
  layerHeight: 0.4,
  perimeters: 1,
  infillDensity: 0.2,
  topLayers: 3,
  bottomLayers: 3,
  supportDensity: 0.25,
  supportsEnabled: true,
  uploadUnit: 'mm',
  simSpeed: 1,
  showToolpath: false,
  // Off by default — the translucent source mesh drowns out the printed
  // filament during sim, which is the whole reason to watch the sim.
  showPartsDuringSim: false,
  sections: { bed: true, upload: true, parts: true, slicer: true, simulation: true },
};

function loadPrefs() {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return DEFAULT_PREFS;
    const parsed = JSON.parse(raw);
    // Merge so a new default key (added in a later release) is still present
    // even if the stored blob pre-dates it.
    return {
      ...DEFAULT_PREFS,
      ...parsed,
      bed: { ...DEFAULT_PREFS.bed, ...(parsed.bed || {}) },
      sections: { ...DEFAULT_PREFS.sections, ...(parsed.sections || {}) },
    };
  } catch {
    return DEFAULT_PREFS;
  }
}

// Walks the move list once and returns the index of the first move in each
// layer (any move whose Z strictly exceeds the previous one's). Used by the
// ±layer step buttons to snap the playback cursor to layer boundaries.
function computeLayerStarts(moves) {
  if (!moves || moves.length === 0) return [];
  const starts = [0];
  let prevZ = moves[0].z;
  for (let i = 1; i < moves.length; i++) {
    const z = moves[i].z;
    if (z > prevZ + 1e-6) {
      starts.push(i);
      prevZ = z;
    }
  }
  return starts;
}

// Binary-search the layer index containing move `cursor`. layerStarts is sorted
// ascending, so we find the largest start ≤ cursor.
function layerForCursor(layerStarts, cursor) {
  if (layerStarts.length === 0) return 0;
  let lo = 0;
  let hi = layerStarts.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (layerStarts[mid] <= cursor) lo = mid;
    else hi = mid - 1;
  }
  return lo;
}

// Backend sends the part's current orientation as a 3x3 matrix. We disable the
// "Reset" button when it's effectively identity so a fresh part doesn't
// advertise a no-op reset affordance.
function isIdentityRotation(m) {
  if (!Array.isArray(m) || m.length !== 3) return true;
  for (let i = 0; i < 3; i++) {
    for (let j = 0; j < 3; j++) {
      const target = i === j ? 1 : 0;
      if (Math.abs((m[i][j] ?? target) - target) > 1e-6) return false;
    }
  }
  return true;
}

// Tiny inline spinner that inherits the surrounding text color. Rendered in
// place of a button label (or part icon) while an in-flight request resolves.
function Spinner({ className = '' }) {
  return <span className={`spinner ${className}`.trim()} aria-label="loading" role="status" />;
}

// Simple collapsible section. Persists the open/closed state through the
// parent's `sections` map so the layout is stable across reloads.
function Section({ id, title, aside, open, onToggle, children, testid }) {
  return (
    <section className={`panel ${open ? 'open' : 'closed'}`} data-testid={testid}>
      <button
        type="button"
        className="panel-header"
        onClick={() => onToggle(id)}
        aria-expanded={open}
        data-testid={`toggle-${id}`}
      >
        <span className="caret">{open ? '▾' : '▸'}</span>
        <span className="panel-title">{title}</span>
        {aside ? <span className="panel-aside">{aside}</span> : null}
      </button>
      {open ? <div className="panel-body">{children}</div> : null}
    </section>
  );
}

export default function App() {
  const canvasRef = useRef(null);
  const sceneRef = useRef(null);

  // Boot from localStorage once — subsequent updates flow through the
  // individual setters and a single persist effect at the bottom.
  const initialPrefs = useMemo(() => loadPrefs(), []);
  const [bed, setBed] = useState(initialPrefs.bed);
  const [parts, setParts] = useState([]);
  const [sliceSummary, setSliceSummary] = useState(null);
  const [sim, setSim] = useState({ running: false, cursor: 0, total: 0, speed: initialPrefs.simSpeed });
  const [layerStarts, setLayerStarts] = useState([]);
  const [layerHeight, setLayerHeight] = useState(initialPrefs.layerHeight);
  const [perimeters, setPerimeters] = useState(initialPrefs.perimeters);
  const [infillDensity, setInfillDensity] = useState(initialPrefs.infillDensity);
  const [topLayers, setTopLayers] = useState(initialPrefs.topLayers);
  const [bottomLayers, setBottomLayers] = useState(initialPrefs.bottomLayers);
  const [supportDensity, setSupportDensity] = useState(initialPrefs.supportDensity);
  const [supportsEnabled, setSupportsEnabled] = useState(initialPrefs.supportsEnabled);
  const [uploadUnit, setUploadUnit] = useState(initialPrefs.uploadUnit);
  const [scaleDraft, setScaleDraft] = useState({}); // per-part in-progress scale input
  const [posDraft, setPosDraft] = useState({});     // per-part in-progress x/y input
  const [error, setError] = useState('');
  const [dragging, setDragging] = useState(false);
  const [pending, setPending] = useState({});
  const [showToolpath, setShowToolpath] = useState(initialPrefs.showToolpath);
  const [showPartsDuringSim, setShowPartsDuringSim] = useState(initialPrefs.showPartsDuringSim);
  const [sections, setSections] = useState(initialPrefs.sections);
  const [helpOpen, setHelpOpen] = useState(false);
  const [printerPresetName, setPrinterPresetName] = useState('');
  // Printer technology — 'FDM' or 'LPBF'. Set by the backend from the .env
  // file; the frontend just reflects whatever the server reports.
  const [printerType, setPrinterType] = useState('FDM');
  // Synchronous mirror of `pending`. React's setPending is async/batched, so a
  // guard that reads the state-updater closure can miss a rapid re-entrant
  // click. The ref gives us an immediate lockout before we kick off `fn`.
  const pendingRef = useRef({});

  const toggleSection = useCallback((id) => {
    setSections((prev) => ({ ...prev, [id]: !prev[id] }));
  }, []);

  // Persist preferences whenever any watched value changes. Throttled only by
  // the React scheduler — the payload is tiny (~200 bytes) so this is fine.
  useEffect(() => {
    const prefs = {
      bed,
      layerHeight,
      perimeters,
      infillDensity,
      topLayers,
      bottomLayers,
      supportDensity,
      supportsEnabled,
      uploadUnit,
      simSpeed: sim.speed,
      showToolpath,
      showPartsDuringSim,
      sections,
    };
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(prefs));
    } catch {
      // Private-mode browsers throw on writes; preferences are non-essential.
    }
  }, [bed, layerHeight, perimeters, infillDensity, topLayers, bottomLayers,
      supportDensity, supportsEnabled, uploadUnit, sim.speed, showToolpath,
      showPartsDuringSim, sections]);

  const resetPrefs = useCallback(() => {
    if (!window.confirm('Clear saved UI preferences? Your parts stay on the bed.')) return;
    try { window.localStorage.removeItem(STORAGE_KEY); } catch {}
    setBed(DEFAULT_PREFS.bed);
    setLayerHeight(DEFAULT_PREFS.layerHeight);
    setPerimeters(DEFAULT_PREFS.perimeters);
    setInfillDensity(DEFAULT_PREFS.infillDensity);
    setTopLayers(DEFAULT_PREFS.topLayers);
    setBottomLayers(DEFAULT_PREFS.bottomLayers);
    setSupportDensity(DEFAULT_PREFS.supportDensity);
    setSupportsEnabled(DEFAULT_PREFS.supportsEnabled);
    setUploadUnit(DEFAULT_PREFS.uploadUnit);
    setSim((s) => ({ ...s, speed: DEFAULT_PREFS.simSpeed }));
    setShowToolpath(DEFAULT_PREFS.showToolpath);
    setShowPartsDuringSim(DEFAULT_PREFS.showPartsDuringSim);
    setSections(DEFAULT_PREFS.sections);
    setPrinterPresetName('');
  }, []);

  // Wrap an async handler so the corresponding `pending[key]` flag is set for
  // the duration of the request.
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

  // Initialize Three.js scene once. The scene emits a `partdragend` callback
  // on mouseup after a user drags a part — we push that to the backend so the
  // placement persists.
  useEffect(() => {
    if (!canvasRef.current) return;
    const scene = new PrinterScene(canvasRef.current);
    sceneRef.current = scene;
    scene.onPartDragEnd = async (partId, x, y) => {
      try {
        await api.setPartPosition(partId, x, y);
        await refreshState();
      } catch (e) {
        setError(String(e));
      }
    };
    // expose for Playwright:
    window.__printerScene = scene;
    return () => {
      scene.dispose();
      sceneRef.current = null;
      window.__printerScene = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const focusView = useCallback(() => {
    if (sceneRef.current) sceneRef.current.focus();
  }, []);

  const applyCameraPreset = useCallback((name) => {
    if (sceneRef.current) sceneRef.current.setView(name);
  }, []);

  // Keyboard shortcuts. Ignored when the user is typing into a form field so
  // `f` inside a number input doesn't teleport the camera.
  useEffect(() => {
    const onKey = (e) => {
      const t = e.target;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA'
                || t.tagName === 'SELECT' || t.isContentEditable)) return;
      if (e.key === 'f' || e.key === 'F') { e.preventDefault(); focusView(); return; }
      if (e.key === '?') { e.preventDefault(); setHelpOpen((v) => !v); return; }
      if (e.key === 'Escape') { setHelpOpen(false); return; }
      if (e.key === ' ') {
        // space = play/pause — only useful after a slice has loaded moves.
        if (sim.total > 0) {
          e.preventDefault();
          toggleRunning();
        }
        return;
      }
      if (e.key === '[') { e.preventDefault(); stepLayer(-1); return; }
      if (e.key === ']') { e.preventDefault(); stepLayer(1); return; }
      if (e.key === ',') { e.preventDefault(); stepCursor(-1); return; }
      if (e.key === '.') { e.preventDefault(); stepCursor(1); return; }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusView, sim.total, sim.cursor, sim.running, layerStarts]);

  // Held in a ref so the polling effect below can call the latest
  // `refreshState` without putting it in the dep array — `refreshState`
  // is declared with `const` further down the component body, and listing
  // it in deps here would hit the temporal dead zone at render time.
  const refreshStateRef = useRef(null);

  // Subscribe to server-sent events for this session. The backend emits a
  // `state` event on every mutation (carrying the new `state_revision`) and
  // a `focus` event when `focus_viewer()` is called, so MCP-driven changes
  // (LLM uploads a part, slices, advances the simulation) reach the UI
  // within milliseconds instead of the old 2-second polling floor.
  //
  // If the SSE connection fails (proxy strips it, network blip) the browser
  // auto-reconnects. As a belt-and-braces fallback we also run a slow
  // poll against `/api/viewer/requests`; it gets promoted to the primary
  // source only if SSE errors sustain.
  useEffect(() => {
    let cancelled = false;
    let es = null;
    let fallbackTimer = null;
    let lastFocus = 0;
    let lastRevision = -1;  // -1 so the hello-event seeds without a refetch

    const applyRevision = async (rev, { fromHello = false } = {}) => {
      if (typeof rev !== 'number') return;
      if (lastRevision < 0) {
        // Seed — the mount-time refreshState() already fetched state.
        lastRevision = rev;
        return;
      }
      if (rev <= lastRevision) return;
      lastRevision = rev;
      if (fromHello) return;
      await refreshStateRef.current?.();
    };

    const applyFocus = (focusRequest) => {
      if (typeof focusRequest !== 'number') return;
      if (focusRequest > lastFocus) {
        focusView();
        lastFocus = focusRequest;
      }
    };

    const startFallback = () => {
      // Fallback polling at 5s — slower than the old primary poll because
      // SSE is the happy path; we only need a safety net when the stream
      // can't be held open.
      if (fallbackTimer) return;
      const tick = async () => {
        if (cancelled) return;
        try {
          const r = await api.viewerRequests();
          if (cancelled) return;
          applyFocus(r.focus_request);
          await applyRevision(r.state_revision);
        } catch {
          // intentional swallow
        }
      };
      tick();
      fallbackTimer = setInterval(tick, 5000);
    };

    const stopFallback = () => {
      if (fallbackTimer) {
        clearInterval(fallbackTimer);
        fallbackTimer = null;
      }
    };

    try {
      es = api.openEvents();
      es.addEventListener('hello', (e) => {
        try {
          const data = JSON.parse(e.data);
          applyFocus(data.focus_request);
          applyRevision(data.state_revision, { fromHello: true });
        } catch {}
        stopFallback();
      });
      es.addEventListener('state', (e) => {
        try {
          const data = JSON.parse(e.data);
          applyRevision(data.state_revision);
        } catch {}
      });
      es.addEventListener('focus', (e) => {
        try {
          const data = JSON.parse(e.data);
          applyFocus(data.focus_request);
        } catch {}
      });
      es.addEventListener('error', () => {
        // EventSource auto-reconnects, but if it's flapping we want the UI
        // to still make progress — kick the fallback poller until the next
        // successful `hello`.
        if (!cancelled) startFallback();
      });
    } catch {
      // EventSource unsupported or blocked by a sandboxed iframe — fall
      // back to polling for the lifetime of this mount.
      startFallback();
    }

    return () => {
      cancelled = true;
      if (es) es.close();
      stopFallback();
    };
  }, [focusView]);

  // Keep the scene's toolpath visibility in sync with the sidebar toggle.
  useEffect(() => {
    if (sceneRef.current) sceneRef.current.setToolpathVisible(showToolpath);
  }, [showToolpath]);

  useEffect(() => {
    if (sceneRef.current) sceneRef.current.setPartsSimVisible(showPartsDuringSim);
  }, [showPartsDuringSim]);

  const refreshState = useCallback(async () => {
    try {
      const st = await api.state();
      setBed({ x: st.bed_size[0], y: st.bed_size[1], z: st.bed_size[2] });
      setParts(st.parts);
      setSliceSummary(st.slice);
      const nextPrinterType = (st.printer_type || 'FDM').toUpperCase();
      setPrinterType(nextPrinterType);
      if (sceneRef.current) sceneRef.current.setPrinterType(nextPrinterType);
      setSim((s) => ({
        ...s,
        // Don't let a backend refresh clobber a client-side simulation in
        // flight; the local RAF loop is authoritative while running.
        running: s.running ? s.running : st.simulation.running,
        cursor: s.running ? s.cursor : st.simulation.cursor,
        total: st.simulation.total_moves,
        // Keep the user's speed choice authoritative on the frontend; the
        // backend only ever receives it at /simulation/start time.
        speed: s.speed,
      }));
      if (sceneRef.current) sceneRef.current.setBed(st.bed_size[0], st.bed_size[1], st.bed_size[2]);
    } catch (e) {
      setError(String(e));
    }
  }, []);

  // Keep the ref the polling effect reads in sync with the latest
  // `refreshState`. `refreshState` is a stable useCallback (deps: []), so
  // in practice this only runs once, but doing it via effect is what makes
  // the forward-reference safe across future deps changes.
  useEffect(() => { refreshStateRef.current = refreshState; }, [refreshState]);

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

  // Track sim.cursor in a ref so the slice-toolpath effect (which is async
  // and shouldn't refire on every cursor tick) can read the *current* cursor
  // when reapplying it after a toolpath rebuild.
  const simCursorRef = useRef(0);
  useEffect(() => { simCursorRef.current = sim.cursor; }, [sim.cursor]);

  // Push slice toolpath into the scene whenever the slice itself changes.
  // refreshState() (called by SSE state events) only updates React state;
  // without this effect, MCP-driven slice_all calls never reach the
  // Three.js scene — only handleSlice/handleStartSim push moves into it.
  // Keyed off move_count (a primitive) so cursor-only mutations (which
  // also bump state_revision) don't trigger a redundant slice refetch.
  //
  // Mirrors handleSlice's ordering: setToolpath FIRST, then setCursor.
  // setToolpath rebuilds the scene's render-up-to index, so without the
  // trailing setCursor the scene shows nothing until the next cursor
  // mutation — which on the MCP path may never come (slice_all parks the
  // cursor at the end and that's it).
  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (!sceneRef.current) return;
      if (!sliceSummary) {
        sceneRef.current.setToolpath([]);
        setLayerStarts([]);
        return;
      }
      const slicePayload = await api.getSlice();
      if (cancelled || !slicePayload.ready) return;
      sceneRef.current.setToolpath(slicePayload.moves);
      setLayerStarts(computeLayerStarts(slicePayload.moves));
      sceneRef.current.setCursor(simCursorRef.current);
    })();
    return () => { cancelled = true; };
  }, [sliceSummary?.move_count]);

  // Sync the scene cursor with React state so MCP-driven cursor moves
  // (start_simulation, step_simulation, set_simulation_cursor) appear
  // in the viewer. The RAF loop also calls setCursor directly while a
  // run is active; this effect catches the static / external-update cases.
  useEffect(() => {
    if (sceneRef.current) sceneRef.current.setCursor(sim.cursor);
  }, [sim.cursor]);

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
      support_density: supportsEnabled ? Number(supportDensity) : 0,
    });
    setSliceSummary(summary);
    const slicePayload = await api.getSlice();
    if (sceneRef.current && slicePayload.ready) {
      sceneRef.current.setToolpath(slicePayload.moves);
    }
    setLayerStarts(slicePayload.ready ? computeLayerStarts(slicePayload.moves) : []);
    if (slicePayload.ready) {
      const total = slicePayload.moves.length;
      if (sceneRef.current) sceneRef.current.setCursor(total);
      setSim((s) => ({ ...s, running: false, cursor: total, total }));
      await api.setCursor(total);
    }
    await refreshState();
  });

  const handleStartSim = run('start-sim', async () => {
    const slicePayload = await api.getSlice();
    if (!slicePayload.ready) return;
    if (sceneRef.current) sceneRef.current.setToolpath(slicePayload.moves);
    const total = slicePayload.moves.length;
    setLayerStarts(computeLayerStarts(slicePayload.moves));
    await api.startSim(sim.speed || 1);
    if (sceneRef.current) sceneRef.current.setCursor(0);
    setSim((s) => ({ running: true, cursor: 0, total, speed: s.speed || 1 }));
  });

  const toggleRunning = () => {
    // Finished? Loop back to start on next press.
    if (sim.total === 0) return;
    if (sim.cursor >= sim.total) {
      handleCursor(0);
      setSim((s) => ({ ...s, running: true, cursor: 0 }));
      return;
    }
    setSim((s) => ({ ...s, running: !s.running }));
  };

  const stepCursor = (delta) => {
    const next = Math.max(0, Math.min(sim.total, sim.cursor + delta));
    if (next === sim.cursor) return;
    handleCursor(next);
  };

  const stepLayer = (delta) => {
    if (layerStarts.length === 0) return;
    let target;
    if (delta > 0) {
      target = layerStarts.find((idx) => idx > sim.cursor);
      if (target === undefined) target = sim.total;
    } else {
      const atOrBefore = [...layerStarts].reverse().find((idx) => idx <= sim.cursor);
      if (atOrBefore === undefined) return;
      if (atOrBefore === sim.cursor) {
        const prior = [...layerStarts].reverse().find((idx) => idx < sim.cursor);
        target = prior ?? 0;
      } else {
        target = atOrBefore;
      }
    }
    handleCursor(target);
  };

  const jumpToLayer = (layerIndex) => {
    const n = Number(layerIndex);
    if (!Number.isFinite(n) || layerStarts.length === 0) return;
    const clamped = Math.max(0, Math.min(layerStarts.length - 1, Math.floor(n)));
    handleCursor(layerStarts[clamped]);
  };

  const setSpeed = (v) => {
    const next = Number(v) || 1;
    setSim((s) => ({ ...s, speed: next }));
  };

  // Client-side RAF loop. Watches sim.speed so changing it mid-run seamlessly
  // re-pitches without a restart.
  useEffect(() => {
    if (!sim.running || sim.total === 0) return;
    const total = sim.total;
    const start = performance.now();
    const startCursor = sim.cursor;
    const movesPerSec = Math.max(50, total / 10) * (sim.speed || 1);
    let rafId;
    const step = (now) => {
      const elapsed = (now - start) / 1000;
      const next = Math.min(total, Math.floor(startCursor + movesPerSec * elapsed));
      if (sceneRef.current) sceneRef.current.setCursor(next);
      if (next >= total) {
        setSim((s) => ({ ...s, cursor: total, running: false }));
        api.setCursor(total).catch(() => {});
        return;
      }
      setSim((s) => ({ ...s, cursor: next }));
      rafId = requestAnimationFrame(step);
    };
    rafId = requestAnimationFrame(step);
    return () => cancelAnimationFrame(rafId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sim.running, sim.total, sim.speed]);

  const handleCursor = run('cursor', async (v) => {
    const cursor = Number(v);
    if (sceneRef.current) sceneRef.current.setCursor(cursor);
    setSim((s) => ({ ...s, cursor, running: false }));
    await api.setCursor(cursor);
  });

  const handleBedChange = run('apply-bed', async () => {
    await api.setBed(Number(bed.x), Number(bed.y), Number(bed.z));
    await refreshState();
  });

  const applyPreset = (name) => {
    const preset = PRINTER_PRESETS.find((p) => p.name === name);
    if (!preset) return;
    setPrinterPresetName(name);
    setBed({ x: preset.bed[0], y: preset.bed[1], z: preset.bed[2] });
  };

  const removePart = (id, name) => {
    if (!window.confirm(`Remove "${name}"?`)) return;
    run(`remove:${id}`, async () => {
      await api.removePart(id);
      await refreshState();
    })();
  };

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

  const rotatePart = (id, axis, degrees) => run(`rotate:${id}`, async () => {
    await api.rotatePart(id, axis, degrees);
    await refreshState();
  })();

  const resetPartRotation = (id) => run(`rotate:${id}`, async () => {
    await api.resetPartRotation(id);
    await refreshState();
  })();

  const applyPartPosition = (id) => run(`pos:${id}`, async () => {
    const draft = posDraft[id];
    if (!draft) return;
    const x = Number(draft.x);
    const y = Number(draft.y);
    if (!Number.isFinite(x) || !Number.isFinite(y)) {
      setError('position must be numeric');
      return;
    }
    await api.setPartPosition(id, x, y);
    setPosDraft((d) => {
      const n = { ...d };
      delete n[id];
      return n;
    });
    await refreshState();
  })();

  const clearAll = () => {
    if (!window.confirm(`Clear all ${parts.length} parts?`)) return;
    run('clear', async () => {
      await api.clearParts();
      if (sceneRef.current) sceneRef.current.setToolpath([]);
      await refreshState();
    })();
  };

  const onDrop = (e) => {
    e.preventDefault();
    setDragging(false);
    const f = e.dataTransfer.files[0];
    if (f) handleUpload(f);
  };

  const uploadBusy = isPending('upload');
  const currentLayer = sliceSummary && layerStarts.length > 0
    ? layerForCursor(layerStarts, sim.cursor) + 1
    : null;
  const totalLayers = sliceSummary?.layer_count ?? 0;

  return (
    <div className="app">
      <aside className="sidebar" data-testid="sidebar">
        <div className="sidebar-head">
          <h1>3D Print Sim</h1>
          <button
            className="icon help-btn"
            onClick={() => setHelpOpen(true)}
            title="Keyboard shortcuts (?)"
            data-testid="help-button"
          >?</button>
        </div>
        {api.isEmbedded && (
          <div className="session-badge" data-testid="session-badge" title={`MCP session ${api.sessionId}`}>
            Atlas · session {api.sessionId.slice(0, 8)}…
          </div>
        )}
        {error && <div className="error" data-testid="error">{error}</div>}

        <Section
          id="bed"
          title="Bed"
          aside={`${printerType} · ${bed.x}×${bed.y}×${bed.z}`}
          open={sections.bed}
          onToggle={toggleSection}
          testid="section-bed"
        >
          <div className="row" data-testid="printer-type-row">
            <label>Printer</label>
            <span data-testid="printer-type">
              {printerType === 'LPBF'
                ? 'LPBF (Laser Powder Bed Fusion)'
                : 'FDM (Fused Deposition Modeling)'}
            </span>
          </div>
          <div className="row">
            <label>Preset</label>
            <select
              value={printerPresetName}
              onChange={(e) => applyPreset(e.target.value)}
              data-testid="printer-preset"
            >
              <option value="">Custom…</option>
              {PRINTER_PRESETS.map((p) => (
                <option key={p.name} value={p.name}>{p.name}</option>
              ))}
            </select>
          </div>
          <div className="row">
            <label>X</label>
            <input type="number" value={bed.x} onChange={(e) => { setPrinterPresetName(''); setBed({ ...bed, x: e.target.value }); }} data-testid="bed-x" />
            <label>Y</label>
            <input type="number" value={bed.y} onChange={(e) => { setPrinterPresetName(''); setBed({ ...bed, y: e.target.value }); }} data-testid="bed-y" />
            <label>Z</label>
            <input type="number" value={bed.z} onChange={(e) => { setPrinterPresetName(''); setBed({ ...bed, z: e.target.value }); }} data-testid="bed-z" />
          </div>
          <button
            className="secondary"
            onClick={handleBedChange}
            disabled={isPending('apply-bed')}
            data-testid="apply-bed"
          >
            {isPending('apply-bed') ? <Spinner /> : 'Apply bed size'}
          </button>
        </Section>

        <Section
          id="upload"
          title="Upload STL"
          open={sections.upload}
          onToggle={toggleSection}
          testid="section-upload"
        >
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
                e.target.value = '';
                handleUpload(f);
              }}
              data-testid="file-input"
            />
          </label>
        </Section>

        <Section
          id="parts"
          title="Parts"
          aside={parts.length}
          open={sections.parts}
          onToggle={toggleSection}
          testid="section-parts"
        >
          <ul className="parts-list" data-testid="parts-list">
            {parts.map((p) => {
              const scaleBusy = isPending(`scale:${p.id}`);
              const removeBusy = isPending(`remove:${p.id}`);
              const rotateBusy = isPending(`rotate:${p.id}`);
              const posBusy = isPending(`pos:${p.id}`);
              const draft = scaleDraft[p.id];
              const shownScale = draft !== undefined ? draft : (p.scale ?? 1);
              const rotated = p.rotation && !isIdentityRotation(p.rotation);
              const outOfBed = p.placement === null;
              const hasWarnings = Array.isArray(p.warnings) && p.warnings.length > 0;
              const pos = p.placement ?? { x: 0, y: 0 };
              const pd = posDraft[p.id];
              const xVal = pd?.x ?? pos.x.toFixed(1);
              const yVal = pd?.y ?? pos.y.toFixed(1);
              return (
                <li
                  key={p.id}
                  className={outOfBed ? 'out-of-bed' : ''}
                  data-testid={`part-${p.id}`}
                >
                  <div className="part-main">
                    <div className="part-name-row">
                      <span className="part-name">{p.name}</span>
                      {outOfBed && (
                        <span className="badge danger" data-testid={`badge-unplaced-${p.id}`} title="Doesn't fit on the bed">
                          off-bed
                        </span>
                      )}
                      {hasWarnings && (
                        <span className="badge warn" title={p.warnings.join('\n')} data-testid={`badge-warn-${p.id}`}>
                          !
                        </span>
                      )}
                    </div>
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
                    <div className="part-rotate" data-testid={`part-rotate-${p.id}`}>
                      <label>rotate</label>
                      {['x', 'y', 'z'].map((axis) => (
                        <span className="rotate-axis" key={axis}>
                          <button className="icon" title={`Rotate -90° around ${axis.toUpperCase()}`} onClick={() => rotatePart(p.id, axis, -90)} disabled={rotateBusy} data-testid={`rotate-${axis}-minus-${p.id}`}>−{axis.toUpperCase()}</button>
                          <button className="icon" title={`Rotate +90° around ${axis.toUpperCase()}`} onClick={() => rotatePart(p.id, axis, 90)} disabled={rotateBusy} data-testid={`rotate-${axis}-plus-${p.id}`}>+{axis.toUpperCase()}</button>
                        </span>
                      ))}
                      <button
                        className="secondary"
                        onClick={() => resetPartRotation(p.id)}
                        disabled={rotateBusy || !rotated}
                        title="Clear rotation"
                        data-testid={`rotate-reset-${p.id}`}
                      >
                        {rotateBusy ? <Spinner /> : 'Reset'}
                      </button>
                    </div>
                    <div className="part-pos" data-testid={`part-pos-${p.id}`}>
                      <label>pos</label>
                      <input
                        type="number"
                        step="1"
                        value={xVal}
                        onChange={(e) => setPosDraft({ ...posDraft, [p.id]: { x: e.target.value, y: yVal } })}
                        disabled={posBusy}
                        data-testid={`part-pos-x-${p.id}`}
                        aria-label="x (mm)"
                      />
                      <input
                        type="number"
                        step="1"
                        value={yVal}
                        onChange={(e) => setPosDraft({ ...posDraft, [p.id]: { x: xVal, y: e.target.value } })}
                        disabled={posBusy}
                        data-testid={`part-pos-y-${p.id}`}
                        aria-label="y (mm)"
                      />
                      <button
                        className="secondary"
                        onClick={() => applyPartPosition(p.id)}
                        disabled={posBusy || !pd}
                        data-testid={`apply-pos-${p.id}`}
                      >
                        {posBusy ? <Spinner /> : 'Set'}
                      </button>
                    </div>
                  </div>
                  <button
                    className="remove"
                    onClick={() => removePart(p.id, p.name)}
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
        </Section>

        <Section
          id="slicer"
          title="Slicer"
          aside={sliceSummary ? `${sliceSummary.layer_count}L` : null}
          open={sections.slicer}
          onToggle={toggleSection}
          testid="section-slicer"
        >
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
          <div className="row">
            <label className="checkbox-inline">
              <input
                type="checkbox"
                checked={supportsEnabled}
                onChange={(e) => setSupportsEnabled(e.target.checked)}
                data-testid="toggle-supports"
              />
              Supports
            </label>
            <input
              type="number"
              step="5"
              min="0"
              max="100"
              value={Math.round(supportDensity * 100)}
              onChange={(e) => setSupportDensity(Math.max(0, Math.min(100, Number(e.target.value))) / 100)}
              disabled={!supportsEnabled}
              data-testid="support-density"
              title="Support density %"
            />
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
        </Section>

        <Section
          id="simulation"
          title="Simulation"
          aside={sliceSummary && currentLayer ? `L${currentLayer}/${totalLayers}` : null}
          open={sections.simulation}
          onToggle={toggleSection}
          testid="section-simulation"
        >
          <div className="button-row">
            <button
              onClick={sim.running || sim.cursor > 0 ? toggleRunning : handleStartSim}
              disabled={!sliceSummary || isPending('start-sim')}
              data-testid="start-sim"
            >
              {isPending('start-sim') ? <Spinner /> : (sim.running ? 'Pause' : (sim.cursor > 0 && sim.cursor < sim.total ? 'Resume' : 'Start'))}
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
            <>
              <div className="row" style={{ marginTop: 8 }}>
                <label>Speed</label>
                <select
                  value={sim.speed}
                  onChange={(e) => setSpeed(e.target.value)}
                  data-testid="sim-speed"
                >
                  <option value="0.25">0.25×</option>
                  <option value="0.5">0.5×</option>
                  <option value="1">1×</option>
                  <option value="2">2×</option>
                  <option value="4">4×</option>
                  <option value="8">8×</option>
                </select>
                <label>Layer</label>
                <input
                  type="number"
                  min="1"
                  max={totalLayers}
                  value={currentLayer ?? ''}
                  onChange={(e) => jumpToLayer(Number(e.target.value) - 1)}
                  data-testid="layer-jump"
                  title="Jump to layer"
                />
              </div>
              <div className="button-row step-row" data-testid="step-row">
                <button className="secondary" onClick={() => stepLayer(-1)} disabled={!sliceSummary || isPending('cursor') || layerStarts.length === 0} title="Previous layer ([)" data-testid="step-layer-back">⟪ layer</button>
                <button className="secondary" onClick={() => stepCursor(-1)} disabled={!sliceSummary || isPending('cursor') || sim.cursor <= 0} title="Previous move (,)" data-testid="step-move-back">‹ step</button>
                <button className="secondary" onClick={() => stepCursor(1)} disabled={!sliceSummary || isPending('cursor') || sim.cursor >= sim.total} title="Next move (.)" data-testid="step-move-fwd">step ›</button>
                <button className="secondary" onClick={() => stepLayer(1)} disabled={!sliceSummary || isPending('cursor') || layerStarts.length === 0} title="Next layer (])" data-testid="step-layer-fwd">layer ⟫</button>
              </div>
              <div className="slider-row" style={{ marginTop: 8 }}>
                <input
                  type="range"
                  min="0"
                  max={sim.total || 0}
                  value={sim.cursor}
                  onChange={(e) => handleCursor(e.target.value)}
                  data-testid="sim-slider"
                />
                <span className="value" data-testid="sim-cursor">
                  {sim.cursor} / {sim.total}
                  {currentLayer !== null && ` · layer ${currentLayer}/${totalLayers}`}
                </span>
              </div>
            </>
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
          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={showPartsDuringSim}
              onChange={(e) => setShowPartsDuringSim(e.target.checked)}
              data-testid="toggle-parts-sim"
            />
            Show part mesh during sim
          </label>
        </Section>

        <div className="sidebar-footer">
          <button
            className="link-btn"
            onClick={resetPrefs}
            data-testid="reset-prefs"
            title="Clear saved UI preferences"
          >
            Reset saved preferences
          </button>
        </div>
      </aside>

      <main className="viewer">
        <canvas ref={canvasRef} data-testid="viewer-canvas" />
        <div className="viewer-toolbar">
          <div className="toolbar-pill" data-testid="view-presets">
            {['iso', 'top', 'front', 'right'].map((v) => (
              <button
                key={v}
                className="pill-btn"
                onClick={() => applyCameraPreset(v)}
                title={`${v} view`}
                data-testid={`view-${v}`}
              >
                {v}
              </button>
            ))}
            <span className="pill-divider" />
            <button
              className="pill-btn"
              onClick={focusView}
              title="Focus on parts (f)"
              data-testid="focus-view"
            >
              focus
            </button>
          </div>
        </div>
        <div className="overlay" data-testid="overlay">
          <span>{bed.x}×{bed.y}×{bed.z}</span>
          <span>· {parts.length} {parts.length === 1 ? 'part' : 'parts'}</span>
          {sliceSummary ? <span>· {sliceSummary.layer_count}L</span> : null}
          {sim.total ? <span>· {Math.round((sim.cursor / sim.total) * 100)}%</span> : null}
          {currentLayer !== null ? <span>· L{currentLayer}/{totalLayers}</span> : null}
        </div>
        {sliceSummary && (
          <div className="legend" data-testid="legend">
            <div className="legend-row"><span className="swatch" style={{ background: '#2e9ff2' }} />perimeter</div>
            <div className="legend-row"><span className="swatch" style={{ background: '#ff6130' }} />overhang</div>
            <div className="legend-row"><span className="swatch" style={{ background: '#f2d14d' }} />solid fill</div>
            <div className="legend-row"><span className="swatch" style={{ background: '#8c8c9e' }} />sparse infill</div>
            <div className="legend-row"><span className="swatch" style={{ background: '#58cc8c' }} />support</div>
            {typeof sliceSummary.support_cell_count === 'number' && (
              <div className="legend-note">supports: {sliceSummary.support_cell_count} cells</div>
            )}
          </div>
        )}
      </main>

      {helpOpen && (
        <div className="modal-backdrop" onClick={() => setHelpOpen(false)} data-testid="help-modal">
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h3>Keyboard shortcuts</h3>
            <dl>
              <dt>f</dt><dd>Focus view on parts</dd>
              <dt>space</dt><dd>Play / pause simulation</dd>
              <dt>[ / ]</dt><dd>Previous / next layer</dd>
              <dt>, / .</dt><dd>Previous / next move</dd>
              <dt>?</dt><dd>Toggle this help</dd>
              <dt>Esc</dt><dd>Close dialogs</dd>
            </dl>
            <h4>Mouse</h4>
            <ul>
              <li>Left-drag empty space: orbit the view</li>
              <li>Shift + left or right-drag: pan</li>
              <li>Wheel: zoom</li>
              <li>Left-drag on a part: reposition it on the bed</li>
            </ul>
            <button className="secondary" onClick={() => setHelpOpen(false)}>Close</button>
          </div>
        </div>
      )}
    </div>
  );
}
