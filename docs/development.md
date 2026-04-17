# Development

## Layout

```
backend/   FastAPI app, slicer, STL loader, MCP server, unit tests
frontend/  Vite + React + Three.js UI
tests/     Playwright E2E (UI + HTTP API + MCP client)
docs/      This folder — markdown + generated screenshots + capture script
```

## Running locally

Dependencies are managed with [uv](https://docs.astral.sh/uv/). Install it
once (`curl -LsSf https://astral.sh/uv/install.sh | sh`) and the rest is
driven by `uv sync` / `uv run`.

```bash
# Backend
cd backend
uv sync                                     # creates .venv from uv.lock
uv run uvicorn app.main:app --reload --port 8000

# Frontend (separate terminal)
cd frontend
npm install
npm run dev  # http://localhost:5173
```

With both running, open `http://localhost:5173`. The
[`getting-started`](./getting-started.md) guide walks through the UI.

## Tests

### Python unit tests

```bash
cd backend
uv run pytest
```

Covered: STL parsing (ASCII + binary), arrange packing, slicer cross-section
math, and the full pipeline end-to-end against `PrinterService` in-process.

### Playwright E2E

```bash
cd tests
npm install
npx playwright install chromium   # first time only
npx playwright test
```

The Playwright config at
[`tests/playwright.config.js`](../tests/playwright.config.js) auto-starts the
backend and frontend (with `reuseExistingServer` outside CI). Tests live in
`tests/e2e/`:

- `human.spec.js` — drives the UI, asserts scene state.
- `api.spec.js` — hits the FastAPI routes directly.
- `mcp.spec.js` — spawns a Python agent that uses `fastmcp`'s client.

## Regenerating screenshots

The images under `docs/screenshots/` are captured by
[`_capture_screenshots.mjs`](./_capture_screenshots.mjs). It drives the real
UI through Playwright so the documentation is always "true".

```bash
# 1. Install Playwright browsers once (in tests/)
cd tests && npm install && npx playwright install chromium

# 2. Start the services
(cd backend && uv run uvicorn app.main:app --port 8000) &
(cd frontend && npm run dev) &

# 3. Run the capture script — requires docs/ to see @playwright/test.
# Easiest: run node from tests/ and point at the script.
ln -s ../tests/node_modules docs/node_modules
node docs/_capture_screenshots.mjs
rm docs/node_modules
```

The script resets the backend between scenes, uploads the STL fixtures in
`tests/fixtures/`, and saves PNGs into `docs/screenshots/`.

## Docker

The repo ships a RHEL 9 (UBI 9) Dockerfile that builds both services into a
single image. See [`docker.md`](./docker.md) for full instructions.

```bash
docker build -t 3dprintsim .
docker run --rm -p 8000:8000 -p 5173:5173 3dprintsim
```

## Code style

- Python: formatted with default `black` rules; type hints preferred but not
  enforced.
- JS: two-space indent, single quotes, no trailing semicolons inside JSX
  props. Follow the existing files.
- Commits should describe *why*, not just *what*.
