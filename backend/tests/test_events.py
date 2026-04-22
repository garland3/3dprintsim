"""Tests for the /api/events SSE stream.

The SSE stream is how MCP-driven mutations propagate to the browser UI in
real time: whenever a PrinterService mutation bumps `state_revision`, the
EventBroker publishes to every subscriber registered for that session id.
These tests cover the publish fan-out, session isolation, and the raw HTTP
framing a browser's EventSource parser expects.
"""

from __future__ import annotations

import asyncio
import json
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.state import EventBroker, get_broker, get_service, reset_service

from .fixtures import make_binary_cube_stl


@pytest.fixture
def client():
    reset_service()
    app = create_app()
    with TestClient(app) as c:
        yield c


# --- broker unit tests -----------------------------------------------------


def test_broker_delivers_to_same_session_only():
    """Publish fan-out is strictly scoped: a subscriber in session A must
    never see events published to session B. Otherwise an LLM driving
    Alice's printer would nudge Bob's UI."""

    async def run():
        broker = EventBroker()
        loop = asyncio.get_running_loop()
        alice = broker.subscribe("alice", loop)
        bob = broker.subscribe("bob", loop)
        # Publish to alice only.
        broker.publish("alice", {"type": "state", "state_revision": 7})
        # Give call_soon_threadsafe a tick to deliver.
        await asyncio.sleep(0)
        # Alice sees it; Bob's queue is empty.
        assert alice.queue.get_nowait() == {"type": "state", "state_revision": 7}
        assert bob.queue.empty()
        broker.unsubscribe("alice", alice)
        broker.unsubscribe("bob", bob)

    asyncio.run(run())


def test_broker_publish_is_thread_safe():
    """PrinterService mutations run on FastAPI's threadpool; the broker
    must hop onto the subscriber's event loop via call_soon_threadsafe so
    asyncio.Queue.put_nowait isn't called from the wrong thread."""

    async def run():
        broker = EventBroker()
        loop = asyncio.get_running_loop()
        sub = broker.subscribe("t", loop)

        def worker():
            for i in range(5):
                broker.publish("t", {"type": "state", "state_revision": i})

        t = threading.Thread(target=worker)
        t.start()
        received: list[int] = []
        # Drain five events.
        for _ in range(5):
            evt = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
            received.append(evt["state_revision"])
        t.join(timeout=1.0)
        assert received == [0, 1, 2, 3, 4]
        broker.unsubscribe("t", sub)

    asyncio.run(run())


def test_broker_drops_on_full_queue():
    """A slow subscriber must not block the publisher — once the queue is
    at its cap, newer events are dropped for that subscriber rather than
    stalling the mutation path."""

    async def run():
        broker = EventBroker()
        loop = asyncio.get_running_loop()
        sub = broker.subscribe("t", loop)
        # Fill past the cap. _SSE_QUEUE_MAXSIZE = 64, so 200 is comfortably over.
        for i in range(200):
            broker.publish("t", {"type": "state", "state_revision": i})
        # Let every call_soon_threadsafe run.
        await asyncio.sleep(0.05)
        # Queue capped at its max — excess events silently dropped.
        assert sub.queue.qsize() <= 64
        broker.unsubscribe("t", sub)

    asyncio.run(run())


def test_broker_unsubscribe_stops_delivery():
    async def run():
        broker = EventBroker()
        loop = asyncio.get_running_loop()
        sub = broker.subscribe("t", loop)
        broker.unsubscribe("t", sub)
        # Publish after unsubscribe — nothing in the queue.
        broker.publish("t", {"type": "state", "state_revision": 1})
        await asyncio.sleep(0)
        assert sub.queue.empty()
        assert "t" not in broker.active_sessions()

    asyncio.run(run())


# --- wiring: PrinterService mutations → broker -----------------------------


def test_printer_service_publishes_on_mutation():
    """Every _bump_revision() must fan out to the broker, keyed by the
    service's own session id. That's the contract the SSE endpoint relies
    on: the handler subscribes and expects state events without any
    additional wiring."""
    reset_service()
    broker = get_broker()
    svc = get_service("pubsub-test")

    async def run():
        loop = asyncio.get_running_loop()
        sub = broker.subscribe("pubsub-test", loop)
        try:
            svc.set_bed_size(200, 200, 200)
            # Threaded publish → give the loop a beat.
            evt = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
            assert evt["type"] == "state"
            assert evt["state_revision"] > 0
        finally:
            broker.unsubscribe("pubsub-test", sub)

    asyncio.run(run())


def test_focus_request_publishes_focus_event():
    reset_service()
    broker = get_broker()
    svc = get_service("focus-test")

    async def run():
        loop = asyncio.get_running_loop()
        sub = broker.subscribe("focus-test", loop)
        try:
            svc.request_focus()
            evt = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
            assert evt["type"] == "focus"
            assert evt["focus_request"] == 1
        finally:
            broker.unsubscribe("focus-test", sub)

    asyncio.run(run())


# --- HTTP: /api/events framing ---------------------------------------------


def test_sse_frame_format():
    """Directly exercise the SSE frame serializer so its wire format stays
    stable. EventSource parsers reject frames that don't terminate with a
    blank line, so the serializer MUST always emit `\\n\\n`."""
    from app.main import _sse_format

    frame = _sse_format("state", {"state_revision": 3})
    assert frame.startswith("event: state\n")
    assert frame.endswith("\n\n")
    # Payload is JSON on a single `data:` line.
    lines = frame.split("\n")
    data_lines = [l for l in lines if l.startswith("data:")]
    assert len(data_lines) == 1
    payload = json.loads(data_lines[0].removeprefix("data:").strip())
    assert payload == {"state_revision": 3}


# NOTE: the wire-level /api/events HTTP behaviour (reading the actual SSE
# frames from a live stream) is covered by the Playwright e2e spec at
# tests/e2e/sse.spec.js — a real HTTP client against a real uvicorn server.
# Unit-testing it here with httpx.ASGITransport or starlette.TestClient would
# require babysitting a long-lived stream from a sync test harness, which
# always ends up racy; the broker unit tests above already cover the
# semantics we care about (publish scoping, thread-safety, drop-on-full).


def test_events_rejects_bad_session_id(client: TestClient):
    """Charset validation still applies to the SSE endpoint — otherwise a
    hostile client could smuggle a path-style id into the registry."""
    r = client.get("/api/events?session=bad%2Fid", headers={}, follow_redirects=False)
    assert r.status_code == 400
