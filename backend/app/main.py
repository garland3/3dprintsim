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

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

from .arrange import ArrangeError
from .mcp_server import build_mcp
from .state import DEFAULT_SESSION_ID, PrinterService, get_service, reset_service


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


class SimulationStartRequest(BaseModel):
    speed: float = Field(1.0, gt=0)


class SimulationStepRequest(BaseModel):
    steps: int = Field(1, ge=1)


class SimulationCursorRequest(BaseModel):
    cursor: int = Field(0, ge=0)


class PartScaleRequest(BaseModel):
    scale: float = Field(gt=0)


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

    return app


app = create_app()
