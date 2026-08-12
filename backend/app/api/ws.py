"""
Furi OS — WebSocket API (Phase 4, Part 1)

The /ws endpoint frontends connect to for server-initiated messages. The
channel is strictly server→client: inbound frames are read only to keep the
connection alive and detect disconnects — they are never interpreted as
commands (actions go through the normal HTTP API and its approval gates).

POST /ws/test is a local-only dev utility: it broadcasts a `test` event so
the channel can be exercised end-to-end without waiting for a real
proactive feature (reminders arrive in Part 4).
"""
from typing import Any

from fastapi import APIRouter, Body, WebSocket, WebSocketDisconnect
from loguru import logger

from app.core.push import push_manager

router = APIRouter()


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """Register the client and hold the connection open until it closes."""
    await push_manager.connect(websocket)
    # Hello frame: confirms to the client that the channel is live (and gives
    # reconnect logic a deterministic first message to observe).
    try:
        from app.core.push import envelope
        await websocket.send_json(envelope("connected"))
        while True:
            # Server→client channel: inbound text is intentionally ignored.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning(f"Push channel connection errored: {e}")
    finally:
        await push_manager.disconnect(websocket)


@router.post("/ws/test", summary="Broadcast a test push event (dev utility)")
async def push_test(payload: dict[str, Any] = Body(default={})) -> dict:
    """Broadcast the given payload as a `test` event to every connected
    client. Returns how many clients received it."""
    delivered = await push_manager.push("test", payload)
    return {"delivered": delivered, "connections": push_manager.connection_count}
