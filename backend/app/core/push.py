"""
Jarvis OS — Push Channel (Phase 4, Part 1)

The server→client message channel: anything in the backend can call
`push(type, payload)` and every connected frontend window receives the event
over the /ws WebSocket. This is what lets Jarvis speak FIRST — reminders,
task completions, approval requests — without a request asking for it.

Design rules:
- One typed envelope for every event: {"type": ..., "payload": {...}, "ts": ...}.
  Later parts only ever ADD event types; the frontend ignores types it does
  not know, so old frontends never break on new events.
- push() NEVER raises and never blocks on a broken client: a socket that
  fails to send is pruned and the rest still receive the event. Proactive
  features must never die because a window closed mid-send.
- Everything runs on the backend's single asyncio loop — background tasks,
  the scheduler (Part 2), and request handlers can all just `await push(...)`.
- The manager holds live sockets only. There is no queue and no persistence
  here: an event pushed while no window is connected is simply not delivered.
  Features that must survive a closed window persist their OWN state (e.g.
  reminders in SQLite) and use the push channel as best-effort delivery.
"""
import asyncio
from datetime import datetime, timezone
from typing import Any, Optional

from loguru import logger
from pydantic import BaseModel, Field


class PushEvent(BaseModel):
    """The envelope every pushed message uses. `type` routes the event in the
    frontend dispatcher; `payload` is type-specific data."""

    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    ts: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


def envelope(event_type: str, payload: Optional[dict[str, Any]] = None) -> dict:
    """A ready-to-send event dict (JSON-safe via pydantic serialization)."""
    return PushEvent(type=event_type, payload=payload or {}).model_dump(mode="json")


class PushManager:
    """Registry of live WebSocket connections + broadcast. One global
    instance (`push_manager`); tests may construct their own."""

    def __init__(self) -> None:
        self._connections: set = set()
        self._lock = asyncio.Lock()

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    async def connect(self, websocket) -> None:
        """Accept and register a client socket."""
        await websocket.accept()
        async with self._lock:
            self._connections.add(websocket)
        logger.info(f"Push channel: client connected ({self.connection_count} live)")

    async def disconnect(self, websocket) -> None:
        """Deregister a socket. Safe to call twice."""
        async with self._lock:
            self._connections.discard(websocket)

    async def push(
        self, event_type: str, payload: Optional[dict[str, Any]] = None
    ) -> int:
        """Broadcast one event to every connected client. Returns how many
        clients actually received it. Never raises: a failing socket is
        pruned and the remaining clients still get the event."""
        try:
            message = envelope(event_type, payload)
        except Exception as e:  # unserializable payload — a programming error,
            logger.warning(f"Push event '{event_type}' could not be serialized: {e}")
            return 0  # but proactive callers must never crash on it

        async with self._lock:
            targets = list(self._connections)

        delivered = 0
        dead = []
        for websocket in targets:
            try:
                await websocket.send_json(message)
                delivered += 1
            except Exception:
                dead.append(websocket)

        for websocket in dead:
            await self.disconnect(websocket)
        if dead:
            logger.info(
                f"Push channel: pruned {len(dead)} dead connection(s) "
                f"({self.connection_count} live)"
            )
        return delivered


# The global push channel. Business code imports `push` and calls it.
push_manager = PushManager()


async def push(event_type: str, payload: Optional[dict[str, Any]] = None) -> int:
    """Module-level convenience: broadcast on the global channel."""
    return await push_manager.push(event_type, payload)
