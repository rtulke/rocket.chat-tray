from __future__ import annotations

import logging
from enum import Enum, auto

from PySide6.QtCore import QObject, QPointF, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QInputDialog, QMenu, QSystemTrayIcon

from . import auth
from .about_dialog import AboutDialog

logger = logging.getLogger(__name__)

BLINK_INTERVAL_MS = 600
BADGE_ICON_SIZE = 128

STATUS_LABELS = (
    ("auto", "Auto"),
    ("online", "Online"),
    ("away", "Abwesend"),
    ("busy", "Beschäftigt"),
    ("offline", "Offline"),
)


class ConnectionState(Enum):
    DISCONNECTED = auto()
    CONNECTING = auto()
    CONNECTED = auto()
    AUTH_ERROR = auto()


def _with_unread_badge(icon: QIcon) -> QIcon:
    """Render `icon` onto a pixmap with a small red dot added in the corner,
    used for the unread-message blink instead of separate pre-made per-color
    "alert" icon files."""
    pixmap = QPixmap(icon.pixmap(BADGE_ICON_SIZE, BADGE_ICON_SIZE))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    radius = BADGE_ICON_SIZE * 0.22
    center = QPointF(BADGE_ICON_SIZE - radius, radius)
    painter.setBrush(QColor("#ffffff"))
    painter.drawEllipse(center, radius, radius)
    painter.setBrush(QColor("#e74c3c"))
    painter.drawEllipse(center, radius * 0.72, radius * 0.72)
    painter.end()
    return QIcon(pixmap)


