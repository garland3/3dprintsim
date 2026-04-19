"""Manual part positioning tests.

Covers the `POST /api/parts/{id}/position` endpoint added so the UI can drag
parts around the bed (or an MCP agent can override auto-arrange for a
specific part). The backend clamps to `[0, bed - size]` so the UI doesn't
have to know the exact bed dimensions when translating drag deltas into
absolute coords.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.state import reset_service

from .fixtures import make_binary_cube_stl


@pytest.fixture
def client():
    reset_service()
    yield TestClient(create_app())
    reset_service()


def _upload_cube(client: TestClient, size: float = 20.0) -> str:
    stl = make_binary_cube_stl(size)
    r = client.post(
        "/api/parts/upload",
        files={"file": ("cube.stl", stl, "application/octet-stream")},
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_set_position_updates_placement(client: TestClient) -> None:
    part_id = _upload_cube(client, size=20.0)
    r = client.post(f"/api/parts/{part_id}/position", json={"x": 50.0, "y": 30.0})
    assert r.status_code == 200, r.text
    placement = r.json()["placement"]
    assert placement["x"] == pytest.approx(50.0)
    assert placement["y"] == pytest.approx(30.0)


def test_set_position_clamps_to_bed(client: TestClient) -> None:
    # Default bed is 250x210. A 20mm cube dragged to (999, 999) should clamp
    # to (230, 190) — i.e., bed - size.
    part_id = _upload_cube(client, size=20.0)
    r = client.post(f"/api/parts/{part_id}/position", json={"x": 999.0, "y": 999.0})
    assert r.status_code == 200
    placement = r.json()["placement"]
    assert placement["x"] == pytest.approx(230.0)
    assert placement["y"] == pytest.approx(190.0)


def test_set_position_clamps_negative_below_zero(client: TestClient) -> None:
    # Pydantic's `ge=0` on the model catches this before the service does —
    # the UI should never send a negative value. Confirming the contract.
    part_id = _upload_cube(client)
    r = client.post(f"/api/parts/{part_id}/position", json={"x": -10.0, "y": 5.0})
    assert r.status_code == 422


def test_set_position_404_on_unknown_part(client: TestClient) -> None:
    r = client.post("/api/parts/does-not-exist/position", json={"x": 10.0, "y": 10.0})
    assert r.status_code == 404


def test_set_position_preserves_rotation_flag(client: TestClient) -> None:
    """Positioning shouldn't stomp on the part's rotation state."""
    part_id = _upload_cube(client)
    client.post(f"/api/parts/{part_id}/rotate", json={"axis": "z", "degrees": 90.0})
    before_rot = client.get("/api/parts").json()[0]["rotation"]

    client.post(f"/api/parts/{part_id}/position", json={"x": 20.0, "y": 20.0})
    after_rot = client.get("/api/parts").json()[0]["rotation"]
    assert after_rot == before_rot
