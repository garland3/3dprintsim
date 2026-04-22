"""Factory-as-a-service layer: a grid of virtual printers driven by a job queue.

A `FactoryService` owns N `FactoryPrinter`s arranged on a virtual grid of
shelves, plus a `Robot` that picks finished parts off a bed and deposits them
onto a shelf. Jobs submitted via `submit_job()` enter a FIFO queue and get
routed to the first idle printer (scanning the grid top-left to bottom-right).

Unlike the underlying `PrinterService` (where simulation is "advance the move
cursor"), prints here take *real wall-clock time*: each job has a `duration_s`
derived from the slice's extrusion length, and the factory's `tick()` compares
the elapsed time against that duration to decide when to flip a printer from
PRINTING → FINISHED → UNLOADING → IDLE. Callers (HTTP handlers, MCP tools,
tests) invoke `tick()` every time they read state, so there's no background
thread — the factory makes progress lazily, driven by observation.

Gated behind the `FACTORY_ENABLED` feature flag (see `is_factory_enabled()`).
When the flag is off, HTTP endpoints return 404 and MCP tools raise — the
underlying single-printer PrinterService is unaffected.
"""

from __future__ import annotations

import base64
import math
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

from .slicer import slice_meshes
from .state import PrinterService


# Filament geometry: a 1mm length of 1.75mm-diameter PLA filament is ~2.405 mm³.
# At PLA's density of ~1.24 g/cm³ that's ~2.98 mg per mm extruded.
_FILAMENT_DIAMETER_MM = 1.75
_FILAMENT_CROSS_SECTION_MM2 = math.pi * (_FILAMENT_DIAMETER_MM / 2.0) ** 2
_PLA_DENSITY_G_PER_MM3 = 0.00124

# Default material + machine pricing. Loosely calibrated to hobbyist PLA on a
# Prusa-class printer — not authoritative, but in the right order of magnitude.
DEFAULT_FILAMENT_PRICE_PER_KG_USD = 25.0
DEFAULT_MACHINE_COST_PER_HOUR_USD = 0.15

# Sim-time calibration. Total extrusion × seconds_per_mm_extruded ≈ print time.
# A 15mm cube at 20% infill produces ~500mm of extrusion, so 0.03 s/mm puts its
# print at ~15s — fast enough to watch the grid fill up, slow enough that you
# can see the progress bars move.
DEFAULT_SECONDS_PER_MM_EXTRUDED = 0.03
MIN_PRINT_DURATION_S = 5.0

# How long the robot takes to pick a finished part off the bed and deposit it
# on its shelf. Intentionally short so the UI animation is visible without
# stalling the queue.
DEFAULT_UNLOAD_DURATION_S = 3.0

# Grid layout defaults — 3x3 was the spec, leaving the cell pitch at a round
# number so the shelf coordinates (grid_x, grid_y) render as sensible mm-space
# positions in the 3D viewer. Rows/cols can be overridden per-deployment via
# `FACTORY_ROWS` / `FACTORY_COLS` env vars (clamped to 1..10 to match the
# live-config limits).
DEFAULT_GRID_ROWS = 3
DEFAULT_GRID_COLS = 3
DEFAULT_SHELF_PITCH_MM = 400.0


def _env_grid_dim(var: str, fallback: int) -> int:
    raw = os.getenv(var, "").strip()
    if not raw:
        return fallback
    try:
        n = int(raw)
    except ValueError:
        return fallback
    return max(1, min(10, n))


# Job + printer status strings live in one place so UI + MCP clients get a
# closed vocabulary and can render badges from a known list.
JOB_STATUSES = ("queued", "printing", "finished", "unloaded", "cancelled", "failed")
PRINTER_STATUSES = ("idle", "printing", "finished", "unloading", "offline")


