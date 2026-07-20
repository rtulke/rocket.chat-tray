from __future__ import annotations

from PySide6.QtNetwork import QLocalServer, QLocalSocket

SERVER_NAME = "rocketchat-tray"
PROBE_TIMEOUT_MS = 500


class SingleInstanceGuard:
    """QLocalServer/QLocalSocket-based single-instance guard, so the tray app
    doesn't start twice if autostart and a manual launch overlap."""

    def __init__(self) -> None:
        self._server: QLocalServer | None = None

    def acquire(self) -> bool:
        """Returns True if this process should proceed as the sole instance,
        False if another instance is already running (caller should exit)."""
        probe = QLocalSocket()
        probe.connectToServer(SERVER_NAME)
        already_running = probe.waitForConnected(PROBE_TIMEOUT_MS)
        probe.close()
        if already_running:
            return False

        # No live listener answered — clean up a stale socket path possibly
        # left behind by a crash, then bind our own.
        QLocalServer.removeServer(SERVER_NAME)
        self._server = QLocalServer()
        self._server.listen(SERVER_NAME)
        return True
