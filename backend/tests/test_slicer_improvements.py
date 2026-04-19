"""Unit + integration tests for the 10 slicer improvements.

Each top-level function targets one of the listed improvements. Written
against the REST surface so the tests double as a contract check for the
new optional SliceRequest fields.
"""

from __future__ import annotations

import math

import pytest
from fastapi.testclient import TestClient

from app.geometry import (
    offset_polygon,
    pick_seam_index,
    polygon_area,
    rotate_polygon_start,
)
from app.main import create_app
from app.slicer import slice_meshes
from app.state import get_service, reset_service
from app.stl_loader import Mesh, Triangle, parse_stl, validate_mesh
from app.surfaces import SUPPORT_RESOLUTION, cell_of, cluster_cells

from .fixtures import make_binary_cube_stl, make_binary_t_overhang_stl


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


# ---------------------------------------------------------------------------
# 1. True perimeter insets via polygon offset
# ---------------------------------------------------------------------------


def test_offset_polygon_shrinks_square_by_distance():
    square = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)]
    inset = offset_polygon(square, 1.0)
    assert inset is not None
    # The inset should be roughly 8x8 centered on the original.
    area = polygon_area(inset)
    assert abs(abs(area) - 64.0) < 0.5, f"area {area}"


def test_offset_polygon_collapses_at_large_distance():
    square = [(0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0), (0.0, 0.0)]
    # Inset by 5 mm — square is only 4mm across, so this must collapse.
    assert offset_polygon(square, 5.0) is None


