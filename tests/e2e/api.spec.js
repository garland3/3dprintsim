// Backend API exercised directly (as a human cURL user or a web UI would).
import { test, expect } from '@playwright/test';
import { fileURLToPath } from 'url';
import fs from 'fs';
import path from 'path';

const here = path.dirname(fileURLToPath(import.meta.url));
const CUBE = fs.readFileSync(path.resolve(here, '../fixtures/cube20.stl'));

const BASE = 'http://127.0.0.1:8000';

test.beforeEach(async ({ request }) => {
  await request.post(`${BASE}/api/reset`);
});

test('health endpoint is up', async ({ request }) => {
  const r = await request.get(`${BASE}/api/health`);
  expect(r.ok()).toBeTruthy();
  const body = await r.json();
  expect(body.ok).toBe(true);
});

test('default state returns Prusa bed', async ({ request }) => {
  const r = await request.get(`${BASE}/api/state`);
  const body = await r.json();
  expect(body.bed_size).toEqual([250, 210, 210]);
  expect(body.parts).toEqual([]);
});

test('upload → arrange → slice → simulate round trip', async ({ request }) => {
  const upload = await request.post(`${BASE}/api/parts/upload`, {
    multipart: {
      file: { name: 'cube20.stl', mimeType: 'model/stl', buffer: CUBE },
    },
  });
  expect(upload.ok()).toBeTruthy();

  const arrange = await request.post(`${BASE}/api/arrange`);
  expect(arrange.ok()).toBeTruthy();

  const slice = await request.post(`${BASE}/api/slice`, {
    data: { layer_height: 1.0, perimeters: 1 },
  });
  expect(slice.ok()).toBeTruthy();
  const summary = await slice.json();
  expect(summary.layer_count).toBeGreaterThan(10);

  const start = await request.post(`${BASE}/api/simulation/start`, {
    data: { speed: 1.0 },
  });
  expect(start.ok()).toBeTruthy();

  const step = await request.post(`${BASE}/api/simulation/step`, {
    data: { steps: 10 },
  });
  const stepBody = await step.json();
  expect(stepBody.cursor).toBe(10);

  const frame = await request.get(`${BASE}/api/simulation/frame`);
  const fBody = await frame.json();
  expect(fBody.ready).toBe(true);
  expect(fBody.head).toBeTruthy();
});

test('gcode endpoint returns plausible G-code', async ({ request }) => {
  await request.post(`${BASE}/api/parts/upload`, {
    multipart: { file: { name: 'cube.stl', mimeType: 'model/stl', buffer: CUBE } },
  });
  await request.post(`${BASE}/api/slice`, { data: { layer_height: 1.0 } });

  const r = await request.get(`${BASE}/api/gcode`);
  const text = await r.text();
  expect(text).toContain('G21');
  expect(text).toContain('G1 ');
});

test('slicing without parts returns 400', async ({ request }) => {
  const r = await request.post(`${BASE}/api/slice`, { data: { layer_height: 0.4 } });
  expect(r.status()).toBe(400);
});

test('arrange fails when a part is larger than the bed', async ({ request }) => {
  await request.post(`${BASE}/api/bed`, { data: { x: 15, y: 15, z: 50 } });
  await request.post(`${BASE}/api/parts/upload`, {
    multipart: { file: { name: 'huge.stl', mimeType: 'model/stl', buffer: CUBE } },
  });
  const r = await request.post(`${BASE}/api/arrange`);
  expect(r.status()).toBe(409);
});
