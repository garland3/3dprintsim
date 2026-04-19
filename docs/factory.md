# Factory-as-a-service

A grid of simulated 3D printers with a job queue and a pick-and-place robot,
layered on top of the existing single-printer flow. Lets you submit multiple
print jobs with a single command and watch them get routed to whichever
printer opens up first.

Gated behind the `FACTORY_ENABLED` feature flag — off by default, so the
single-printer surface stays unchanged until you opt in.

## Enabling

Set the flag in your `.env` (or via the shell):

```bash
FACTORY_ENABLED=1
```

Restart the backend. The factory endpoints (`/api/factory/*`) and MCP tools
(`factory_*`) become available; the UI sidebar grows a **Factory →** toggle
button.

When the flag is off, `/api/factory/status` still responds with
`{"enabled": false}` so a UI or agent can detect the feature cleanly without
relying on 404s.

## Architecture

- **`FactoryService`** (`backend/app/factory.py`) — per-session owner of the
  printer grid, job queue, and robot. Time-based state machine: each job has a
  `duration_s` derived from total extrusion length, and `tick()` uses the
  monotonic clock to advance printer/job/robot status. No background thread;
  the state machine runs lazily whenever `tick()` is called (HTTP reads call
  it automatically).
- **Grid layout** — rows × cols of `FactoryPrinter`s (default 3×3). Each
  printer has shelf coordinates (`grid_x`, `grid_y`, `grid_z`) in mm for UI
  layout. The UI renders the grid on a "shelf" background with the robot
  overlayed as an animated emoji.
- **Job queue** — FIFO. `submit_job()` pre-slices the STL once to estimate
  duration + filament + cost, then appends to the queue. `tick()` pops the
  head job onto the first idle printer (grid order: top-left to
  bottom-right).
- **Robot** — single pick-and-place actor. When a printer finishes, the
  robot walks over (3s default), "unloads" the part, and the printer returns
  to idle for the next job. Only one printer is unloaded at a time; a
  backlog parks in `finished` status until the robot gets to them.

### Time + cost model

A print's `duration_s` is computed at submit time as

    max(5s, total_extrusion_mm × seconds_per_mm_extruded / sim_speed)

where `seconds_per_mm_extruded` (default 0.03) and `sim_speed` (default 1.0)
are live-editable via `factory_configure()` or `POST /api/factory/config`.

Filament mass is computed from the 1.75mm PLA cross-section and a density of
1.24 g/cm³. Cost is `filament_g × filament_price_per_kg / 1000 + duration_s ×
machine_cost_per_hour / 3600` with defaults of $25/kg filament and $0.15/hr
machine time.

Lifetime totals accumulate per printer: number of prints, total extrusion,
total filament mass, total print time, and total cost. `factory_stats()` /
`/api/factory/state` surfaces the aggregate across the grid.

## HTTP endpoints

All gated on `FACTORY_ENABLED` except `/api/factory/status`.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/factory/status` | `{enabled: bool}` — always available |
| `GET` | `/api/factory/state` | Printers, queue, jobs, robot, stats |
| `POST` | `/api/factory/tick` | Force a state-machine tick |
| `POST` | `/api/factory/reset` | Wipe all printers, jobs, robot state |
| `POST` | `/api/factory/config` | Update grid/sim/pricing knobs |
| `POST` | `/api/factory/jobs/upload` | Multipart STL + slice params |
| `POST` | `/api/factory/jobs` | Base64 STL + slice params (JSON body) |
| `GET` | `/api/factory/jobs?status=` | List jobs (optional status filter) |
| `GET` | `/api/factory/jobs/{id}` | Single job details |
| `POST` | `/api/factory/jobs/{id}/cancel` | Cancel a queued or in-flight job |

## MCP tools

All tools raise if the flag is off with a clear error message.

- `factory_status()` — check the flag
- `factory_state()` — full state snapshot
- `factory_list_printers()` — printers + lifetime stats
- `factory_list_jobs(status?)` — all jobs, optionally filtered
- `factory_get_job(job_id)` — one job's status + progress + cost
- `factory_cancel_job(job_id)` — cancel + free the printer
- `factory_stats()` — aggregate totals across grid
- `factory_configure(rows?, cols?, sim_speed?, ...)` — live-edit config
- `factory_reset()` — wipe + rebuild
- **`factory_submit_job(name, stl_base64, ...)`** — the single-command entry
  point. One call uploads, slices, queues, and routes to the next free printer.
- `factory_atlas_submit_job(filename, ...)` — same, but downloads the STL
  from an Atlas file-vault URL (identical SSRF hardening to `atlas_upload`).

## Frontend

Toggle into factory mode via the **Factory →** button in the sidebar
(visible only when the flag is on). The factory view replaces the
single-printer layout with:

- **Header stats** — queued / printing / finished job counts, lifetime
  prints, total filament, total cost.
- **Printer grid** — one tile per printer, colored left border + LED by
  status, live progress bar, current job name, lifetime totals.
- **Robot overlay** — animated emoji that tweens to the active printer when
  unloading, with its own mini-progress bar.
- **Job queue panel** — most recent jobs on top, with status badges and
  cancel buttons.
- **Sidebar** — job submission dropzone, per-job slice params, grid
  configure (rows/cols/sim speed), reset.

## Deterministic routing

Routing is FIFO-onto-first-idle-in-grid-order. Cancellation frees the printer
immediately so the next queued job moves up. There's no reorder / priority /
per-printer affinity yet; the design keeps the placement logic simple so an
AI agent acting as "factory manager" can override via explicit submission
timing rather than having to defeat a built-in scheduler.
