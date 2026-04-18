// MCP flow: spawn a python subprocess that acts as an AI agent using the
// fastmcp client, talking over streamable-http to the running backend. This
// proves the same printer an agent drives is visible to the UI.
import { test, expect } from '@playwright/test';
import { spawnSync } from 'child_process';
import { fileURLToPath } from 'url';
import path from 'path';
import fs from 'fs';

const here = path.dirname(fileURLToPath(import.meta.url));
const CUBE_PATH = path.resolve(here, '../fixtures/cube20.stl');
const CUBE_B64 = fs.readFileSync(CUBE_PATH).toString('base64');
const BACKEND_DIR = path.resolve(here, '../../backend');

function runAgent(script, env = {}) {
  // Run through `uv` so the backend's .venv (with fastmcp) is used.
  const res = spawnSync('uv', ['run', '--frozen', 'python', '-c', script], {
    encoding: 'utf-8',
    env: { ...process.env, ...env },
    cwd: BACKEND_DIR,
    timeout: 30_000,
  });
  if (res.status !== 0) {
    throw new Error(`python agent failed (code=${res.status}):\nSTDERR:\n${res.stderr}\nSTDOUT:\n${res.stdout}`);
  }
  return res.stdout.trim();
}

// Shared session id for the agent's MCP client and the UI-style REST call —
// this is the same trick Atlas uses (the `open_viewer` tool hands the agent's
// session id to the iframe, which echoes it back as `X-Session-Id` on every
// REST call). Fresh per run so tests don't collide across retries.
const E2E_SESSION = `e2e-${Date.now().toString(36)}`;
const E2E_HEADERS = { 'X-Session-Id': E2E_SESSION };

test.beforeEach(async ({ request }) => {
  // No X-Session-Id here — the bare /api/reset wipes every session so stale
  // state from a previous run can't leak into this one.
  await request.post('http://127.0.0.1:8000/api/reset');
});

test('AI agent can drive the full pipeline via MCP tools', async ({ request }) => {
  const script = `
import asyncio, json, os
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

async def main():
    # X-Session-Id pins every tool call to the shared test session so the
    # REST assertion below sees the same PrinterService the agent drove.
    transport = StreamableHttpTransport(
        "http://127.0.0.1:8000/mcp/",
        headers={"X-Session-Id": os.environ["E2E_SESSION"]},
    )
    async with Client(transport) as c:
        tools = [t.name for t in await c.list_tools()]
        assert "upload_stl" in tools, tools

        await c.call_tool("set_bed_size", {"x_mm": 220, "y_mm": 220, "z_mm": 210})
        up = await c.call_tool("upload_stl", {"name": "agent-cube.stl", "stl_base64": os.environ["CUBE_B64"]})
        up_data = up.structured_content or (up.data if hasattr(up, "data") else None)
        assert up_data and "id" in up_data

        await c.call_tool("auto_arrange", {})
        slice_res = await c.call_tool("slice_all", {"layer_height_mm": 1.0, "perimeters": 1})
        s = slice_res.structured_content or (slice_res.data if hasattr(slice_res, "data") else None)
        assert s["layer_count"] > 10, s

        await c.call_tool("start_simulation", {"speed": 1.0})
        for _ in range(20):
            await c.call_tool("step_simulation", {"steps": 10})

        frame = await c.call_tool("get_simulation_frame", {})
        f = frame.structured_content or (frame.data if hasattr(frame, "data") else None)
        print(json.dumps({"cursor": f["cursor"], "total": f["total_moves"], "head": f["head"] is not None}))

asyncio.run(main())
  `;
  const out = runAgent(script, { CUBE_B64, E2E_SESSION });
  const parsed = JSON.parse(out);
  expect(parsed.cursor).toBeGreaterThan(0);
  expect(parsed.total).toBeGreaterThan(0);
  expect(parsed.head).toBe(true);

  // The same printer is visible to the HTTP API when the REST call reuses
  // the agent's session id — this is the Atlas contract.
  const state = await (
    await request.get('http://127.0.0.1:8000/api/state', { headers: E2E_HEADERS })
  ).json();
  expect(state.bed_size).toEqual([220, 220, 210]);
  expect(state.parts.length).toBe(1);
  expect(state.slice).not.toBeNull();

  // And a REST call with no session header lands in a DIFFERENT (default)
  // PrinterService — proves isolation is real, not accidental.
  const isolated = await (
    await request.get('http://127.0.0.1:8000/api/state')
  ).json();
  expect(isolated.parts.length).toBe(0);
});

test('MCP tools include the full pipeline surface', async ({}) => {
  const script = `
import asyncio, json
from fastmcp import Client
async def main():
    async with Client("http://127.0.0.1:8000/mcp/") as c:
        tools = [t.name for t in await c.list_tools()]
        print(json.dumps(tools))
asyncio.run(main())
  `;
  const out = runAgent(script);
  const tools = JSON.parse(out);
  const expected = [
    'get_printer_state', 'set_bed_size', 'upload_stl', 'list_parts',
    'remove_part', 'clear_parts', 'auto_arrange', 'slice_all', 'get_gcode',
    'start_simulation', 'step_simulation', 'set_simulation_cursor',
    'get_simulation_frame',
  ];
  for (const name of expected) {
    expect(tools).toContain(name);
  }
});
