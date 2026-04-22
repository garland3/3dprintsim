import React, { useCallback, useEffect, useRef, useState } from 'react';
import { api } from './api.js';
import { PrinterScene } from './PrinterScene.js';

// Status → label color for the floating pill above each rig.
const STATUS_COLORS = {
  idle: '#8b9299',
  printing: '#ff9130',
  finished: '#58cc8c',
  unloading: '#f2d14d',
  offline: '#5c6270',
};

// How often the factory view repolls state. Short enough that progress
// bars feel live; long enough that it doesn't hammer the backend.
const POLL_MS = 750;

// Cursor-to-time ratio. Each printer's toolpath is advanced by
// `progress × move_count` so the head sweeps the full path over the job's
// simulated duration.
function cursorForJob(job, moveCount) {
  if (!job || !moveCount) return 0;
  const p = Math.max(0, Math.min(1, job.progress ?? 0));
  return Math.floor(p * moveCount);
}

function fmtDuration(seconds) {
  if (!Number.isFinite(seconds) || seconds <= 0) return '0s';
  const s = Math.round(seconds);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rem = s % 60;
  if (m < 60) return rem ? `${m}m ${rem}s` : `${m}m`;
  const h = Math.floor(m / 60);
  const mm = m % 60;
  return mm ? `${h}h ${mm}m` : `${h}h`;
}

function fmtGrams(g) {
  if (!Number.isFinite(g)) return '0 g';
  if (g >= 1000) return `${(g / 1000).toFixed(2)} kg`;
  return `${g.toFixed(1)} g`;
}

function fmtUsd(v) {
  if (!Number.isFinite(v)) return '$0.00';
  if (v >= 100) return `$${v.toFixed(0)}`;
  return `$${v.toFixed(2)}`;
}

