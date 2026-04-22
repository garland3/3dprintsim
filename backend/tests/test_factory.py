"""Tests for the factory-as-a-service layer.

Exercises the feature flag gate, deterministic FIFO routing across a grid of
printers, time-based simulation (prints don't finish instantly), the robot's
pick-and-place handoff, cancellation, filament/cost bookkeeping, and the
MCP-friendly HTTP surface.
"""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from app.factory import (
    FactoryConfig,
    FactoryService,
    MIN_PRINT_DURATION_S,
    extrusion_to_mass_g,
    is_factory_enabled,
)
from app.main import create_app
from app.state import reset_service

from .fixtures import make_binary_cube_stl


# --- helpers ---


class Clock:
    """Controllable monotonic clock for deterministic timing tests."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _params() -> dict:
    # Keep the slice lightweight so the tests run quickly.
    return {"layer_height": 0.8, "perimeters": 1, "infill_density": 0.1}


# --- feature flag ---


def test_is_factory_enabled_off_by_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("FACTORY_ENABLED", raising=False)
    assert is_factory_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "True", "yes", "on"])
def test_is_factory_enabled_on(monkeypatch: pytest.MonkeyPatch, val: str):
    monkeypatch.setenv("FACTORY_ENABLED", val)
    assert is_factory_enabled() is True


def test_is_factory_enabled_off_values(monkeypatch: pytest.MonkeyPatch):
    for v in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("FACTORY_ENABLED", v)
        assert is_factory_enabled() is False


# --- direct FactoryService tests ---


def test_default_grid_is_3x3(monkeypatch):
    # Clear any deployment-level overrides so the built-in defaults stand.
    monkeypatch.delenv("FACTORY_ROWS", raising=False)
    monkeypatch.delenv("FACTORY_COLS", raising=False)
    fac = FactoryService()
    assert len(fac.printers) == 9
    # Grid order: top-left (0,0) is first, bottom-right (2,2) is last.
    assert fac.printers[0].id == "p00"
    assert fac.printers[-1].id == "p22"
    # Shelf coords advance by shelf_pitch_mm in x across a row and y across rows.
    assert fac.printers[0].grid_x == 0.0
    assert fac.printers[1].grid_x == fac.config.shelf_pitch_mm


def test_env_overrides_default_grid(monkeypatch):
    monkeypatch.setenv("FACTORY_ROWS", "2")
    monkeypatch.setenv("FACTORY_COLS", "4")
    fac = FactoryService()
    assert fac.config.rows == 2
    assert fac.config.cols == 4
    assert len(fac.printers) == 8


def test_env_grid_dim_clamps_and_falls_back(monkeypatch):
    # Out-of-range: clamped to 1..10.
    monkeypatch.setenv("FACTORY_ROWS", "999")
    monkeypatch.setenv("FACTORY_COLS", "0")
    fac = FactoryService()
    assert fac.config.rows == 10
    assert fac.config.cols == 1
    # Garbage: falls back to the built-in default.
    monkeypatch.setenv("FACTORY_ROWS", "three")
    monkeypatch.delenv("FACTORY_COLS", raising=False)
    fac2 = FactoryService()
    assert fac2.config.rows == 3  # DEFAULT_GRID_ROWS


def test_submit_job_assigns_to_first_idle_printer():
    clock = Clock()
    fac = FactoryService(clock=clock)
    stl = make_binary_cube_stl(size=10.0)
    job = fac.submit_job("cube", stl, slice_params=_params())
    assert job.status == "printing"
    assert job.printer_id == "p00"
    assert job.duration_s >= MIN_PRINT_DURATION_S
    assert job.total_extrusion_mm > 0
    assert job.filament_g > 0


def test_jobs_queue_when_all_printers_busy():
    clock = Clock()
    # 1x1 grid so a second job has nowhere to go.
    fac = FactoryService(FactoryConfig(rows=1, cols=1), clock=clock)
    stl = make_binary_cube_stl(size=10.0)
    j1 = fac.submit_job("a", stl, slice_params=_params())
    j2 = fac.submit_job("b", stl, slice_params=_params())
    assert j1.status == "printing"
    assert j2.status == "queued"
    assert fac.queue == [j2.id]


def test_print_takes_real_time():
    """A print of non-trivial extrusion must not finish instantly."""
    clock = Clock()
    fac = FactoryService(clock=clock)
    stl = make_binary_cube_stl(size=10.0)
    job = fac.submit_job("cube", stl, slice_params=_params())
    # Immediately after submission we're still printing — no instant finish.
    assert job.status == "printing"
    fac.tick()
    assert fac.get_job(job.id).status == "printing"
    # Advance past the duration: print finishes, robot starts unloading.
    clock.advance(job.duration_s + 0.01)
    fac.tick()
    printer = fac._printer_by_id(job.printer_id)
    assert printer is not None
    assert printer.status == "unloading"
    # After the robot's unload window, printer is idle and job is unloaded.
    clock.advance(fac.config.unload_duration_s + 0.01)
    fac.tick()
    assert fac.get_job(job.id).status == "unloaded"
    assert printer.status == "idle"


def test_queue_drains_onto_freed_printer():
    clock = Clock()
    fac = FactoryService(FactoryConfig(rows=1, cols=1), clock=clock)
    stl = make_binary_cube_stl(size=10.0)
    j1 = fac.submit_job("a", stl, slice_params=_params())
    j2 = fac.submit_job("b", stl, slice_params=_params())
    assert j2.status == "queued"
    # Finish the first print + robot unload cycle.
    clock.advance(j1.duration_s + fac.config.unload_duration_s + 0.02)
    fac.tick()
    # j2 should now be on the printer.
    assert fac.get_job(j2.id).status == "printing"


def test_cancel_queued_job():
    clock = Clock()
    fac = FactoryService(FactoryConfig(rows=1, cols=1), clock=clock)
    stl = make_binary_cube_stl(size=10.0)
    fac.submit_job("a", stl, slice_params=_params())
    j2 = fac.submit_job("b", stl, slice_params=_params())
    cancelled = fac.cancel_job(j2.id)
    assert cancelled.status == "cancelled"
    assert j2.id not in fac.queue


def test_cancel_active_job_frees_printer():
    clock = Clock()
    fac = FactoryService(FactoryConfig(rows=1, cols=1), clock=clock)
    stl = make_binary_cube_stl(size=10.0)
    j1 = fac.submit_job("a", stl, slice_params=_params())
    assert j1.status == "printing"
    fac.cancel_job(j1.id)
    printer = fac._printer_by_id(j1.printer_id)
    assert printer.status == "idle"
    assert printer.current_job_id is None


def test_filament_and_cost_tracking():
    """Lifetime filament + cost totals accumulate across prints."""
    clock = Clock()
    fac = FactoryService(FactoryConfig(rows=1, cols=1), clock=clock)
    stl = make_binary_cube_stl(size=10.0)
    j1 = fac.submit_job("a", stl, slice_params=_params())
    # Fast-forward past print + unload.
    clock.advance(j1.duration_s + fac.config.unload_duration_s + 0.1)
    fac.tick()
    p = fac.printers[0]
    assert p.lifetime_prints == 1
    assert p.lifetime_extrusion_mm == pytest.approx(j1.total_extrusion_mm)
    assert p.lifetime_filament_g == pytest.approx(j1.filament_g)
    assert p.lifetime_cost_usd == pytest.approx(j1.total_cost_usd())

    # Second print on the same printer stacks totals.
    j2 = fac.submit_job("b", stl, slice_params=_params())
    clock.advance(j2.duration_s + fac.config.unload_duration_s + 0.1)
    fac.tick()
    assert p.lifetime_prints == 2
    assert p.lifetime_extrusion_mm == pytest.approx(
        j1.total_extrusion_mm + j2.total_extrusion_mm
    )


def test_extrusion_to_mass_g_matches_pla_density():
    """1000 mm of 1.75mm PLA should weigh roughly 3g."""
    g = extrusion_to_mass_g(1000.0)
    assert 2.5 < g < 3.5


def test_stats_roll_up_across_grid():
    clock = Clock()
    fac = FactoryService(clock=clock)
    stl = make_binary_cube_stl(size=10.0)
    a = fac.submit_job("a", stl, slice_params=_params())
    b = fac.submit_job("b", stl, slice_params=_params())
    # Two printers should be busy, seven idle.
    assert a.status == "printing" and b.status == "printing"
    assert a.printer_id != b.printer_id
    stats = fac.stats()
    assert stats["printing_jobs"] == 2
    assert stats["queued_jobs"] == 0


def test_configure_rebuilds_grid():
    fac = FactoryService()
    fac.configure(rows=2, cols=4)
    assert len(fac.printers) == 8


def test_configure_rejects_unknown_key():
    fac = FactoryService()
    with pytest.raises(ValueError):
        fac.configure(rows=2, not_a_real_key=5)


def test_reset_clears_everything():
    clock = Clock()
    fac = FactoryService(clock=clock)
    stl = make_binary_cube_stl(size=10.0)
    fac.submit_job("a", stl, slice_params=_params())
    assert len(fac.jobs) == 1
    fac.reset()
    assert len(fac.jobs) == 0
    assert all(p.status == "idle" for p in fac.printers)


# --- HTTP surface ---


@pytest.fixture
def factory_client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FACTORY_ENABLED", "1")
    # Isolate tests from whatever the repo `.env` happens to set for the
    # default grid — they assert against the built-in 3x3 defaults.
    monkeypatch.delenv("FACTORY_ROWS", raising=False)
    monkeypatch.delenv("FACTORY_COLS", raising=False)
    reset_service()
    app = create_app()
    with TestClient(app) as c:
        yield c


@pytest.fixture
def disabled_factory_client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("FACTORY_ENABLED", raising=False)
    reset_service()
    app = create_app()
    with TestClient(app) as c:
        yield c


def test_factory_status_when_disabled(disabled_factory_client: TestClient):
    r = disabled_factory_client.get("/api/factory/status")
    assert r.status_code == 200
    assert r.json() == {"enabled": False}


def test_factory_state_404_when_disabled(disabled_factory_client: TestClient):
    r = disabled_factory_client.get("/api/factory/state")
    assert r.status_code == 404


def test_factory_state_ok_when_enabled(factory_client: TestClient):
    r = factory_client.get("/api/factory/state")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert len(body["printers"]) == 9
    assert body["config"]["rows"] == 3
    assert body["stats"]["queued_jobs"] == 0


def test_http_submit_and_list_jobs(factory_client: TestClient):
    stl = make_binary_cube_stl(size=10.0)
    r = factory_client.post(
        "/api/factory/jobs/upload",
        files={"file": ("cube.stl", stl, "model/stl")},
        data={"layer_height": "0.8", "perimeters": "1", "infill_density": "0.1"},
    )
    assert r.status_code == 200, r.text
    job = r.json()
    assert job["status"] in ("printing", "queued")
    assert job["duration_s"] >= MIN_PRINT_DURATION_S
    # Listing reflects the submission.
    listed = factory_client.get("/api/factory/jobs").json()
    assert len(listed) == 1
    assert listed[0]["id"] == job["id"]


def test_http_submit_base64(factory_client: TestClient):
    stl = make_binary_cube_stl(size=10.0)
    r = factory_client.post(
        "/api/factory/jobs",
        json={
            "name": "cube.stl",
            "stl_base64": base64.b64encode(stl).decode("ascii"),
            "layer_height": 0.8,
            "perimeters": 1,
            "infill_density": 0.1,
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] in ("printing", "queued")


def test_http_cancel_job(factory_client: TestClient):
    stl = make_binary_cube_stl(size=10.0)
    job = factory_client.post(
        "/api/factory/jobs/upload",
        files={"file": ("cube.stl", stl, "model/stl")},
        data={"layer_height": "0.8", "perimeters": "1", "infill_density": "0.1"},
    ).json()
    r = factory_client.post(f"/api/factory/jobs/{job['id']}/cancel")
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"


def test_http_config_endpoint_resizes_grid(factory_client: TestClient):
    r = factory_client.post("/api/factory/config", json={"rows": 2, "cols": 2})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["config"]["rows"] == 2
    assert len(body["printers"]) == 4
