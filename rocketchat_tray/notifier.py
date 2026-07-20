from __future__ import annotations

import logging
import re
from typing import Callable

from PySide6.QtCore import QObject, QProcess, QTimer, SLOT, Slot
from PySide6.QtDBus import QDBusConnection, QDBusInterface

from . import sounds

logger = logging.getLogger(__name__)

NOTIFY_SERVICE = "org.freedesktop.Notifications"
NOTIFY_PATH = "/org/freedesktop/Notifications"
NOTIFY_INTERFACE = "org.freedesktop.Notifications"

APP_NAME = "Rocket.Chat Tray"
NOTIFICATION_TIMEOUT_MS = 8000
GDBUS_CALL_TIMEOUT_MS = 3000
_NOTIFY_ID_RE = re.compile(r"uint32 (\d+)")


class NotificationManager(QObject):
    """Sends desktop notifications via raw DBus (org.freedesktop.Notifications)
    rather than QSystemTrayIcon.showMessage(): showMessage() only supports one
    pending balloon with no per-notification identity, so a burst of messages
    from different rooms would lose information and clicks couldn't be routed
    to the right room."""

    def __init__(self, settings, app_icon_name: str = "rocketchat-tray", parent=None):
        super().__init__(parent)
        self._settings = settings
        self._app_icon_name = app_icon_name
        self._id_to_rid: dict[int, str] = {}
        self._on_room_clicked: Callable[[str], None] | None = None

        bus = QDBusConnection.sessionBus()
        self._interface = QDBusInterface(NOTIFY_SERVICE, NOTIFY_PATH, NOTIFY_INTERFACE, bus)

        # registerObject() is required here even though we're not exporting
        # anything: without it, QDBusConnection.connect() below intermittently
        # fails to resolve the slot (a known PySide6 quirk, PYSIDE-2547).
        bus.registerObject("/", self)
        # The service filter is deliberately left empty ("" = match any
        # sender): on GNOME Shell, the well-known name org.freedesktop.
        # Notifications is owned by a gjs helper process, but the actual
        # ActionInvoked/NotificationClosed signals are emitted by gnome-shell
        # itself under its own unique connection name — filtering by the
        # well-known service name silently drops every signal.
        bus.connect(
            "", NOTIFY_PATH, NOTIFY_INTERFACE, "ActionInvoked",
            self, SLOT("_on_action_invoked(uint,QString)"),
        )
        bus.connect(
            "", NOTIFY_PATH, NOTIFY_INTERFACE, "NotificationClosed",
            self, SLOT("_on_notification_closed(uint,uint)"),
        )

    def set_click_handler(self, handler: Callable[[str], None]) -> None:
        """handler(rid) is called when a notification's body is clicked."""
        self._on_room_clicked = handler

    def notify_message(self, rid: str, title: str, text: str) -> None:
        if not self._interface.isValid():
            logger.warning("Benachrichtigungsdienst nicht verfuegbar; ueberspringe Desktop-Benachrichtigung")
        else:
            self._send_notify(rid, title or "Rocket.Chat", text)

        if self._settings.sound_enabled:
            self._play_sound()

    def _send_notify(self, rid: str, title: str, text: str) -> None:
        # PySide6 cannot marshal an explicitly-typed uint32/empty-dict
        # argument for QDBusInterface.call() (no QVariant exposed to Python,
        # see PYSIDE-1904), so the outgoing Notify() call is made via the
        # gdbus CLI instead, which has its own correct GVariant type system.
        # repr() produces GVariant-compatible quoted/escaped string literals
        # (GVariant text format is modelled on Python literal syntax).
        args = [
            "call", "--session",
            "--dest", NOTIFY_SERVICE,
            "--object-path", NOTIFY_PATH,
            "--method", f"{NOTIFY_INTERFACE}.Notify",
            repr(APP_NAME),
            "uint32 0",  # replaces_id: 0 = always a new notification
            repr(self._app_icon_name),
            repr(title),
            repr(text),
            "['default', '']",
            "{}",
            f"int32 {NOTIFICATION_TIMEOUT_MS}",
        ]
        # Started asynchronously (not waitForFinished()) so the GUI thread's
        # event loop never blocks on the subprocess round-trip — a blocking
        # call here is exactly what made GNOME show a spinning busy cursor
        # over the tray icon for a couple of seconds on every notification.
        process = QProcess(self)
        process.finished.connect(lambda: self._handle_notify_finished(process, rid))
        process.start("gdbus", args)
        QTimer.singleShot(GDBUS_CALL_TIMEOUT_MS, lambda: self._kill_if_still_running(process))

    def _handle_notify_finished(self, process: QProcess, rid: str) -> None:
        if process.exitCode() == 0:
            output = bytes(process.readAllStandardOutput()).decode(errors="replace")
            match = _NOTIFY_ID_RE.search(output)
            if match:
                self._id_to_rid[int(match.group(1))] = rid
        else:
            logger.warning(
                "gdbus Notify-Aufruf fehlgeschlagen: %s",
                bytes(process.readAllStandardError()).decode(errors="replace").strip(),
            )
        process.deleteLater()

    def _kill_if_still_running(self, process: QProcess) -> None:
        if process.state() != QProcess.ProcessState.NotRunning:
            logger.warning("gdbus Notify-Aufruf hat nicht rechtzeitig geantwortet, breche ab")
            process.kill()

    def _play_sound(self) -> None:
        # Played via paplay (PulseAudio/PipeWire's own player, the same way
        # GNOME plays its own notification sounds) rather than Qt's
        # QSoundEffect: that would pull in the ~175MB PySide6-Addons wheel
        # for QtMultimedia alone, and even then can't decode the compressed
        # system sounds (Ogg Vorbis) — only the app's own bundled .wav.
        # paplay handles both uniformly, with the same volume control.
        path = sounds.resolve(self._settings.sound_choice)
        volume = self._settings.sound_volume
        QProcess.startDetached("paplay", ["--volume", str(round(volume * 65536)), str(path)])

    def play_test_sound(self) -> None:
        self._play_sound()

    @Slot('uint', 'QString')
    def _on_action_invoked(self, notif_id: int, action_key: str) -> None:
        if action_key != "default":
            return
        rid = self._id_to_rid.pop(notif_id, None)
        if rid and self._on_room_clicked:
            self._on_room_clicked(rid)

    @Slot('uint', 'uint')
    def _on_notification_closed(self, notif_id: int, reason: int) -> None:
        self._id_to_rid.pop(notif_id, None)
