"""FastAPI application exposing the virtual printer to HTTP clients.

The same PrinterService is also exposed via fastmcp at /mcp, so an AI agent
and the React UI share a single virtual printer.
"""

from __future__ import annotations

import contextlib
from dataclasses import asdict

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from .arrange import ArrangeError
from .mcp_server import build_mcp
from .state import get_service, reset_service


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


class SimulationStartRequest(BaseModel):
    speed: float = Field(1.0, gt=0)


class SimulationStepRequest(BaseModel):
    steps: int = Field(1, ge=1)


class SimulationCursorRequest(BaseModel):
    cursor: int = Field(0, ge=0)


class PartScaleRequest(BaseModel):
    scale: float = Field(gt=0)


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
        description="Virtual FDM printer simulator with MCP support.",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.mount("/mcp", mcp_app)

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "service": "3dprintsim"}

    @app.get("/api/state")
    def state() -> dict:
        return get_service().get_state()

    @app.post("/api/reset")
    def reset() -> dict:
        reset_service()
        return get_service().get_state()

    @app.post("/api/bed")
    def set_bed(body: BedSize) -> dict:
        return get_service().set_bed_size(body.x, body.y, body.z)

    @app.post("/api/parts/upload")
    async def upload(
        file: UploadFile = File(...),
        scale: float = Form(1.0),
    ) -> dict:
        """Upload an STL.

        Pass `scale` as a form field to convert non-mm units at import time —
        e.g. `scale=25.4` for an STL authored in inches.
        """
        data = await file.read()
        try:
            part = get_service().add_part_from_bytes(
                file.filename or "part.stl", data, scale=scale
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return part.to_public()

    @app.post("/api/parts/{part_id}/scale")
    def set_part_scale(part_id: str, body: PartScaleRequest) -> dict:
        try:
            part = get_service().set_part_scale(part_id, body.scale)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return part.to_public()

    @app.get("/api/parts")
    def list_parts() -> list[dict]:
        return [p.to_public() for p in get_service().parts.values()]

    @app.get("/api/parts/{part_id}/geometry")
    def part_geometry(part_id: str) -> dict:
        try:
            return get_service().get_part_geometry(part_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.delete("/api/parts/{part_id}")
    def delete_part(part_id: str) -> dict:
        try:
            get_service().remove_part(part_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/api/parts/clear")
    def clear_parts() -> dict:
        get_service().clear_parts()
        return {"ok": True}

    @app.post("/api/arrange")
    def arrange_parts() -> dict:
        try:
            placements = get_service().auto_arrange()
        except ArrangeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "placements": [
                {"part_id": p.part_id, "x": p.x, "y": p.y, "rotation_deg": p.rotation_deg}
                for p in placements
            ]
        }

    @app.post("/api/slice")
    def slice_all(req: SliceRequest) -> dict:
        try:
            result = get_service().slice_all(
                layer_height=req.layer_height,
                perimeters=req.perimeters,
                infill_density=req.infill_density,
                top_layers=req.top_layers,
                bottom_layers=req.bottom_layers,
                nozzle_width=req.nozzle_width,
            )
        except ArrangeError as exc:
            # slice_all() implicitly auto-arranges unplaced parts; surface bed
            # fit failures as 409 so the UI can distinguish them from validation.
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return result.summary()

    @app.get("/api/slice")
    def get_slice() -> dict:
        return get_service().get_slice_payload()

    @app.get("/api/gcode", response_class=PlainTextResponse)
    def get_gcode() -> str:
        svc = get_service()
        if svc.slice_result is None:
            raise HTTPException(status_code=400, detail="slice first")
        return svc.slice_result.gcode

    @app.post("/api/simulation/start")
    def start_sim(req: SimulationStartRequest) -> dict:
        try:
            sim = get_service().start_simulation(speed=req.speed)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return asdict(sim)

    @app.post("/api/simulation/step")
    def step_sim(req: SimulationStepRequest) -> dict:
        try:
            sim = get_service().step_simulation(steps=req.steps)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return asdict(sim)

    @app.post("/api/simulation/cursor")
    def cursor_sim(req: SimulationCursorRequest) -> dict:
        try:
            sim = get_service().set_simulation_cursor(req.cursor)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return asdict(sim)

    @app.get("/api/simulation/frame")
    def sim_frame() -> dict:
        return get_service().get_simulation_frame()

    return app


app = create_app()