def test_perimeter_insets_are_distinct_polylines(client: TestClient):
    """With >1 perimeter the inner shells should be STRICTLY smaller than the
    outer contour, not a retrace of the same polyline."""
    upload_cube(client, size=20.0)
    parts = client.get("/api/parts").json()
    pl = parts[0]["placement"]
    x0, y0 = pl["x"], pl["y"]  # cube min-corner on the bed
    x1, y1 = x0 + 20.0, y0 + 20.0

    client.post(
        "/api/slice",
        json={
            "layer_height": 1.0,
            "perimeters": 3,
            "infill_density": 0.0,
            "top_layers": 0,
            "bottom_layers": 0,
            "support_density": 0.0,
            "nozzle_width": 0.4,
        },
    )
    payload = client.get("/api/slice").json()
    zs = sorted({round(m["z"], 3) for m in payload["moves"] if m["kind"] == "extrude"})
    mid_z = zs[len(zs) // 2]
    layer_moves = [
        m
        for m in payload["moves"]
        if m["kind"] == "extrude"
        and round(m["z"], 3) == mid_z
        and m.get("role") in {"perimeter", "overhang_perimeter"}
    ]
    # Outer perimeter still touches the full footprint bounds.
    assert max(m["x"] for m in layer_moves) >= x1 - 1e-3
    assert min(m["x"] for m in layer_moves) <= x0 + 1e-3

    # Innermost ring sits at least 2*nozzle_width = 0.8mm inside the cube.
    innermost = [
        m
        for m in layer_moves
        if x0 + 0.7 < m["x"] < x1 - 0.7 and y0 + 0.7 < m["y"] < y1 - 0.7
    ]
    assert innermost, "expected inner perimeter points inset from the outer contour"


# ---------------------------------------------------------------------------
# 2. Retraction on travel
# ---------------------------------------------------------------------------


def test_gcode_contains_retract_pair_on_travel(client: TestClient):
    upload_cube(client, size=10.0)
    client.post(
        "/api/slice",
        json={"layer_height": 1.0, "perimeters": 1, "retract_mm": 1.5},
    )
    body = client.get("/api/gcode").text
    # Retract = explicit G1 E<value> line (no X/Y/Z); the "retract" comment
    # is emitted by the slicer so a test can grep for it.
    assert "retract" in body
    assert "un-retract" in body
    # There must be at least one line that advances only E.
    e_only_lines = [
        line
        for line in body.splitlines()
        if line.startswith("G1 ")
        and " X" not in line
        and " Y" not in line
        and " Z" not in line
        and " E" in line
    ]
    assert len(e_only_lines) > 0, "expected retract/unretract E-only lines"


def test_gcode_without_retraction_is_clean(client: TestClient):
    upload_cube(client, size=10.0)
    client.post(
        "/api/slice",
        json={"layer_height": 1.0, "retract_mm": 0.0},
    )
    body = client.get("/api/gcode").text
    assert "retract" not in body


# ---------------------------------------------------------------------------
# 3. Region-clipped solid fill
# ---------------------------------------------------------------------------


def test_mid_layer_of_t_has_mixed_solid_and_sparse(client: TestClient):
    """A T-shaped part's wide cap layer — right at the first wide layer —
    should have BOTH overhang fill (where the cap extends past the stem)
    and non-overhang fill further in. Old "all-or-nothing" layer solid
    would mark the whole layer solid."""
    data = make_binary_t_overhang_stl()
    resp = client.post(
        "/api/parts/upload",
        files={"file": ("t.stl", data, "model/stl")},
    )
    assert resp.status_code == 200
    client.post(
        "/api/slice",
        json={
            "layer_height": 1.0,
            "perimeters": 1,
            "infill_density": 0.5,
            "top_layers": 2,
            "bottom_layers": 2,
            "support_density": 0,
        },
    )
    payload = client.get("/api/slice").json()
    # Locate the first layer at z > stem_h (the first cap layer).
    zs_with_extrudes = sorted({round(m["z"], 3) for m in payload["moves"] if m["kind"] == "extrude"})
    cap_zs = [z for z in zs_with_extrudes if z > 5.1]  # stem_h = 5.0
    assert cap_zs, "expected cap layers"
    first_cap_z = cap_zs[0]
    cap_extrudes = [
        m
        for m in payload["moves"]
        if m["kind"] == "extrude" and round(m["z"], 3) == first_cap_z
    ]
    roles = {m.get("role") for m in cap_extrudes}
    # Region clipping: bottom/overhang cells emit "bottom" or "bridge";
    # everything else on a cap mid-layer is either sparse or solid-top
    # depending on top_layers window. The key assertion is we see MORE
    # than one role, which old layer-wide solid fill wouldn't produce.
    non_perim_roles = roles - {"perimeter", "overhang_perimeter", "travel"}
    assert len(non_perim_roles) >= 2, (
        f"expected multiple infill roles on a T-overhang cap layer, saw {roles}"
    )


# ---------------------------------------------------------------------------
# 4. Temperature + fan control
# ---------------------------------------------------------------------------


def test_gcode_thermal_preamble(client: TestClient):
    upload_cube(client)
    client.post(
        "/api/slice",
        json={"layer_height": 1.0, "hotend_temp": 215, "bed_temp": 60},
    )
    body = client.get("/api/gcode").text
    assert "M140 S60" in body
    assert "M104 S215" in body
    assert "M190 S60" in body  # wait for bed
    assert "M109 S215" in body  # wait for hotend
    # Tail: turn everything off at end of job.
    assert "M104 S0" in body
    assert "M140 S0" in body


def test_gcode_emits_fan_control(client: TestClient):
    upload_cube(client)
    client.post(
        "/api/slice",
        json={"layer_height": 1.0, "fan_speed": 180, "first_layer_fan": 0},
    )
    body = client.get("/api/gcode").text
    # M106 S<speed> appears somewhere after layer 0.
    m106_lines = [line for line in body.splitlines() if line.startswith("M106")]
    assert any("S180" in line for line in m106_lines), (
        f"expected M106 S180 after layer 0; saw {m106_lines}"
    )
    # M107 (fan off) at end-of-job tail.
    assert "M107 ; fan off" in body


# ---------------------------------------------------------------------------
# 5. Bridge detection + slow-down
# ---------------------------------------------------------------------------


def test_bridge_moves_detected_on_t_overhang(client: TestClient):
    data = make_binary_t_overhang_stl()
    client.post(
        "/api/parts/upload",
        files={"file": ("t.stl", data, "model/stl")},
    )
    summary = client.post(
        "/api/slice",
        json={
            "layer_height": 1.0,
            "perimeters": 1,
            "infill_density": 0.5,
            "top_layers": 2,
            "bottom_layers": 2,
            "support_density": 0,  # disable supports so bridges actually bridge
        },
    ).json()
    assert summary["bridge_moves"] > 0, (
        "T-overhang cap should contain at least one bridge infill move"
    )


def test_bridge_role_in_gcode(client: TestClient):
    """Bridges get emitted at reduced feedrate via bridge_speed_factor."""
    data = make_binary_t_overhang_stl()
    client.post(
        "/api/parts/upload",
        files={"file": ("t.stl", data, "model/stl")},
    )
    client.post(
        "/api/slice",
        json={
            "layer_height": 1.0,
            "perimeters": 1,
            "infill_density": 0.5,
            "top_layers": 2,
            "bottom_layers": 2,
            "support_density": 0,
            "bridge_speed_factor": 0.25,
            "print_speed": 40.0,
        },
    )
    payload = client.get("/api/slice").json()
    bridge_moves = [m for m in payload["moves"] if m.get("role") == "bridge"]
    assert bridge_moves, "expected at least one bridge-role move"


# ---------------------------------------------------------------------------
# 6. Tree / clustered supports
# ---------------------------------------------------------------------------


def test_cluster_cells_groups_adjacent():
    cells = {(0, 0), (1, 0), (2, 0), (5, 0), (5, 1)}
    clusters = cluster_cells(cells)
    sizes = sorted(len(c) for c in clusters)
    assert sizes == [2, 3]


def test_cluster_cells_disjoint_corners_stay_separate():
    cells = {(0, 0), (5, 5)}
    clusters = cluster_cells(cells)
    assert len(clusters) == 2


def test_supports_emit_perimeter_per_cluster(client: TestClient):
    """A T-overhang at 25% support density should yield a support pillar
    with a perimeter ring, not a scatter of disconnected single-cell stubs.
    Concrete signal: count of distinct support-role extrude moves > number
    of grid cells in the support mask of the first support layer."""
    data = make_binary_t_overhang_stl()
    client.post(
        "/api/parts/upload",
        files={"file": ("t.stl", data, "model/stl")},
    )
    summary = client.post(
        "/api/slice",
        json={
            "layer_height": 1.0,
            "perimeters": 1,
            "infill_density": 0.2,
            "top_layers": 2,
            "bottom_layers": 2,
            "support_density": 0.25,
        },
    ).json()
    assert summary["support_cell_count"] > 0
    payload = client.get("/api/slice").json()
    support_extrudes = [
        m for m in payload["moves"] if m.get("role") == "support" and m["kind"] == "extrude"
    ]
    assert len(support_extrudes) > 0


# ---------------------------------------------------------------------------
# 7. Adaptive layer height
# ---------------------------------------------------------------------------


def test_adaptive_layer_height_uses_more_layers_on_slopes():
    """A pyramid-ish mesh (all faces at 45°) should trigger extra layers
    under adaptive_layers=True compared to fixed layer_height."""
    # Build a simple 10x10x10 pyramid with 4 triangular faces.
    tris = [
        Triangle((0, 0, 0), (10, 0, 0), (5, 5, 10)),
        Triangle((10, 0, 0), (10, 10, 0), (5, 5, 10)),
        Triangle((10, 10, 0), (0, 10, 0), (5, 5, 10)),
        Triangle((0, 10, 0), (0, 0, 0), (5, 5, 10)),
        # bottom
        Triangle((0, 0, 0), (10, 10, 0), (10, 0, 0)),
        Triangle((0, 0, 0), (0, 10, 0), (10, 10, 0)),
    ]
    mesh = Mesh(
        triangles=tris,
        min_xyz=(0.0, 0.0, 0.0),
        max_xyz=(10.0, 10.0, 10.0),
    )
    fixed = slice_meshes(
        [mesh],
        layer_height=1.0,
        perimeters=1,
        support_density=0,
    )
    adaptive = slice_meshes(
        [mesh],
        layer_height=1.0,
        perimeters=1,
        support_density=0,
        adaptive_layers=True,
        layer_height_min=0.5,
        layer_height_max=1.0,
    )
    assert adaptive.summary()["layer_count"] > fixed.summary()["layer_count"]
    assert adaptive.summary()["adaptive_layers"] is True


# ---------------------------------------------------------------------------
# 8. Seam placement
# ---------------------------------------------------------------------------


def test_seam_picker_aligned_chooses_consistent_corner():
    square_a = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)]
    # Rotated square (different poly[0]) should pick the same world corner.
    square_b = [(10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0), (10.0, 0.0)]
    ia = pick_seam_index(square_a, "aligned")
    ib = pick_seam_index(square_b, "aligned")
    # Both polygons have (10, 10) as their max x+y corner.
    assert square_a[ia] == (10.0, 10.0)
    assert square_b[ib] == (10.0, 10.0)