function FactorySidebar({
  factory,
  onSubmit,
  onConfig,
  onReset,
  onFrameAll,
  onFrameFocused,
  busy,
  lastError,
  onBack,
}) {
  const cfg = factory?.config;
  const [uploadUnit, setUploadUnit] = useState('mm');
  const [dragging, setDragging] = useState(false);
  const [layerHeight, setLayerHeight] = useState('0.4');
  const [infill, setInfill] = useState('20');
  const [copies, setCopies] = useState('1');
  const [rowsDraft, setRowsDraft] = useState(cfg?.rows ?? 3);
  const [colsDraft, setColsDraft] = useState(cfg?.cols ?? 3);
  const [speedDraft, setSpeedDraft] = useState(cfg?.sim_speed ?? 1);
  const dragDepthRef = useRef(0);
  const fileInputRef = useRef(null);

  useEffect(() => {
    if (!cfg) return;
    setRowsDraft(cfg.rows);
    setColsDraft(cfg.cols);
    setSpeedDraft(cfg.sim_speed);
  }, [cfg?.rows, cfg?.cols, cfg?.sim_speed]);

  const handleFile = (file) => {
    if (!file) return;
    const scale = uploadUnit === 'in' ? 25.4 : 1;
    const count = Math.max(1, Math.min(100, Math.floor(Number(copies) || 1)));
    onSubmit(file, {
      scale,
      layer_height: Number(layerHeight) || 0.4,
      infill_density: Math.max(0, Math.min(100, Number(infill) || 0)) / 100,
      count,
    });
  };

  const onDragEnter = (e) => {
    if (busy) return;
    e.preventDefault();
    dragDepthRef.current += 1;
    setDragging(true);
  };
  const onDragOver = (e) => {
    if (busy) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
  };
  const onDragLeave = () => {
    dragDepthRef.current = Math.max(0, dragDepthRef.current - 1);
    if (dragDepthRef.current === 0) setDragging(false);
  };
  const onDrop = (e) => {
    e.preventDefault();
    dragDepthRef.current = 0;
    setDragging(false);
    if (busy) return;
    const f = e.dataTransfer.files[0];
    if (f) handleFile(f);
  };

  return (
    <aside className="sidebar factory-sidebar" data-testid="factory-sidebar">
      <div className="sidebar-head">
        <h1>Factory</h1>
        <button
          className="icon"
          onClick={onBack}
          title="Back to single-printer view"
          data-testid="factory-back"
        >
          ‹
        </button>
      </div>

      {lastError ? <div className="error" data-testid="factory-error">{lastError}</div> : null}

      <h2>Submit job</h2>
      <div className="row">
        <label>Units</label>
        <select value={uploadUnit} onChange={(e) => setUploadUnit(e.target.value)}>
          <option value="mm">mm</option>
          <option value="in">inches</option>
        </select>
      </div>
      <div className="row">
        <label>Layer</label>
        <input
          type="number"
          step="0.1"
          value={layerHeight}
          onChange={(e) => setLayerHeight(e.target.value)}
          data-testid="factory-layer-height"
        />
        <label>Infill %</label>
        <input
          type="number"
          step="5"
          min="0"
          max="100"
          value={infill}
          onChange={(e) => setInfill(e.target.value)}
          data-testid="factory-infill"
        />
      </div>
      <div className="row">
        <label>Copies</label>
        <input
          type="number"
          step="1"
          min="1"
          max="100"
          value={copies}
          onChange={(e) => setCopies(e.target.value)}
          title="How many copies of the file to enqueue at once"
          data-testid="factory-copies"
        />
      </div>
      <div
        className={`dropzone ${dragging ? 'dragging' : ''} ${busy ? 'busy' : ''}`}
        onDragEnter={onDragEnter}
        onDragOver={onDragOver}
        onDragLeave={onDragLeave}
        onDrop={onDrop}
        data-testid="factory-dropzone"
      >
        {busy
          ? 'Submitting…'
          : (() => {
              const n = Math.max(1, Math.min(100, Math.floor(Number(copies) || 1)));
              return `Drop .stl here — queues ${n} job${n === 1 ? '' : 's'} (${uploadUnit})`;
            })()}
      </div>
      <div className="button-row">
        <button
          type="button"
          className="secondary"
          onClick={() => { if (!busy) fileInputRef.current?.click(); }}
          disabled={busy}
          data-testid="factory-browse"
        >
          Browse files…
        </button>
        <input
          ref={fileInputRef}
          type="file"
          accept=".stl"
          style={{ display: 'none' }}
          disabled={busy}
          onChange={(e) => {
            const f = e.target.files[0];
            e.target.value = '';
            handleFile(f);
          }}
          data-testid="factory-file-input"
        />
      </div>

      <h2>View</h2>
      <div className="button-row">
        <button className="secondary" onClick={onFrameAll} data-testid="factory-frame-all">
          Frame all
        </button>
        <button className="secondary" onClick={onFrameFocused} data-testid="factory-frame-focused">
          Frame focus
        </button>
      </div>

      <h2>Grid</h2>
      <div className="row">
        <label>Rows</label>
        <input
          type="number"
          min="1"
          max="10"
          value={rowsDraft}
          onChange={(e) => setRowsDraft(e.target.value)}
          data-testid="factory-rows"
        />
        <label>Cols</label>
        <input
          type="number"
          min="1"
          max="10"
          value={colsDraft}
          onChange={(e) => setColsDraft(e.target.value)}
          data-testid="factory-cols"
        />
      </div>
      <div className="row">
        <label>Sim×</label>
        <input
          type="number"
          step="0.5"
          min="0.1"
          value={speedDraft}
          onChange={(e) => setSpeedDraft(e.target.value)}
          title="Simulation speed multiplier — higher finishes prints faster"
        />
      </div>
      <div className="button-row">
        <button
          className="secondary"
          onClick={() => onConfig({
            rows: Number(rowsDraft),
            cols: Number(colsDraft),
            sim_speed: Number(speedDraft) || 1,
          })}
          data-testid="factory-apply-config"
        >
          Apply
        </button>
        <button
          className="danger"
          onClick={() => {
            if (window.confirm('Reset the factory? All jobs + printer state will be wiped.')) {
              onReset();
            }
          }}
          data-testid="factory-reset"
        >
          Reset factory
        </button>
      </div>
    </aside>
  );
}