class TrayController(QObject):
    """Owns the QSystemTrayIcon. Must be a QObject (not a plain class) so
    that connecting RocketChatWorker's cross-thread signals directly to its
    methods uses Qt's automatic queued connection instead of running GUI
    code on the worker thread."""

    def __init__(self, icons: dict[str, QIcon], settings, on_open_chat, on_open_settings,
                 on_reenter_password, on_set_status, on_set_status_message, on_quit, parent=None):
        super().__init__(parent)
        self._icons = icons  # keys: online/away/busy/offline
        self._badged_icons = {key: _with_unread_badge(icon) for key, icon in icons.items()}
        self._settings = settings
        self._on_open_chat = on_open_chat
        self._on_set_status = on_set_status
        self._on_set_status_message = on_set_status_message
        self._state = ConnectionState.DISCONNECTED
        self._presence_status = "offline"
        self._unread_rids: set[str] = set()
        self._blink_visible = False
        self._username = ""

        self._tray = QSystemTrayIcon(icons["offline"], self)
        self._tray.activated.connect(self._handle_activation)

        self._blink_timer = QTimer(self)
        self._blink_timer.setInterval(BLINK_INTERVAL_MS)
        self._blink_timer.timeout.connect(self._toggle_blink)

        self._status_action = QAction("Getrennt", self)
        self._status_action.setEnabled(False)
        self._open_chat_action = QAction("Chat öffnen", self)
        self._open_chat_action.triggered.connect(lambda: self._defer(self._handle_open_chat))
        status_menu = self._build_status_menu(settings.forced_status)
        status_message_action = QAction("Statusmeldung…", self)
        status_message_action.triggered.connect(lambda: self._defer(self._handle_set_status_message))
        settings_action = QAction("Einstellungen…", self)
        settings_action.triggered.connect(on_open_settings)
        self._reenter_password_action = QAction("Passwort erneut eingeben…", self)
        self._reenter_password_action.triggered.connect(on_reenter_password)
        self._reenter_password_action.setVisible(False)
        about_action = QAction("Info…", self)
        about_action.triggered.connect(self._handle_show_about)
        quit_action = QAction("Beenden", self)
        quit_action.triggered.connect(on_quit)

        menu = QMenu()
        menu.addAction(self._status_action)
        menu.addAction(self._open_chat_action)
        menu.addMenu(status_menu)
        menu.addSeparator()
        menu.addAction(about_action)
        menu.addAction(status_message_action)
        menu.addSeparator()
        menu.addAction(settings_action)
        menu.addAction(self._reenter_password_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        self._tray.setContextMenu(menu)
        self._menu = menu  # keep a reference alive

        self._update_status_text("Getrennt")
        self._tray.show()

    @staticmethod
    def _defer(fn) -> None:
        # Runs `fn` on the next event-loop iteration instead of inline from
        # within the QAction's triggered handler. On GNOME, clicking a tray
        # menu item goes through the DBusMenu D-Bus protocol; gnome-shell
        # appears to hold the busy/wait cursor until that click round-trip
        # is acknowledged, which happens only once our triggered handler
        # returns. Deferring the actual work lets the click be acknowledged
        # immediately, so the cursor doesn't sit in the "busy" state for
        # however long our own handling takes.
        QTimer.singleShot(0, fn)

    def _build_status_menu(self, current_status: str) -> QMenu:
        menu = QMenu("Status", None)
        group = QActionGroup(self)
        group.setExclusive(True)
        for value, label in STATUS_LABELS:
            action = QAction(label, self)
            action.setCheckable(True)
            action.setChecked(value == current_status)
            action.triggered.connect(lambda checked, v=value: self._defer(lambda: self._on_set_status(v)))
            group.addAction(action)
            menu.addAction(action)
        self._status_action_group = group  # keep a reference alive
        return menu

    def _handle_set_status_message(self) -> None:
        text, ok = QInputDialog.getText(
            None, "Statusmeldung", "Eigene Statusmeldung (leer lassen zum Entfernen):",
            text=self._settings.status_message,
        )
        if ok:
            self._on_set_status_message(text)

    def _handle_show_about(self) -> None:
        AboutDialog().exec()

    # --- connection state ---------------------------------------------------

    def set_connecting(self) -> None:
        self._state = ConnectionState.CONNECTING
        self._reenter_password_action.setVisible(False)
        self._stop_blink()
        self._update_status_text("Verbindung wird hergestellt…")

    def set_connected(self, user_id: str) -> None:
        self._state = ConnectionState.CONNECTED
        self._username = auth.current_username()
        self._reenter_password_action.setVisible(False)
        self._refresh_unread_display()

    def set_disconnected(self, reason: str = "") -> None:
        self._state = ConnectionState.DISCONNECTED
        self._stop_blink()
        self._update_status_text("Getrennt — erneuter Verbindungsversuch…")

    def set_reconnect_scheduled(self, seconds: float) -> None:
        if self._state != ConnectionState.AUTH_ERROR:
            self._update_status_text(f"Getrennt — neuer Versuch in {seconds:.0f}s")

    def set_auth_error(self, message: str) -> None:
        self._state = ConnectionState.AUTH_ERROR
        self._stop_blink()
        self._reenter_password_action.setVisible(True)
        self._update_status_text("Anmeldung fehlgeschlagen — Passwort erneut eingeben")

    def set_presence_status(self, status: str) -> None:
        """Called whenever the *actual*, server-confirmed Rocket.Chat status
        is known (echoed back via the realtime stream, so this reflects
        status changes made anywhere — our own menu, the web client, a
        phone — not just ones made through this app)."""
        if status not in self._icons:
            return
        self._presence_status = status
        self._refresh_icon()

    # --- unread / blink -------------------------------------------------------

    def notify_unread(self, rid: str) -> None:
        self._unread_rids.add(rid)
        self._refresh_unread_display()

    def clear_unread(self, rid: str) -> None:
        self._unread_rids.discard(rid)
        self._refresh_unread_display()

    def clear_all_unread(self) -> None:
        self._unread_rids.clear()
        self._refresh_unread_display()

    def _refresh_unread_display(self) -> None:
        if self._state != ConnectionState.CONNECTED:
            return
        if self._unread_rids and self._settings.blink_enabled:
            self._start_blink()
        else:
            self._stop_blink()
        if self._unread_rids:
            self._update_status_text(f"Verbunden — neue Nachrichten in {len(self._unread_rids)} Chat(s)")
        else:
            self._update_status_text(f"Verbunden als {self._username}")

    def _start_blink(self) -> None:
        if not self._blink_timer.isActive():
            self._blink_timer.start()

    def _stop_blink(self) -> None:
        self._blink_timer.stop()
        self._blink_visible = False
        self._refresh_icon()

    def _toggle_blink(self) -> None:
        self._blink_visible = not self._blink_visible
        self._refresh_icon()

    def _current_status_key(self) -> str:
        # Icon colour always reflects real Rocket.Chat presence while
        # connected; any not-fully-connected state shows as "offline" grey
        # regardless of whatever presence was last observed.
        return self._presence_status if self._state == ConnectionState.CONNECTED else "offline"

    def _refresh_icon(self) -> None:
        key = self._current_status_key()
        icon_set = self._badged_icons if self._blink_visible else self._icons
        self._tray.setIcon(icon_set.get(key, icon_set["offline"]))

    # --- menu / tooltip / clicks -----------------------------------------------

    def _handle_activation(self, reason) -> None:
        # On stock GNOME (AppIndicator extension), left-click already opens
        # the context menu, so Trigger/DoubleClick rarely fire there — but on
        # X11/other desktop environments this gives a genuine one-click open.
        # MiddleClick is not swallowed by the AppIndicator menu on GNOME
        # either way, so it's wired as a direct-open shortcut everywhere
        # (untested on GNOME specifically — harmless if it's a no-op there).
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
            QSystemTrayIcon.ActivationReason.MiddleClick,
        ):
            self._handle_open_chat()

    def _handle_open_chat(self) -> None:
        self.clear_all_unread()
        self._on_open_chat(None)

    def _update_status_text(self, text: str) -> None:
        self._refresh_icon()
        self._status_action.setText(text)
        if self._settings.tooltip_enabled:
            self._tray.setToolTip(f"Rocket.Chat: {text}")
        else:
            self._tray.setToolTip("Rocket.Chat")
