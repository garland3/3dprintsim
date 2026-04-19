// Thin wrapper over the backend REST API. Deliberately no auth, no retries —
// this is a developer tool running on localhost or inside the Atlas canvas.
//
// When embedded in Atlas (open_viewer MCP tool), the iframe URL carries
// `?session=<mcp-session-id>&embed=1`. We pick that session id up once at
// boot and stamp every REST call with `X-Session-Id` so the backend routes
// the call to the same PrinterService the LLM's MCP tools are driving.

// Printer state is dynamic: parts, slice, and simulation cursor all change
// without cache-busting URL params. Tell the browser not to cache GETs so we
// never show stale data after a backend restart or a sibling POST.
const GET = { cache: 'no-store' };

// Resolve the session id for this tab. Preference order:
//   1. ?session=<id> in the current URL (what Atlas injects via open_viewer)
//   2. sessionStorage (stable across reloads of the same tab)
//   3. a freshly minted random id (so two browser tabs don't stomp each other)
function resolveSessionId() {
  const params = new URLSearchParams(window.location.search);
  const fromUrl = params.get('session');
  if (fromUrl) {
    try {
      window.sessionStorage.setItem('printsim.sessionId', fromUrl);
    } catch (_) {
      // sessionStorage can throw in sandboxed iframes without `allow-same-origin`;
      // falling through just means we re-read the URL on every reload, which is fine.
    }
    return fromUrl;
  }
  try {
    const stored = window.sessionStorage.getItem('printsim.sessionId');
    if (stored) return stored;
  } catch (_) {
    // same sandbox caveat as above
  }
  const fresh =
    globalThis.crypto && globalThis.crypto.randomUUID
      ? globalThis.crypto.randomUUID()
      : `tab-${Math.random().toString(36).slice(2, 10)}-${Date.now().toString(36)}`;
  try {
    window.sessionStorage.setItem('printsim.sessionId', fresh);
  } catch (_) {
    // see above
  }
  return fresh;
}

export const sessionId = resolveSessionId();
// Exposed for diagnostic overlays (small corner badge in embed mode).
export const isEmbedded = (() => {
  const params = new URLSearchParams(window.location.search);
  if (params.get('embed') === '1' || params.get('embed') === 'true') return true;
  try {
    return window.self !== window.top;
  } catch (_) {
    // Cross-origin frame access throws — if it threw, we're definitely embedded.
    return true;
  }
})();

const SESSION_HEADERS = { 'X-Session-Id': sessionId };

function withSession(headers = {}) {
  return { ...SESSION_HEADERS, ...headers };
}

async function json(resp) {
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`${resp.status} ${resp.statusText}: ${text}`);
  }
  const ct = resp.headers.get('content-type') || '';
  return ct.includes('application/json') ? resp.json() : resp.text();
}

function getJSON(path) {
  return fetch(path, { ...GET, headers: SESSION_HEADERS }).then(json);
}

function postJSON(path, body) {
  return fetch(path, {
    method: 'POST',
    headers: withSession({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(body),
  }).then(json);
}

export const api = {
  sessionId,
  isEmbedded,
  health: () => getJSON('/api/health'),
  sessionInfo: () => getJSON('/api/session'),
  state: () => getJSON('/api/state'),
  setBed: (x, y, z) => postJSON('/api/bed', { x, y, z }),
  upload: (file, scale = 1) => {
    const fd = new FormData();
    fd.append('file', file);
    if (scale && scale !== 1) fd.append('scale', String(scale));
    return fetch('/api/parts/upload', {
      method: 'POST',
      headers: SESSION_HEADERS,
      body: fd,
    }).then(json);
  },
  listParts: () => getJSON('/api/parts'),
  partGeometry: (id) => getJSON(`/api/parts/${id}/geometry`),
  removePart: (id) =>
    fetch(`/api/parts/${id}`, { method: 'DELETE', headers: SESSION_HEADERS }).then(json),
  clearParts: () =>
    fetch('/api/parts/clear', { method: 'POST', headers: SESSION_HEADERS }).then(json),
  setPartScale: (id, scale) => postJSON(`/api/parts/${id}/scale`, { scale }),
  rotatePart: (id, axis, degrees) =>
    postJSON(`/api/parts/${id}/rotate`, { axis, degrees }),
  resetPartRotation: (id) =>
    postJSON(`/api/parts/${id}/rotate`, { axis: 'z', degrees: 0, reset: true }),
  setPartPosition: (id, x, y) =>
    postJSON(`/api/parts/${id}/position`, { x, y }),
  arrange: () =>
    fetch('/api/arrange', { method: 'POST', headers: SESSION_HEADERS }).then(json),
  slice: (params) => postJSON('/api/slice', params),
  getSlice: () => getJSON('/api/slice'),
  gcode: () =>
    fetch('/api/gcode', { ...GET, headers: SESSION_HEADERS }).then((r) => r.text()),
  startSim: (speed = 1.0) => postJSON('/api/simulation/start', { speed }),
  stepSim: (steps) => postJSON('/api/simulation/step', { steps }),
  setCursor: (cursor) => postJSON('/api/simulation/cursor', { cursor }),
  viewerRequests: () => getJSON('/api/viewer/requests'),
  requestFocus: () =>
    fetch('/api/viewer/focus', { method: 'POST', headers: SESSION_HEADERS }).then(json),
};
