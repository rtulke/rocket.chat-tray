from __future__ import annotations

import logging
import random
import threading
import time

from PySide6.QtCore import QThread, Signal

from . import auth
from .ddp import DDPConnection, DDPError

logger = logging.getLogger(__name__)

IDLE_TIMEOUT_SECONDS = 45.0
BACKOFF_BASE_SECONDS = 2.0
BACKOFF_CAP_SECONDS = 60.0
AUTH_RETRY_SECONDS = 300.0  # safety-net retry interval while in the auth-error state
EXPECT_TIMEOUT_SECONDS = 15.0

# Rocket.Chat's stream-notify-logged "user-status" event payload encodes
# presence as a small int rather than a string.
STATUS_CODE_MAP = {0: "offline", 1: "online", 2: "away", 3: "busy"}


class _AuthFailure(Exception):
    """Raised internally when the server rejects the stored credentials."""


class RocketChatWorker(QThread):
    """Owns the Rocket.Chat realtime (DDP) connection on a background thread.
    Only ever communicates with the GUI thread via Qt signals — callers must
    connect these directly to real QObject bound methods (not plain
    functions/lambdas) so Qt's automatic queued-connection mechanism keeps
    all GUI mutation on the main thread."""

    connecting = Signal()
    connected = Signal(str)  # user_id
    disconnected = Signal(str)  # reason
    auth_failed = Signal(str)  # message
    notification_received = Signal(str, str, str, str)  # rid, title, text, room_type
    reconnect_scheduled = Signal(float)  # seconds
    presence_status_changed = Signal(str)  # online/away/busy/offline — our own, server-confirmed

    def __init__(self, server_url: str, verify_ssl: bool, password_provider, parent=None):
        super().__init__(parent)
        self._server_url = server_url
        self._verify_ssl = verify_ssl
        # Callable returning the current password, kept out of the worker's
        # own state so a password update via the login dialog is picked up
        # on the very next (re)connect attempt without restarting the thread.
        self._password_provider = password_provider
        self._stop_requested = False
        self._reconfigure_requested = False
        self._wake_event = threading.Event()
        self._backoff = BACKOFF_BASE_SECONDS

        # Set while a session is fully connected; read by deeplink resolution
        # from the GUI thread. Plain string assignment is atomic in CPython,
        # so no lock is needed for this read-mostly, best-effort use.
        self.current_auth_token: str | None = None
        self.current_user_id: str | None = None

    def stop(self) -> None:
        self._stop_requested = True
        self._wake_event.set()

    def retry_now(self) -> None:
        """Wake the worker immediately, e.g. after the user re-enters their
        password following an auth failure."""
        self._wake_event.set()

    def update_server(self, server_url: str, verify_ssl: bool) -> None:
        """Point the worker at a different server and force an immediate
        reconnect using it — even if a session is currently active, unlike
        retry_now() which only wakes a *waiting* worker."""
        self._server_url = server_url
        self._verify_ssl = verify_ssl
        self._reconfigure_requested = True
        self._wake_event.set()

    def run(self) -> None:
        while not self._stop_requested:
            self.connecting.emit()
            try:
                self._run_one_session()
            except _AuthFailure as exc:
                self.auth_failed.emit(str(exc))
                if self._sleep_interruptible(AUTH_RETRY_SECONDS):
                    return
                continue
            except DDPError as exc:
                self.disconnected.emit(str(exc))
            except Exception as exc:  # noqa: BLE001 - last-resort guard for a background thread
                logger.exception("Unerwarteter Fehler im Rocket.Chat-Worker")
                self.disconnected.emit(str(exc))

            if self._stop_requested:
                return

            if self._reconfigure_requested:
                # A new server URL was set (possibly while this very session
                # was active — see the _read_loop() exit condition below):
                # retry immediately with it rather than waiting out the
                # normal backoff, which exists for transient network
                # failures, not deliberate user-triggered reconfiguration.
                self._reconfigure_requested = False
                self._backoff = BACKOFF_BASE_SECONDS
                continue

            delay = random.uniform(0, min(BACKOFF_CAP_SECONDS, self._backoff))
            self._backoff = min(BACKOFF_CAP_SECONDS, self._backoff * 2)
            self.reconnect_scheduled.emit(delay)
            if self._sleep_interruptible(delay):
                return

    def _sleep_interruptible(self, seconds: float) -> bool:
        """Sleep up to `seconds`, waking early if stop()/retry_now() is
        called. Returns True if the caller should stop the run loop."""
        self._wake_event.wait(timeout=seconds)
        self._wake_event.clear()
        return self._stop_requested

    def _run_one_session(self) -> None:
        password = self._password_provider()
        if not password:
            raise _AuthFailure("Kein Passwort hinterlegt")

        try:
            auth_token, user_id = auth.rest_login(
                self._server_url, auth.current_username(), password, self._verify_ssl
            )
        except auth.LoginError as exc:
            if str(exc) == "invalid_credentials":
                raise _AuthFailure("Benutzername oder Passwort falsch") from exc
            raise DDPError(str(exc)) from exc

        ws_url = self._server_url.replace("https://", "wss://").replace("http://", "ws://") + "/websocket"
        sslopt = {"cert_reqs": 0} if not self._verify_ssl else None
        conn = DDPConnection(ws_url, sslopt=sslopt)
        conn.connect()
        try:
            self._handshake(conn)
            self._ddp_login(conn, auth_token)
            self._subscribe_notifications(conn, user_id)
            self._subscribe_user_status(conn)

            self._backoff = BACKOFF_BASE_SECONDS  # reset only after a fully successful session
            self.current_auth_token = auth_token
            self.current_user_id = user_id
            self.connected.emit(user_id)
            self._read_loop(conn, user_id)
        finally:
            self.current_auth_token = None
            self.current_user_id = None
            conn.close()

    def _handshake(self, conn: DDPConnection) -> None:
        conn.send_json({"msg": "connect", "version": "1", "support": ["1"]})
        msg = self._expect(conn, {"connected", "failed"})
        if msg["msg"] == "failed":
            raise DDPError(f"DDP-Handshake abgelehnt: {msg}")

    def _ddp_login(self, conn: DDPConnection, auth_token: str) -> None:
        conn.send_json(
            {"msg": "method", "method": "login", "id": "login-1", "params": [{"resume": auth_token}]}
        )
        msg = self._expect(conn, {"result"})
        if msg.get("error"):
            raise _AuthFailure(f"DDP-Resume-Login fehlgeschlagen: {msg['error']}")

    def _subscribe_notifications(self, conn: DDPConnection, user_id: str) -> None:
        conn.send_json(
            {
                "msg": "sub",
                "id": "sub-notify",
                "name": "stream-notify-user",
                "params": [f"{user_id}/notification", False],
            }
        )
        while True:
            msg = self._expect(conn, {"ready", "nosub"})
            if msg["msg"] == "nosub":
                raise DDPError(f"Subscription abgelehnt: {msg}")
            if "sub-notify" in msg.get("subs", []):
                return

    def _subscribe_user_status(self, conn: DDPConnection) -> None:
        # Broadcasts every logged-in user's presence changes (not just ours)
        # — filtered by user id in _handle_user_status_event. This is what
        # keeps the tray icon's colour in sync with status changes made
        # anywhere (Rocket.Chat's own web/mobile clients included), not just
        # ones made through this app's own status menu.
        conn.send_json(
            {"msg": "sub", "id": "sub-user-status", "name": "stream-notify-logged", "params": ["user-status", False]}
        )
        while True:
            msg = self._expect(conn, {"ready", "nosub"})
            if msg["msg"] == "nosub":
                raise DDPError(f"Subscription abgelehnt: {msg}")
            if "sub-user-status" in msg.get("subs", []):
                return

    def _read_loop(self, conn: DDPConnection, own_user_id: str) -> None:
        last_activity = time.monotonic()
        while not self._stop_requested and not self._reconfigure_requested:
            remaining = IDLE_TIMEOUT_SECONDS - (time.monotonic() - last_activity)
            if remaining <= 0:
                raise DDPError("Keine Aktivitaet vom Server (Idle-Watchdog)")
            msg = conn.recv_json(timeout=min(5.0, remaining))
            if msg is None:
                continue
            last_activity = time.monotonic()

            if msg.get("msg") == "ping":
                conn.send_json({"msg": "pong"})
            elif msg.get("msg") == "changed" and msg.get("collection") == "stream-notify-user":
                self._handle_notification_event(msg)
            elif msg.get("msg") == "changed" and msg.get("collection") == "stream-notify-logged":
                self._handle_user_status_event(msg, own_user_id)

    def _handle_notification_event(self, msg: dict) -> None:
        fields = msg.get("fields", {})
        args = fields.get("args") or []
        if not args:
            return
        event = args[0]
        payload = event.get("payload", {})
        rid = payload.get("rid")
        if not rid:
            return
        self.notification_received.emit(
            rid, event.get("title", ""), event.get("text", ""), payload.get("type", "")
        )

    def _handle_user_status_event(self, msg: dict, own_user_id: str) -> None:
        # fields.args is a *list containing one* [user_id, username, status,
        # statusText] tuple, not the tuple itself -- confirmed via a live
        # capture against the real server (see project memory). Unwrap it
        # before reading fields.
        fields = msg.get("fields", {})
        if fields.get("eventName") != "user-status":
            return
        args = fields.get("args") or []
        if not args:
            return
        event = args[0]
        if len(event) < 3 or event[0] != own_user_id:
            return
        status = STATUS_CODE_MAP.get(event[2])
        if status:
            self.presence_status_changed.emit(status)

    def _expect(self, conn: DDPConnection, accepted_types: set[str]) -> dict:
        """Read frames until one whose msg is in accepted_types arrives,
        transparently answering any server pings encountered along the way."""
        while True:
            msg = conn.recv_json(timeout=EXPECT_TIMEOUT_SECONDS)
            if msg is None:
                raise DDPError("Zeitueberschreitung beim Warten auf Server-Antwort")
            if msg.get("msg") == "ping" and "ping" not in accepted_types:
                conn.send_json({"msg": "pong"})
                continue
            if msg.get("msg") in accepted_types:
                return msg
