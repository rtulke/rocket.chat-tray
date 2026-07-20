from __future__ import annotations

import logging
import threading

import requests
from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtGui import QDesktopServices

logger = logging.getLogger(__name__)


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


def open_url(url: str) -> None:
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

    resolved = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.resolved.connect(self._open)

    def _open(self, url: str) -> None:
        open_url(url)

    def open_room(
        self, server_url: str, auth_token: str | None, user_id: str | None, rid: str | None,
        verify_ssl: bool = True,
    ) -> None:
        if not (rid and auth_token and user_id):
            open_url(f"{server_url}/home")
            return

        def worker() -> None:
            url = resolve_and_build_url(server_url, auth_token, user_id, rid, verify_ssl)
            self.resolved.emit(url)

        threading.Thread(target=worker, daemon=True).start()
