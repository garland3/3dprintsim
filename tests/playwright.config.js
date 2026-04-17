import { defineConfig } from '@playwright/test';

const BACKEND = 'http://127.0.0.1:8000';
const FRONTEND = 'http://127.0.0.1:5173';

export default defineConfig({
  testDir: './e2e',
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL: FRONTEND,
    trace: 'retain-on-failure',
    viewport: { width: 1400, height: 900 },
  },
  webServer: [
    {
      command: 'uv run --frozen uvicorn app.main:app --host 127.0.0.1 --port 8000',
      cwd: '../backend',
      url: `${BACKEND}/api/health`,
      reuseExistingServer: !process.env.CI,
      timeout: 30_000,
      stdout: 'pipe',
      stderr: 'pipe',
    },
    {
      command: 'npm run dev',
      cwd: '../frontend',
      url: FRONTEND,
      reuseExistingServer: !process.env.CI,
      timeout: 30_000,
      stdout: 'pipe',
      stderr: 'pipe',
    },
  ],
});
