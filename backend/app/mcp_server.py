"""MCP tools exposing the virtual printer to AI agents via fastmcp.

The tools intentionally cover the full pipeline — upload, arrange, slice,
simulate, introspect — so an agent can operate the printer end to end without
ever opening the web UI.

Every tool is scoped to the caller's MCP session. FastMCP 3.x's streamable
HTTP transport mints a session id per client and surfaces it via
`Context.session_id`; we use that id to key into the SessionRegistry so each
Atlas conversation gets its own virtual printer. The matching browser iframe
receives the same id in its URL (see `open_viewer`) and sends it as the
`X-Session-Id` header on every REST call, keeping the LLM's view of the
printer and the user's view in sync — without either leaking across users.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urljoin, urlparse

import httpx
from fastmcp import Context, FastMCP
from fastmcp.server.dependencies import get_http_headers

from .state import DEFAULT_SESSION_ID, PrinterService, get_service


# Atlas file-upload handoff: when an Atlas host injects a file into an MCP call
# it rewrites the user-facing filename into a signed download URL. That URL may
# arrive either as an absolute URL or as a relative path the tool is expected
# to resolve against the backend's public base.
_ATLAS_FETCH_TIMEOUT = 30.0
# Cap the byte stream we're willing to ingest. Keeps a hostile or accidental
# multi-GB STL from pinning the event loop and the in-memory triangle list.
_ATLAS_MAX_BYTES = 200 * 1024 * 1024  # 200 MiB


def _backend_public_url() -> str:
    """Resolved at call time so tests can flip BACKEND_PUBLIC_URL per-case
    without having to reload this module. Falls back to BACKEND_PORT from
    the .env so the iframe URL matches the port the server actually binds."""
    explicit = os.getenv("BACKEND_PUBLIC_URL")
    if explicit:
        return explicit
    port = os.getenv("BACKEND_PORT", "8000")
    return f"http://localhost:{port}"


def _atlas_base_url() -> str:
    """Origin that serves Atlas's `/mcp/files/download/...` endpoints.

    Atlas hands the MCP tool either an absolute URL or a path-only string;
    paths are resolved against this base. In production Atlas runs on a
    different host than us, so set `ATLAS_BASE_URL` to its public origin
    (e.g. `http://atlas:8000`). When unset we fall back to BACKEND_PUBLIC_URL,
    which preserves the legacy "Atlas and the simulator share an origin"
    assumption baked into the existing tests.
    """
    return os.getenv("ATLAS_BASE_URL") or _backend_public_url()


def _viewer_public_url() -> str:
    """Where the React viewer is served from when embedded in an iframe.

    Defaults to the backend URL (same origin in prod because the backend
    serves the built frontend), but the dev setup runs the Vite dev server on
    port 5173 so operators can override via VIEWER_PUBLIC_URL.
    """
    return os.getenv("VIEWER_PUBLIC_URL", _backend_public_url())


def _normalize_atlas_url(filename: str) -> str:
    """Resolve an Atlas `filename` handoff to an absolute URL we can GET."""
    if filename.startswith("/"):
        return urljoin(_atlas_base_url(), filename)
    return filename


def _allowed_hosts() -> set[str]:
    """Hosts the Atlas downloader is willing to talk to.

    Always includes the configured backend's host plus the Atlas base host
    (so relative `/mcp/files/download/...` paths resolve cleanly), plus any
    operator-supplied ATLAS_ALLOWED_HOSTS entries. Anything else is rejected
    up front so a hostile prompt can't coerce the server into SSRF requests
    to cloud metadata, RFC1918 services, or unrelated third-party hosts.
    """
    hosts: set[str] = {
        h.strip().lower()
        for h in os.getenv("ATLAS_ALLOWED_HOSTS", "").split(",")
        if h.strip()
    }
    for url in (_backend_public_url(), _atlas_base_url()):
        netloc = urlparse(url).netloc.lower()
        if netloc:
            hosts.add(netloc)
    return hosts


def _reject_ssrf(url: str) -> None:
    """Raise ValueError if `url` is not a safe Atlas download target.

    Enforces: http/https scheme, host present, host matches an allowlisted
    origin (backend or ATLAS_ALLOWED_HOSTS), and — for non-localhost
    allowlist entries — that the resolved IP is not in an internal/reserved
    range. Localhost is allowed because the default dev deployment has the
    backend talking to itself for /mcp/files/download paths.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Atlas URL must be http/https, got {parsed.scheme!r}")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("Atlas URL missing host")
    netloc = parsed.netloc.lower()
    allowed = _allowed_hosts()
    if netloc not in allowed and host not in allowed:
        raise ValueError(
            f"Atlas host {netloc!r} is not in the allowlist; "
            "set ATLAS_ALLOWED_HOSTS, ATLAS_BASE_URL, or BACKEND_PUBLIC_URL "
            "to permit it"
        )
    # Skip the private-IP check for loopback since the default backend runs
    # on localhost and legitimately resolves to 127.0.0.1/::1.
    if host in ("localhost", "127.0.0.1", "::1"):
        return
    # Also skip for hosts the operator explicitly configured as the backend
    # or Atlas origin — those are deliberate trust decisions, e.g. pointing
    # at `host.containers.internal` (resolves to a private IP) so a podman
    # container can reach Atlas on the host. The wider ATLAS_ALLOWED_HOSTS
    # list still gets the private-IP guard since it's meant for public peers.
    operator_hosts = {
        urlparse(u).netloc.lower()
        for u in (os.getenv("ATLAS_BASE_URL"), os.getenv("BACKEND_PUBLIC_URL"))
        if u
    }
    if netloc in operator_hosts:
        return
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ValueError(f"Atlas host {host!r} failed DNS lookup") from exc
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError(
                f"Atlas host {host!r} resolves to internal address {addr}; refusing"
            )


