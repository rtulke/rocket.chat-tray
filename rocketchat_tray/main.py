from __future__ import annotations

import logging
import signal
import sys

import urllib3
from PySide6.QtCore import QProcess, Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from . import auth, autostart, presence
from .config import AdminConfig, ConfigError, UserSettings
from .deeplink import RoomOpener
from .i18n import tr
from .idle_watch import IdleWatcher
from .notifier import NotificationManager
from .quick_reply import QuickReplyDialog
from .rc_client import RocketChatWorker
from .resources import ICON_NAMES, icon_path
from .settings_dialog import SettingsDialog
from .single_instance import SingleInstanceGuard
from .tray import TrayController
from .widgets import BUTTON_STYLE

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def _load_icons() -> dict[str, QIcon]:
    return {name: QIcon(str(icon_path(name))) for name in ICON_NAMES}


def main() -> int:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("Rocket.Chat Tray")
    # GNOME (particularly on Wayland) resolves a window's dock/taskbar/
    # alt-tab icon by matching its Wayland app_id (or X11 WM_CLASS) against
    # an installed .desktop entry's Icon=. Qt sets neither by default, so
    # every window (Settings/Login/About) fell back to a generic placeholder
    # icon instead of ours. setDesktopFileName ties us to
    # packaging/launcher.desktop for that lookup; setWindowIcon is the
    # direct Qt-level fallback so windows still get the right icon even
    # where the desktop-file match fails (e.g. a plain X11 session).
    app.setDesktopFileName("rocketchat-tray")
    app.setWindowIcon(QIcon(str(icon_path("online"))))
    app.setStyleSheet(BUTTON_STYLE)

    # Qt's C++ event loop blocks Python from checking for pending signals,
    # so a plain Ctrl+C in a terminal can take dozens of presses (or never)
    # to register. An explicit handler plus a periodic no-op timer — which
    # hands control back to the interpreter often enough to notice the
    # signal — fixes that. Installed before any dialogs can run their own
    # nested event loop, so Ctrl+C works during the login dialog too.
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    sigint_timer = QTimer()
    sigint_timer.timeout.connect(lambda: None)
    sigint_timer.start(200)

    guard = SingleInstanceGuard()
    if not guard.acquire():
        logger.info("Es laeuft bereits eine Instanz, beende.")
        return 0

    settings = UserSettings.load()

    try:
        admin_config = AdminConfig.load()
    except ConfigError as exc:
        if not settings.server_url_override:
            logger.error("%s", exc)
            QMessageBox.critical(None, "Rocket.Chat Tray", tr("main.config_error", error=exc))
            return 1
        # No usable /etc config, but the user has already set their own
        # server via the settings dialog on a previous run — use that.
        admin_config = AdminConfig(server_url=settings.server_url_override, verify_ssl=settings.verify_ssl_override)
    else:
        if settings.server_url_override:
            # A per-user override (set via the settings dialog) always wins
            # over the admin-provisioned default.
            admin_config.server_url = settings.server_url_override
            admin_config.verify_ssl = settings.verify_ssl_override

    if not admin_config.verify_ssl:
        # verify_ssl=false is an explicit choice (e.g. self-signed cert) —
        # no need to nag about it on every single request.
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    if auth.get_stored_password() is None:
        login_dialog = auth.LoginDialog(admin_config.server_url, admin_config.verify_ssl)
        if login_dialog.exec() != auth.LoginDialog.DialogCode.Accepted:
            return 0

    icons = _load_icons()
    worker = RocketChatWorker(admin_config.server_url, admin_config.verify_ssl, auth.get_stored_password)
    notifier = NotificationManager(settings)
    idle_watcher = IdleWatcher()
    idle_watcher.set_threshold_minutes(settings.idle_threshold_minutes)
    idle_watcher.set_enabled(settings.idle_detection_enabled)
    presence_coordinator = presence.PresenceCoordinator(admin_config, settings, worker, idle_watcher)
    room_opener = RoomOpener()

    def open_room(rid: str | None) -> None:
        room_opener.open_room(
            admin_config.server_url, worker.current_auth_token, worker.current_user_id,
            rid, admin_config.verify_ssl, app_mode=settings.app_mode_enabled,
        )

    def reenter_password() -> None:
        auth.delete_stored_password()
        dialog = auth.LoginDialog(admin_config.server_url, admin_config.verify_ssl)
        if dialog.exec() == auth.LoginDialog.DialogCode.Accepted:
            worker.retry_now()

    def update_server(server_url: str, verify_ssl: bool) -> None:
        if server_url == admin_config.server_url and verify_ssl == admin_config.verify_ssl:
            return
        admin_config.server_url = server_url
        admin_config.verify_ssl = verify_ssl
        settings.server_url_override = server_url
        settings.verify_ssl_override = verify_ssl
        settings.save()
        if not verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        # A different server almost certainly means a different account —
        # clear the old password and force the worker to reconnect with the
        # new URL right away (update_server() interrupts an active session
        # too, not just a waiting one). If the user cancels the re-login
        # prompt below, the worker will simply sit in the auth-error state
        # with "Passwort erneut eingeben" available in the tray menu, same
        # as any other failed login.
        auth.delete_stored_password()
        worker.update_server(server_url, verify_ssl)
        login_dialog = auth.LoginDialog(server_url, verify_ssl)
        if login_dialog.exec() == auth.LoginDialog.DialogCode.Accepted:
            worker.retry_now()

    def open_settings() -> None:
        dialog = SettingsDialog(
            settings, admin_config.server_url, admin_config.verify_ssl, autostart.is_enabled(),
            notifier.play_test_sound, update_server, reenter_password, autostart.set_enabled,
        )
        if dialog.exec() == SettingsDialog.DialogCode.Accepted:
            idle_watcher.set_threshold_minutes(settings.idle_threshold_minutes)
            idle_watcher.set_enabled(settings.idle_detection_enabled)
            presence_coordinator.apply()

    def set_status(status: str) -> None:
        settings.forced_status = status
        settings.save()
        presence_coordinator.apply()

    def set_status_message(message: str) -> None:
        settings.status_message = message
        settings.save()
        # force=True: the status itself may be unchanged, but the message
        # text always needs to reach the server, and apply() otherwise skips
        # pushing when the resolved status already matches the last-known one.
        presence_coordinator.apply(force=True)

    def quit_app() -> None:
        worker.stop()
        worker.wait(3000)
        app.quit()

    # Kept alive here: a QuickReplyDialog with no Python reference (and no
    # parent widget) is garbage-collected as soon as open_quick_reply()
    # returns, which would destroy the just-shown window immediately.
    # WA_DeleteOnClose + the destroyed-signal cleanup below keeps this from
    # growing unbounded across a long-running session.
    quick_reply_dialogs: list[QuickReplyDialog] = []

    def open_quick_reply(rid: str, room_type: str, title: str) -> None:
        dialog = QuickReplyDialog(
            admin_config.server_url, worker.current_auth_token, worker.current_user_id,
            rid, room_type, title, admin_config.verify_ssl,
        )
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        quick_reply_dialogs.append(dialog)
        dialog.destroyed.connect(lambda: quick_reply_dialogs.remove(dialog))
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def handle_restart_requested() -> None:
        # packaging/postinstall.sh sends this after a .deb upgrade so the
        # already-running (now outdated) instance relaunches itself instead
        # of the user having to notice a stale tray icon and do it by hand.
        # Only wired up from here on, i.e. once the app is past the initial
        # config/login checks -- a restart request arriving during that
        # brief early window (first run, or right after "Anmeldung
        # zuruecksetzen") is silently missed, same as before this feature
        # existed, rather than risk interacting with LoginDialog's own
        # nested event loop.
        logger.info("Neustart angefordert (Paket-Upgrade), starte neu...")
        # Must happen BEFORE starting the new process: quit_app()'s
        # worker.stop()/wait() can take a few seconds, during which this
        # process is still alive and would otherwise still be holding the
        # single-instance socket -- the new process's own acquire() runs
        # almost immediately after being spawned, well before that, and
        # without releasing first it would see the socket still taken,
        # conclude another instance is already running, and quit right
        # away (confirmed live: this is exactly what happened before this
        # release() call was added).
        guard.release()
        QProcess.startDetached(sys.executable, ["-m", "rocketchat_tray.main"])
        quit_app()

    guard.restart_requested.connect(handle_restart_requested)

    tray = TrayController(
        icons,
        settings,
        on_open_chat=open_room,
        on_open_settings=open_settings,
        on_reenter_password=reenter_password,
        on_set_status=set_status,
        on_set_status_message=set_status_message,
        on_quit=quit_app,
        on_quick_reply=open_quick_reply,
    )

    def handle_notification_click(rid: str) -> None:
        tray.clear_unread(rid)
        open_room(rid)

    notifier.set_click_handler(handle_notification_click)

    # All of these cross from the worker's background QThread. Connecting
    # directly to bound methods of the QObject-derived tray/notifier/
    # presence_coordinator (rather than to plain functions or lambdas) is
    # what makes Qt deliver them via a thread-safe queued connection instead
    # of running GUI code on the worker thread.
    worker.connecting.connect(tray.set_connecting)
    worker.connected.connect(tray.set_connected)
    worker.connected.connect(presence_coordinator.handle_connected)
    worker.disconnected.connect(tray.set_disconnected)
    worker.reconnect_scheduled.connect(tray.set_reconnect_scheduled)
    worker.auth_failed.connect(tray.set_auth_error)
    worker.notification_received.connect(tray.notify_unread)
    worker.notification_received.connect(notifier.notify_message)
    worker.presence_status_changed.connect(tray.set_presence_status)
    worker.presence_status_changed.connect(presence_coordinator.note_external_status)
    presence_coordinator.status_applied.connect(tray.set_presence_status)

    idle_watcher.became_idle.connect(presence_coordinator.apply)
    idle_watcher.became_active.connect(presence_coordinator.apply)

    worker.start()

    exit_code = app.exec()
    worker.stop()
    worker.wait(3000)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
