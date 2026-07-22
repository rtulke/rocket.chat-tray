from __future__ import annotations

import logging
import threading

import requests
from PySide6.QtCore import QObject, Signal

logger = logging.getLogger(__name__)


def set_status(
    server_url: str, auth_token: str, user_id: str, status: str, message: str | None = None,
    verify_ssl: bool = True,
) -> bool:
    """Force the user's Rocket.Chat presence via POST /api/v1/users.setStatus.
    `status` must be one of online/away/busy/offline (Rocket.Chat has no
    "auto" concept server-side — callers wanting "auto" simply skip calling
    this at all). `message` is the free-text custom status message shown next
    to the user's name; pass "" to clear it, None to leave it unchanged.
    Returns True on success, False otherwise (logged, not raised — a failed
    status push shouldn't take down the tray app). This does blocking network
    I/O — callers on the GUI thread should run it off-thread (see
    PresenceCoordinator.apply below)."""
    body = {"status": status}
    if message is not None:
        body["message"] = message
    try:
        response = requests.post(
            f"{server_url}/api/v1/users.setStatus",
            json=body,
            headers={"X-Auth-Token": auth_token, "X-User-Id": user_id},
            verify=verify_ssl,
            timeout=10,
        )
    except requests.RequestException as exc:
        logger.warning("Status konnte nicht gesetzt werden (%s): %s", status, exc)
        return False

    if response.status_code != 200:
        logger.warning("Status setzen fehlgeschlagen (%s): HTTP %s", status, response.status_code)
        return False
    return True


class PresenceCoordinator(QObject):
    """Resolves and pushes the correct Rocket.Chat status: a manual override
    (settings.forced_status) always wins; otherwise, if idle detection is
    enabled and the user is currently idle, "away" is pushed; otherwise
    "online". The custom status message (settings.status_message) is sent
    along on every push. A QObject (not a plain function) so it can be
    connected directly to RocketChatWorker's cross-thread `connected` signal
    and still get Qt's automatic thread-safe queued dispatch.

    Tracks the last status actually confirmed on the server (our own pushes,
    plus the realtime stream-notify-logged/user-status echo of changes made
    anywhere else -- web client, mobile, ...) and skips re-pushing when the
    locally resolved status already matches it. Without this, apply() being
    wired to RocketChatWorker's `connected` signal meant *every* reconnect
    (network blips, the DDP idle-timeout cycling every ~45s) unconditionally
    re-pushed the locally computed status, silently clobbering a status
    someone had just set from elsewhere in the meantime. Explicit local
    intent (the tray's status menu, the status-message dialog) still always
    pushes via apply(force=True), since a message-only change needs to reach
    the server even when the status itself didn't change.

    Note: this used to go through requests on a background thread, was
    briefly switched to QNetworkAccessManager to rule out Python-thread/GIL
    involvement, but that introduced a real "QIODevice::read (QSslSocket):
    device not open" warning against the user's real server (not
    reproducible against a local test server, likely a stale pooled
    HTTPS-connection edge case) without fixing the reported symptom — so
    reverted back to this version, which is verified via an isolated test
    (a background thread blocked in requests.post() against a deliberately
    slow local server, with a QTimer ticking on the main loop throughout,
    real app.exec()) to never stall the Qt event loop."""

    # Emitted after a status push we made ourselves is confirmed successful.
    # The tray icon's colour is driven from this directly rather than
    # waiting on the realtime stream-notify-logged/user-status echo alone:
    # Rocket.Chat only broadcasts that event on an actual *change*, so
    # pushing a status that already matches the current one (e.g. "online"
    # right after connecting, when the account was already online) never
    # fires it, and the icon would otherwise stay on its startup default
    # forever despite the push having worked.
    status_applied = Signal(str)

    def __init__(self, admin_config, settings, worker, idle_watcher, parent=None):
        super().__init__(parent)
        self._admin_config = admin_config
        self._settings = settings
        self._worker = worker
        self._idle_watcher = idle_watcher
        self._last_known_status: str | None = None
        self._connected_once = False

    def resolve_status(self) -> str:
        if self._settings.forced_status != "auto":
            return self._settings.forced_status
        if self._settings.idle_detection_enabled and self._idle_watcher.is_idle:
            return "away"
        return "online"

    def note_external_status(self, status: str) -> None:
        """Record the server-confirmed status from the realtime echo, so a
        later apply() can tell whether it actually needs to push anything."""
        self._last_known_status = status

    def handle_connected(self, *_args) -> None:
        """Wired to RocketChatWorker.connected. Only asserts the locally
        configured status on the *first* connection of this run -- later
        reconnects leave whatever's currently set on the server alone (the
        icon still tracks it live via note_external_status/set_presence_status)
        instead of re-pushing on every reconnect."""
        if self._connected_once:
            return
        self._connected_once = True
        self.apply()

    def apply(self, *_args, force: bool = False) -> None:
        if not (self._worker.current_auth_token and self._worker.current_user_id):
            return
        status = self.resolve_status()
        if not force and status == self._last_known_status:
            return
        server_url = self._admin_config.server_url
        auth_token = self._worker.current_auth_token
        user_id = self._worker.current_user_id
        message = self._settings.status_message
        verify_ssl = self._admin_config.verify_ssl

        def push() -> None:
            if set_status(server_url, auth_token, user_id, status, message, verify_ssl):
                self._last_known_status = status
                self.status_applied.emit(status)

        threading.Thread(target=push, daemon=True).start()