function JobQueueList({ jobs, onCancel, onFocus, focusedId }) {
  if (!jobs.length) {
    return <div className="job-list empty">No jobs yet — drop an STL to queue one.</div>;
  }
  const sorted = [...jobs].sort((a, b) => (b.submitted_at ?? 0) - (a.submitted_at ?? 0));
  return (
    <ul className="job-list" data-testid="factory-job-list">
      {sorted.map((j) => (
        <li
          key={j.id}
          className={`job job-status-${j.status} ${j.printer_id === focusedId ? 'focused' : ''}`}
          data-testid={`job-${j.id}`}
          onClick={() => j.printer_id && onFocus(j.printer_id)}
          title={j.printer_id ? `Click to focus ${j.printer_id}` : ''}
          style={{ cursor: j.printer_id ? 'pointer' : 'default' }}
        >
          <div className="job-top">
            <span className="job-name" title={j.id}>{j.name}</span>
            <span className={`badge status-${j.status}`}>{j.status}</span>
          </div>
          <div className="job-meta">
            {j.printer_id ? <span>on {j.printer_id}</span> : <span>queued</span>}
            <span>{fmtDuration(j.duration_s)}</span>
            <span>{fmtGrams(j.filament_g)}</span>
            <span>{fmtUsd(j.total_cost_usd)}</span>
          </div>
          {j.status === 'printing' ? (
            <div className="progress-bar small">
              <div
                className="progress-fill"
                style={{ width: `${(j.progress ?? 0) * 100}%` }}
              />
            </div>
          ) : null}
          {j.error ? <div className="job-error">{j.error}</div> : null}
          {j.status === 'queued' || j.status === 'printing' ? (
            <button
              className="secondary small"
              onClick={(e) => { e.stopPropagation(); onCancel(j.id); }}
              data-testid={`job-cancel-${j.id}`}
            >
              Cancel
            </button>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

export default function FactoryView({ onBack }) {
  const [factory, setFactory] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [focusedPrinterId, setFocusedPrinterId] = useState(null);
  const canvasRef = useRef(null);
  const sceneRef = useRef(null);
  const pollRef = useRef(null);

  // Tracks which slice_result we've pushed into each rig, so we don't reload
  // the toolpath on every poll. Keyed by printer_id → job_id.
  const sliceCacheRef = useRef(new Map());
  // Whether we've already framed the shelf after the first rig was added.
  const framedRef = useRef(false);

  const refresh = useCallback(async () => {
    try {
      const state = await api.factoryState();
      setFactory(state);
    } catch (e) {
      setError(String(e));
    }
  }, []);

  // Mount a dedicated PrinterScene for the factory shelf. Separate instance
  // from the single-printer scene (different canvas, different renderer) so
  // neither view blocks the other.
  useEffect(() => {
    if (!canvasRef.current) return;
    const scene = new PrinterScene(canvasRef.current);
    // Drop the default rig the PrinterScene constructor auto-creates — on
    // the shelf every rig is positioned explicitly from the factory state.
    scene.removeRig('default');
    sceneRef.current = scene;
    window.__factoryScene = scene;
    return () => {
      scene.dispose();
      sceneRef.current = null;
      window.__factoryScene = null;
      sliceCacheRef.current.clear();
      framedRef.current = false;
    };
  }, []);

  // Sync rigs to the latest factory state whenever it changes.
  useEffect(() => {
    const scene = sceneRef.current;
    if (!scene || !factory?.printers) return;

    const desired = new Set();
    for (const p of factory.printers) {
      desired.add(p.id);
      let rig = scene.rig(p.id);
      if (!rig) {
        rig = scene.addRig(p.id, { x: p.grid_x, z: p.grid_y });
        rig.setBed(250, 210, 210);
        rig.setLabel(p.name);
      } else {
        rig.setShelfPosition(p.grid_x, p.grid_y);
      }
      const color = STATUS_COLORS[p.status] || STATUS_COLORS.offline;
      rig.setStatus(p.status, color);
    }

    // Drop rigs for printers that no longer exist (grid resize).
    for (const id of [...scene.rigs.keys()]) {
      if (!desired.has(id)) {
        scene.removeRig(id);
        sliceCacheRef.current.delete(id);
      }
    }

    // Render budget: small grids show full toolpath detail on every rig;
    // larger grids would eat the frame rate (each rig rebuilds a LineSegments2
    // per setCursor), so we downgrade all but the focused one to head-only.
    // Threshold picked by eye — 3 rigs still render smoothly on an iGPU;
    // 4+ starts to stutter.
    const budgetAll = scene.rigs.size > 3;
    for (const [id, rig] of scene.rigs) {
      const fullDetail = !budgetAll || id === focusedPrinterId;
      rig.setRenderBudget(fullDetail);
    }

    // First-render auto-frame so users see the whole shelf.
    if (!framedRef.current && scene.rigs.size > 0) {
      scene.setView('iso', null);
      scene.focus(null);
      framedRef.current = true;
    }

    // Sync toolpath + cursor per printer.
    const jobsById = new Map();
    for (const j of factory.jobs || []) jobsById.set(j.id, j);

    const loadTasks = [];
    for (const p of factory.printers) {
      const rig = scene.rig(p.id);
      if (!rig) continue;
      const jobId = p.current_job_id;
      if (!jobId) {
        // Idle or finished — clear any leftover toolpath.
        if (sliceCacheRef.current.get(p.id)) {
          rig.setToolpath([]);
          sliceCacheRef.current.delete(p.id);
        }
        continue;
      }
      const job = jobsById.get(jobId);
      if (!job) continue;

      const cached = sliceCacheRef.current.get(p.id);
      if (cached?.jobId !== jobId) {
        // Fetch slice lazily — one request per assigned job.
        loadTasks.push(
          api.factoryPrinterSlice(p.id)
            .then((payload) => {
              if (!payload?.ready || payload.job_id !== jobId) return;
              const fresh = sceneRef.current?.rig(p.id);
              if (!fresh) return;
              fresh.setToolpath(payload.moves || []);
              sliceCacheRef.current.set(p.id, {
                jobId,
                moveCount: (payload.moves || []).length,
              });
              fresh.setCursor(cursorForJob(job, (payload.moves || []).length));
            })
            .catch(() => {
              /* transient — retry on next poll */
            }),
        );
      } else {
        rig.setCursor(cursorForJob(job, cached.moveCount));
      }
    }
    // Fire-and-forget fetches run in parallel with next poll.
    if (loadTasks.length) Promise.all(loadTasks).catch(() => {});
  }, [factory, focusedPrinterId]);

  // Poll the factory state on a short interval.
  useEffect(() => {
    refresh();
    pollRef.current = setInterval(refresh, POLL_MS);
    return () => clearInterval(pollRef.current);
  }, [refresh]);

  const submitJob = useCallback(async (file, params) => {
    setBusy(true);
    setError('');
    try {
      await api.factorySubmitJobFile(file, params);
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }, [refresh]);

  const cancelJob = useCallback(async (id) => {
    try {
      await api.factoryCancelJob(id);
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  }, [refresh]);

  const applyConfig = useCallback(async (cfg) => {
    try {
      await api.factoryConfig(cfg);
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  }, [refresh]);

  const resetFactory = useCallback(async () => {
    try {
      await api.factoryReset();
      framedRef.current = false;
      sliceCacheRef.current.clear();
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  }, [refresh]);

  const focusPrinter = useCallback((printerId) => {
    const scene = sceneRef.current;
    if (!scene) return;
    scene.setFocusedRig(printerId);
    scene.focus(printerId);
    setFocusedPrinterId(printerId);
  }, []);

  const frameAll = useCallback(() => {
    sceneRef.current?.setView('iso', null);
    sceneRef.current?.focus(null);
    setFocusedPrinterId(null);
  }, []);

  const frameFocused = useCallback(() => {
    const scene = sceneRef.current;
    if (!scene || !focusedPrinterId) return;
    scene.focus(focusedPrinterId);
  }, [focusedPrinterId]);

  const jobs = factory?.jobs ?? [];
  const stats = factory?.stats ?? {};

  return (
    <div className="app factory-app">
      <FactorySidebar
        factory={factory}
        onSubmit={submitJob}
        onConfig={applyConfig}
        onReset={resetFactory}
        onFrameAll={frameAll}
        onFrameFocused={frameFocused}
        busy={busy}
        lastError={error}
        onBack={onBack}
      />

      <main className="factory-main">
        <header className="factory-header" data-testid="factory-header">
          <div className="header-stat">
            <div className="stat-label">Queued</div>
            <div className="stat-value">{stats.queued_jobs ?? 0}</div>
          </div>
          <div className="header-stat">
            <div className="stat-label">Printing</div>
            <div className="stat-value">{stats.printing_jobs ?? 0}</div>
          </div>
          <div className="header-stat">
            <div className="stat-label">Finished</div>
            <div className="stat-value">{stats.finished_jobs ?? 0}</div>
          </div>
          <div className="header-stat">
            <div className="stat-label">Lifetime prints</div>
            <div className="stat-value">{stats.prints ?? 0}</div>
          </div>
          <div className="header-stat">
            <div className="stat-label">Filament</div>
            <div className="stat-value">{fmtGrams(stats.filament_g ?? 0)}</div>
          </div>
          <div className="header-stat">
            <div className="stat-label">Cost</div>
            <div className="stat-value">{fmtUsd(stats.cost_usd ?? 0)}</div>
          </div>
        </header>

        <div className="factory-body">
          <div className="factory-shelf-wrap" data-testid="factory-shelf-wrap">
            <canvas ref={canvasRef} className="factory-canvas" data-testid="factory-canvas" />
          </div>

          <aside className="factory-queue">
            <h2>Job queue</h2>
            <JobQueueList
              jobs={jobs}
              onCancel={cancelJob}
              onFocus={focusPrinter}
              focusedId={focusedPrinterId}
            />
          </aside>
        </div>
      </main>
    </div>
  );
}
