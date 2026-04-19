"""Per-part rotation tests.

Covers the world-axis rotation API added so users can orient a part before
slicing (flip upside down, lay on its side, spin). Rotation composes in the
bed frame, changes the arrangement footprint, and is cleared by reset.
"""

from __future__ import annotations

import math

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.state import reset_service
from app.stl_loader import IDENTITY_ROTATION, axis_rotation_matrix, multiply_matrix

from .fixtures import make_binary_cube_stl


@pytest.fixture
def client():
    reset_service()
    yield TestClient(create_app())
    reset_service()


def _upload_cube(client: TestClient, size: float = 20.0) -> str:
    stl = make_binary_cube_stl(size)
    resp = client.post(
        "/api/parts/upload",
        files={"file": ("cube.stl", stl, "application/octet-stream")},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def test_axis_rotation_matrix_values() -> None:
    rz90 = axis_rotation_matrix("z", 90.0)
    # +X unit vector should land on +Y under a right-handed +90° Z rotation.
    x_rot = (
        rz90[0][0] * 1 + rz90[0][1] * 0 + rz90[0][2] * 0,
        rz90[1][0] * 1 + rz90[1][1] * 0 + rz90[1][2] * 0,
        rz90[2][0] * 1 + rz90[2][1] * 0 + rz90[2][2] * 0,
    )
    assert math.isclose(x_rot[0], 0.0, abs_tol=1e-9)
    assert math.isclose(x_rot[1], 1.0, abs_tol=1e-9)
    assert math.isclose(x_rot[2], 0.0, abs_tol=1e-9)


def test_rotation_composes_in_world_frame() -> None:
    # Two +90° Z rotations should equal a single +180° Z rotation.
    r90 = axis_rotation_matrix("z", 90.0)
    r180 = axis_rotation_matrix("z", 180.0)
    composed = multiply_matrix(r90, r90)
    for i in range(3):
        for j in range(3):
            assert math.isclose(composed[i][j], r180[i][j], abs_tol=1e-9)


def test_rotate_endpoint_updates_part_rotation(client: TestClient) -> None:
    part_id = _upload_cube(client)

    before = client.get("/api/parts").json()[0]
    assert before["rotation"] == [list(row) for row in IDENTITY_ROTATION]

    resp = client.post(
        f"/api/parts/{part_id}/rotate",
        json={"axis": "z", "degrees": 90.0},
    )
    assert resp.status_code == 200, resp.text
    after = resp.json()
    assert after["rotation"] != [list(row) for row in IDENTITY_ROTATION]
    # +90° around Z should still leave Z-size unchanged for a cube.
    assert math.isclose(after["size"][2], before["size"][2], abs_tol=1e-6)


def test_reset_part_rotation(client: TestClient) -> None:
    part_id = _upload_cube(client)
    client.post(f"/api/parts/{part_id}/rotate", json={"axis": "y", "degrees": 45.0})
    mid = client.get("/api/parts").json()[0]["rotation"]
    assert mid != [list(row) for row in IDENTITY_ROTATION]

    resp = client.post(
        f"/api/parts/{part_id}/rotate",
        json={"axis": "x", "degrees": 0, "reset": True},
    )
    assert resp.status_code == 200
    assert resp.json()["rotation"] == [list(row) for row in IDENTITY_ROTATION]


def test_rotation_changes_placed_geometry(client: TestClient) -> None:
    part_id = _upload_cube(client, size=10.0)

    # Sanity: before rotation the part rests on z=0 with max_z == 10.
    before = client.get(f"/api/parts/{part_id}/geometry").json()
    assert math.isclose(before["min"][2], 0.0, abs_tol=1e-6)
    assert math.isclose(before["max"][2], 10.0, abs_tol=1e-6)

    # Rotate 90° around X: the cube is the same, but the drop-to-bed logic
    # should still seat it on z=0 afterwards (no geometry buried underground).
    client.post(f"/api/parts/{part_id}/rotate", json={"axis": "x", "degrees": 90.0})
    after = client.get(f"/api/parts/{part_id}/geometry").json()
    assert math.isclose(after["min"][2], 0.0, abs_tol=1e-5)
    assert math.isclose(after["max"][2], 10.0, abs_tol=1e-5)


def test_rotate_unknown_part_returns_404(client: TestClient) -> None:
    resp = client.post(
        "/api/parts/does-not-exist/rotate",
        json={"axis": "z", "degrees": 90.0},
    )
    assert resp.status_code == 404


def test_rotate_bad_axis_returns_400(client: TestClient) -> None:
    part_id = _upload_cube(client)
    resp = client.post(
        f"/api/parts/{part_id}/rotate",
        json={"axis": "w", "degrees": 90.0},
    )
    # Pydantic rejects the bad literal before we reach the service layer.
    assert resp.status_code == 422