def is_factory_enabled() -> bool:
    """Return whether the factory feature flag is on.

    Resolved per-call so tests can flip `FACTORY_ENABLED` via monkeypatch
    without having to reload this module. Default is off (the single-printer
    flow keeps its pre-factory behavior until an operator opts in).
    """
    raw = os.getenv("FACTORY_ENABLED", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def extrusion_to_mass_g(extrusion_mm: float) -> float:
    """Convert filament length extruded (mm) to filament mass (grams)."""
    return extrusion_mm * _FILAMENT_CROSS_SECTION_MM2 * _PLA_DENSITY_G_PER_MM3


@dataclass
class Job:
    """A print job: STL + slice params + bookkeeping for its trip through the queue."""

    id: str
    name: str
    stl_bytes: bytes
    # Slice params captured at submit time so re-running a job later (or
    # auditing via get_job) shows exactly what was printed.
    slice_params: dict
    status: str = "queued"
    printer_id: str | None = None
    # Timestamps are monotonic seconds so they're safe for duration math even
    # if the wall clock jumps. UI surfaces them as "elapsed" / "remaining".
    submitted_at: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None
    unloaded_at: float | None = None
    # Populated once the job has sliced successfully.
    duration_s: float = 0.0
    total_extrusion_mm: float = 0.0
    filament_g: float = 0.0
    filament_cost_usd: float = 0.0
    machine_cost_usd: float = 0.0
    error: str | None = None
    # Full slice result (layers + moves). Retained on the job so the factory
    # 3D view can stream toolpath geometry per printer without re-slicing on
    # every poll. `None` until the job has been sliced successfully.
    slice_result: object = None

    def total_cost_usd(self) -> float:
        return self.filament_cost_usd + self.machine_cost_usd

    def progress(self, now: float) -> float:
        """Fraction in [0, 1] of the print that's elapsed as of `now`."""
        if self.started_at is None or self.duration_s <= 0:
            return 0.0 if self.status == "queued" else 1.0
        raw = (now - self.started_at) / self.duration_s
        return max(0.0, min(1.0, raw))

    def to_public(self, now: float) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "printer_id": self.printer_id,
            "submitted_at": self.submitted_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "unloaded_at": self.unloaded_at,
            "duration_s": self.duration_s,
            "progress": self.progress(now),
            "total_extrusion_mm": self.total_extrusion_mm,
            "filament_g": self.filament_g,
            "filament_cost_usd": self.filament_cost_usd,
            "machine_cost_usd": self.machine_cost_usd,
            "total_cost_usd": self.total_cost_usd(),
            "error": self.error,
        }


@dataclass
class FactoryPrinter:
    """One printer in the grid. Wraps a `PrinterService` for slice/state reuse
    plus factory-level bookkeeping (shelf position, lifetime totals, status)."""

    id: str
    name: str
    row: int
    col: int
    # Virtual "shelf" coordinates in mm — not tied to the printer's own bed
    # space, but to the factory floor layout. Used by the UI to place printer
    # tiles on a floor plan and by the robot to compute travel targets.
    grid_x: float
    grid_y: float
    grid_z: float
    service: PrinterService = field(default_factory=PrinterService)
    status: str = "idle"
    current_job_id: str | None = None
    # Lifetime counters, never reset on job completion — these are the numbers
    # an operator cares about when budgeting for the next spool of filament.
    lifetime_prints: int = 0
    lifetime_extrusion_mm: float = 0.0
    lifetime_filament_g: float = 0.0
    lifetime_print_time_s: float = 0.0
    lifetime_cost_usd: float = 0.0

    def to_public(self) -> dict:
        bed = self.service.bed_size
        return {
            "id": self.id,
            "name": self.name,
            "row": self.row,
            "col": self.col,
            "grid_x": self.grid_x,
            "grid_y": self.grid_y,
            "grid_z": self.grid_z,
            "bed_size": list(bed),
            "status": self.status,
            "current_job_id": self.current_job_id,
            "lifetime_prints": self.lifetime_prints,
            "lifetime_extrusion_mm": self.lifetime_extrusion_mm,
            "lifetime_filament_g": self.lifetime_filament_g,
            "lifetime_print_time_s": self.lifetime_print_time_s,
            "lifetime_cost_usd": self.lifetime_cost_usd,
        }