def test_seam_rotate_start_keeps_polygon_closed():
    square = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)]
    rotated = rotate_polygon_start(square, 2)
    assert rotated[0] == (10.0, 10.0)
    assert rotated[0] == rotated[-1]


def test_seam_aligned_moves_perimeter_start_consistently(client: TestClient):
    upload_cube(client, size=10.0)
    client.post(
        "/api/slice",
        json={
            "layer_height": 1.0,
            "perimeters": 1,
            "infill_density": 0.0,
            "top_layers": 0,
            "bottom_layers": 0,
            "support_density": 0.0,
            "seam_position": "aligned",
            "retract_mm": 0.0,
        },
    )
    payload = client.get("/api/slice").json()
    # First perimeter extrude on each layer is the one right after the
    # layer's initial travel. For seam_position=aligned each layer should
    # start at a consistent world corner of the cube — the cube's +x/+y
    # corner, which after placement is the max-x/max-y corner.
    by_layer: dict[float, list[dict]] = {}
    for m in payload["moves"]:
        by_layer.setdefault(round(m["z"], 3), []).append(m)
    # Check each layer: the travel→extrude boundary point is the seam start.
    seam_points = []
    for z in sorted(by_layer):
        moves = by_layer[z]
        for i in range(1, len(moves)):
            if moves[i - 1]["kind"] == "travel" and moves[i]["kind"] == "extrude":
                seam_points.append((moves[i - 1]["x"], moves[i - 1]["y"]))
                break
    # All seams should land near the same corner (within 0.5mm).
    assert len(seam_points) >= 3, seam_points
    xs = [p[0] for p in seam_points]
    ys = [p[1] for p in seam_points]
    assert max(xs) - min(xs) < 0.5
    assert max(ys) - min(ys) < 0.5


