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

import asyncio
import contextlib
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

from .arrange import ArrangeError
from .env import load_dotenv
from .mcp_server import build_mcp
from .state import (
    DEFAULT_SESSION_ID,
    PrinterService,
    get_broker,
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


def _sse_format(event: str, data: dict) -> str:
    """Serialize a single Server-Sent Event frame.

    EventSource parsers require each event terminate with a blank line. We
    put the JSON payload on a single `data:` line since our payloads are
    small and never contain newlines; the spec's multi-line rule only matters
    for multi-line strings.
    """
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


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

    @app.get("/api/events")
    async def events(
        request: Request,
        session: str | None = Query(default=None),
    ) -> StreamingResponse:
        """Server-Sent Events stream: pushes `state` and `focus` events the
        moment a mutation (HTTP or MCP) happens in this session.

        EventSource can't set custom headers, so the session id comes in via
        the `?session=` query param (same fallback the rest of /api uses).
        A malformed id → 400 so callers don't silently get the default
        session's stream.

        The stream emits:
          - `event: state` with a JSON payload carrying the new
            `state_revision`. The client re-fetches `/api/state`.
          - `event: focus` for camera-focus requests from `focus_viewer()`.
          - `event: hello` on subscribe, carrying the current revision and
            focus counter — lets the client seed its state and skip a
            pointless refresh if nothing has changed since page load.
          - `: ping` comments every 15s so intermediate proxies don't kill
            the connection as idle.
        """
        try:
            sid = _session_id(request, session)
            svc = get_service(sid)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        broker = get_broker()
        loop = asyncio.get_running_loop()
        sub = broker.subscribe(sid, loop)

        # Seed the stream with the current counters so the client doesn't
        # have to race a first /api/state against an early event.
        snapshot = svc.get_viewer_requests()

        async def generator():
            try:
                yield _sse_format(
                    "hello",
                    {
                        "session_id": sid,
                        "state_revision": snapshot["state_revision"],
                        "focus_request": snapshot["focus_request"],
                    },
                )
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        event = await asyncio.wait_for(
                            sub.queue.get(), timeout=15.0
                        )
                    except asyncio.TimeoutError:
                        # Keepalive comment — ignored by EventSource but keeps
                        # the TCP connection warm through proxies.
                        yield ": ping\n\n"
                        continue
                    kind = event.get("type", "message")
                    payload = {k: v for k, v in event.items() if k != "type"}
                    yield _sse_format(kind, payload)
            finally:
                broker.unsubscribe(sid, sub)

        return StreamingResponse(
            generator(),
            media_type="text/event-stream",
            # Disable buffering in upstream proxies (nginx respects this).
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Session-Id": sid,
            },
        )

    @app.get("/api/viewer/requests")
    def viewer_requests(svc: PrinterService = Depends(session_service)) -> dict:
        """Polling fallback for clients that can't hold an SSE connection
        open (some iframes, stricter corporate proxies). The SSE stream at
        `/api/events` is the preferred path — it surfaces mutations within
        milliseconds, where polling's ceiling is the client's tick rate."""
        return svc.get_viewer_requests()

    @app.post("/api/viewer/focus")
    def request_focus(svc: PrinterService = Depends(session_service)) -> dict:
        return {"focus_request": svc.request_focus()}

    # Register SPA routes last so the `/{path:path}` catch-all can't preempt
    # any `/api/*` handler above. See `_mount_frontend` for detail.
    _mount_frontend(app)

    return app


app = create_app()
