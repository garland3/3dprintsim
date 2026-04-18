// Playwright helpers for session-scoped API assertions.
//
// The frontend mints a session id per browser tab and sends it as
// `X-Session-Id` on every REST call. When a test needs to poke the backend
// directly (e.g. to read /api/state and compare against what the UI did),
// it must use the *same* session id — otherwise it'll hit the "default"
// PrinterService and see an empty printer. These helpers pull the session
// id out of the page's sessionStorage and stamp it onto `request` calls.

export async function pageSessionId(page) {
  // The frontend stores the resolved id here (see frontend/src/api.js).
  return await page.evaluate(() => window.sessionStorage.getItem('printsim.sessionId'));
}

export async function apiHeaders(page) {
  const sid = await pageSessionId(page);
  return sid ? { 'X-Session-Id': sid } : {};
}

export async function apiGetJSON(page, request, path) {
  const headers = await apiHeaders(page);
  const resp = await request.get(`http://127.0.0.1:8000${path}`, { headers });
  return await resp.json();
}
