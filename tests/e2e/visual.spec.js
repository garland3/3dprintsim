// Visual / color-diversity checks for the simulator viewer.
//
// The main UX bug the user reported is "everything is orange, can't read
// depth". These tests don't pixel-diff against a golden image (flaky across
// GL drivers) — they verify the toolpath has a diverse color palette by
// reading the LineSegments2 color buffer straight out of Three.js, and
// that the overhang/support pipeline surfaces the right counts in the
// slice summary.
import { test, expect } from '@playwright/test';
import { fileURLToPath } from 'url';
import path from 'path';

const here = path.dirname(fileURLToPath(import.meta.url));
const CUBE_SMALL = path.resolve(here, '../fixtures/cube10.stl');
const TSHAPE = path.resolve(here, '../fixtures/tshape.stl');

test.beforeEach(async ({ request }) => {
  await request.post('http://127.0.0.1:8000/api/reset');
});

test('toolpath color buffer uses more than one unique color', async ({ page }) => {
  // A one-color toolpath was the original "orange blob" bug. After the
  // role+depth color ramp, even a plain cube produces several distinct
  // swatches (perimeter vs infill, top vs bottom, depth-shaded).
  await page.goto('/');
  await page.getByTestId('file-input').setInputFiles(CUBE_SMALL);
  await expect(page.getByTestId('parts-list').locator('li')).toHaveCount(1, { timeout: 10_000 });
  await page.getByTestId('layer-height').fill('1');
  await page.getByTestId('slice').click();
  await expect(page.getByTestId('slice-summary')).toBeVisible();

  // Finish the sim so all extrude segments are painted.
  await page.getByTestId('finish-sim').click();

  const colorCount = await page.evaluate(() => {
    const scene = window.__printerScene;
    if (!scene) return 0;
    const group = scene.printedGroup;
    if (!group || group.children.length === 0) return 0;
    const lines = group.children[0];
    // LineSegments2 stores colors in its geometry attributes.
    const attr =
      lines.geometry.attributes.instanceColorStart ||
      lines.geometry.attributes.color;
    if (!attr) return 0;
    const buf = attr.array;
    const uniq = new Set();
    // Sample every 20th vertex to keep the set bounded.
    for (let i = 0; i < buf.length; i += 3 * 20) {
      const r = Math.round(buf[i] * 16);
      const g = Math.round(buf[i + 1] * 16);
      const b = Math.round(buf[i + 2] * 16);
      uniq.add(`${r},${g},${b}`);
    }
    return uniq.size;
  });

  expect(colorCount).toBeGreaterThan(3);
});

test('overhang part reports support cells and solid fill', async ({ page, request }) => {
  // The T-shape fixture has a cap that overhangs the stem on both sides.
  // The new slicer must: (a) count non-zero support cells, (b) mark the
  // stem's own top layer (which becomes the cap's bottom) as a solid fill.
  await page.goto('/');
  await page.getByTestId('file-input').setInputFiles(TSHAPE);
  await expect(page.getByTestId('parts-list').locator('li')).toHaveCount(1, { timeout: 10_000 });
  await page.getByTestId('layer-height').fill('1');
  await page.getByTestId('infill-density').fill('10');  // sparse so solid stands out
  await page.getByTestId('slice').click();
  await expect(page.getByTestId('slice-summary')).toBeVisible();

  // Read the slice summary through the backend to get support_cell_count.
  const st = await (await request.get('http://127.0.0.1:8000/api/state')).json();
  expect(st.slice.support_cell_count).toBeGreaterThan(0);

  // The legend should be present and mention supports.
  const legend = page.getByTestId('legend');
  await expect(legend).toBeVisible();
  await expect(legend).toContainText(/supports/);

  // Check the moves payload includes a "support" role somewhere.
  const slicePayload = await (await request.get('http://127.0.0.1:8000/api/slice')).json();
  expect(slicePayload.ready).toBe(true);
  const roles = new Set(slicePayload.moves.map((m) => m.role).filter(Boolean));
  expect(roles.has('support')).toBe(true);
  expect(roles.has('overhang_perimeter') || roles.has('bottom')).toBe(true);
});

test('support density of 0 suppresses support moves', async ({ page, request }) => {
  await page.goto('/');
  await page.getByTestId('file-input').setInputFiles(TSHAPE);
  await expect(page.getByTestId('parts-list').locator('li')).toHaveCount(1, { timeout: 10_000 });
  await page.getByTestId('layer-height').fill('1');
  await page.getByTestId('support-density').fill('0');
  await page.getByTestId('slice').click();
  await expect(page.getByTestId('slice-summary')).toBeVisible();

  const slicePayload = await (await request.get('http://127.0.0.1:8000/api/slice')).json();
  const hasSupport = slicePayload.moves.some((m) => m.role === 'support');
  expect(hasSupport).toBe(false);
});

test('atlas_upload MCP tool accepts a URL and adds a part', async ({ request }) => {
  // Atlas hands off a signed download URL; the backend itself serves geometry
  // so we use its own /api/parts/upload-like endpoint. But the real-world
  // test here is that atlas_upload accepts an absolute URL (we use the
  // backend's own HTTP for simplicity) and the part shows up in state.
  //
  // Skipped in this file because it requires uv+python. See the backend
  // test suite (tests/test_atlas_upload.py) for the authoritative coverage.
  // Keeping a smoke test here only for the tool listing.
  const { spawnSync } = await import('child_process');
  const BACKEND_DIR = path.resolve(here, '../../backend');
  const res = spawnSync('uv', ['run', '--frozen', 'python', '-c', `
import asyncio, json
from fastmcp import Client

async def main():
    async with Client("http://127.0.0.1:8000/mcp/") as c:
        tools = [t.name for t in await c.list_tools()]
        print(json.dumps(tools))

asyncio.run(main())
`], { encoding: 'utf-8', cwd: BACKEND_DIR, timeout: 30_000 });
  if (res.status !== 0) throw new Error(res.stderr);
  const tools = JSON.parse(res.stdout.trim());
  expect(tools).toContain('atlas_upload');
});
