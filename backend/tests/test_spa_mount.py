"""SPA mount regression tests.

The container image serves the built React bundle from the same FastAPI
process that serves `/api` and `/mcp/`. A previous version of this mount
registered a `/{path:path}` catch-all before the rest of the `/api/*`
handlers, causing Starlette's top-down matcher to return `index.html` (or
404) for most GET endpoints. These tests pin down the invariant: when a
dist directory is present, `/api/*` still wins, and unknown paths fall
back to `index.html`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.main import create_app


INDEX_HTML = "<!doctype html><html><body>printsim-spa</body></html>"
APP_JS = "console.log('hi')"


@pytest.fixture
def spa_dist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(INDEX_HTML)
    (dist / "assets" / "app.js").write_text(APP_JS)
    (dist / "favicon.ico").write_bytes(b"\x00")
    monkeypatch.setenv("FRONTEND_DIST", str(dist))
    return dist


def test_spa_mount_serves_index_and_assets(spa_dist: Path) -> None:
    with TestClient(create_app()) as client:
        root = client.get("/")
        assert root.status_code == 200
        assert "printsim-spa" in root.text

        asset = client.get("/assets/app.js")
        assert asset.status_code == 200
        assert asset.text == APP_JS

        favicon = client.get("/favicon.ico")
        assert favicon.status_code == 200


def test_spa_mount_does_not_shadow_api(spa_dist: Path) -> None:
    """The catch-all must not preempt `/api/*` — regression for PR #8."""
    with TestClient(create_app()) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json() == {"ok": True, "service": "3dprintsim"}

        state = client.get("/api/state")
        assert state.status_code == 200
        assert state.headers["content-type"].startswith("application/json")
        # Real state payload has bed_size/parts keys — not index.html.
        body = state.json()
        assert "bed_size" in body and "parts" in body


def test_spa_fallback_serves_index_for_unknown_path(spa_dist: Path) -> None:
    resp = TestClient(create_app()).get("/some/client-route")
    assert resp.status_code == 200
    assert "printsim-spa" in resp.text


def test_spa_mount_skipped_when_index_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    monkeypatch.setenv("FRONTEND_DIST", str(dist))
    with TestClient(create_app()) as client:
        # Still 404 for `/` because we refuse to serve an incomplete dist.
        assert client.get("/").status_code == 404
        # But the API keeps working.
        assert client.get("/api/health").status_code == 200


def test_no_mount_when_dist_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dev case: no dist dir → API works, `/` returns 404 (no SPA)."""
    monkeypatch.setenv("FRONTEND_DIST", str(tmp_path / "does-not-exist"))
    # Also block the repo-fallback path that _resolve_frontend_dist walks up to.
    monkeypatch.setattr(main_module, "_resolve_frontend_dist", lambda: None)
    with TestClient(create_app()) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/").status_code == 404
