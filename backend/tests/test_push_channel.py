"""
Phase 4 Part 1 — Push channel.

Unit tests exercise PushManager directly with fake sockets (broadcast,
pruning, never-raises). Integration tests run the real /ws endpoint through
starlette's in-process TestClient — no network, no lifespan (the push
channel is deliberately DB-free).
"""
import time

import pytest
from starlette.testclient import TestClient

from app.core.push import PushManager, envelope, push_manager
from main import app


class FakeSocket:
    """Minimal stand-in for a starlette WebSocket."""

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[dict] = []
        self.fail = fail
        self.accepted = False

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, data: dict) -> None:
        if self.fail:
            raise RuntimeError("dead socket")
        self.sent.append(data)


@pytest.fixture(autouse=True)
def _clean_global_manager():
    """Integration tests register real sockets on the global manager —
    never let one test's connections leak into the next."""
    push_manager._connections.clear()
    yield
    push_manager._connections.clear()


# ================================================================ unit tests

async def test_envelope_shape():
    event = envelope("reminder", {"text": "call Jamil"})
    assert event["type"] == "reminder"
    assert event["payload"] == {"text": "call Jamil"}
    assert "ts" in event


async def test_push_delivers_to_all_connections():
    mgr = PushManager()
    a, b = FakeSocket(), FakeSocket()
    await mgr.connect(a)
    await mgr.connect(b)
    assert a.accepted and b.accepted

    delivered = await mgr.push("test", {"x": 1})

    assert delivered == 2
    assert a.sent[0]["type"] == "test"
    assert a.sent[0]["payload"] == {"x": 1}
    assert b.sent[0] == a.sent[0] | {"ts": b.sent[0]["ts"]}


async def test_push_with_no_connections_delivers_zero():
    mgr = PushManager()
    assert await mgr.push("test") == 0


async def test_default_payload_is_empty_dict():
    mgr = PushManager()
    sock = FakeSocket()
    await mgr.connect(sock)
    await mgr.push("connected")
    assert sock.sent[0]["payload"] == {}


async def test_dead_connection_is_pruned_and_others_still_receive():
    mgr = PushManager()
    dead, live = FakeSocket(fail=True), FakeSocket()
    await mgr.connect(dead)
    await mgr.connect(live)

    delivered = await mgr.push("test")

    assert delivered == 1
    assert live.sent[0]["type"] == "test"
    assert mgr.connection_count == 1  # the dead socket is gone
    assert await mgr.push("again") == 1  # and never retried


async def test_disconnect_is_idempotent():
    mgr = PushManager()
    sock = FakeSocket()
    await mgr.connect(sock)
    await mgr.disconnect(sock)
    await mgr.disconnect(sock)  # second call must not raise
    assert mgr.connection_count == 0


async def test_push_never_raises_on_unserializable_payload():
    mgr = PushManager()
    sock = FakeSocket()
    await mgr.connect(sock)
    delivered = await mgr.push("test", {"bad": object()})
    assert delivered == 0  # dropped, logged — the caller survives
    assert mgr.connection_count == 1  # the connection is NOT punished


# ========================================================= integration tests

def test_ws_endpoint_sends_hello_and_receives_broadcasts():
    client = TestClient(app)
    with client.websocket_connect("/ws") as websocket:
        hello = websocket.receive_json()
        assert hello["type"] == "connected"

        response = client.post("/ws/test", json={"message": "hi"})
        assert response.status_code == 200
        assert response.json()["delivered"] == 1

        event = websocket.receive_json()
        assert event["type"] == "test"
        assert event["payload"] == {"message": "hi"}


def test_ws_two_clients_both_receive():
    client = TestClient(app)
    with client.websocket_connect("/ws") as ws_a, \
            client.websocket_connect("/ws") as ws_b:
        ws_a.receive_json()  # hello frames
        ws_b.receive_json()

        response = client.post("/ws/test", json={"n": 2})
        assert response.json()["delivered"] == 2

        assert ws_a.receive_json()["payload"] == {"n": 2}
        assert ws_b.receive_json()["payload"] == {"n": 2}


def test_ws_disconnect_deregisters_the_client():
    client = TestClient(app)
    with client.websocket_connect("/ws") as websocket:
        websocket.receive_json()
        assert push_manager.connection_count == 1

    # The server handler notices the close asynchronously — give it a moment.
    deadline = time.time() + 2
    while push_manager.connection_count and time.time() < deadline:
        time.sleep(0.01)
    assert push_manager.connection_count == 0

    response = client.post("/ws/test", json={})
    assert response.json()["delivered"] == 0


def test_ws_inbound_text_is_ignored_not_interpreted():
    """The channel is server→client only: sending text must neither crash
    the connection nor produce a response frame."""
    client = TestClient(app)
    with client.websocket_connect("/ws") as websocket:
        websocket.receive_json()
        websocket.send_text('{"type": "evil", "command": "delete everything"}')

        # The connection is still alive and still receives broadcasts.
        response = client.post("/ws/test", json={"still": "alive"})
        assert response.json()["delivered"] == 1
        assert websocket.receive_json()["payload"] == {"still": "alive"}
