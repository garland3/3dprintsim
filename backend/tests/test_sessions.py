"""Session-scoped state tests.

The printer simulator is multi-user: each Atlas conversation (or each browser
tab) gets its own virtual printer via the SessionRegistry. These tests assert
two PrinterService consumers keyed off different session ids never see each
other's parts, and that the iframe `open_viewer` tool returns a URL carrying
the caller's MCP session id.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.state import (
    DEFAULT_SESSION_ID,
    SessionRegistry,
    get_registry,
    get_service,
    reset_service,
)

from .fixtures import make_binary_cube_stl


@pytest.fixture
def client():
    reset_service()
    app = create_app()
    with TestClient(app) as c:
        yield c


def _upload(client: TestClient, session: str, size: float = 10.0) -> str:
    """Upload a cube via the HTTP API scoped to `session`."""
    data = make_binary_cube_stl(size=size)
    resp = client.post(
        "/api/parts/upload",
        headers={"X-Session-Id": session},
        files={"file": ("cube.stl", data, "model/stl")},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def test_sessions_are_isolated(client: TestClient):
    """Alice's upload must not appear in Bob's parts list."""
    alice_pid = _upload(client, "alice", size=10.0)
    _upload(client, "bob", size=20.0)

    alice_parts = client.get("/api/parts", headers={"X-Session-Id": "alice"}).json()
    bob_parts = client.get("/api/parts", headers={"X-Session-Id": "bob"}).json()

    assert [p["id"] for p in alice_parts] == [alice_pid]
    assert len(bob_parts) == 1
    assert bob_parts[0]["id"] != alice_pid
    # And sanity-check that the sizes are the session's own uploads.
    assert alice_parts[0]["size"] == [10.0, 10.0, 10.0]
    assert bob_parts[0]["size"] == [20.0, 20.0, 20.0]


def test_slice_is_session_scoped(client: TestClient):
    """Slicing in one session must not invalidate or populate another."""
    _upload(client, "alice")
    _upload(client, "bob")

    r = client.post(
        "/api/slice",
        headers={"X-Session-Id": "alice"},
        json={"layer_height": 1.0, "perimeters": 1},
    )
    assert r.status_code == 200
    assert client.get("/api/slice", headers={"X-Session-Id": "alice"}).json()["ready"] is True
    # Bob never sliced — his slice should still be empty.
    assert client.get("/api/slice", headers={"X-Session-Id": "bob"}).json()["ready"] is False


def test_query_param_session_fallback(client: TestClient):
    """A first page load without the header should still pick up ?session=."""
    # Populate via header, fetch via query — must see the same state.
    pid = _upload(client, "alice")
    r = client.get("/api/parts?session=alice")
    assert r.status_code == 200
    assert [p["id"] for p in r.json()] == [pid]


def test_session_info_echoes_resolved_id(client: TestClient):
    r = client.get("/api/session", headers={"X-Session-Id": "alice"}).json()
    assert r["session_id"] == "alice"
    assert r["is_default"] is False

    r = client.get("/api/session").json()
    assert r["session_id"] == DEFAULT_SESSION_ID
    assert r["is_default"] is True


def test_header_takes_precedence_over_query(client: TestClient):
    """When both ?session= and X-Session-Id are present, the header wins."""
    _upload(client, "alice", size=10.0)
    _upload(client, "bob", size=20.0)
    # Header says alice, query says bob — we should see alice's parts.
    r = client.get(
        "/api/parts?session=bob",
        headers={"X-Session-Id": "alice"},
    ).json()
    assert r[0]["size"] == [10.0, 10.0, 10.0]


def test_reset_without_args_wipes_all_sessions():
    """`reset_service()` is the test hook — must drop every session so the
    next test starts from a clean registry."""
    get_service("alice").set_bed_size(100, 100, 100)
    get_service("bob").set_bed_size(50, 50, 50)
    assert "alice" in get_registry().active_sessions()
    assert "bob" in get_registry().active_sessions()
    reset_service()
    assert get_registry().active_sessions() == []


def test_reset_with_session_drops_only_that_session():
    get_service("alice").set_bed_size(100, 100, 100)
    get_service("bob").set_bed_size(50, 50, 50)
    reset_service("alice")
    active = set(get_registry().active_sessions())
    assert "alice" not in active
    assert "bob" in active
    reset_service()  # cleanup


def test_iframe_csp_header_is_permissive(client: TestClient):
    """The embed middleware must set frame-ancestors so Atlas can iframe us."""
    r = client.get("/api/health")
    csp = r.headers.get("content-security-policy", "")
    assert "frame-ancestors" in csp
    # Default FRAME_ANCESTORS is "*", which allows any parent origin.
    assert "*" in csp
    # X-Frame-Options should NOT be set (it would veto the CSP frame-ancestors).
    assert "x-frame-options" not in {k.lower() for k in r.headers.keys()}


# --- MCP: open_viewer + per-session tool calls ------------------------------


def test_mcp_open_viewer_returns_atlas_envelope():
    """The open_viewer tool must return the v2 display envelope Atlas reads
    to pop an iframe into the canvas panel."""
    from app.mcp_server import build_mcp

    mcp = build_mcp()

    async def run():
        return await mcp.call_tool("open_viewer", {})

    result = asyncio.run(run())
    data = result.structured_content or (result.data if hasattr(result, "data") else None)
    assert data is not None
    display = data["display"]
    assert display["open_canvas"] is True
    assert display["type"] == "iframe"
    assert "session=" in display["url"]
    assert "embed=1" in display["url"]
    assert "allow-scripts" in display["sandbox"]
    # Results should carry the session id for the agent to echo back to users.
    assert "session_id" in data["results"]


def test_mcp_open_viewer_url_obeys_viewer_public_url(monkeypatch):
    """Operators override the embedded iframe origin via VIEWER_PUBLIC_URL so
    Atlas and the browser hit the publicly routable host, not localhost."""
    from app.mcp_server import build_mcp

    monkeypatch.setenv("VIEWER_PUBLIC_URL", "https://sim.example.com")
    mcp = build_mcp()

    async def run():
        return await mcp.call_tool("open_viewer", {})

    result = asyncio.run(run())
    data = result.structured_content or (result.data if hasattr(result, "data") else None)
    assert data["display"]["url"].startswith("https://sim.example.com/?embed=1&session=")


def test_mcp_session_id_helper_honors_x_session_id_header(monkeypatch):
    """The MCP session resolver must prefer an explicit X-Session-Id header
    over ctx.session_id. That's what lets Atlas (and our e2e harness) bind
    one MCP call to the same PrinterService a browser iframe is touching."""
    from app import mcp_server

    class StubCtx:
        session_id = "mcp-auto-minted-id"

    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda *a, **kw: {"x-session-id": "explicit-browser-id"},
    )
    assert mcp_server._session_id(StubCtx()) == "explicit-browser-id"

    # With no header, it falls back to ctx.session_id.
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda *a, **kw: {})
    assert mcp_server._session_id(StubCtx()) == "mcp-auto-minted-id"


def test_mcp_tool_calls_without_ctx_use_default_session():
    """In-process MCP calls (tests, stdio clients, dev harnesses) have no MCP
    handshake yet — ctx.session_id raises — so every tool must fall back to
    the DEFAULT_SESSION_ID rather than crashing or silently making a new per-
    call printer."""
    from app.mcp_server import build_mcp

    reset_service()
    mcp = build_mcp()
    import base64

    cube_b64 = base64.b64encode(make_binary_cube_stl(10.0)).decode("ascii")

    async def run():
        await mcp.call_tool("upload_stl", {"name": "x.stl", "stl_base64": cube_b64})
        return await mcp.call_tool("get_printer_state", {})

    state_result = asyncio.run(run())
    data = state_result.structured_content or state_result.data
    # Part landed in the "default" session's service, not some ephemeral one.
    assert len(data["parts"]) == 1
    assert len(get_service(DEFAULT_SESSION_ID).parts) == 1


# --- review-fix regressions ------------------------------------------------


def test_api_reset_without_session_wipes_all_sessions(client: TestClient):
    """`/api/reset` with no header/query must wipe every session.

    The Playwright MCP suite's beforeEach hook posts to /api/reset to start
    each run from a clean slate; if that only cleared the "default" session,
    Atlas-spawned sessions would leak parts/slices across test runs.
    """
    _upload(client, "alice")
    _upload(client, "bob")
    assert "alice" in get_registry().active_sessions()
    assert "bob" in get_registry().active_sessions()

    r = client.post("/api/reset")
    assert r.status_code == 200
    # Every session got dropped — alice and bob are gone, the default session
    # was lazily recreated to serve the response payload, so it's the only
    # entry left.
    active = set(get_registry().active_sessions())
    assert "alice" not in active
    assert "bob" not in active


def test_api_reset_with_explicit_session_only_drops_that_one(client: TestClient):
    _upload(client, "alice")
    _upload(client, "bob")

    r = client.post("/api/reset", headers={"X-Session-Id": "alice"})
    assert r.status_code == 200
    # The response payload is alice's freshly-recreated state — empty parts.
    assert r.json()["parts"] == []

    # Bob is untouched: still has his cube.
    bob_parts = client.get("/api/parts", headers={"X-Session-Id": "bob"}).json()
    assert len(bob_parts) == 1

    # And alice's state is genuinely empty.
    alice_parts = client.get("/api/parts", headers={"X-Session-Id": "alice"}).json()
    assert alice_parts == []


def test_invalid_session_id_returns_400(client: TestClient):
    """Charset-validated session ids: an attacker can't smuggle nul bytes,
    very long strings, or path-style values into the registry keys."""
    bad_ids = ["a/b", "x" * 200, "has space", "ctl\x01char", ""]
    for bad in bad_ids:
        r = client.get("/api/parts", headers={"X-Session-Id": bad})
        # Empty-string falls through to DEFAULT_SESSION_ID rather than failing.
        if bad == "":
            assert r.status_code == 200
        else:
            assert r.status_code == 400, f"expected 400 for {bad!r}, got {r.status_code}"


def test_session_registry_cap_blocks_unbounded_growth():
    """A hostile client that rotates session ids must hit a hard cap rather
    than OOM the process."""
    reg = SessionRegistry(ttl_seconds=3600.0, max_sessions=3)
    reg.get("a")
    reg.get("b")
    reg.get("c")
    # The cap is honored — a fresh fourth id is rejected.
    with pytest.raises(ValueError, match="session cap"):
        reg.get("d")
    # But re-using an existing session still works (no new allocation).
    assert reg.get("a") is reg.get("a")


def test_csp_middleware_preserves_existing_directives():
    """The IframeEmbedMiddleware must splice frame-ancestors into an existing
    CSP rather than nuking it. Otherwise a stricter upstream policy
    (e.g. `default-src 'self'`) would silently disappear."""
    from app.main import IframeEmbedMiddleware

    mw = IframeEmbedMiddleware(app=None, frame_ancestors="https://atlas.example")

    existing = "default-src 'self'; script-src 'self' 'unsafe-inline'"
    merged = mw._merged_csp(existing)
    assert "default-src 'self'" in merged
    assert "script-src 'self' 'unsafe-inline'" in merged
    assert "frame-ancestors https://atlas.example" in merged

    # Existing frame-ancestors gets replaced, not duplicated.
    with_old = "default-src 'self'; frame-ancestors 'none'"
    merged = mw._merged_csp(with_old)
    assert merged.count("frame-ancestors") == 1
    assert "'none'" not in merged
    assert "default-src 'self'" in merged

    # No prior CSP → just our directive.
    assert mw._merged_csp(None) == "frame-ancestors https://atlas.example"
    assert mw._merged_csp("") == "frame-ancestors https://atlas.example"


def test_session_ttl_env_falls_back_on_garbage(monkeypatch, caplog):
    """A misconfigured `SESSION_TTL_SECONDS=forever` must not crash import."""
    from app.state import _env_float

    monkeypatch.setenv("BAD", "forever")
    with caplog.at_level("WARNING"):
        assert _env_float("BAD", 7.0) == 7.0
    assert any("BAD" in r.message for r in caplog.records)


def test_mcp_open_viewer_listed_in_tools():
    """Atlas discovers tools via list_tools; open_viewer must be there so the
    agent knows it can pop the canvas iframe."""
    from app.mcp_server import build_mcp

    mcp = build_mcp()
    tools = asyncio.run(mcp.list_tools())
    assert "open_viewer" in {t.name for t in tools}
