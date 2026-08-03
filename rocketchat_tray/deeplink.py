from __future__ import annotations

import logging
import shutil
import threading

import requests
from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtGui import QDesktopServices

from . import browser_app

logger = logging.getLogger(__name__)

# Checked in this order via shutil.which(). xdg-open (the QDesktopServices
# fallback) has no concept of "reuse an existing tab for this URL" -- that's
# not part of the freedesktop URL-open protocol at all, regardless of which
# browser ends up handling it. Chromium-family browsers are the one thing
# that can provide that, via browser_app.open_in_app_window() (see that
# module for how -- plain `--app=URL` alone does NOT auto-reuse a window,
# confirmed live; only installed PWAs get that treatment from Chrome).
# No equivalent exists in stable Firefox (its Site-Specific-Browser/PWA
# support is experimental/Nightly-only), so this is a no-op there --
# RoomOpener falls back to the normal xdg-open behaviour whenever no
# Chromium-family binary is found on PATH.
_CHROMIUM_BROWSER_CANDIDATES = (
    "google-chrome",
    "google-chrome-stable",
    "chrome",
    "chromium",
    "chromium-browser",
    "brave-browser",
    "microsoft-edge",
)


def find_chromium_browser() -> str | None:
    for name in _CHROMIUM_BROWSER_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    return None


def resolve_and_build_url(
    server_url: str, auth_token: str, user_id: str, rid: str, verify_ssl: bool = True
) -> str:
    """Look up a room's type/name and build its web URL. Called lazily, only
    when the user actually clicks a notification or menu item — never for
    every incoming notification."""
    try:
        response = requests.get(
            f"{server_url}/api/v1/rooms.info",
            params={"roomId": rid},
            headers={"X-Auth-Token": auth_token, "X-User-Id": user_id},
            verify=verify_ssl,
            timeout=10,
        )
        response.raise_for_status()
        room = response.json()["room"]
    except (requests.RequestException, KeyError, ValueError) as exc:
        logger.warning("Konnte Raum %s nicht aufloesen, oeffne Startseite: %s", rid, exc)
        return f"{server_url}/home"

    room_type = room.get("t")
    if room_type == "d":
        return f"{server_url}/direct/{rid}"
    if room_type == "c":
        return f"{server_url}/channel/{room.get('name', rid)}"
    if room_type == "p":
        return f"{server_url}/group/{room.get('name', rid)}"
    return f"{server_url}/home"


def _try_app_mode(url: str) -> bool:
    """Attempts to open `url` via a dedicated, reused Chromium app window.
    Does blocking network I/O (CDP HTTP/WebSocket calls, each up to a
    couple of seconds) -- callers must run this off the GUI thread. Returns
    True if handled, False if the caller should fall back to
    QDesktopServices.openUrl() instead (no Chromium-family browser found,
    or the attempt itself failed)."""
    browser = find_chromium_browser()
    if not browser:
        logger.info("Kein Chromium-basierter Browser gefunden, verwende Standard-Browser")
        return False
    if browser_app.open_in_app_window(browser, url):
        return True
    logger.warning("Konnte %s nicht im App-Modus oeffnen, verwende Standard-Browser", browser)
    return False


class RoomOpener(QObject):
    """Resolves a room id to a URL and opens it, without blocking the GUI
    thread: both rooms.info (a blocking REST call) and, when app_mode is
    on, _try_app_mode() (blocking CDP HTTP/WebSocket calls against the
    browser) run on a throwaway daemon thread. A QObject (not a plain
    function) is what reports back through, so Qt delivers `resolved` via
    its automatic thread-safe queued connection and the actual
    QDesktopServices.openUrl() fallback call still happens on the GUI
    thread, as its docs require."""

    resolved = Signal(str)  # url -- only emitted when the GUI thread still needs to open it itself

    def __init__(self, parent=None):
        super().__init__(parent)
        self.resolved.connect(self._open)

    def _open(self, url: str) -> None:
        QDesktopServices.openUrl(QUrl(url))

    def open_room(
        self, server_url: str, auth_token: str | None, user_id: str | None, rid: str | None,
        verify_ssl: bool = True, app_mode: bool = False,
    ) -> None:
        def worker() -> None:
            if rid and auth_token and user_id:
                url = resolve_and_build_url(server_url, auth_token, user_id, rid, verify_ssl)
            else:
                url = f"{server_url}/home"
            if app_mode and _try_app_mode(url):
                return
            self.resolved.emit(url)

        threading.Thread(target=worker, daemon=True).start()
