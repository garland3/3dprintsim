import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api } from './api.js';

// Status → color. Used for both the tile's left border and the little status
// LED dot, so the grid reads at a glance (blue = idle, orange = printing, ...).
const STATUS_COLORS = {
  idle: '#3a82e4',
  printing: '#ff9130',
  finished: '#58cc8c',
  unloading: '#f2d14d',
  offline: '#8b9299',
};

// How often the factory view repolls state. Short enough that progress bars
// feel live; long enough that it doesn't hammer the backend when the page
// has focus but nothing is printing.
const POLL_MS = 750;

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

// Printer tile — renders status, current job progress, and lifetime totals.
// No 3D here on purpose: 9 live Three.js scenes side-by-side tanks the frame
// rate, and the factory view is fundamentally about the queue-level state,
// not per-bed toolpath detail.
function PrinterTile({ printer, job, onCancel, now }) {
  const color = STATUS_COLORS[printer.status] || STATUS_COLORS.offline;
  const progress = job ? Math.min(1, Math.max(0, job.progress ?? 0)) : 0;
  const pctLabel = job
    ? `${Math.round(progress * 100)}%`
    : printer.status === 'idle'
    ? 'ready'
    : printer.status;
  const remaining = job && job.duration_s
    ? Math.max(0, job.duration_s * (1 - progress))
    : 0;

  return (
    <div
      className={`printer-tile status-${printer.status}`}
      style={{ borderLeftColor: color }}
      data-testid={`printer-${printer.id}`}
    >
      <div className="tile-head">
        <span className="led" style={{ background: color }} />
        <span className="tile-name">{printer.name}</span>
        <span className="tile-status">{pctLabel}</span>
      </div>
      {job ? (
        <>
          <div className="tile-job" title={job.name}>{job.name}</div>
          <div className="progress-bar">
            <div
              className="progress-fill"
              style={{ width: `${progress * 100}%`, background: color }}
            />
          </div>
          <div className="tile-meta">
            <span>{fmtDuration(remaining)} left</span>
            <span>{fmtUsd(job.total_cost_usd)}</span>
          </div>
        </>
      ) : (
        <>
          <div className="tile-job muted">— no active job —</div>
          <div className="progress-bar">
            <div className="progress-fill" style={{ width: '0%' }} />
          </div>
          <div className="tile-meta">
            <span>{printer.lifetime_prints} prints</span>
            <span>{fmtUsd(printer.lifetime_cost_usd)}</span>
          </div>
        </>
      )}
      <div className="tile-lifetime">
        filament: {fmtGrams(printer.lifetime_filament_g)} ·
        time: {fmtDuration(printer.lifetime_print_time_s)}
      </div>
      {job && job.status === 'printing' ? (
        <button
          className="tile-cancel danger"
          onClick={() => onCancel(job.id)}
          data-testid={`cancel-${job.id}`}
        >
          Cancel
        </button>
      ) : null}
    </div>
  );
}

// Pick-and-place robot overlay. Positioned absolutely over the printer grid;
// its CSS transform tweens between a home position and the active printer so
// "the robot moves to unload" is visible without needing a full 3D scene.
function Robot({ robot, printerIdToPos }) {
  const home = { left: '50%', top: 'calc(100% + 12px)' };
  const pos = useMemo(() => {
    if (robot.status !== 'unloading' || !robot.target_printer_id) return home;
    const p = printerIdToPos[robot.target_printer_id];
    if (!p) return home;
    return { left: `${p.left}%`, top: `${p.top}%` };
  }, [robot.status, robot.target_printer_id, printerIdToPos]);

  return (
    <div
      className={`factory-robot status-${robot.status}`}
      style={pos}
      data-testid="factory-robot"
      title={
        robot.status === 'unloading'
          ? `Unloading ${robot.target_printer_id} (${Math.round(
              (robot.progress ?? 0) * 100,
            )}%)`
          : 'Robot idle'
      }
    >
      <span className="robot-body">🤖</span>
      {robot.status === 'unloading' ? (
        <div className="robot-progress">
          <div
            className="robot-progress-fill"
            style={{ width: `${(robot.progress ?? 0) * 100}%` }}
          />
        </div>
      ) : null}
    </div>
  );
}