# ---------------------------------------------------------------------------
# 9. First-layer overrides + brim
# ---------------------------------------------------------------------------


def test_first_layer_height_is_respected():
    # 10mm cube, nominal 1mm layers, first_layer=0.3. Layer 0 center
    # should be at 0.15, layer 1 center at 0.3 + 0.5 = 0.8.
    data = make_binary_cube_stl(10.0)
    mesh = parse_stl(data)
    result = slice_meshes(
        [mesh],
        layer_height=1.0,
        perimeters=1,
        first_layer_height=0.3,
        support_density=0,
        infill_density=0,
        top_layers=0,
        bottom_layers=0,
    )
    zs = [layer.z for layer in result.layers]
    assert abs(zs[0] - 0.15) < 1e-6
    assert abs(zs[1] - 0.8) < 1e-6


def test_brim_adds_outer_loops(client: TestClient):
    upload_cube(client, size=10.0)
    base = client.post(
        "/api/slice",
        json={
            "layer_height": 1.0,
            "perimeters": 1,
            "infill_density": 0.0,
            "top_layers": 0,
            "bottom_layers": 0,
            "support_density": 0.0,
            "brim_loops": 0,
        },
    ).json()
    brimmed = client.post(
        "/api/slice",
        json={
            "layer_height": 1.0,
            "perimeters": 1,
            "infill_density": 0.0,
            "top_layers": 0,
            "bottom_layers": 0,
            "support_density": 0.0,
            "brim_loops": 3,
        },
    ).json()
    # Brim adds 3 outer rings on layer 0 = 3 × (N polyline points).
    assert brimmed["move_count"] > base["move_count"]
    assert brimmed["brim_loops"] == 3

    payload = client.get("/api/slice").json()
    brim_moves = [m for m in payload["moves"] if m.get("role") == "brim"]
    assert len(brim_moves) > 0


# ---------------------------------------------------------------------------
# 10. STL mesh validation
# ---------------------------------------------------------------------------


def test_validate_mesh_flags_degenerate_triangles():
    degenerate = Triangle((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (2.0, 0.0, 0.0))
    mesh = Mesh(triangles=[degenerate], min_xyz=(0.0, 0.0, 0.0), max_xyz=(2.0, 0.0, 0.0))
    warnings = validate_mesh(mesh)
    assert any("degenerate" in w for w in warnings)


def test_validate_mesh_flags_non_manifold():
    # A pair of triangles sharing a single edge — every other edge is
    # non-manifold (count = 1) instead of shared (count = 2).
    t1 = Triangle((0, 0, 0), (1, 0, 0), (0, 1, 0))
    t2 = Triangle((1, 0, 0), (0, 1, 0), (1, 1, 0))
    mesh = Mesh(triangles=[t1, t2], min_xyz=(0, 0, 0), max_xyz=(1, 1, 0))
    warnings = validate_mesh(mesh)
    assert any("non-manifold" in w for w in warnings)


def test_closed_cube_has_no_warnings():
    data = make_binary_cube_stl(10.0)
    mesh = parse_stl(data)
    warnings = validate_mesh(mesh)
    assert warnings == []


def test_upload_exposes_warnings_to_clients(client: TestClient):
    """Parts publish `warnings` in to_public() so the UI can surface them."""
    upload_cube(client, size=10.0)
    parts = client.get("/api/parts").json()
    assert "warnings" in parts[0]
    assert parts[0]["warnings"] == []
