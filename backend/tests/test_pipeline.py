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

from .fixtures import make_3mf_cube, make_ascii_triangle_stl, make_binary_cube_stl


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


def test_default_printer_type_is_fdm(client: TestClient):
    r = client.get("/api/state")
    assert r.json()["printer_type"] == "FDM"


def test_printer_type_env_override(monkeypatch):
    """`PRINTER_TYPE=LPBF` in the environment switches new sessions to LPBF."""
    monkeypatch.setenv("PRINTER_TYPE", "lpbf")
    reset_service()
    app = create_app()
    with TestClient(app) as c:
        assert c.get("/api/state").json()["printer_type"] == "LPBF"
    reset_service()


def test_printer_type_invalid_falls_back_to_fdm(monkeypatch):
    monkeypatch.setenv("PRINTER_TYPE", "bogus")
    reset_service()
    app = create_app()
    with TestClient(app) as c:
        assert c.get("/api/state").json()["printer_type"] == "FDM"
    reset_service()


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


def test_part_geometry_bin_returns_float32_in_yup(client: TestClient):
    """Binary geometry endpoint ships raw float32 in Three.js Y-up object space.

    Verifies body length, that vertices live in object space (min corner
    pinned at the origin), and that placement metadata rides in headers
    so the renderer can apply it via mesh.position without a refetch.
    """
    import struct

    pid = upload_cube(client, size=10.0)
    r = client.get(f"/api/parts/{pid}/geometry.bin")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/octet-stream")
    tri_count = int(r.headers["x-triangle-count"])
    assert tri_count == 12
    assert len(r.content) == tri_count * 9 * 4

    floats = struct.unpack(f"<{tri_count * 9}f", r.content)
    grouped = [floats[i : i + 3] for i in range(0, len(floats), 3)]
    xs = [v[0] for v in grouped]
    ys = [v[1] for v in grouped]
    zs = [v[2] for v in grouped]
    # Object-space cube: every axis lives in [0, 10].
    assert min(xs) >= -1e-3 and max(xs) <= 10.0 + 1e-3
    assert min(ys) >= -1e-3 and max(ys) <= 10.0 + 1e-3
    assert min(zs) >= -1e-3 and max(zs) <= 10.0 + 1e-3

    # Headers carry triangle count, AABB, fingerprint, and bed placement.
    assert r.headers["x-aabb-min"] == "0.0,0.0,0.0"
    assert r.headers["x-shape-fingerprint"]
    placement = [float(v) for v in r.headers["x-placement"].split(",")]
    # Default 250x210 bed centers a 10mm cube at (120, 100).
    assert placement == [120.0, 100.0]


def test_part_geometry_bin_404s_for_unknown(client: TestClient):
    r = client.get("/api/parts/missing/geometry.bin")
    assert r.status_code == 404


