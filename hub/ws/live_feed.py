"""
WebSocket Live Feed

Endpoint: /ws/live?token=<jwt>

Pushes real-time events to connected dashboard clients:
  - tick:     BTC price update
  - trade:    New trade placed or resolved
  - signal:   VPIN / cascade / arb signal
  - cascade:  Cascade state machine transition
  - system:   Kill switch, paper mode, connectivity changes

Event format (JSON):
  {
    "type": "tick" | "trade" | "signal" | "cascade" | "system",
    "data": { ... event-specific payload ... },
    "ts":   "2024-01-01T00:00:00Z"
  }

Authentication:
  Token must be a valid access JWT passed as a query parameter.
  Connections with invalid tokens are closed immediately (code 4001).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Optional, Set

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query

from auth.jwt import verify_token

log = structlog.get_logger(__name__)
router = APIRouter()


class ConnectionManager:
    """Manages all active WebSocket connections and broadcasts events."""

    def __init__(self) -> None:
        self._connections: Set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.add(ws)
        log.info("ws.connected", total=len(self._connections))

    def disconnect(self, ws: WebSocket) -> None:
        self._connections.discard(ws)
        log.info("ws.disconnected", total=len(self._connections))

    async def broadcast(self, event_type: str, data: dict) -> None:
        """Broadcast a typed event to all connected clients."""
        payload = json.dumps({
            "type": event_type,
            "data": data,
            "ts": datetime.now(timezone.utc).isoformat(),
        })

        dead: list[WebSocket] = []
        for ws in list(self._connections):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)

        for ws in dead:
            self.disconnect(ws)

    async def send_to(self, ws: WebSocket, event_type: str, data: dict) -> None:
        """Send an event to a single client."""
        payload = json.dumps({
            "type": event_type,
            "data": data,
            "ts": datetime.now(timezone.utc).isoformat(),
        })
        await ws.send_text(payload)

    @property
    def connection_count(self) -> int:
        return len(self._connections)


# Global connection manager (singleton per process)
manager = ConnectionManager()


@router.websocket("/ws/live")
async def websocket_live_feed(
    websocket: WebSocket,
    token: Optional[str] = Query(None),
) -> None:
    """
    Real-time event stream for the dashboard.

    Authentication:
      Pass ?token=<jwt> as a query parameter.
      Connection is rejected (code 4001) if token is invalid.
    """
    # Authenticate before accepting
    if not token:
        await websocket.close(code=4001, reason="Missing token")
        return

    token_data = verify_token(token, expected_type="access")
    if token_data is None:
        await websocket.close(code=4001, reason="Invalid or expired token")
        return

    await manager.connect(websocket)

    try:
        # Send initial connection acknowledgement
        await manager.send_to(websocket, "system", {
            "message": "connected",
            "user": token_data.username,
        })

        # Keep connection alive — receive pings, handle disconnects
        while True:
            try:
                # Wait for client messages (pings, close frames)
                message = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)

                # Echo pings back as pongs
                if message == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))

            except asyncio.TimeoutError:
                # Send keepalive ping to detect dead connections
                await websocket.send_text(json.dumps({"type": "ping"}))

    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(websocket)
