// Thin wrapper over the backend REST API. Deliberately no auth, no retries —
// this is a developer tool running on localhost.

async function json(resp) {
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`${resp.status} ${resp.statusText}: ${text}`);
  }
  const ct = resp.headers.get('content-type') || '';
  return ct.includes('application/json') ? resp.json() : resp.text();
}

export const api = {
  health: () => fetch('/api/health').then(json),
  state: () => fetch('/api/state').then(json),
  setBed: (x, y, z) =>
    fetch('/api/bed', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ x, y, z }),
    }).then(json),
  upload: (file) => {
    const fd = new FormData();
    fd.append('file', file);
    return fetch('/api/parts/upload', { method: 'POST', body: fd }).then(json);
  },
  listParts: () => fetch('/api/parts').then(json),
  partGeometry: (id) => fetch(`/api/parts/${id}/geometry`).then(json),
  removePart: (id) => fetch(`/api/parts/${id}`, { method: 'DELETE' }).then(json),
  clearParts: () => fetch('/api/parts/clear', { method: 'POST' }).then(json),
  arrange: () => fetch('/api/arrange', { method: 'POST' }).then(json),
  slice: (layer_height, perimeters) =>
    fetch('/api/slice', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ layer_height, perimeters }),
    }).then(json),
  getSlice: () => fetch('/api/slice').then(json),
  gcode: () => fetch('/api/gcode').then((r) => r.text()),
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
};
