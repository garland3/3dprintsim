"""Tests for the atlas_upload MCP tool.

The tool downloads an STL from an Atlas-style signed URL, so we stand up a
TestClient-served download endpoint and point BACKEND_PUBLIC_URL at it.
"""

from __future__ import annotations

import asyncio
import os
from unittest import mock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.testclient import TestClient

from app.mcp_server import build_mcp
from app.state import get_service, reset_service

from .fixtures import make_binary_cube_stl


@pytest.fixture(autouse=True)
def fresh_service():
    reset_service()
    yield
    reset_service()


def _call_tool_sync(mcp, name, args):
    return asyncio.run(mcp.call_tool(name, args))


def _tool_data(result):
    return result.structured_content or (result.data if hasattr(result, "data") else None)


def test_atlas_upload_fetches_and_adds_part():
    """Given an absolute Atlas URL, the tool downloads the STL and the part
    is visible in the shared PrinterService state."""
    cube_bytes = make_binary_cube_stl(size=10.0)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/mcp/files/download/abc123"
        return httpx.Response(200, content=cube_bytes)

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client
    # `localhost` is allowlisted by default (it matches the dev backend) so
    # we don't need extra env vars. The SSRF guard also skips DNS for
    # localhost to avoid trying to egress in CI.
    with mock.patch(
        "app.mcp_server.httpx.Client",
        side_effect=lambda *a, **kw: real_client(*a, transport=transport, **kw),
    ):
        mcp = build_mcp()
        result = _call_tool_sync(
            mcp,
            "atlas_upload",
            {"filename": "http://localhost:8000/mcp/files/download/abc123"},
        )
    data = _tool_data(result)
    assert data is not None
    assert data["triangle_count"] == 12
    assert data["size"] == [10.0, 10.0, 10.0]
    # The shared service has the part too (proves the HTTP and MCP worlds agree).
    assert len(get_service().parts) == 1


def test_atlas_upload_normalizes_relative_path():
    """A relative `/mcp/files/download/...` filename is resolved against
    BACKEND_PUBLIC_URL before the GET is issued."""
    cube_bytes = make_binary_cube_stl(size=8.0)
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, content=cube_bytes)

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client
    # BACKEND_PUBLIC_URL's host is automatically added to the allowlist, so
    # pointing it at `backend.local` also whitelists that host. Stub DNS so
    # the SSRF private-IP check sees a public address.
    with mock.patch.dict(os.environ, {"BACKEND_PUBLIC_URL": "http://backend.local:8000"}):
        # Re-import to pick up the env override.
        import importlib

        import app.mcp_server as ms

        importlib.reload(ms)

        # Stub DNS to a genuinely public address — `203.0.113.x` is TEST-NET-3
        # which Python's ipaddress module flags as reserved.
        fake_infos = [(2, 1, 0, "", ("8.8.8.8", 0))]
        with mock.patch("app.mcp_server.socket.getaddrinfo", return_value=fake_infos), \
             mock.patch(
                 "app.mcp_server.httpx.Client",
                 side_effect=lambda *a, **kw: real_client(*a, transport=transport, **kw),
             ):
            mcp = ms.build_mcp()
            result = _call_tool_sync(
                mcp,
                "atlas_upload",
                {"filename": "/mcp/files/download/xyz?token=secret", "name": "foo.stl"},
            )
    data = _tool_data(result)
    assert data["name"] == "foo.stl"
    assert captured["url"] == "http://backend.local:8000/mcp/files/download/xyz?token=secret"


def test_atlas_upload_surfaces_http_errors():
    """A 404 from Atlas should raise a clean error, not a half-uploaded part."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client
    with mock.patch(
        "app.mcp_server.httpx.Client",
        side_effect=lambda *a, **kw: real_client(*a, transport=transport, **kw),
    ):
        mcp = build_mcp()
        # fastmcp wraps the ValueError as a ToolError — assert the call fails
        # and no part sneaks through.
        with pytest.raises(Exception) as exc_info:
            _call_tool_sync(
                mcp,
                "atlas_upload",
                {"filename": "http://localhost:8000/mcp/files/download/gone"},
            )
        assert "404" in str(exc_info.value) or "failed" in str(exc_info.value).lower()
    assert len(get_service().parts) == 0


def test_atlas_upload_requires_filename():
    mcp = build_mcp()
    with pytest.raises(Exception):
        _call_tool_sync(mcp, "atlas_upload", {"filename": ""})
    assert len(get_service().parts) == 0


def test_atlas_upload_listed_in_tools():
    """The tool must be advertised so Atlas knows to route file uploads to it."""
    mcp = build_mcp()
    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    assert "atlas_upload" in names


# --- SSRF hardening ---------------------------------------------------------


def test_atlas_upload_rejects_non_http_scheme():
    mcp = build_mcp()
    with pytest.raises(Exception) as exc_info:
        _call_tool_sync(mcp, "atlas_upload", {"filename": "file:///etc/passwd"})
    assert "http" in str(exc_info.value).lower()
    assert len(get_service().parts) == 0


def test_atlas_upload_rejects_non_allowlisted_host():
    """Default allowlist is just the configured backend; an arbitrary third
    party host must be rejected before any socket is opened."""
    mcp = build_mcp()
    with pytest.raises(Exception) as exc_info:
        _call_tool_sync(
            mcp,
            "atlas_upload",
            {"filename": "http://attacker.example.com/payload.stl"},
        )
    msg = str(exc_info.value).lower()
    assert "allowlist" in msg or "not in" in msg
    assert len(get_service().parts) == 0


def test_atlas_upload_rejects_private_ip_target():
    """An allowlisted host that resolves to an internal address must still
    be rejected — otherwise a DNS rebind or an operator misconfig leaks into
    cloud-metadata-style SSRF."""
    with mock.patch.dict(
        os.environ,
        {"ATLAS_ALLOWED_HOSTS": "internal.example.com"},
    ):
        import importlib

        import app.mcp_server as ms

        importlib.reload(ms)
        # Stub DNS so "internal.example.com" resolves to RFC1918.
        fake_infos = [(2, 1, 0, "", ("10.0.0.5", 0))]
        with mock.patch("app.mcp_server.socket.getaddrinfo", return_value=fake_infos):
            mcp = ms.build_mcp()
            with pytest.raises(Exception) as exc_info:
                _call_tool_sync(
                    mcp,
                    "atlas_upload",
                    {"filename": "http://internal.example.com/stl"},
                )
    assert "internal" in str(exc_info.value).lower() or "10.0.0.5" in str(exc_info.value)
    assert len(get_service().parts) == 0


def test_atlas_upload_does_not_follow_redirects():
    """A 302 from an allowlisted host to an internal one must not bypass
    the check. We return a redirect response and assert the tool raises —
    httpx with follow_redirects=False surfaces the 3xx as an error via
    raise_for_status()."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://10.0.0.5/internal"})

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client
    with mock.patch(
        "app.mcp_server.httpx.Client",
        side_effect=lambda *a, **kw: real_client(*a, transport=transport, **kw),
    ):
        mcp = build_mcp()
        with pytest.raises(Exception):
            _call_tool_sync(
                mcp,
                "atlas_upload",
                {"filename": "http://localhost:8000/mcp/files/download/abc"},
            )
    assert len(get_service().parts) == 0
