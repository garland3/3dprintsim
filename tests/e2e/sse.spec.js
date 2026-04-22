// Server-Sent-Events flow: an MCP client acting as an AI agent invokes the
// printer (upload → slice → step) and the browser UI must reflect the changes
// in real time through `/api/events`, not via the legacy 2s polling.
//
// The browser tab binds its EventSource to the session id in its URL. We mint
// that id up front and hand the same id to both the MCP client and the
// Playwright page, so a single PrinterService is driven by MCP and observed
// in the UI — the Atlas contract.
import { test, expect } from '@playwright/test';
import { spawn } from 'child_process';
import { fileURLToPath } from 'url';
import path from 'path';
import fs from 'fs';

const here = path.dirname(fileURLToPath(import.meta.url));
const CUBE_PATH = path.resolve(here, '../fixtures/cube20.stl');
const CUBE_B64 = fs.readFileSync(CUBE_PATH).toString('base64');
const BACKEND_DIR = path.resolve(here, '../../backend');

function runAgentAsync(script, env = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn('uv', ['run', '--frozen', 'python', '-c', script], {
      env: { ...process.env, ...env },
      cwd: BACKEND_DIR,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (b) => { stdout += b.toString(); });
    child.stderr.on('data', (b) => { stderr += b.toString(); });
    child.on('close', (code) => {
      if (code === 0) resolve(stdout.trim());
      else reject(new Error(`agent exit ${code}\nSTDERR:\n${stderr}\nSTDOUT:\n${stdout}`));
    });
  });
}

// `addInitScript` instead of `page.evaluate` so the EventSource shim is in
// place BEFORE the React bundle boots — otherwise the shim only catches
// EventSources opened after the reload, and React's first SSE connection
// races past it.
async function instrumentEventSource(page) {
  await page.addInitScript(() => {
    window.__sseEvents = [];
    const orig = window.EventSource;
    window.EventSource = class extends orig {
      constructor(url, init) {
        super(url, init);
        const push = (type) => (e) =>
          window.__sseEvents.push({ type, data: e.data, ts: Date.now() });
        this.addEventListener('hello', push('hello'));
        this.addEventListener('state', push('state'));
        this.addEventListener('focus', push('focus'));
      }
    };
  });
}

const E2E_SESSION = `sse-${Date.now().toString(36)}`;

test.beforeEach(async ({ request }) => {
  await request.post('http://127.0.0.1:8000/api/reset');
});

test('MCP printer invocation propagates to UI via SSE', async ({ page, request }) => {
  await instrumentEventSource(page);

  // Open the UI. The frontend's api.js resolves `?session=...` and stamps
  // every REST call (and the EventSource URL) with it.
  await page.goto(`/?session=${E2E_SESSION}&embed=1`);
  await expect(page.getByTestId('sidebar')).toBeVisible();

  // Confirm the EventSource connected (hello frame arrived).
  await page.waitForFunction(
    () => (window.__sseEvents || []).some((e) => e.type === 'hello'),
    { timeout: 5_000 },
  );

  // Now invoke the printer through MCP. Every mutation must push a `state`
  // SSE frame to the live browser session the moment it lands.
  const agentScript = `
import asyncio, os
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

async def main():
    transport = StreamableHttpTransport(
        "http://127.0.0.1:8000/mcp/",
        headers={"X-Session-Id": os.environ["E2E_SESSION"]},
    )
    async with Client(transport) as c:
        await c.call_tool("set_bed_size", {"x_mm": 220, "y_mm": 220, "z_mm": 210})
        await c.call_tool("upload_stl", {
            "name": "agent-cube.stl",
            "stl_base64": os.environ["CUBE_B64"],
        })
        await c.call_tool("slice_all", {"layer_height_mm": 1.0, "perimeters": 1})
        await c.call_tool("start_simulation", {"speed": 1.0})
        for _ in range(5):
            await c.call_tool("step_simulation", {"steps": 10})
        print("agent-ok")

asyncio.run(main())
`;
  const agentPromise = runAgentAsync(agentScript, { CUBE_B64, E2E_SESSION });

  // The UI must pick up the uploaded part within a couple of seconds —
  // faster than the 5s fallback polling interval, so if the assertion
  // passes quickly we know SSE actually delivered. Scope to <li> rows so
  // we don't accidentally count the child inputs/buttons (which also
  // stamp `data-testid="part-scale-<id>"` etc).
  await expect(
    page.locator('li[data-testid^="part-"]'),
    { timeout: 10_000 },
  ).toHaveCount(1);

  // Slice summary lands in the sidebar once the MCP tool slices.
  await expect(
    page.getByTestId('slice-summary'),
    { timeout: 10_000 },
  ).toContainText(/layers/);

  await agentPromise;

  // Verify we actually received `state` frames over SSE — not just polling.
  const frames = await page.evaluate(() => window.__sseEvents || []);
  const hello = frames.filter((f) => f.type === 'hello');
  const state = frames.filter((f) => f.type === 'state');
  expect(hello.length).toBeGreaterThanOrEqual(1);
  expect(state.length).toBeGreaterThanOrEqual(1);

  // And the backend state mirrors what the UI sees — same PrinterService.
  const backendState = await (
    await request.get('http://127.0.0.1:8000/api/state', {
      headers: { 'X-Session-Id': E2E_SESSION },
    })
  ).json();
  expect(backendState.parts.length).toBe(1);
  expect(backendState.bed_size).toEqual([220, 220, 210]);
  expect(backendState.slice).not.toBeNull();
});

test('focus_viewer MCP tool triggers a camera focus via SSE', async ({ page }) => {
  await instrumentEventSource(page);

  const sid = `${E2E_SESSION}-focus`;
  await page.goto(`/?session=${sid}&embed=1`);
  await expect(page.getByTestId('sidebar')).toBeVisible();

  // Wait for hello so we know the SSE subscription is live.
  await page.waitForFunction(
    () => (window.__sseEvents || []).some((e) => e.type === 'hello'),
    { timeout: 5_000 },
  );

  // Patch the scene's focus() to count invocations. The scene is exposed on
  // `window.__printerScene` by App.jsx for exactly this kind of assertion.
  await page.waitForFunction(() => !!window.__printerScene, { timeout: 5_000 });
  await page.evaluate(() => {
    window.__focusCalls = 0;
    const origFocus = window.__printerScene.focus.bind(window.__printerScene);
    window.__printerScene.focus = () => {
      window.__focusCalls += 1;
      return origFocus();
    };
  });

  await runAgentAsync(
    `
import asyncio, os
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

async def main():
    t = StreamableHttpTransport(
        "http://127.0.0.1:8000/mcp/",
        headers={"X-Session-Id": os.environ["SID"]},
    )
    async with Client(t) as c:
        await c.call_tool("focus_viewer", {})

asyncio.run(main())
`,
    { SID: sid },
  );

  // Focus propagates via SSE — must fire well under the 5s fallback timer.
  await page.waitForFunction(() => (window.__focusCalls || 0) >= 1, {
    timeout: 8_000,
  });
  const sseFocus = await page.evaluate(() =>
    (window.__sseEvents || []).filter((e) => e.type === 'focus'),
  );
  expect(sseFocus.length).toBeGreaterThanOrEqual(1);
});