@dataclass
class Robot:
    """A single shared pick-and-place arm.

    At most one printer is being unloaded at a time. When several printers
    finish in quick succession they park in `finished` status until the robot
    gets to them. `target_printer_id` + `target_started_at` + `unload_duration_s`
    let the UI animate the arm's travel without needing a separate state feed.
    """

    status: str = "idle"  # "idle" | "unloading"
    target_printer_id: str | None = None
    target_job_id: str | None = None
    target_started_at: float | None = None
    unload_duration_s: float = DEFAULT_UNLOAD_DURATION_S

    def progress(self, now: float) -> float:
        if self.target_started_at is None or self.unload_duration_s <= 0:
            return 0.0
        raw = (now - self.target_started_at) / self.unload_duration_s
        return max(0.0, min(1.0, raw))

    def to_public(self, now: float) -> dict:
        return {
            "status": self.status,
            "target_printer_id": self.target_printer_id,
            "target_job_id": self.target_job_id,
            "target_started_at": self.target_started_at,
            "unload_duration_s": self.unload_duration_s,
            "progress": self.progress(now),
        }


@dataclass
class FactoryConfig:
    """Tunable parameters for the factory.

    All live-editable so an operator or AI agent can tweak the sim speed, cost
    model, or grid dimensions without restarting. Grid resize rebuilds the
    printer list (see `FactoryService.configure`), so any in-flight jobs are
    requeued.
    """

    rows: int = field(default_factory=lambda: _env_grid_dim("FACTORY_ROWS", DEFAULT_GRID_ROWS))
    cols: int = field(default_factory=lambda: _env_grid_dim("FACTORY_COLS", DEFAULT_GRID_COLS))
    shelf_pitch_mm: float = DEFAULT_SHELF_PITCH_MM
    seconds_per_mm_extruded: float = DEFAULT_SECONDS_PER_MM_EXTRUDED
    unload_duration_s: float = DEFAULT_UNLOAD_DURATION_S
    filament_price_per_kg_usd: float = DEFAULT_FILAMENT_PRICE_PER_KG_USD
    machine_cost_per_hour_usd: float = DEFAULT_MACHINE_COST_PER_HOUR_USD
    sim_speed: float = 1.0  # multiplier on print duration; 2.0 = twice as fast


