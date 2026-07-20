from __future__ import annotations

import json
import logging
from typing import Any

import websocket

logger = logging.getLogger(__name__)


class DDPError(Exception):
    """Raised for DDP connection / framing failures."""


class DDPConnection:
    """Thin synchronous wrapper around a DDP-over-websocket connection.

    This class knows nothing about Rocket.Chat semantics (login, subscriptions,
    etc.) — it only opens the socket and sends/receives DDP JSON frames.
    Callers are responsible for the DDP handshake and any protocol logic on
    top (see rc_client.py).
    """

    def __init__(self, url: str, sslopt: dict | None = None, timeout: float = 10.0):
        self._url = url
        self._sslopt = sslopt
        self._timeout = timeout
        self._ws: websocket.WebSocket | None = None

    def connect(self) -> None:
        try:
            self._ws = websocket.create_connection(
                self._url, timeout=self._timeout, sslopt=self._sslopt
            )
        except (OSError, websocket.WebSocketException) as exc:
            raise DDPError(f"Verbindung zu {self._url} fehlgeschlagen: {exc}") from exc

    def send_json(self, obj: dict[str, Any]) -> None:
        if self._ws is None:
            raise DDPError("Nicht verbunden")
        try:
            self._ws.send(json.dumps(obj))
        except (OSError, websocket.WebSocketException) as exc:
            raise DDPError(f"Senden fehlgeschlagen: {exc}") from exc

    def recv_json(self, timeout: float | None = None) -> dict[str, Any] | None:
        """Receive one DDP frame, or None if the read timed out (callers
        should treat a None return as "no message yet", not an error)."""
        if self._ws is None:
            raise DDPError("Nicht verbunden")
        if timeout is not None:
            self._ws.settimeout(timeout)
        try:
            raw = self._ws.recv()
        except websocket.WebSocketTimeoutException:
            return None
        except (OSError, websocket.WebSocketException) as exc:
            raise DDPError(f"Empfang fehlgeschlagen: {exc}") from exc
        if not raw:
            raise DDPError("Verbindung wurde vom Server geschlossen")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DDPError(f"Ungueltiger DDP-Frame: {raw!r}") from exc

    def close(self) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            except (OSError, websocket.WebSocketException):
                pass
            self._ws = None
