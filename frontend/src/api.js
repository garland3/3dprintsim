// Thin wrapper over the backend REST API. Deliberately no auth, no retries —
// this is a developer tool running on localhost.

// Printer state is dynamic: parts, slice, and simulation cursor all change
// without cache-busting URL params. Tell the browser not to cache GETs so we
// never show stale data after a backend restart or a sibling POST.
const GET = { cache: 'no-store' };

async function json(resp) {
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`${resp.status} ${resp.statusText}: ${text}`);
  }
  const ct = resp.headers.get('content-type') || '';
  return ct.includes('application/json') ? resp.json() : resp.text();
}

export const api = {
  health: () => fetch('/api/health', GET).then(json),
  state: () => fetch('/api/state', GET).then(json),
  setBed: (x, y, z) =>
    fetch('/api/bed', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ x, y, z }),
    }).then(json),
  upload: (file, scale = 1) => {
    const fd = new FormData();
    fd.append('file', file);
    if (scale && scale !== 1) fd.append('scale', String(scale));
    return fetch('/api/parts/upload', { method: 'POST', body: fd }).then(json);
  },
  listParts: () => fetch('/api/parts', GET).then(json),
  partGeometry: (id) => fetch(`/api/parts/${id}/geometry`, GET).then(json),
  removePart: (id) => fetch(`/api/parts/${id}`, { method: 'DELETE' }).then(json),
  clearParts: () => fetch('/api/parts/clear', { method: 'POST' }).then(json),
  setPartScale: (id, scale) =>
    fetch(`/api/parts/${id}/scale`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scale }),
    }).then(json),
  arrange: () => fetch('/api/arrange', { method: 'POST' }).then(json),
  slice: (params) =>
    fetch('/api/slice', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(params),
    }).then(json),
  getSlice: () => fetch('/api/slice', GET).then(json),
  gcode: () => fetch('/api/gcode', GET).then((r) => r.text()),
  startSim: (speed = 1.0) =>
    fetch('/api/simulation/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ speed }),
    }).then(json),
  stepSim: (steps) =>
    fetch('/api/simulation/step', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ steps }),
    }).then(json),
  setCursor: (cursor) =>
    fetch('/api/simulation/cursor', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cursor }),
    }).then(json),
  viewerRequests: () => fetch('/api/viewer/requests', GET).then(json),
  requestFocus: () =>
    fetch('/api/viewer/focus', { method: 'POST' }).then(json),
};