def _atlas_display_name(url: str, fallback: str = "atlas.stl") -> str:
    """Best-effort filename for display — Atlas URLs expose the original name
    in the path segment (e.g. /mcp/files/download/<id>/my-model.stl) but many
    handoffs strip it, so default to something sensible rather than "".
    """
    try:
        path = urlparse(url).path
    except ValueError:
        return fallback
    tail = path.rsplit("/", 1)[-1]
    if tail and tail.lower().endswith(".stl"):
        return tail
    return fallback


def _session_id(ctx: Context | None) -> str:
    """Resolve the active session id for this tool invocation.

    Lookup order (first match wins):
      1. `X-Session-Id` HTTP request header — lets a client (Atlas, the e2e
         harness, a curl-based MCP caller) bind the MCP call to the *same*
         session id the browser iframe uses for its REST calls. This is the
         mechanism `open_viewer` relies on: the tool returns a URL carrying
         `?session=<mcp_session_id>` and the frontend echoes it back on
         every REST call, so tools and UI end up in the same PrinterService.
      2. `ctx.session_id` — FastMCP's auto-minted streamable-HTTP session id.
         Always available once the MCP handshake completes.
      3. `DEFAULT_SESSION_ID` — in-process tool calls (tests, stdio clients)
         have no HTTP transport, so both of the above are unavailable.

    Never raises; falls through on any error.
    """
    try:
        headers = get_http_headers()
    except Exception:
        headers = {}
    explicit = headers.get("x-session-id") if headers else None
    if explicit:
        return explicit
    if ctx is None:
        return DEFAULT_SESSION_ID
    try:
        sid = ctx.session_id
    except (RuntimeError, AttributeError):
        return DEFAULT_SESSION_ID
    return sid or DEFAULT_SESSION_ID


def _svc(ctx: Context | None) -> PrinterService:
    return get_service(_session_id(ctx))


