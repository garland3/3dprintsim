import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(__dirname, '..');

export default defineConfig(({ mode }) => {
  // Load the repo-root .env so one file drives both backend and frontend.
  // Passing '' as the prefix loads every var (not just VITE_*).
  const env = loadEnv(mode, REPO_ROOT, '');
  const frontendPort = Number(env.FRONTEND_PORT) || 5173;
  const backendPort = Number(env.BACKEND_PORT) || 8000;

  return {
    plugins: [react()],
    server: {
      host: '127.0.0.1',
      port: frontendPort,
      strictPort: true,
      proxy: {
        '/api': `http://127.0.0.1:${backendPort}`,
        '/mcp': `http://127.0.0.1:${backendPort}`,
      },
    },
    preview: {
      host: '127.0.0.1',
      port: frontendPort,
    },
  };
});