// Sidebar for the factory view — replaces the default printer sidebar when
// factory mode is active. Lives here (not in App.jsx) so the factory feature
// stays a self-contained module that's easy to delete behind its flag.
function FactorySidebar({
  factory,
  onSubmit,
  onConfig,
  onReset,
  busy,
  lastError,
  onBack,
}) {
  const cfg = factory?.config;
  const [uploadUnit, setUploadUnit] = useState('mm');
  const [dragging, setDragging] = useState(false);
  const [layerHeight, setLayerHeight] = useState('0.4');
  const [infill, setInfill] = useState('20');
  const [rowsDraft, setRowsDraft] = useState(cfg?.rows ?? 3);
  const [colsDraft, setColsDraft] = useState(cfg?.cols ?? 3);
  const [speedDraft, setSpeedDraft] = useState(cfg?.sim_speed ?? 1);

  useEffect(() => {
    if (!cfg) return;
    setRowsDraft(cfg.rows);
    setColsDraft(cfg.cols);
    setSpeedDraft(cfg.sim_speed);
  }, [cfg?.rows, cfg?.cols, cfg?.sim_speed]);

  const handleFile = (file) => {
    if (!file) return;
    const scale = uploadUnit === 'in' ? 25.4 : 1;
    onSubmit(file, {
      scale,
      layer_height: Number(layerHeight) || 0.4,
      infill_density: Math.max(0, Math.min(100, Number(infill) || 0)) / 100,
    });
  };

  const onDrop = (e) => {
    e.preventDefault();
    setDragging(false);
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
      <label
        className={`dropzone ${dragging ? 'dragging' : ''} ${busy ? 'busy' : ''}`}
        onDragOver={(e) => { if (!busy) { e.preventDefault(); setDragging(true); } }}
        onDragLeave={() => setDragging(false)}
        onDrop={busy ? (e) => e.preventDefault() : onDrop}
        data-testid="factory-dropzone"
      >
        {busy ? 'Submitting…' : `Drop .stl to queue a job (${uploadUnit})`}
        <input
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
      </label>

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

function JobQueueList({ jobs, onCancel }) {
  if (!jobs.length) {
    return <div className="job-list empty">No jobs yet — drop an STL to queue one.</div>;
  }
  // Most recent on top so new submissions appear without scrolling.
  const sorted = [...jobs].sort((a, b) => (b.submitted_at ?? 0) - (a.submitted_at ?? 0));
  return (
    <ul className="job-list" data-testid="factory-job-list">
      {sorted.map((j) => (
        <li key={j.id} className={`job job-status-${j.status}`} data-testid={`job-${j.id}`}>
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
              onClick={() => onCancel(j.id)}
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
  const pollRef = useRef(null);

  const refresh = useCallback(async () => {
    try {
      const state = await api.factoryState();
      setFactory(state);
    } catch (e) {
      setError(String(e));
    }
  }, []);

  // Poll the factory state on a short interval. Every tick also advances the
  // server-side simulation state machine (factory.tick() runs inside
  // factoryState()), so prints make visible progress without the client
  // needing to maintain its own clock.
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
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  }, [refresh]);

  const printers = factory?.printers ?? [];
  const jobs = factory?.jobs ?? [];
  const stats = factory?.stats ?? {};
  const robot = factory?.robot ?? { status: 'idle', progress: 0 };
  const cfg = factory?.config;

  // Lookup table: printer id → {job, position-on-grid percentage}. The robot
  // uses this to tween its CSS transform toward the active printer without
  // re-measuring DOM nodes every frame.
  const { jobByPrinter, printerPositions } = useMemo(() => {
    const byId = {};
    for (const j of jobs) {
      if (j.printer_id) byId[j.printer_id] = j;
    }
    const pos = {};
    const rows = cfg?.rows ?? 1;
    const cols = cfg?.cols ?? 1;
    for (const p of printers) {
      // Center each printer tile within its grid cell, as a percentage of the
      // grid container. +0.5 centers the dot on the tile rather than its corner.
      pos[p.id] = {
        left: ((p.col + 0.5) / cols) * 100,
        top: ((p.row + 0.5) / rows) * 100,
      };
    }
    return { jobByPrinter: byId, printerPositions: pos };
  }, [jobs, printers, cfg?.rows, cfg?.cols]);

  const gridStyle = cfg
    ? {
        gridTemplateColumns: `repeat(${cfg.cols}, minmax(0, 1fr))`,
        gridTemplateRows: `repeat(${cfg.rows}, minmax(180px, 1fr))`,
      }
    : undefined;

  return (
    <div className="app factory-app">
      <FactorySidebar
        factory={factory}
        onSubmit={submitJob}
        onConfig={applyConfig}
        onReset={resetFactory}
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
          <div className="factory-shelf-wrap">
            <div className="factory-shelf" style={gridStyle} data-testid="factory-grid">
              {printers.map((p) => (
                <PrinterTile
                  key={p.id}
                  printer={p}
                  job={jobByPrinter[p.id] ?? null}
                  onCancel={cancelJob}
                  now={factory?.now ?? 0}
                />
              ))}
              <Robot robot={robot} printerIdToPos={printerPositions} />
            </div>
          </div>

          <aside className="factory-queue">
            <h2>Job queue</h2>
            <JobQueueList jobs={jobs} onCancel={cancelJob} />
          </aside>
        </div>
      </main>
    </div>
  );
}
