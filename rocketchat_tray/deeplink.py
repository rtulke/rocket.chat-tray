from __future__ import annotations

import logging
import shutil
import threading

import requests
from PySide6.QtCore import QObject, QProcess, QUrl, Signal
from PySide6.QtGui import QDesktopServices

logger = logging.getLogger(__name__)

# Checked in this order via shutil.which(). xdg-open (the QDesktopServices
# fallback) has no concept of "reuse an existing tab for this URL" -- that's
# not part of the freedesktop URL-open protocol at all, regardless of which
# browser ends up handling it. Chromium-family browsers' own --app mode is
# the one thing that reliably provides that: launching (or re-launching)
# `<browser> --app=<url>` opens a dedicated, tab-less window for that
# origin, and re-invoking it while a window for the same origin is already
# open focuses that window instead of creating a new one -- Chrome/Chromium
# key that reuse off the URL's origin, not the exact path, so navigating to
# a different room (a different path under the same server origin) still
# reuses and re-navigates the same window rather than opening another one.
# No equivalent exists in stable Firefox (its Site-Specific-Browser/PWA
# support is experimental/Nightly-only), so this is a no-op there --
# open_url() falls back to the normal xdg-open behaviour whenever no
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


def open_url(url: str, app_mode: bool = False) -> None:
    if app_mode:
        browser = find_chromium_browser()
        if browser:
            # Detached: its lifetime must not be tied to ours (QProcess's
            # normal, non-detached mode kills the child if the parent
            # QProcess object is destroyed/GC'd, which would happen almost
            # immediately here since we don't hold a reference).
            if QProcess.startDetached(browser, [f"--app={url}"]):
                return
            logger.warning("Konnte %s nicht im App-Modus starten, verwende Standard-Browser", browser)
        else:
            logger.info("Kein Chromium-basierter Browser gefunden, verwende Standard-Browser")
    QDesktopServices.openUrl(QUrl(url))


class RoomOpener(QObject):
    """Resolves a room id to a URL and opens it, without blocking the GUI
    thread: rooms.info is a blocking REST call, and running it directly on
    the thread that's about to handle a menu click/notification click would
    freeze the event loop for the round-trip (GNOME shows that to the user
    as a spinning busy cursor). The resolution runs on a throwaway daemon
    thread; a QObject (not a plain function) is what it reports back
    through, so Qt delivers `resolved` via its automatic thread-safe queued
    connection and the actual QDesktopServices.openUrl() call still happens
    on the GUI thread."""

    resolved = Signal(str, bool)  # url, app_mode

    def __init__(self, parent=None):
        super().__init__(parent)
        self.resolved.connect(self._open)

    def _open(self, url: str, app_mode: bool) -> None:
        open_url(url, app_mode=app_mode)

    def open_room(
        self, server_url: str, auth_token: str | None, user_id: str | None, rid: str | None,
        verify_ssl: bool = True, app_mode: bool = False,
    ) -> None:
        if not (rid and auth_token and user_id):
            open_url(f"{server_url}/home", app_mode=app_mode)
            return

        def worker() -> None:
            url = resolve_and_build_url(server_url, auth_token, user_id, rid, verify_ssl)
            self.resolved.emit(url, app_mode)

        threading.Thread(target=worker, daemon=True).start()