def build_mcp() -> FastMCP:
    mcp = FastMCP(
        name="3dprintsim",
        instructions=(
            "Tools for driving a virtual FDM 3D printer. Typical flow: "
            "set_bed_size → upload_stl (one or more) → auto_arrange → "
            "slice_all → start_simulation → step_simulation until finished. "
            "Call open_viewer early to pop the live 3D canvas into the Atlas "
            "UI so the user can watch the print. Use get_printer_state at "
            "any time to inspect."
        ),
    )

    @mcp.tool
    def get_printer_state(ctx: Context) -> dict:
        """Return bed size, loaded parts, slice summary, and simulation cursor."""
        return _svc(ctx).get_state()

    @mcp.tool
    def set_bed_size(x_mm: float, y_mm: float, z_mm: float, ctx: Context) -> dict:
        """Resize the virtual print bed. Defaults to Prusa i3-style 250x210x210mm."""
        return _svc(ctx).set_bed_size(x_mm, y_mm, z_mm)

    @mcp.tool
    def atlas_upload(
        filename: str,
        ctx: Context,
        name: str = "",
        scale: float = 1.0,
    ) -> dict:
        """Upload an STL that the Atlas host has already stashed in its file
        vault. `filename` is the secure download URL supplied by Atlas — it
        may be an absolute URL or a relative path (e.g.
        `/mcp/files/download/abc123?token=xyz`); relative paths are resolved
        against `ATLAS_BASE_URL` (Atlas's public origin), or
        `BACKEND_PUBLIC_URL` if the former is unset.

        Use this when the user drops an STL into the chat rather than asking
        the model to base64-encode the file inline (which blows up both the
        tool-call size and the context window for real-world parts).

        `name` overrides the display name; otherwise it's derived from the
        URL path. `scale` works the same as upload_stl (25.4 for inches,
        0.001 for metres, etc).
        """
        if not filename:
            raise ValueError("filename (Atlas download URL) is required")

        url = _normalize_atlas_url(filename)
        # Guard against SSRF: enforce http/https, allowlisted hosts, and
        # reject hosts that resolve to private/loopback/reserved IPs. Must
        # run *before* the HTTP client opens a connection.
        _reject_ssrf(url)

        try:
            # follow_redirects=False keeps a 30x from bouncing the fetch to
            # an internal host after the allowlist check has already passed.
            with httpx.Client(timeout=_ATLAS_FETCH_TIMEOUT, follow_redirects=False) as client:
                with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in resp.iter_bytes():
                        total += len(chunk)
                        if total > _ATLAS_MAX_BYTES:
                            raise ValueError(
                                f"Atlas file exceeds {_ATLAS_MAX_BYTES // (1024 * 1024)} MiB cap"
                            )
                        chunks.append(chunk)
                    data = b"".join(chunks)
        except httpx.HTTPStatusError as exc:
            raise ValueError(
                f"Atlas download failed: HTTP {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            raise ValueError(f"Atlas download failed: {exc}") from exc

        display_name = name.strip() if name and name.strip() else _atlas_display_name(url)
        part = _svc(ctx).add_part_from_bytes(display_name, data, scale=scale)
        return part.to_public()

    @mcp.tool
    def upload_stl(name: str, stl_base64: str, ctx: Context, scale: float = 1.0) -> dict:
        """Upload an STL file as base64 bytes. Returns the new part's metadata.

        `scale` is a linear multiplier applied to every vertex at import — pass
        25.4 for an STL authored in inches, 0.001 for metres, etc.
        """
        part = _svc(ctx).add_part_from_base64(name, stl_base64, scale=scale)
        return part.to_public()

    @mcp.tool
    def set_part_scale(part_id: str, scale: float, ctx: Context) -> dict:
        """Resize a loaded part by a linear scale factor (e.g. 2.0 doubles every dimension)."""
        part = _svc(ctx).set_part_scale(part_id, scale)
        return part.to_public()

    @mcp.tool
    def list_parts(ctx: Context) -> list[dict]:
        """List all loaded parts."""
        return [p.to_public() for p in _svc(ctx).parts.values()]

    @mcp.tool
    def remove_part(part_id: str, ctx: Context) -> dict:
        """Remove a part by id."""
        _svc(ctx).remove_part(part_id)
        return {"ok": True, "removed": part_id}

    @mcp.tool
    def clear_parts(ctx: Context) -> dict:
        """Remove all parts from the bed."""
        _svc(ctx).clear_parts()
        return {"ok": True}

    @mcp.tool
    def auto_arrange(ctx: Context) -> list[dict]:
        """Pack all loaded parts onto the bed using a shelf packer. Returns placements."""
        placements = _svc(ctx).auto_arrange()
        return [
            {"part_id": p.part_id, "x": p.x, "y": p.y, "rotation_deg": p.rotation_deg}
            for p in placements
        ]

    @mcp.tool
    def slice_all(
        ctx: Context,
        layer_height_mm: float = 0.4,
        perimeters: int = 1,
        infill_density: float = 0.2,
        top_layers: int = 3,
        bottom_layers: int = 3,
        nozzle_width_mm: float = 0.4,
        support_density: float = 0.25,
    ) -> dict:
        """Slice every loaded part and return a summary.

        infill_density is 0..1 (fraction of the layer filled with sparse infill).
        Top/bottom/overhang detection is raster-based: the first bottom_layers
        and last top_layers of each part get solid infill, and any intermediate
        layer with an overhang or hollow above/below also becomes solid.
        support_density (0..1) controls how dense the auto-generated support
        columns are — set to 0 to disable supports entirely.
        """
        result = _svc(ctx).slice_all(
            layer_height=layer_height_mm,
            perimeters=perimeters,
            infill_density=infill_density,
            top_layers=top_layers,
            bottom_layers=bottom_layers,
            nozzle_width=nozzle_width_mm,
            support_density=support_density,
        )
        return result.summary()

    @mcp.tool
    def get_gcode(ctx: Context) -> str:
        """Return the latest slice's G-code as a string."""
        svc = _svc(ctx)
        if svc.slice_result is None:
            raise ValueError("slice first")
        return svc.slice_result.gcode

    @mcp.tool
    def start_simulation(ctx: Context, speed: float = 1.0) -> dict:
        """Start/reset the simulation cursor at 0. Returns simulation state."""
        sim = _svc(ctx).start_simulation(speed=speed)
        return {
            "running": sim.running,
            "cursor": sim.cursor,
            "speed": sim.speed,
        }

    @mcp.tool
    def step_simulation(ctx: Context, steps: int = 1) -> dict:
        """Advance the simulation cursor by N moves."""
        svc = _svc(ctx)
        sim = svc.step_simulation(steps=steps)
        total = len(svc.slice_result.moves) if svc.slice_result else 0
        return {
            "running": sim.running,
            "cursor": sim.cursor,
            "total_moves": total,
        }

    @mcp.tool
    def set_simulation_cursor(cursor: int, ctx: Context) -> dict:
        """Jump the simulation cursor to a specific move index."""
        svc = _svc(ctx)
        sim = svc.set_simulation_cursor(cursor)
        total = len(svc.slice_result.moves) if svc.slice_result else 0
        return {
            "running": sim.running,
            "cursor": sim.cursor,
            "total_moves": total,
        }

    @mcp.tool
    def get_simulation_frame(ctx: Context) -> dict:
        """Return the current head position and extruded moves-so-far."""
        return _svc(ctx).get_simulation_frame()

    @mcp.tool
    def focus_viewer(ctx: Context) -> dict:
        """Ask the browser UI to reframe its camera so the loaded parts fill
        ~90% of the viewport. The browser polls for this request, so the
        effect is visible in a running UI session within a couple of seconds.
        """
        return {"focus_request": _svc(ctx).request_focus()}

    @mcp.tool
    def open_viewer(ctx: Context, title: str = "3D Print Simulator") -> dict:
        """Open the live 3D printer viewer in the Atlas canvas panel.

        Returns an Atlas v2 tool-output envelope with `display.type = "iframe"`
        so Atlas opens the viewer in its side canvas. The iframe URL carries
        this MCP session's id as `?session=<id>`, so subsequent uploads,
        slices, and simulation steps made through MCP tools appear live in
        the embedded viewer — and anything the user does in the viewer
        (drag-drop STL, camera tweaks) is visible to the LLM on its next
        get_printer_state call.

        Call this once at the start of a session. Subsequent calls are safe
        (same URL) but redundant.
        """
        sid = _session_id(ctx)
        base = _viewer_public_url().rstrip("/")
        # `embed=1` tells the frontend to hide the standalone chrome and
        # render in iframe-friendly mode; `session=<sid>` wires every REST
        # call from that tab back to this MCP session's PrinterService.
        url = f"{base}/?embed=1&session={sid}"
        return {
            "results": {
                "content": (
                    "Live printer viewer opened in the canvas. The user can "
                    "watch slicing + simulation in real time as you drive it."
                ),
                "session_id": sid,
                "url": url,
            },
            "artifacts": [],
            "display": {
                "open_canvas": True,
                "type": "iframe",
                "url": url,
                "title": title,
                # allow-same-origin lets the iframe's fetch() calls send the
                # X-Session-Id header to /api/* on the same origin; allow-scripts
                # is required for the React app to boot.
                "sandbox": "allow-scripts allow-same-origin allow-downloads",
                "mode": "replace",
            },
        }

    return mcp
