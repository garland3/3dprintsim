"""FastAPI application exposing the virtual printer to HTTP clients.

The same per-session PrinterService is also exposed via fastmcp at /mcp, so an
AI agent and the React UI share one virtual printer — but only within the same
session. See state.py for the registry keying.

Session resolution precedence (first match wins):
  1. `X-Session-Id` HTTP header (what the React frontend sends after reading
     its query param)
  2. `session` query parameter (for the very first page load before JS wires
     the header middleware up)
  3. A sentinel "default" session (for tests / dev clients with no session)
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import asdict
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

from .arrange import ArrangeError
from .env import load_dotenv
from .factory import FactoryService, is_factory_enabled
from .mcp_server import build_mcp
from .state import (
    DEFAULT_SESSION_ID,
    PrinterService,
    get_factory,
    get_service,
    reset_service,
)

load_dotenv()


class BedSize(BaseModel):
    x: float = Field(gt=0)
    y: float = Field(gt=0)
    z: float = Field(gt=0)


class SliceRequest(BaseModel):
    layer_height: float = Field(0.4, gt=0)
    perimeters: int = Field(1, ge=1)
    infill_density: float = Field(0.2, ge=0.0, le=1.0)
    top_layers: int = Field(3, ge=0)
    bottom_layers: int = Field(3, ge=0)
    nozzle_width: float = Field(0.4, gt=0)
    support_density: float = Field(0.25, ge=0.0, le=1.0)
    # Optional G-code / toolpath params. None = use the slicer's default so
    # minimal callers (existing frontend, tests) don't need to set them.
    retract_mm: float | None = Field(default=None, ge=0.0)
    retract_speed: float | None = Field(default=None, gt=0.0)
    hotend_temp: float | None = Field(default=None, ge=0.0)
    bed_temp: float | None = Field(default=None, ge=0.0)
    fan_speed: int | None = Field(default=None, ge=0, le=255)
    first_layer_fan: int | None = Field(default=None, ge=0, le=255)
    bridge_fan: int | None = Field(default=None, ge=0, le=255)
    bridge_speed_factor: float | None = Field(default=None, gt=0.0, le=1.0)
    seam_position: Literal["auto", "aligned", "rear", "nearest"] | None = None
    first_layer_height: float | None = Field(default=None, gt=0.0)
    first_layer_speed: float | None = Field(default=None, gt=0.0)
    brim_loops: int | None = Field(default=None, ge=0)
    adaptive_layers: bool | None = None
    layer_height_min: float | None = Field(default=None, gt=0.0)
    layer_height_max: float | None = Field(default=None, gt=0.0)


class SimulationStartRequest(BaseModel):
    speed: float = Field(1.0, gt=0)


class SimulationStepRequest(BaseModel):
    steps: int = Field(1, ge=1)


class SimulationCursorRequest(BaseModel):
    cursor: int = Field(0, ge=0)


class PartScaleRequest(BaseModel):
    scale: float = Field(gt=0)


class PartRotateRequest(BaseModel):
    """Incremental per-part rotation around a world axis, or a reset to identity.

    `reset=True` clears the stored orientation and ignores axis/degrees, so
    the UI only needs one endpoint for both "rotate" and "unrotate".
    """

    axis: Literal["x", "y", "z"] = "z"
    degrees: float = 0.0
    reset: bool = False


class PartPositionRequest(BaseModel):
    """Manual placement of a part's min-corner on the bed in mm.

    Backend clamps to the bed, so callers can send raw drag deltas without
    having to clamp client-side.
    """

    x: float = Field(ge=0)
    y: float = Field(ge=0)


class FactorySubmitRequest(BaseModel):
    """Base64-encoded STL + slice params for a factory job submission.

    Separate from the multipart `/api/factory/jobs/upload` endpoint so an MCP
    agent or curl-based integration can submit a job without multipart
    encoding. The slice params mirror `SliceRequest` but are all optional —
    agents typically want "just print this" with sensible defaults.
    """

    name: str = ""
    stl_base64: str
    scale: float = Field(1.0, gt=0)
    layer_height: float | None = Field(default=None, gt=0)
    perimeters: int | None = Field(default=None, ge=1)
    infill_density: float | None = Field(default=None, ge=0.0, le=1.0)
    top_layers: int | None = Field(default=None, ge=0)
    bottom_layers: int | None = Field(default=None, ge=0)
    support_density: float | None = Field(default=None, ge=0.0, le=1.0)
    count: int = Field(default=1, ge=1, le=100)


class FactoryConfigRequest(BaseModel):
    """Live-edit knobs for the factory grid + sim + pricing model."""

    rows: int | None = Field(default=None, ge=1, le=10)
    cols: int | None = Field(default=None, ge=1, le=10)
    shelf_pitch_mm: float | None = Field(default=None, gt=0)
    seconds_per_mm_extruded: float | None = Field(default=None, gt=0)
    unload_duration_s: float | None = Field(default=None, gt=0)
    filament_price_per_kg_usd: float | None = Field(default=None, ge=0)
    machine_cost_per_hour_usd: float | None = Field(default=None, ge=0)
    sim_speed: float | None = Field(default=None, gt=0)


def _session_id(request: Request, session: str | None = None) -> str:
    """Resolve the caller's session id from header-then-query, defaulting to
    the shared "default" session when neither is provided.

    `session` is a plain default — FastAPI's `Query(...)` lives only on the
    route/dependency parameters that the framework injects (see
    `session_service` and the explicit endpoints below). Inlining `Query` here
    would mean a manual call like `_session_id(request)` returns a
    `fastapi.Query` instance instead of a string.
    """
    header = request.headers.get("x-session-id")
    if header:
        return header
    if session:
        return session
    return DEFAULT_SESSION_ID


def session_service(
    request: Request,
    session: str | None = Query(default=None),
) -> PrinterService:
    """FastAPI dependency — returns the PrinterService for the caller's session.

    A malformed session id (or one that would push us past the registry's
    max-session cap) surfaces as a 400 instead of bubbling up as a 500.
    """
    try:
        return get_service(_session_id(request, session))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def session_factory(
    request: Request,
    session: str | None = Query(default=None),
) -> FactoryService:
    """FastAPI dependency — returns the FactoryService for the caller's session.

    Gates the whole factory API behind `FACTORY_ENABLED`: when the flag is
    off, every factory endpoint returns 404 so this feature looks like it
    simply doesn't exist. That lets operators ship the code dark and flip
    the flag once they're ready.
    """
    if not is_factory_enabled():
        raise HTTPException(
            status_code=404,
            detail="factory feature flag (FACTORY_ENABLED) is off",
        )
    try:
        return get_factory(_session_id(request, session))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class IframeEmbedMiddleware(BaseHTTPMiddleware):
    """Allow the frontend to be rendered inside Atlas's canvas iframe.

    FastAPI doesn't set X-Frame-Options by default, but many reverse-proxies
    upstream of it do. We set `Content-Security-Policy: frame-ancestors ...`
    so Atlas (or any same-origin SPA) can embed us, and strip
    `X-Frame-Options` so its DENY default doesn't veto the CSP.

    Existing CSP directives set by upstream middleware/proxies are preserved
    — we only replace the `frame-ancestors` directive — so this middleware
    can't accidentally weaken a stricter policy set elsewhere.

    `FRAME_ANCESTORS` lets operators lock this down further without code
    changes; the default (`*`) is fine for a dev-focused simulator.
    """

    def __init__(self, app, frame_ancestors: str = "*") -> None:
        super().__init__(app)
        self.frame_ancestors = frame_ancestors

    def _merged_csp(self, existing_policy: str | None) -> str:
        """Splice (or replace) `frame-ancestors` into an existing CSP string.

        Drops any prior `frame-ancestors` directive so ours wins, but keeps
        every other directive (e.g. `default-src`, `script-src`) intact.
        """
        directive = f"frame-ancestors {self.frame_ancestors}"
        if not existing_policy:
            return directive
        kept: list[str] = []
        for part in existing_policy.split(";"):
            stripped = part.strip()
            if not stripped:
                continue
            if stripped.lower().startswith("frame-ancestors"):
                continue
            kept.append(stripped)
        kept.append(directive)
        return "; ".join(kept)

    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        response.headers["Content-Security-Policy"] = self._merged_csp(
            response.headers.get("Content-Security-Policy")
        )
        # Some WSGI frontends inject X-Frame-Options=DENY by default; strip it
        # so browsers fall back to CSP frame-ancestors instead.
        if "x-frame-options" in response.headers:
            del response.headers["x-frame-options"]
        return response


def _resolve_frontend_dist() -> Path | None:
    """Find the built Vite bundle, if any.

    Checked locations (first hit wins):
      1. `FRONTEND_DIST` env var (explicit override for deployments)
      2. `/app/frontend/dist` (where the podman image drops it)
      3. `<repo>/frontend/dist` (local `npm run build` output)

    Returns None when no bundle is present — backend runs fine without it,
    which is the normal case during local development (Vite serves the UI).
    """
    override = os.getenv("FRONTEND_DIST")
    if override:
        p = Path(override)
        return p if p.is_dir() else None

    container_path = Path("/app/frontend/dist")
    if container_path.is_dir():
        return container_path

    repo_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if repo_dist.is_dir():
        return repo_dist
    return None


def _mount_frontend(app: FastAPI) -> None:
    """Serve the built frontend from the same ASGI app when available.

    Mounted *after* `/api` and `/mcp` are registered so API routes always
    win. The SPA fallback re-serves `index.html` for unknown paths so
    client-side routing (should we ever add any) doesn't 404.
    """
    dist = _resolve_frontend_dist()
    if dist is None:
        return

    index_html = dist / "index.html"
    if not index_html.is_file():
        # Incomplete dist (partial copy, wrong FRONTEND_DIST, pre-build race).
        # Skip the mount loudly instead of 500-ing on every pageload later.
        import logging

        logging.getLogger(__name__).warning(
            "frontend dist %s has no index.html; skipping SPA mount", dist
        )
        return

    assets_dir = dist / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/", include_in_schema=False)
    def _index() -> FileResponse:
        return FileResponse(index_html)

    # Catch-all must stay last in route-registration order. Starlette matches
    # routes top-down, so `/api/*` handlers (registered earlier in create_app)
    # win over this fallback. `_mount_frontend` itself is called right before
    # `return app` to preserve that invariant.
    @app.get("/{path:path}", include_in_schema=False)
    def _spa_fallback(path: str) -> FileResponse:
        # Belt-and-braces guard — if a future refactor moves this registration
        # earlier, we still refuse to shadow the API surface.
        if path.startswith(("api/", "mcp/")) or path in {"api", "mcp"}:
            raise HTTPException(status_code=404)
        candidate = (dist / path).resolve()
        try:
            candidate.relative_to(dist.resolve())
        except ValueError:
            raise HTTPException(status_code=404) from None
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index_html)


def create_app() -> FastAPI:
    mcp = build_mcp()
    mcp_app = mcp.http_app(path="/")

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        async with mcp_app.lifespan(app):
            yield

    app = FastAPI(
        title="3dprintsim",
        version="0.1.0",
        description="Virtual FDM printer simulator with stateful per-session MCP support.",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        # `*` is intentionally permissive (dev-focused tool, no credentials).
        # The explicit `X-Session-Id` echo is for `expose_headers` below — it
        # has no effect on the request-side allowlist, which `*` already
        # covers, but documents that this header is part of the API contract.
        allow_headers=["*"],
        expose_headers=["X-Session-Id"],
    )
    app.add_middleware(
        IframeEmbedMiddleware,
        frame_ancestors=os.getenv("FRAME_ANCESTORS", "*"),
    )

    app.mount("/mcp", mcp_app)

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "service": "3dprintsim"}

    @app.get("/api/session")
    def session_info(request: Request, session: str | None = Query(default=None)) -> dict:
        """Echo the resolved session id so a fresh iframe can confirm wiring.

        Useful for debugging Atlas embeds: the frontend pings this on boot and
        surfaces any mismatch between the URL param and what the backend saw.
        """
        sid = _session_id(request, session)
        return {"session_id": sid, "is_default": sid == DEFAULT_SESSION_ID}

    @app.get("/api/state")
    def state(svc: PrinterService = Depends(session_service)) -> dict:
        return svc.get_state()

    @app.post("/api/reset")
    def reset(request: Request, session: str | None = Query(default=None)) -> dict:
        """Reset printer state.

        Called *with* an explicit `X-Session-Id` header or `?session=` query
        param: drops just that session's PrinterService.

        Called *without* either: wipes every session's state. The pre-multiuser
        contract was a global reset (used by the Playwright suite's
        `beforeEach` hook), and silently demoting that to "default session
        only" let stale agent state leak across runs. Operators who actually
        want a single-session reset can pass the id explicitly.
        """
        header = request.headers.get("x-session-id")
        explicit = header or session
        if explicit:
            reset_service(explicit)
            return get_service(explicit).get_state()
        reset_service()
        return get_service().get_state()

    @app.post("/api/bed")
    def set_bed(body: BedSize, svc: PrinterService = Depends(session_service)) -> dict:
        return svc.set_bed_size(body.x, body.y, body.z)

    @app.post("/api/parts/upload")
    async def upload(
        file: UploadFile = File(...),
        scale: float = Form(1.0),
        svc: PrinterService = Depends(session_service),
    ) -> dict:
        """Upload an STL.

        Pass `scale` as a form field to convert non-mm units at import time —
        e.g. `scale=25.4` for an STL authored in inches.
        """
        data = await file.read()
        try:
            part = svc.add_part_from_bytes(
                file.filename or "part.stl", data, scale=scale
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return part.to_public()

    @app.post("/api/parts/{part_id}/scale")
    def set_part_scale(
        part_id: str,
        body: PartScaleRequest,
        svc: PrinterService = Depends(session_service),
    ) -> dict:
        try:
            part = svc.set_part_scale(part_id, body.scale)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return part.to_public()

    @app.post("/api/parts/{part_id}/rotate")
    def rotate_part(
        part_id: str,
        body: PartRotateRequest,
        svc: PrinterService = Depends(session_service),
    ) -> dict:
        try:
            part = svc.rotate_part(
                part_id, body.axis, body.degrees, reset=body.reset
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return part.to_public()

    @app.post("/api/parts/{part_id}/position")
    def set_part_position(
        part_id: str,
        body: PartPositionRequest,
        svc: PrinterService = Depends(session_service),
    ) -> dict:
        try:
            part = svc.set_part_position(part_id, body.x, body.y)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return part.to_public()

    @app.get("/api/parts")
    def list_parts(svc: PrinterService = Depends(session_service)) -> list[dict]:
        return [p.to_public() for p in svc.parts.values()]

    @app.get("/api/parts/{part_id}/geometry")
    def part_geometry(
        part_id: str,
        svc: PrinterService = Depends(session_service),
    ) -> dict:
        try:
            return svc.get_part_geometry(part_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.delete("/api/parts/{part_id}")
    def delete_part(
        part_id: str,
        svc: PrinterService = Depends(session_service),
    ) -> dict:
        try:
            svc.remove_part(part_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/api/parts/clear")
    def clear_parts(svc: PrinterService = Depends(session_service)) -> dict:
        svc.clear_parts()
        return {"ok": True}

    @app.post("/api/arrange")
    def arrange_parts(svc: PrinterService = Depends(session_service)) -> dict:
        try:
            placements = svc.auto_arrange()
        except ArrangeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "placements": [
                {"part_id": p.part_id, "x": p.x, "y": p.y, "rotation_deg": p.rotation_deg}
                for p in placements
            ]
        }

    @app.post("/api/slice")
    def slice_all(
        req: SliceRequest,
        svc: PrinterService = Depends(session_service),
    ) -> dict:
        try:
            result = svc.slice_all(
                layer_height=req.layer_height,
                perimeters=req.perimeters,
                infill_density=req.infill_density,
                top_layers=req.top_layers,
                bottom_layers=req.bottom_layers,
                nozzle_width=req.nozzle_width,
                support_density=req.support_density,
                retract_mm=req.retract_mm,
                retract_speed=req.retract_speed,
                hotend_temp=req.hotend_temp,
                bed_temp=req.bed_temp,
                fan_speed=req.fan_speed,
                first_layer_fan=req.first_layer_fan,
                bridge_fan=req.bridge_fan,
                bridge_speed_factor=req.bridge_speed_factor,
                seam_position=req.seam_position,
                first_layer_height=req.first_layer_height,
                first_layer_speed=req.first_layer_speed,
                brim_loops=req.brim_loops,
                adaptive_layers=req.adaptive_layers,
                layer_height_min=req.layer_height_min,
                layer_height_max=req.layer_height_max,
            )
        except ArrangeError as exc:
            # slice_all() implicitly auto-arranges unplaced parts; surface bed
            # fit failures as 409 so the UI can distinguish them from validation.
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return result.summary()

    @app.get("/api/slice")
    def get_slice(svc: PrinterService = Depends(session_service)) -> dict:
        return svc.get_slice_payload()

    @app.get("/api/gcode", response_class=PlainTextResponse)
    def get_gcode(svc: PrinterService = Depends(session_service)) -> str:
        if svc.slice_result is None:
            raise HTTPException(status_code=400, detail="slice first")
        return svc.slice_result.gcode

    @app.post("/api/simulation/start")
    def start_sim(
        req: SimulationStartRequest,
        svc: PrinterService = Depends(session_service),
    ) -> dict:
        try:
            sim = svc.start_simulation(speed=req.speed)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return asdict(sim)

    @app.post("/api/simulation/step")
    def step_sim(
        req: SimulationStepRequest,
        svc: PrinterService = Depends(session_service),
    ) -> dict:
        try:
            sim = svc.step_simulation(steps=req.steps)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return asdict(sim)

    @app.post("/api/simulation/cursor")
    def cursor_sim(
        req: SimulationCursorRequest,
        svc: PrinterService = Depends(session_service),
    ) -> dict:
        try:
            sim = svc.set_simulation_cursor(req.cursor)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return asdict(sim)

    @app.get("/api/simulation/frame")
    def sim_frame(svc: PrinterService = Depends(session_service)) -> dict:
        return svc.get_simulation_frame()

    @app.get("/api/viewer/requests")
    def viewer_requests(svc: PrinterService = Depends(session_service)) -> dict:
        """Tiny polling endpoint: returns one-shot UI request counters."""
        return svc.get_viewer_requests()

    @app.post("/api/viewer/focus")
    def request_focus(svc: PrinterService = Depends(session_service)) -> dict:
        return {"focus_request": svc.request_focus()}

    # --- factory-as-a-service endpoints ---

    @app.get("/api/factory/status")
    def factory_status() -> dict:
        """Report whether the factory feature flag is currently on.

        Unconditionally available (no 404 when off) so the UI can query at
        boot and decide whether to render the factory tab. Every other
        `/api/factory/*` route is gated on `is_factory_enabled()`.
        """
        return {"enabled": is_factory_enabled()}

    @app.get("/api/factory/state")
    def factory_state(fac: FactoryService = Depends(session_factory)) -> dict:
        return fac.get_state()

    @app.post("/api/factory/tick")
    def factory_tick(fac: FactoryService = Depends(session_factory)) -> dict:
        """Force a state-machine tick and return the new state.

        Normally tick() runs on every read, but an explicit tick endpoint is
        handy for tests and for UIs that want to advance the simulation at a
        known cadence without polling GETs.
        """
        fac.tick()
        return fac.get_state()

    @app.post("/api/factory/reset")
    def factory_reset(fac: FactoryService = Depends(session_factory)) -> dict:
        fac.reset()
        return fac.get_state()

    @app.post("/api/factory/config")
    def factory_config(
        body: FactoryConfigRequest,
        fac: FactoryService = Depends(session_factory),
    ) -> dict:
        try:
            fac.configure(**body.model_dump(exclude_none=True))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return fac.get_state()

    @app.post("/api/factory/jobs/upload")
    async def factory_upload_job(
        file: UploadFile = File(...),
        scale: float = Form(1.0),
        layer_height: float | None = Form(None),
        perimeters: int | None = Form(None),
        infill_density: float | None = Form(None),
        top_layers: int | None = Form(None),
        bottom_layers: int | None = Form(None),
        support_density: float | None = Form(None),
        count: int = Form(1, ge=1, le=100),
        fac: FactoryService = Depends(session_factory),
    ) -> dict:
        """Submit a factory job from a multipart STL upload.

        This is the single "upload + slice + start" command the spec calls
        for: drop a file, it gets sliced, queued, and routed to the next
        available printer. `count` lets the caller enqueue N copies of the
        same STL in one upload — the user picks 10 once instead of dragging
        the file ten times.
        """
        data = await file.read()
        base_name = file.filename or "job.stl"
        params = {
            k: v
            for k, v in {
                "layer_height": layer_height,
                "perimeters": perimeters,
                "infill_density": infill_density,
                "top_layers": top_layers,
                "bottom_layers": bottom_layers,
                "support_density": support_density,
            }.items()
            if v is not None
        }
        jobs = []
        try:
            for i in range(count):
                name = base_name if count == 1 else f"{base_name} #{i + 1}/{count}"
                job = fac.submit_job(name, data, scale=scale, slice_params=params)
                jobs.append(job)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        now = fac._clock()
        first = jobs[0].to_public(now)
        if count > 1:
            first["copies"] = [j.to_public(now) for j in jobs]
        return first

    @app.post("/api/factory/jobs")
    def factory_submit_job(
        body: FactorySubmitRequest,
        fac: FactoryService = Depends(session_factory),
    ) -> dict:
        """Submit a factory job via base64-encoded STL (MCP-friendly path)."""
        params = {
            k: v
            for k, v in {
                "layer_height": body.layer_height,
                "perimeters": body.perimeters,
                "infill_density": body.infill_density,
                "top_layers": body.top_layers,
                "bottom_layers": body.bottom_layers,
                "support_density": body.support_density,
            }.items()
            if v is not None
        }
        base_name = body.name or "job.stl"
        jobs = []
        try:
            for i in range(body.count):
                name = base_name if body.count == 1 else f"{base_name} #{i + 1}/{body.count}"
                job = fac.submit_job_base64(
                    name,
                    body.stl_base64,
                    scale=body.scale,
                    slice_params=params,
                )
                jobs.append(job)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        now = fac._clock()
        first = jobs[0].to_public(now)
        if body.count > 1:
            first["copies"] = [j.to_public(now) for j in jobs]
        return first

    @app.get("/api/factory/jobs")
    def factory_list_jobs(
        status: str | None = Query(default=None),
        fac: FactoryService = Depends(session_factory),
    ) -> list[dict]:
        now = fac._clock()
        return [j.to_public(now) for j in fac.list_jobs(status=status)]

    @app.get("/api/factory/jobs/{job_id}")
    def factory_get_job(
        job_id: str,
        fac: FactoryService = Depends(session_factory),
    ) -> dict:
        try:
            job = fac.get_job(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return job.to_public(fac._clock())

    @app.post("/api/factory/jobs/{job_id}/cancel")
    def factory_cancel_job(
        job_id: str,
        fac: FactoryService = Depends(session_factory),
    ) -> dict:
        try:
            job = fac.cancel_job(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return job.to_public(fac._clock())

    @app.get("/api/factory/printers/{printer_id}/slice")
    def factory_printer_slice(
        printer_id: str,
        fac: FactoryService = Depends(session_factory),
    ) -> dict:
        """Toolpath for whatever job is currently on the given printer.

        Shape matches `/api/slice` so the same PrinterRig.setToolpath() call
        path can consume it. Returns `{ready: false}` for idle printers or
        while their job is still being sliced.
        """
        return fac.printer_slice_payload(printer_id)

    # Register SPA routes last so the `/{path:path}` catch-all can't preempt
    # any `/api/*` handler above. See `_mount_frontend` for detail.
    _mount_frontend(app)

    return app


app = create_app()
