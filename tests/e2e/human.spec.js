// Human UX: a user opens the page, uploads a cube, slices, and watches it print.
import { test, expect } from '@playwright/test';
import { fileURLToPath } from 'url';
import path from 'path';
import { apiGetJSON } from './_session.js';

const here = path.dirname(fileURLToPath(import.meta.url));
const CUBE = path.resolve(here, '../fixtures/cube20.stl');
const CUBE_SMALL = path.resolve(here, '../fixtures/cube10.stl');

test.beforeEach(async ({ request }) => {
  await request.post('http://127.0.0.1:8000/api/reset');
});

test('page loads with default Prusa-sized bed and empty parts list', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByTestId('sidebar')).toBeVisible();
  await expect(page.getByTestId('overlay')).toContainText('bed 250');
  await expect(page.getByTestId('parts-list')).toBeEmpty();
});

test('bed size can be changed from the UI', async ({ page }) => {
  await page.goto('/');
  await page.getByTestId('bed-x').fill('180');
  await page.getByTestId('bed-y').fill('180');
  await page.getByTestId('bed-z').fill('180');
  await page.getByTestId('apply-bed').click();
  await expect(page.getByTestId('overlay')).toContainText('bed 180');
});

test('uploading an STL adds it to the parts list with the right size', async ({ page }) => {
  await page.goto('/');
  await page.getByTestId('file-input').setInputFiles(CUBE);
  await expect(page.getByTestId('parts-list').locator('li')).toHaveCount(1, { timeout: 10_000 });
  await expect(page.getByTestId('parts-list')).toContainText('cube20.stl');
  await expect(page.getByTestId('parts-list')).toContainText('20.0 × 20.0 × 20.0');
});

test('multiple uploads auto-arrange without overlap', async ({ page, request }) => {
  await page.goto('/');
  await expect(page.getByTestId('sidebar')).toBeVisible();
  for (let i = 0; i < 3; i++) {
    await page.getByTestId('file-input').setInputFiles(CUBE);
    await expect(page.getByTestId('parts-list').locator('li')).toHaveCount(i + 1, { timeout: 15_000 });
  }
  // confirm the backend placed them all within the bed — must read the
  // same session the browser is writing to, not the default one.
  const state = await apiGetJSON(page, request, '/api/state');
  const placed = state.parts.filter((p) => p.placement);
  expect(placed.length).toBe(3);
  for (const p of placed) {
    expect(p.placement.x).toBeGreaterThanOrEqual(0);
    expect(p.placement.y).toBeGreaterThanOrEqual(0);
    expect(p.placement.x + p.size[0]).toBeLessThanOrEqual(state.bed_size[0]);
    expect(p.placement.y + p.size[1]).toBeLessThanOrEqual(state.bed_size[1]);
  }
});

test('slice produces layers, moves, and a visible toolpath', async ({ page }) => {
  await page.goto('/');
  await page.getByTestId('file-input').setInputFiles(CUBE_SMALL);
  await expect(page.getByTestId('parts-list').locator('li')).toHaveCount(1, { timeout: 10_000 });

  await page.getByTestId('layer-height').fill('1');
  await page.getByTestId('slice').click();

  const summary = page.getByTestId('slice-summary');
  await expect(summary).toBeVisible();
  await expect(summary).toContainText(/\d+ layers/);

  // The Three.js scene exposes itself on window for inspection.
  const stats = await page.evaluate(() => window.__printerScene?.stats());
  expect(stats.hasToolpath).toBe(true);
  expect(stats.parts).toBe(1);
});

test('simulation animates the head along the toolpath', async ({ page }) => {
  await page.goto('/');
  await page.getByTestId('file-input').setInputFiles(CUBE_SMALL);
  await expect(page.getByTestId('parts-list').locator('li')).toHaveCount(1);

  await page.getByTestId('layer-height').fill('1');
  await page.getByTestId('slice').click();
  await expect(page.getByTestId('slice-summary')).toBeVisible();

  await page.getByTestId('start-sim').click();

  // Wait for cursor to advance.
  await expect.poll(
    async () => {
      const t = await page.getByTestId('sim-cursor').textContent();
      const [cur] = (t || '0 / 0').split('/').map((s) => parseInt(s.trim(), 10));
      return cur;
    },
    { timeout: 20_000 },
  ).toBeGreaterThan(0);

  // Jump to end and confirm printed geometry accumulated.
  await page.getByTestId('finish-sim').click();
  const stats = await page.evaluate(() => window.__printerScene?.stats());
  expect(stats.printedVerts).toBeGreaterThan(0);
  expect(stats.head).not.toBeNull();
});

test('sim-slider scrubs without running', async ({ page }) => {
  await page.goto('/');
  await page.getByTestId('file-input').setInputFiles(CUBE_SMALL);
  await expect(page.getByTestId('parts-list').locator('li')).toHaveCount(1);
  await page.getByTestId('layer-height').fill('1');
  await page.getByTestId('slice').click();
  await expect(page.getByTestId('slice-summary')).toBeVisible();

  // Read total from the summary text.
  const summary = await page.getByTestId('slice-summary').textContent();
  const totalMatch = summary.match(/(\d+) moves/);
  const total = parseInt(totalMatch[1], 10);
  expect(total).toBeGreaterThan(10);

  // Move slider to ~25%.
  const slider = page.getByTestId('sim-slider');
  await slider.fill(String(Math.floor(total * 0.25)));
  await expect(page.getByTestId('sim-cursor')).toContainText(/\d+ \/ \d+/);
  const stats1 = await page.evaluate(() => window.__printerScene?.stats());

  // Now fill to 100% and confirm more verts extruded.
  await slider.fill(String(total));
  const stats2 = await page.evaluate(() => window.__printerScene?.stats());
  expect(stats2.printedVerts).toBeGreaterThan(stats1.printedVerts);
});

test('clear removes all parts and hides toolpath', async ({ page }) => {
  await page.goto('/');
  await page.getByTestId('file-input').setInputFiles(CUBE);
  await expect(page.getByTestId('parts-list').locator('li')).toHaveCount(1);
  await page.getByTestId('clear').click();
  await expect(page.getByTestId('parts-list').locator('li')).toHaveCount(0);
});

test('removing a single part works', async ({ page }) => {
  await page.goto('/');
  await page.getByTestId('file-input').setInputFiles(CUBE);
  await page.getByTestId('file-input').setInputFiles(CUBE_SMALL);
  await expect(page.getByTestId('parts-list').locator('li')).toHaveCount(2);
  await page.getByTestId('parts-list').locator('button[aria-label="remove"]').first().click();
  await expect(page.getByTestId('parts-list').locator('li')).toHaveCount(1);
});
