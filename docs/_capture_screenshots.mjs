// One-off script that drives the running UI and captures screenshots for docs/.
// Run with: node docs/_capture_screenshots.mjs
// (requires backend on :8000 and frontend on :5173)

import { chromium } from '@playwright/test';
import { fileURLToPath } from 'url';
import path from 'path';
import fs from 'fs';

const here = path.dirname(fileURLToPath(import.meta.url));
const CUBE20 = path.resolve(here, '..', 'tests', 'fixtures', 'cube20.stl');
const CUBE10 = path.resolve(here, '..', 'tests', 'fixtures', 'cube10.stl');
const TSHAPE = path.resolve(here, '..', 'tests', 'fixtures', 'tshape.stl');
const OUT = path.resolve(here, 'screenshots');

async function waitForIdle(page, ms = 400) {
  await page.waitForTimeout(ms);
}

async function resetBackend() {
  const res = await fetch('http://127.0.0.1:8000/api/reset', { method: 'POST' });
  if (!res.ok) throw new Error('reset failed');
}

async function snap(page, name) {
  const file = path.join(OUT, `${name}.png`);
  await page.screenshot({ path: file, fullPage: false });
  console.log('  →', path.relative(path.resolve(here, '..'), file));
}

async function main() {
  fs.mkdirSync(OUT, { recursive: true });

  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1400, height: 900 } });
  const page = await ctx.newPage();

  console.log('1. empty printer');
  await resetBackend();
  await page.goto('http://127.0.0.1:5173/');
  await page.getByTestId('sidebar').waitFor();
  await waitForIdle(page);
  await snap(page, '01-empty-bed');

  console.log('2. one part uploaded');
  await page.getByTestId('file-input').setInputFiles(CUBE20);
  await page.getByTestId('parts-list').locator('li').first().waitFor({ timeout: 15_000 });
  await waitForIdle(page, 800);
  await snap(page, '02-one-part');

  console.log('3. multiple parts, auto-arranged');
  await page.getByTestId('file-input').setInputFiles(CUBE20);
  await page.getByTestId('parts-list').locator('li').nth(1).waitFor({ timeout: 10_000 });
  await page.getByTestId('file-input').setInputFiles(CUBE10);
  await page.getByTestId('parts-list').locator('li').nth(2).waitFor({ timeout: 10_000 });
  await page.getByTestId('file-input').setInputFiles(CUBE10);
  await page.getByTestId('parts-list').locator('li').nth(3).waitFor({ timeout: 10_000 });
  await page.getByTestId('arrange').click();
  await waitForIdle(page, 800);
  await snap(page, '03-arranged-parts');

  console.log('4. sliced — toolpath ghost visible');
  await page.getByTestId('layer-height').fill('1');
  await page.getByTestId('perimeters').fill('1');
  await page.getByTestId('slice').click();
  await page.getByTestId('slice-summary').waitFor();
  await waitForIdle(page, 800);
  await snap(page, '04-sliced-toolpath');

  console.log('5. simulation running partway');
  await page.getByTestId('start-sim').click();
  // Let animation play for a moment, then pause via scrubber at ~35%
  await page.waitForTimeout(1500);
  const cursorText = await page.getByTestId('sim-cursor').textContent();
  const [, total] = cursorText.split('/').map((s) => parseInt(s.trim(), 10));
  const mid = Math.floor(total * 0.35);
  await page.getByTestId('sim-slider').fill(String(mid));
  await waitForIdle(page, 500);
  await snap(page, '05-simulation-mid');

  console.log('6. simulation jumped to end');
  await page.getByTestId('finish-sim').click();
  await waitForIdle(page, 800);
  await snap(page, '06-simulation-end');

  console.log('7. bed-size changed');
  await resetBackend();
  await page.goto('http://127.0.0.1:5173/');
  await page.getByTestId('sidebar').waitFor();
  await page.getByTestId('bed-x').fill('120');
  await page.getByTestId('bed-y').fill('120');
  await page.getByTestId('bed-z').fill('120');
  await page.getByTestId('apply-bed').click();
  await waitForIdle(page, 600);
  await page.getByTestId('file-input').setInputFiles(CUBE20);
  await page.getByTestId('parts-list').locator('li').first().waitFor({ timeout: 10_000 });
  await waitForIdle(page, 600);
  await snap(page, '07-small-bed');

  console.log('8. single-cube full print (close-up)');
  await resetBackend();
  await page.goto('http://127.0.0.1:5173/');
  await page.getByTestId('sidebar').waitFor();
  await page.getByTestId('file-input').setInputFiles(CUBE10);
  await page.getByTestId('parts-list').locator('li').first().waitFor({ timeout: 10_000 });
  await page.getByTestId('layer-height').fill('0.4');
  await page.getByTestId('slice').click();
  await page.getByTestId('slice-summary').waitFor();
  await page.getByTestId('finish-sim').click();
  await waitForIdle(page, 800);
  // Zoom into the part for a useful view of printed filament lines.
  await page.evaluate(() => {
    const s = window.__printerScene;
    if (!s) return;
    s._orbit.radius = 80;
    s._orbit.target.set(15, 5, 15);
    s._orbit.polar = Math.PI / 2.4;
    s._orbit.azimuth = Math.PI / 4;
    s._applyOrbit();
  });
  await waitForIdle(page, 400);
  await snap(page, '08-finished-print');

  console.log('9. sliced toolpath (close-up)');
  await resetBackend();
  await page.goto('http://127.0.0.1:5173/');
  await page.getByTestId('sidebar').waitFor();
  await page.getByTestId('file-input').setInputFiles(CUBE20);
  await page.getByTestId('parts-list').locator('li').first().waitFor({ timeout: 10_000 });
  await page.getByTestId('layer-height').fill('1');
  await page.getByTestId('slice').click();
  await page.getByTestId('slice-summary').waitFor();
  await page.getByTestId('start-sim').click();
  await page.waitForTimeout(1200);
  const cursor2 = await page.getByTestId('sim-cursor').textContent();
  const [, total2] = cursor2.split('/').map((s) => parseInt(s.trim(), 10));
  await page.getByTestId('sim-slider').fill(String(Math.floor(total2 * 0.55)));
  await page.evaluate(() => {
    const s = window.__printerScene;
    if (!s) return;
    s._orbit.radius = 120;
    s._orbit.target.set(15, 10, 15);
    s._orbit.polar = Math.PI / 2.3;
    s._orbit.azimuth = Math.PI / 3.5;
    s._applyOrbit();
  });
  await waitForIdle(page, 400);
  await snap(page, '09-toolpath-closeup');

  console.log('16. overhang T-shape with supports');
  await resetBackend();
  await page.goto('http://127.0.0.1:5173/');
  await page.getByTestId('sidebar').waitFor();
  await page.getByTestId('file-input').setInputFiles(TSHAPE);
  await page.getByTestId('parts-list').locator('li').first().waitFor({ timeout: 10_000 });
  await page.getByTestId('layer-height').fill('1');
  await page.getByTestId('infill-density').fill('10');
  await page.getByTestId('support-density').fill('30');
  await page.getByTestId('slice').click();
  await page.getByTestId('slice-summary').waitFor();
  await page.getByTestId('finish-sim').click();
  await waitForIdle(page, 800);
  // Focus the camera on the part via the built-in fitter, then tighten the
  // radius for a close-in depth-legible shot.
  await page.getByTestId('focus-view').click();
  await waitForIdle(page, 300);
  await page.evaluate(() => {
    const s = window.__printerScene;
    if (!s) return;
    s._orbit.radius *= 0.55;
    s._orbit.polar = Math.PI / 2.6;
    s._orbit.azimuth = Math.PI / 3.5;
    s._applyOrbit();
  });
  await waitForIdle(page, 400);
  await snap(page, '16-tshape-supports');

  console.log('17. depth-colored toolpath (cube close-up, mid-print)');
  await resetBackend();
  await page.goto('http://127.0.0.1:5173/');
  await page.getByTestId('sidebar').waitFor();
  await page.getByTestId('file-input').setInputFiles(CUBE20);
  await page.getByTestId('parts-list').locator('li').first().waitFor({ timeout: 10_000 });
  await page.getByTestId('layer-height').fill('1');
  await page.getByTestId('slice').click();
  await page.getByTestId('slice-summary').waitFor();
  await page.getByTestId('start-sim').click();
  await page.waitForTimeout(1200);
  const cursor3 = await page.getByTestId('sim-cursor').textContent();
  const [, total3] = cursor3.split('/').map((s) => parseInt(s.trim(), 10));
  await page.getByTestId('sim-slider').fill(String(Math.floor(total3 * 0.65)));
  await page.getByTestId('focus-view').click();
  await waitForIdle(page, 300);
  await page.evaluate(() => {
    const s = window.__printerScene;
    if (!s) return;
    s._orbit.radius *= 0.6;
    s._orbit.polar = Math.PI / 2.5;
    s._orbit.azimuth = Math.PI / 4.5;
    s._applyOrbit();
  });
  await waitForIdle(page, 400);
  await snap(page, '17-depth-colored-toolpath');

  console.log('18. legend visible on slice');
  await page.getByTestId('focus-view').click();
  await waitForIdle(page, 300);
  await snap(page, '18-legend-overview');

  await browser.close();
  console.log('done.');
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
