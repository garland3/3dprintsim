"""End-to-end backend tests covering STL → arrange → slice → simulate.

These run against the FastAPI app via TestClient, which also boots the MCP
Starlette sub-app (mounted at /mcp).
"""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.state import reset_service

from .fixtures import make_ascii_triangle_stl, make_binary_cube_stl


@pytest.fixture
def client():
    reset_service()
    app = create_app()
    with TestClient(app) as c:
        yield c


def upload_cube(client: TestClient, size: float = 10.0, name: str = "cube.stl") -> str:
    data = make_binary_cube_stl(size=size)
    resp = client.post(
        "/api/parts/upload",
        files={"file": (name, data, "model/stl")},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def test_health(client: TestClient):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_default_bed_size(client: TestClient):
    r = client.get("/api/state")
    assert r.json()["bed_size"] == [250.0, 210.0, 210.0]


def test_set_bed_size(client: TestClient):
    r = client.post("/api/bed", json={"x": 200, "y": 200, "z": 200})
    assert r.status_code == 200
    assert r.json()["bed_size"] == [200.0, 200.0, 200.0]


def test_upload_cube_reports_size(client: TestClient):
    pid = upload_cube(client, size=20.0)
    r = client.get("/api/parts").json()
    assert len(r) == 1
    assert r[0]["id"] == pid
    assert r[0]["size"] == [20.0, 20.0, 20.0]
    assert r[0]["triangle_count"] == 12


def test_ascii_stl_parses():
    from app.stl_loader import parse_stl

    mesh = parse_stl(make_ascii_triangle_stl())
    assert len(mesh.triangles) == 1


def test_arrange_places_parts_inside_bed(client: TestClient):
    for _ in range(4):
        upload_cube(client, size=30.0)
    r = client.post("/api/arrange")
    assert r.status_code == 200
    placements = r.json()["placements"]
    assert len(placements) == 4
    for p in placements:
        assert 0 <= p["x"] <= 250
        assert 0 <= p["y"] <= 210


def test_slice_generates_layers_and_moves(client: TestClient):
    upload_cube(client, size=10.0)
    r = client.post("/api/slice", json={"layer_height": 1.0, "perimeters": 1})
    assert r.status_code == 200
    summary = r.json()
    # 10mm cube at 1mm layers should give ~10 layers.
    assert 8 <= summary["layer_count"] <= 12
    assert summary["move_count"] > 0
    assert summary["total_extrusion"] > 0


def test_slice_contours_are_closed(client: TestClient):
    upload_cube(client, size=10.0)
    client.post("/api/slice", json={"layer_height": 2.0, "perimeters": 1})
    payload = client.get("/api/slice").json()
    assert payload["ready"]
    # each layer of a cube has exactly one closed square-shaped contour
    for layer in payload["layers"]:
        assert len(layer["contours"]) == 1
        contour = layer["contours"][0]
        # first and last point coincide (closed)
        assert contour[0] == contour[-1]
        # extent covers the full cube footprint (10mm)
        xs = [p[0] for p in contour]
        ys = [p[1] for p in contour]
        assert abs((max(xs) - min(xs)) - 10.0) < 0.5
        assert abs((max(ys) - min(ys)) - 10.0) < 0.5


def test_gcode_endpoint_returns_text(client: TestClient):
    upload_cube(client)
    client.post("/api/slice", json={"layer_height": 1.0, "perimeters": 1})
    r = client.get("/api/gcode")
    assert r.status_code == 200
    body = r.text
    assert "G21" in body
    assert "G1 " in body  # at least one extrude move


def test_simulation_advances_and_completes(client: TestClient):
    upload_cube(client)
    slice_summary = client.post("/api/slice", json={"layer_height": 1.0}).json()
    total = slice_summary["move_count"]

    r = client.post("/api/simulation/start", json={"speed": 1.0}).json()
    assert r["cursor"] == 0
    assert r["running"] is True

    r = client.post("/api/simulation/step", json={"steps": 5}).json()
    assert r["cursor"] == 5

    # jump to end
    r = client.post("/api/simulation/cursor", json={"cursor": total}).json()
    assert r["cursor"] == total
    assert r["running"] is False

    frame = client.get("/api/simulation/frame").json()
    assert frame["ready"]
    assert frame["cursor"] == total
    assert frame["head"] is not None


def test_slice_requires_parts(client: TestClient):
    r = client.post("/api/slice", json={"layer_height": 0.4})
    assert r.status_code == 400


def test_simulation_requires_slice(client: TestClient):
    r = client.post("/api/simulation/start", json={"speed": 1.0})
    assert r.status_code == 400


def test_upload_auto_centers_single_part(client: TestClient):
    """A freshly uploaded single part should be centered on the bed, not at (0,0)."""
    upload_cube(client, size=20.0)
    parts = client.get("/api/parts").json()
    assert len(parts) == 1
    pl = parts[0]["placement"]
    assert pl is not None, "upload should initialize a placement so the part is visible"
    # 250x210 bed, 20mm cube: centered min-corner is (115, 95).
    assert abs(pl["x"] - 115.0) < 0.5
    assert abs(pl["y"] - 95.0) < 0.5


def test_auto_arrange_centers_block(client: TestClient):
    """Auto-arrange should place the packed-block around the bed center."""
    for _ in range(2):
        upload_cube(client, size=20.0)
    placements = client.post("/api/arrange").json()["placements"]
    assert len(placements) == 2
    # Block spans ~45mm (20 + 5 gap + 20). On 250x210 bed centered that means
    # min.x ≈ (250 - 45)/2 = 102.5 and max.x ≈ 147.5.
    xs = sorted(p["x"] for p in placements)
    assert abs(xs[0] - 102.5) < 1.0
    assert abs((xs[1] + 20) - 147.5) < 1.0


def test_slice_with_infill_adds_moves(client: TestClient):
    """Adding infill must produce materially more extrude moves than perimeter-only."""
    upload_cube(client, size=20.0)
    bare = client.post(
        "/api/slice",
        json={"layer_height": 1.0, "perimeters": 1, "infill_density": 0.0, "top_layers": 0, "bottom_layers": 0},
    ).json()
    with_infill = client.post(
        "/api/slice",
        json={"layer_height": 1.0, "perimeters": 1, "infill_density": 0.2, "top_layers": 3, "bottom_layers": 3},
    ).json()
    assert with_infill["move_count"] > bare["move_count"] * 2
    assert with_infill["infill_density"] == 0.2
    assert with_infill["top_layers"] == 3
    assert with_infill["bottom_layers"] == 3


def test_slice_solid_top_bottom_more_than_sparse_middle(client: TestClient):
    """Top/bottom solid layers should each have more infill moves than a sparse layer."""
    upload_cube(client, size=10.0)
    client.post(
        "/api/slice",
        json={"layer_height": 1.0, "perimeters": 1, "infill_density": 0.1, "top_layers": 2, "bottom_layers": 2},
    )
    payload = client.get("/api/slice").json()
    moves = payload["moves"]
    by_z: dict[float, int] = {}
    for m in moves:
        if m["kind"] == "extrude":
            by_z[round(m["z"], 3)] = by_z.get(round(m["z"], 3), 0) + 1
    zs = sorted(by_z)
    solid_counts = [by_z[z] for z in zs[:2]] + [by_z[z] for z in zs[-2:]]
    middle_counts = [by_z[z] for z in zs[2:-2]]
    assert min(solid_counts) > max(middle_counts)


def test_slice_per_part_solid_layers_respect_per_part_height(client: TestClient):
    """Short parts should get solid top layers even when printed alongside taller parts.

    Slices a 4 mm cube and a 20 mm cube together with top_layers=2 / bottom_layers=2 at
    layer_height=1 mm. The short part only has 4 layers — all 4 must be solid. Without
    per-part accounting the short part's top window never triggered because the build's
    total height was 20 layers.
    """
    upload_cube(client, size=4.0, name="short.stl")
    upload_cube(client, size=20.0, name="tall.stl")
    client.post(
        "/api/slice",
        json={"layer_height": 1.0, "perimeters": 1, "infill_density": 0.1,
              "top_layers": 2, "bottom_layers": 2},
    )
    payload = client.get("/api/slice").json()
    parts = client.get("/api/parts").json()
    by_name = {p["name"]: p for p in parts}
    sx, sy = by_name["short.stl"]["placement"]["x"], by_name["short.stl"]["placement"]["y"]
    sw, sd = by_name["short.stl"]["size"][0], by_name["short.stl"]["size"][1]

    def in_short_footprint(m):
        return sx <= m["x"] <= sx + sw and sy <= m["y"] <= sy + sd

    short_moves_by_z: dict[float, int] = {}
    for m in payload["moves"]:
        if m["kind"] == "extrude" and in_short_footprint(m):
            z = round(m["z"], 3)
            short_moves_by_z[z] = short_moves_by_z.get(z, 0) + 1
    zs = sorted(short_moves_by_z)
    # Short cube is 4 mm tall at 1 mm layers → 4 layers; all of them are inside
    # the top OR bottom window and must therefore be solid.
    assert len(zs) == 4, f"expected 4 layers for short part, got {zs}"
    # Solid layers on a 4 mm square at 0.4 mm spacing produce ~10 infill lines
    # plus 4 perimeter points per layer → ≥10 extrude moves per layer. A sparse
    # 10% layer would have ~4 perimeter + ~1 infill = ~5, well below 10.
    for z in zs:
        assert short_moves_by_z[z] >= 10, (
            f"layer at z={z} has only {short_moves_by_z[z]} moves — looks sparse"
        )


def test_upload_oversize_part_stays_unplaced_and_slice_returns_409(client: TestClient):
    """Oversize single-part uploads must not bypass the bed-fit check.

    The UX contract is: upload always succeeds (we want the part in the parts
    list so the user can see it), but if it can't fit the current bed we leave
    placement=None and the subsequent slice fails with a clear 409 instead of
    silently producing out-of-bounds toolpaths.
    """
    # Shrink the bed so a 20 mm cube no longer fits the required 10 mm margin window.
    client.post("/api/bed", json={"x": 20, "y": 20, "z": 50})
    pid = upload_cube(client, size=20.0)
    parts = client.get("/api/parts").json()
    assert parts[0]["id"] == pid
    assert parts[0]["placement"] is None, "oversize part must not be auto-placed"

    r = client.post("/api/slice", json={"layer_height": 1.0})
    assert r.status_code == 409, r.text
    assert "does not fit" in r.json().get("detail", "")


def test_upload_with_scale_applies_factor(client: TestClient):
    """`scale` on the upload form should multiply every dimension at import."""
    data = make_binary_cube_stl(size=10.0)
    r = client.post(
        "/api/parts/upload",
        files={"file": ("cube.stl", data, "model/stl")},
        data={"scale": "25.4"},  # inch → mm
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scale"] == 25.4
    # 10mm × 25.4 = 254 mm per axis
    for dim in body["size"]:
        assert abs(dim - 254.0) < 1e-6


def test_upload_rejects_non_positive_scale(client: TestClient):
    data = make_binary_cube_stl(size=10.0)
    r = client.post(
        "/api/parts/upload",
        files={"file": ("cube.stl", data, "model/stl")},
        data={"scale": "0"},
    )
    assert r.status_code == 400


def test_set_part_scale_updates_size_and_reflows(client: TestClient):
    pid = upload_cube(client, size=10.0)
    r = client.post(f"/api/parts/{pid}/scale", json={"scale": 2.0})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scale"] == 2.0
    assert body["size"] == [20.0, 20.0, 20.0]
    # Single-part after scale must be re-centered on the default bed.
    pl = body["placement"]
    assert pl is not None
    assert abs(pl["x"] - (250 - 20) / 2) < 0.5
    assert abs(pl["y"] - (210 - 20) / 2) < 0.5


def test_set_part_scale_invalidates_slice(client: TestClient):
    pid = upload_cube(client, size=10.0)
    client.post("/api/slice", json={"layer_height": 1.0})
    assert client.get("/api/slice").json()["ready"]
    client.post(f"/api/parts/{pid}/scale", json={"scale": 1.5})
    assert client.get("/api/slice").json()["ready"] is False


def test_set_part_scale_oversize_leaves_unplaced(client: TestClient):
    """Scaling past the bed must drop the placement rather than overflow."""
    client.post("/api/bed", json={"x": 50, "y": 50, "z": 200})
    pid = upload_cube(client, size=10.0)
    body = client.post(f"/api/parts/{pid}/scale", json={"scale": 10.0}).json()
    assert body["placement"] is None


def test_set_part_scale_404_for_unknown(client: TestClient):
    r = client.post("/api/parts/doesnotexist/scale", json={"scale": 2.0})
    assert r.status_code == 404


def test_set_part_scale_rejects_non_positive(client: TestClient):
    pid = upload_cube(client, size=10.0)
    r = client.post(f"/api/parts/{pid}/scale", json={"scale": 0})
    assert r.status_code in (400, 422)


def test_remove_and_clear(client: TestClient):
    pid = upload_cube(client)
    client.delete(f"/api/parts/{pid}")
    assert client.get("/api/parts").json() == []

    upload_cube(client)
    upload_cube(client)
    client.post("/api/parts/clear")
    assert client.get("/api/parts").json() == []


def test_mcp_endpoint_mounted(client: TestClient):
    # The MCP server is mounted at /mcp; a plain GET should not 404.
    r = client.get("/mcp/")
    # It will return an MCP-level error or 406/400 for a non-MCP client,
    # but crucially NOT 404, which would mean the sub-app failed to mount.
    assert r.status_code != 404


def test_mcp_tools_exposed():
    """The MCP server must expose the full pipeline as tools."""
    import asyncio

    from app.mcp_server import build_mcp

    mcp = build_mcp()
    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    expected = {
        "get_printer_state",
        "set_bed_size",
        "upload_stl",
        "list_parts",
        "remove_part",
        "clear_parts",
        "auto_arrange",
        "slice_all",
        "get_gcode",
        "start_simulation",
        "step_simulation",
        "set_simulation_cursor",
        "get_simulation_frame",
        "set_part_scale",
    }
    assert expected <= names


def test_mcp_upload_base64_roundtrip(client: TestClient):
    """Simulate an AI agent calling upload_stl via the MCP tool directly."""
    import asyncio

    from app.mcp_server import build_mcp
    from app.state import get_service

    svc = get_service()
    assert len(svc.parts) == 0

    mcp = build_mcp()
    cube_b64 = base64.b64encode(make_binary_cube_stl(10.0)).decode("ascii")

    async def run():
        await mcp.call_tool("upload_stl", {"name": "agent.stl", "stl_base64": cube_b64})
        await mcp.call_tool("auto_arrange", {})
        return await mcp.call_tool("slice_all", {"layer_height_mm": 1.0, "perimeters": 1})

    result = asyncio.run(run())
    # fastmcp call_tool returns a ToolResult; unwrap the structured content
    data = result.structured_content or (result.data if hasattr(result, "data") else None)
    assert data is not None
    assert data["layer_count"] > 0
    assert svc.slice_result is not None
    assert len(svc.parts) == 1
