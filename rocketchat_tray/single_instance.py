from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

SERVER_NAME = "rocketchat-tray"
PROBE_TIMEOUT_MS = 500
RESTART_COMMAND = b"restart"


class SingleInstanceGuard(QObject):
    """QLocalServer/QLocalSocket-based single-instance guard, so the tray app
    doesn't start twice if autostart and a manual launch overlap.

    Doubles as a restart channel: packaging/postinstall.sh connects to this
    same socket after a .deb upgrade and writes RESTART_COMMAND, so the
    already-running (now outdated) instance relaunches itself automatically
    instead of the user having to notice and do it by hand. A plain
    single-instance probe (connect, send nothing, disconnect) is
    unaffected -- see _handle_data."""

    restart_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
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
        self._server.newConnection.connect(self._handle_connection)
        self._server.listen(SERVER_NAME)
        return True

    def _handle_connection(self) -> None:
        socket = self._server.nextPendingConnection()
        if socket is None:
            return
        socket.readyRead.connect(lambda: self._handle_data(socket))
        socket.disconnected.connect(socket.deleteLater)

    def _handle_data(self, socket: QLocalSocket) -> None:
        if bytes(socket.readAll()).strip() == RESTART_COMMAND:
            self.restart_requested.emit()