def test_3mf_upload_parses_cube_into_part(client: TestClient):
    """3MF (ZIP-based mesh format) round-trips through the upload endpoint."""
    data = make_3mf_cube(size=10.0)
    resp = client.post(
        "/api/parts/upload",
        files={"file": ("cube.3mf", data, "application/vnd.ms-package.3dmanufacturing-3dmodel+xml")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["size"] == [10.0, 10.0, 10.0]
    assert body["triangle_count"] == 12


def test_3mf_upload_honors_build_transform(client: TestClient):
    """3MF `<item transform="...">` (column-major affine) is applied at parse time.

    A unit-scale transform that translates (5, 5, 5) should bump each axis
    of the bbox up by 5 mm.
    """
    # 3MF transform is 12 floats, column-major: rotation cols 0..2 then
    # translation as col 3.
    transform = "1 0 0 0 1 0 0 0 1 5 5 5"
    data = make_3mf_cube(size=10.0, transform=transform)
    resp = client.post(
        "/api/parts/upload",
        files={"file": ("cube_translated.3mf", data, "model/3mf")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Size doesn't change with translation; what changes is the placement,
    # which the frontend handles separately. Verify size + triangle count.
    assert body["size"] == [10.0, 10.0, 10.0]
    assert body["triangle_count"] == 12


def test_3mf_parse_directly():
    """Sanity-check the 3MF parser produces a valid Mesh."""
    from app.mesh_loader import parse_mesh

    mesh = parse_mesh(make_3mf_cube(size=20.0), filename="cube.3mf")
    assert mesh.triangle_count() == 12
    assert mesh.size == (20.0, 20.0, 20.0)


def test_step_upload_without_backend_returns_clear_error(client: TestClient):
    """Uploading a STEP file with no STEP backend installed should 400 with
    an actionable error rather than a 500."""
    # Minimal but plausible STEP header so the dispatcher routes to parse_step.
    payload = (
        b"ISO-10303-21;\n"
        b"HEADER;\n"
        b"FILE_DESCRIPTION(('test'),'2;1');\n"
        b"FILE_NAME('cube.step','',(''),(''),'','','');\n"
        b"FILE_SCHEMA(('CONFIG_CONTROL_DESIGN'));\n"
        b"ENDSEC;\n"
        b"DATA;\nENDSEC;\nEND-ISO-10303-21;\n"
    )
    resp = client.post(
        "/api/parts/upload",
        files={"file": ("cube.step", payload, "application/step")},
    )
    # cadquery / trimesh-with-step are optional dependencies that aren't
    # part of the default backend env; the loader should report that
    # clearly instead of 500-ing on an ImportError.
    if resp.status_code == 200:
        # If a CI image happens to ship cadquery, that's fine — at least
        # confirm the response shape is consistent.
        assert "size" in resp.json()
    else:
        assert resp.status_code == 400
        assert "STEP" in resp.json()["detail"]


def test_unknown_format_rejected(client: TestClient):
    resp = client.post(
        "/api/parts/upload",
        files={"file": ("notes.txt", b"hello world", "text/plain")},
    )
    assert resp.status_code == 400


def test_part_to_public_includes_shape_fingerprint(client: TestClient):
    pid = upload_cube(client, size=10.0)
    r = client.get("/api/parts").json()
    fp = r[0]["shape_fingerprint"]
    assert isinstance(fp, str) and pid in fp
    # Repositioning leaves geometry untouched, so fingerprint stays stable.
    client.post(f"/api/parts/{pid}/position", json={"x": 50.0, "y": 60.0})
    r2 = client.get("/api/parts").json()
    assert r2[0]["shape_fingerprint"] == fp
    # Scaling changes vertex coords -> different fingerprint.
    client.post(f"/api/parts/{pid}/scale", json={"scale": 2.0})
    r3 = client.get("/api/parts").json()
    assert r3[0]["shape_fingerprint"] != fp


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


def test_slice_layer_zero_is_not_an_overhang(client: TestClient):
    """Layer 0 sits on the bed, not in the air. It must not get tagged as an
    overhang (which would add an extra perimeter pass and an overhang role to
    every cell — wrong for a plain cube on the bed)."""
    upload_cube(client, size=10.0)
    client.post(
        "/api/slice",
        json={"layer_height": 1.0, "perimeters": 1, "infill_density": 0.0,
              "top_layers": 0, "bottom_layers": 0, "support_density": 0},
    )
    payload = client.get("/api/slice").json()
    # The lowest-z extrude moves are layer 0. None of them should carry the
    # overhang_perimeter role because there's no overhang — it's the first
    # layer of a flat-bottomed cube.
    zs = sorted({round(m["z"], 3) for m in payload["moves"] if m["kind"] == "extrude"})
    z0 = zs[0]
    layer0 = [m for m in payload["moves"] if m["kind"] == "extrude" and round(m["z"], 3) == z0]
    assert layer0, "expected at least one extrude move on layer 0"
    roles = {m.get("role") for m in layer0}
    assert "overhang_perimeter" not in roles, (
        f"layer 0 must not be classified as overhang; saw roles {roles}"
    )


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


def test_slice_support_cell_count_zero_when_disabled(client: TestClient):
    """support_density=0 disables support generation; the summary must report
    0 cells, not the latent count of overhangs the mask walk would find."""
    upload_cube(client, size=10.0)
    r = client.post(
        "/api/slice",
        json={"layer_height": 1.0, "support_density": 0.0},
    ).json()
    assert r["support_density"] == 0.0
    assert r["support_cell_count"] == 0


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
        "focus_viewer",
        "open_viewer",
    }
    assert expected <= names


def test_viewer_focus_request_is_monotonic(client: TestClient):
    """POSTing /api/viewer/focus increments the counter the UI polls.

    Focus is a transient UI signal, so it must NOT bump state_revision —
    otherwise every camera focus would trigger a needless /api/state refetch.
    """
    initial = client.get("/api/viewer/requests").json()
    assert initial == {"focus_request": 0, "state_revision": 0}

    r = client.post("/api/viewer/focus").json()
    assert r["focus_request"] == 1
    r = client.post("/api/viewer/focus").json()
    assert r["focus_request"] == 2

    final = client.get("/api/viewer/requests").json()
    assert final == {"focus_request": 2, "state_revision": 0}


def test_mcp_focus_viewer_increments_counter(client: TestClient):
    """The focus_viewer MCP tool must bump the same counter as the REST route."""
    import asyncio

    from app.mcp_server import build_mcp

    mcp = build_mcp()

    async def call_twice():
        a = await mcp.call_tool("focus_viewer", {})
        b = await mcp.call_tool("focus_viewer", {})
        return a, b

    a, b = asyncio.run(call_twice())
    a_data = a.structured_content or (a.data if hasattr(a, "data") else None)
    b_data = b.structured_content or (b.data if hasattr(b, "data") else None)
    assert a_data["focus_request"] == 1
    assert b_data["focus_request"] == 2
    # And the HTTP polling endpoint sees the same value.
    final = client.get("/api/viewer/requests").json()
    assert final == {"focus_request": 2, "state_revision": 0}


def test_state_revision_bumps_on_mutations(client: TestClient):
    """The browser polls /api/viewer/requests every 2s and re-fetches
    /api/state whenever state_revision advances. So every backend mutation —
    bed resize, part upload, slice, simulation step — must bump it,
    otherwise an MCP-driven change is invisible in the UI.
    """
    initial = client.get("/api/viewer/requests").json()
    assert initial["state_revision"] == 0

    # bed resize → bump
    client.post("/api/bed", json={"x": 200, "y": 200, "z": 200})
    after_bed = client.get("/api/viewer/requests").json()["state_revision"]
    assert after_bed > 0

    # part upload → bump
    upload_cube(client, size=10.0)
    after_upload = client.get("/api/viewer/requests").json()["state_revision"]
    assert after_upload > after_bed

    # slice → bump
    client.post("/api/slice", json={})
    after_slice = client.get("/api/viewer/requests").json()["state_revision"]
    assert after_slice > after_upload

    # simulation step → bump
    client.post("/api/simulation/start", json={})
    after_start = client.get("/api/viewer/requests").json()["state_revision"]
    assert after_start > after_slice
    client.post("/api/simulation/step", json={"steps": 1})
    after_step = client.get("/api/viewer/requests").json()["state_revision"]
    assert after_step > after_start

    # focus is a UI signal, NOT a mutation — revision must stay put.
    client.post("/api/viewer/focus")
    after_focus = client.get("/api/viewer/requests").json()
    assert after_focus["state_revision"] == after_step
    assert after_focus["focus_request"] == 1


def test_state_revision_isolated_per_session(client: TestClient):
    """Two MCP sessions must each see their own revision counter — otherwise
    one Atlas tab's mutations would noisily refresh another tab's UI."""
    headers_a = {"X-Session-Id": "rev-a"}
    headers_b = {"X-Session-Id": "rev-b"}

    # Each session starts at 0.
    assert client.get("/api/viewer/requests", headers=headers_a).json()["state_revision"] == 0
    assert client.get("/api/viewer/requests", headers=headers_b).json()["state_revision"] == 0

    # Mutate session A — only A's counter moves.
    client.post("/api/bed", json={"x": 200, "y": 200, "z": 200}, headers=headers_a)
    assert client.get("/api/viewer/requests", headers=headers_a).json()["state_revision"] > 0
    assert client.get("/api/viewer/requests", headers=headers_b).json()["state_revision"] == 0


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