class FactoryService:
    """Per-session grid of printers + a job queue driven by `tick()`.

    Thread-safe via a coarse RLock (same pattern as `PrinterService`). All
    public methods call `_tick_locked()` before acting so queries never see
    stale printer status — this is what lets the factory run without a
    background thread.
    """

    def __init__(
        self,
        config: FactoryConfig | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._lock = threading.RLock()
        self._clock = clock
        self.config = config or FactoryConfig()
        self.printers: list[FactoryPrinter] = []
        self.jobs: dict[str, Job] = {}
        self.queue: list[str] = []  # job ids in FIFO submit order
        self.robot = Robot(unload_duration_s=self.config.unload_duration_s)
        # Monotonic revision counter the UI polls to notice factory-side
        # changes (new jobs, status transitions) and refresh without a full
        # page reload. Mirrors PrinterService.state_revision.
        self.state_revision: int = 0
        self._build_grid()

    # --- grid setup ---

    def _build_grid(self) -> None:
        """(Re)build the printer list from `self.config`. Caller holds the lock."""
        self.printers = []
        pitch = self.config.shelf_pitch_mm
        for r in range(self.config.rows):
            for c in range(self.config.cols):
                pid = f"p{r}{c}"
                self.printers.append(
                    FactoryPrinter(
                        id=pid,
                        name=f"Printer {r+1}-{c+1}",
                        row=r,
                        col=c,
                        grid_x=c * pitch,
                        grid_y=r * pitch,
                        grid_z=0.0,
                    )
                )

    def configure(self, **kwargs) -> FactoryConfig:
        """Update factory config. Rebuilds the grid if rows/cols/pitch changed.

        Any in-flight jobs on rebuilt printers are requeued so the operator
        can re-dispatch them without losing them entirely — but they lose
        their progress, which is acceptable for a config-reload-level action.
        """
        with self._lock:
            prev_shape = (self.config.rows, self.config.cols, self.config.shelf_pitch_mm)
            for k, v in kwargs.items():
                if not hasattr(self.config, k):
                    raise ValueError(f"unknown factory config key: {k!r}")
                if v is None:
                    continue
                setattr(self.config, k, v)
            if (self.config.rows, self.config.cols) != prev_shape[:2] or (
                self.config.shelf_pitch_mm != prev_shape[2]
            ):
                # Requeue any printing jobs — they'll lose progress but stay
                # in the queue so nothing is silently dropped.
                for job_id in list(self.jobs):
                    job = self.jobs[job_id]
                    if job.status in ("printing", "finished", "unloaded"):
                        continue
                if any(p.status != "idle" for p in self.printers):
                    for p in self.printers:
                        if p.current_job_id:
                            job = self.jobs.get(p.current_job_id)
                            if job and job.status in ("printing", "finished"):
                                job.status = "queued"
                                job.printer_id = None
                                job.started_at = None
                                job.finished_at = None
                                if job.id not in self.queue:
                                    self.queue.insert(0, job.id)
                    self.robot = Robot(unload_duration_s=self.config.unload_duration_s)
                self._build_grid()
            else:
                self.robot.unload_duration_s = self.config.unload_duration_s
            self._bump_revision()
            return self.config

    # --- job submission ---

    def submit_job(
        self,
        name: str,
        stl_bytes: bytes,
        *,
        scale: float = 1.0,
        slice_params: dict | None = None,
    ) -> Job:
        """Queue a print job. Returns the created `Job`.

        The actual slice runs on a disposable `PrinterService` so the factory
        doesn't hold onto triangle geometry for every queued job — we only
        keep the slice *summary* (extrusion length) needed for timing and
        cost, and the raw STL bytes for the run-time printer assignment.
        """
        if not stl_bytes:
            raise ValueError("stl_bytes is empty")
        sp = dict(slice_params or {})
        job_id = uuid.uuid4().hex[:10]
        with self._lock:
            now = self._clock()
            job = Job(
                id=job_id,
                name=name or f"job-{job_id}",
                stl_bytes=stl_bytes,
                slice_params=sp,
                submitted_at=now,
            )
            # Pre-slice on a throwaway service to estimate duration + cost
            # up front. An estimate-at-submit model means the queue view can
            # show "estimated finish" without needing a printer assignment.
            try:
                self._estimate_job_locked(job, scale=scale)
            except Exception as exc:
                job.status = "failed"
                job.error = f"pre-slice failed: {exc}"
                self.jobs[job_id] = job
                self._bump_revision()
                return job
            self.jobs[job_id] = job
            self.queue.append(job_id)
            self._tick_locked(now)
            return job

    def submit_job_base64(
        self,
        name: str,
        stl_base64: str,
        *,
        scale: float = 1.0,
        slice_params: dict | None = None,
    ) -> Job:
        try:
            data = base64.b64decode(stl_base64, validate=False)
        except Exception as exc:
            raise ValueError(f"invalid base64 STL: {exc}") from exc
        return self.submit_job(name, data, scale=scale, slice_params=slice_params)

    def _estimate_job_locked(self, job: Job, scale: float = 1.0) -> None:
        """Slice once to get extrusion length + time + cost. Caller holds the lock.

        The slice runs on a fresh PrinterService so repeated submits don't
        pollute the factory's own state. We keep the slice result around on
        the job so `assign` can hand it straight to the printer service
        without re-slicing.
        """
        svc = PrinterService()
        svc.add_part_from_bytes(job.name, job.stl_bytes, scale=scale)
        result = svc.slice_all(**_filtered_slice_params(job.slice_params))
        job.slice_result = result
        job.total_extrusion_mm = result.moves[-1].e if result.moves else 0.0
        job.filament_g = extrusion_to_mass_g(job.total_extrusion_mm)
        sec_per_mm = max(1e-6, self.config.seconds_per_mm_extruded)
        raw_duration = job.total_extrusion_mm * sec_per_mm
        speed = max(0.01, self.config.sim_speed)
        job.duration_s = max(MIN_PRINT_DURATION_S, raw_duration / speed)
        job.filament_cost_usd = (
            job.filament_g / 1000.0 * self.config.filament_price_per_kg_usd
        )
        job.machine_cost_usd = (
            job.duration_s / 3600.0 * self.config.machine_cost_per_hour_usd
        )

    # --- job management ---

    def cancel_job(self, job_id: str) -> Job:
        with self._lock:
            now = self._clock()
            self._tick_locked(now)
            job = self.jobs.get(job_id)
            if job is None:
                raise KeyError(f"unknown job {job_id}")
            if job.status in ("unloaded", "cancelled", "failed"):
                return job
            if job.status == "queued":
                if job_id in self.queue:
                    self.queue.remove(job_id)
                job.status = "cancelled"
            elif job.status in ("printing", "finished"):
                # Kick the printer back to idle so the queue can keep moving.
                printer = self._printer_for_job(job_id)
                if printer is not None:
                    printer.status = "idle"
                    printer.current_job_id = None
                job.status = "cancelled"
                if self.robot.target_job_id == job_id:
                    self.robot.status = "idle"
                    self.robot.target_printer_id = None
                    self.robot.target_job_id = None
                    self.robot.target_started_at = None
            self._bump_revision()
            return job

    def get_job(self, job_id: str) -> Job:
        with self._lock:
            self._tick_locked(self._clock())
            job = self.jobs.get(job_id)
            if job is None:
                raise KeyError(f"unknown job {job_id}")
            return job

    def list_jobs(self, status: str | None = None) -> list[Job]:
        with self._lock:
            self._tick_locked(self._clock())
            jobs = list(self.jobs.values())
            if status is not None:
                jobs = [j for j in jobs if j.status == status]
            return jobs

    # --- tick / routing ---

    def tick(self) -> None:
        """Advance factory state to the current clock. Safe to call any time."""
        with self._lock:
            self._tick_locked(self._clock())

    def _tick_locked(self, now: float) -> None:
        """Core state machine. Caller holds the lock.

        Advances each "frontier" transition once per pass:
          1. Finish any prints whose duration has elapsed.
          2. Complete an in-flight robot unload if its window has elapsed.
          3. If the robot is idle and a printer is FINISHED, send the robot.
             The robot's start time snaps to the print's finish time (not
             `now`) so a long tick interval can collapse finish → unload
             cleanly in one call.
          4. Route queued jobs onto idle printers. A freshly unloaded
             printer's idle_since is the robot's finish time, so newly
             assigned jobs inherit that as their started_at — otherwise
             `now` wins. Either way a single tick can cascade
             finish → unload → assign → finish again for fast prints.

        Iterates until no transitions fire, so a single `tick()` call with a
        long elapsed time can replay several queue cycles at once.
        """
        for _ in range(256):  # hard cap to avoid pathological loops
            progressed = self._tick_once_locked(now)
            if not progressed:
                break

    def _tick_once_locked(self, now: float) -> bool:
        """Run one pass of the state machine; return True if anything changed."""
        progressed = False

        for printer in self.printers:
            if printer.status != "printing":
                continue
            job = self.jobs.get(printer.current_job_id or "")
            if job is None or job.started_at is None:
                printer.status = "idle"
                printer.current_job_id = None
                progressed = True
                continue
            if now - job.started_at >= job.duration_s:
                job.status = "finished"
                job.finished_at = job.started_at + job.duration_s
                printer.status = "finished"
                # Lifetime tallies land on the owning printer exactly once, at
                # print-complete. Cancellation path in cancel_job handles no-op.
                printer.lifetime_prints += 1
                printer.lifetime_extrusion_mm += job.total_extrusion_mm
                printer.lifetime_filament_g += job.filament_g
                printer.lifetime_print_time_s += job.duration_s
                printer.lifetime_cost_usd += job.total_cost_usd()
                progressed = True

        if self.robot.status == "unloading" and self.robot.target_started_at is not None:
            if now - self.robot.target_started_at >= self.robot.unload_duration_s:
                pid = self.robot.target_printer_id
                jid = self.robot.target_job_id
                printer = self._printer_by_id(pid) if pid else None
                job = self.jobs.get(jid or "")
                if printer is not None:
                    printer.status = "idle"
                    printer.current_job_id = None
                if job is not None:
                    job.status = "unloaded"
                    job.unloaded_at = (
                        self.robot.target_started_at + self.robot.unload_duration_s
                    )
                self.robot.status = "idle"
                self.robot.target_printer_id = None
                self.robot.target_job_id = None
                self.robot.target_started_at = None
                progressed = True

        if self.robot.status == "idle":
            # Dispatch to the printer that finished earliest so a backlog of
            # simultaneously-finished printers drains in fair order.
            candidates = [
                p for p in self.printers
                if p.status == "finished" and p.current_job_id
            ]
            if candidates:
                def finished_at(p: FactoryPrinter) -> float:
                    j = self.jobs.get(p.current_job_id or "")
                    return j.finished_at if j and j.finished_at is not None else now

                printer = min(candidates, key=finished_at)
                start_at = finished_at(printer)
                self.robot.status = "unloading"
                self.robot.target_printer_id = printer.id
                self.robot.target_job_id = printer.current_job_id
                self.robot.target_started_at = start_at
                self.robot.unload_duration_s = self.config.unload_duration_s
                printer.status = "unloading"
                progressed = True

        while self.queue:
            idle = self._first_idle_printer()
            if idle is None:
                break
            job_id = self.queue[0]
            job = self.jobs.get(job_id)
            if job is None or job.status != "queued":
                # Job was cancelled or mutated out from under us; drop it.
                self.queue.pop(0)
                progressed = True
                continue
            self.queue.pop(0)
            # Anchor the new print's start to the later of "now" and the
            # moment the printer became idle. For a backlog being drained,
            # that's the robot's most recent finish time — lets a single
            # tick cascade through "print, unload, print, unload, ..." and
            # still surface timestamps that match when each event actually
            # would have happened in real time.
            idle_since = self._printer_idle_since(idle, now)
            self._assign_locked(idle, job, max(now, idle_since))
            progressed = True

        if progressed:
            self._bump_revision()
        return progressed

    def _printer_idle_since(self, printer: FactoryPrinter, now: float) -> float:
        """Best estimate of when `printer` became idle (for chained ticks)."""
        # If the robot just deposited a part from this printer, use its
        # finish time; otherwise the printer has been idle since `now` or
        # earlier and we can safely start the next job immediately.
        if (
            self.robot.status == "idle"
            and self.robot.target_printer_id is None
            and self.jobs
        ):
            # Inspect the most recent unloaded job for this printer.
            best = 0.0
            for job in self.jobs.values():
                if (
                    job.printer_id == printer.id
                    and job.unloaded_at is not None
                    and job.unloaded_at > best
                ):
                    best = job.unloaded_at
            if best > 0:
                return best
        return now

    def _assign_locked(self, printer: FactoryPrinter, job: Job, now: float) -> None:
        """Load `job` onto `printer` and start the clock. Caller holds the lock.

        We also replay the slice onto the printer's own PrinterService so the
        per-printer state (parts list, simulation cursor) looks right if the
        operator peeks into that printer via the legacy single-printer API.
        """
        try:
            printer.service.clear_parts()
            printer.service.add_part_from_bytes(
                job.name,
                job.stl_bytes,
                scale=job.slice_params.get("scale", 1.0),
            )
            printer.service.slice_all(**_filtered_slice_params(job.slice_params))
            printer.service.start_simulation(speed=1.0)
        except Exception as exc:
            job.status = "failed"
            job.error = f"assign failed: {exc}"
            self._bump_revision()
            return
        job.status = "printing"
        job.printer_id = printer.id
        job.started_at = now
        printer.status = "printing"
        printer.current_job_id = job.id
        self._bump_revision()

    def _first_idle_printer(self) -> FactoryPrinter | None:
        """Return the grid-order-first idle printer, or None if all are busy."""
        for p in self.printers:
            if p.status == "idle":
                return p
        return None

    def _printer_by_id(self, pid: str) -> FactoryPrinter | None:
        for p in self.printers:
            if p.id == pid:
                return p
        return None

    def _printer_for_job(self, job_id: str) -> FactoryPrinter | None:
        for p in self.printers:
            if p.current_job_id == job_id:
                return p
        return None

    # --- slice streaming (for the factory 3D view) ---

    def printer_slice_payload(self, printer_id: str) -> dict:
        """Toolpath payload for the printer's currently-assigned job.

        Mirrors the shape of the single-printer `/api/slice` response so
        PrinterRig.setToolpath() can consume either. Returns `{ready: False}`
        when the printer is idle or its job hasn't been sliced yet.
        """
        from dataclasses import asdict  # local import: only used here

        with self._lock:
            self._tick_locked(self._clock())
            printer = next((p for p in self.printers if p.id == printer_id), None)
            if printer is None or not printer.current_job_id:
                return {"ready": False}
            job = self.jobs.get(printer.current_job_id)
            if job is None or job.slice_result is None:
                return {"ready": False}
            result = job.slice_result
            return {
                "ready": True,
                "job_id": job.id,
                "summary": result.summary(),
                "moves": [asdict(m) for m in result.moves],
            }

    # --- introspection ---

    def stats(self) -> dict:
        """Aggregate lifetime totals across the whole grid."""
        with self._lock:
            self._tick_locked(self._clock())
            totals = {
                "prints": 0,
                "extrusion_mm": 0.0,
                "filament_g": 0.0,
                "print_time_s": 0.0,
                "cost_usd": 0.0,
            }
            for p in self.printers:
                totals["prints"] += p.lifetime_prints
                totals["extrusion_mm"] += p.lifetime_extrusion_mm
                totals["filament_g"] += p.lifetime_filament_g
                totals["print_time_s"] += p.lifetime_print_time_s
                totals["cost_usd"] += p.lifetime_cost_usd
            queued = sum(1 for j in self.jobs.values() if j.status == "queued")
            printing = sum(1 for j in self.jobs.values() if j.status == "printing")
            finished = sum(
                1 for j in self.jobs.values() if j.status in ("finished", "unloaded")
            )
            totals["queued_jobs"] = queued
            totals["printing_jobs"] = printing
            totals["finished_jobs"] = finished
            return totals

    def get_state(self) -> dict:
        with self._lock:
            now = self._clock()
            self._tick_locked(now)
            return {
                "enabled": True,
                "config": {
                    "rows": self.config.rows,
                    "cols": self.config.cols,
                    "shelf_pitch_mm": self.config.shelf_pitch_mm,
                    "seconds_per_mm_extruded": self.config.seconds_per_mm_extruded,
                    "unload_duration_s": self.config.unload_duration_s,
                    "filament_price_per_kg_usd": self.config.filament_price_per_kg_usd,
                    "machine_cost_per_hour_usd": self.config.machine_cost_per_hour_usd,
                    "sim_speed": self.config.sim_speed,
                },
                "printers": [p.to_public() for p in self.printers],
                "robot": self.robot.to_public(now),
                "queue": list(self.queue),
                "jobs": [j.to_public(now) for j in self.jobs.values()],
                "stats": self.stats(),
                "state_revision": self.state_revision,
                "now": now,
            }

    def reset(self) -> None:
        """Wipe every printer, job, and robot state back to a fresh factory."""
        with self._lock:
            self.jobs.clear()
            self.queue.clear()
            self.robot = Robot(unload_duration_s=self.config.unload_duration_s)
            self._build_grid()
            self._bump_revision()

    # --- internal ---

    def _bump_revision(self) -> None:
        self.state_revision += 1


# Only a subset of slice kwargs make sense at job-submit time. Filtering up
# front keeps a stray key from blowing up the pre-slice estimate halfway
# through `submit_job`. The whitelist mirrors `PrinterService.slice_all`.
_ALLOWED_SLICE_KWARGS: set[str] = {
    "layer_height",
    "perimeters",
    "infill_density",
    "top_layers",
    "bottom_layers",
    "nozzle_width",
    "support_density",
    "retract_mm",
    "retract_speed",
    "hotend_temp",
    "bed_temp",
    "fan_speed",
    "first_layer_fan",
    "bridge_fan",
    "bridge_speed_factor",
    "seam_position",
    "first_layer_height",
    "first_layer_speed",
    "brim_loops",
    "adaptive_layers",
    "layer_height_min",
    "layer_height_max",
}


def _filtered_slice_params(params: dict) -> dict:
    return {k: v for k, v in params.items() if k in _ALLOWED_SLICE_KWARGS and v is not None}


__all__ = [
    "FactoryConfig",
    "FactoryPrinter",
    "FactoryService",
    "Job",
    "Robot",
    "is_factory_enabled",
    "extrusion_to_mass_g",
    "JOB_STATUSES",
    "PRINTER_STATUSES",
]
