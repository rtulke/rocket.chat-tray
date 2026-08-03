from __future__ import annotations

import logging
import threading

import requests
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)

from .i18n import tr

logger = logging.getLogger(__name__)

HISTORY_COUNT = 10
_HTTP_TIMEOUT = 10

# Rocket.Chat splits message history across three endpoints by room type
# (room.t: "d"/"c"/"p", matching deeplink.py's room_type) -- chat.postMessage
# below, by contrast, is one single endpoint that works for all of them.
_HISTORY_ENDPOINT_BY_TYPE = {
    "d": "im.history",
    "c": "channels.history",
    "p": "groups.history",
}


def fetch_history(
    server_url: str, auth_token: str, user_id: str, rid: str, room_type: str, verify_ssl: bool = True,
) -> list[dict]:
    """Returns the last HISTORY_COUNT messages, oldest first. Best-effort:
    an unknown room_type or a failed request just yields an empty list
    (context is a nice-to-have for the reply dialog, not essential)."""
    endpoint = _HISTORY_ENDPOINT_BY_TYPE.get(room_type)
    if not endpoint:
        return []
    try:
        response = requests.get(
            f"{server_url}/api/v1/{endpoint}",
            params={"roomId": rid, "count": HISTORY_COUNT},
            headers={"X-Auth-Token": auth_token, "X-User-Id": user_id},
            verify=verify_ssl,
            timeout=_HTTP_TIMEOUT,
        )
        response.raise_for_status()
        messages = response.json().get("messages", [])
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Konnte Verlauf fuer Raum %s nicht laden: %s", rid, exc)
        return []
    return list(reversed(messages))  # API returns newest-first


def send_message(
    server_url: str, auth_token: str, user_id: str, rid: str, text: str, verify_ssl: bool = True,
) -> tuple[bool, str]:
    """Returns (success, error_message). error_message is "" on success."""
    try:
        response = requests.post(
            f"{server_url}/api/v1/chat.postMessage",
            json={"roomId": rid, "text": text},
            headers={"X-Auth-Token": auth_token, "X-User-Id": user_id},
            verify=verify_ssl,
            timeout=_HTTP_TIMEOUT,
        )
    except requests.RequestException as exc:
        return False, str(exc)
    if response.status_code != 200:
        try:
            error = response.json().get("error", f"HTTP {response.status_code}")
        except ValueError:
            error = f"HTTP {response.status_code}"
        return False, error
    return True, ""


class QuickReplyDialog(QDialog):
    """Small native reply window opened from the tray's "Verpasste
    Nachrichten" submenu -- shows the last few messages for context and
    lets the user send a reply via Rocket.Chat's REST API directly,
    without needing a browser. All network I/O (history fetch, send) runs
    on background threads; only the signal handlers below touch widgets,
    same threading pattern as RoomOpener/PresenceCoordinator elsewhere in
    this app."""

    _history_loaded = Signal(list)
    _send_finished = Signal(bool, str)

    def __init__(
        self, server_url: str, auth_token: str, user_id: str, rid: str, room_type: str,
        title: str, verify_ssl: bool = True, parent=None,
    ):
        super().__init__(parent)
        self._server_url = server_url
        self._auth_token = auth_token
        self._user_id = user_id
        self._rid = rid
        self._room_type = room_type
        self._verify_ssl = verify_ssl

        self.setWindowTitle(tr("quick_reply.title", sender=title))
        self.setModal(False)
        self.resize(420, 360)

        self._history_view = QTextEdit()
        self._history_view.setReadOnly(True)
        self._history_view.setPlainText(tr("quick_reply.loading"))

        self._input = QLineEdit()
        self._input.setPlaceholderText(tr("quick_reply.placeholder"))
        self._input.returnPressed.connect(self._handle_send)

        self._send_button = QPushButton(tr("quick_reply.send"))
        self._send_button.setDefault(True)
        self._send_button.clicked.connect(self._handle_send)

        self._status_label = QLabel()
        self._status_label.setVisible(False)

        input_row = QHBoxLayout()
        input_row.addWidget(self._input, stretch=1)
        input_row.addWidget(self._send_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self._history_view, stretch=1)
        layout.addWidget(self._status_label)
        layout.addLayout(input_row)

        self._history_loaded.connect(self._display_history)
        self._send_finished.connect(self._handle_send_finished)

        self._input.setFocus()
        threading.Thread(target=self._fetch_history_worker, daemon=True).start()

    def _fetch_history_worker(self) -> None:
        messages = fetch_history(
            self._server_url, self._auth_token, self._user_id, self._rid, self._room_type, self._verify_ssl,
        )
        self._history_loaded.emit(messages)

    def _display_history(self, messages: list[dict]) -> None:
        if not messages:
            self._history_view.setPlainText(tr("quick_reply.no_history"))
            return
        lines = []
        for message in messages:
            sender = message.get("u", {}).get("username", "?")
            text = message.get("msg", "")
            lines.append(f"{sender}: {text}")
        self._history_view.setPlainText("\n".join(lines))
        scrollbar = self._history_view.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _handle_send(self) -> None:
        text = self._input.text().strip()
        if not text:
            return
        self._input.setEnabled(False)
        self._send_button.setEnabled(False)
        self._status_label.setVisible(False)
        threading.Thread(target=self._send_worker, args=(text,), daemon=True).start()

    def _send_worker(self, text: str) -> None:
        success, error = send_message(
            self._server_url, self._auth_token, self._user_id, self._rid, text, self._verify_ssl,
        )
        self._send_finished.emit(success, error)

    def _handle_send_finished(self, success: bool, error: str) -> None:
        self._input.setEnabled(True)
        self._send_button.setEnabled(True)
        if success:
            self._input.clear()
            threading.Thread(target=self._fetch_history_worker, daemon=True).start()
        else:
            self._status_label.setText(tr("quick_reply.send_failed", error=error))
            self._status_label.setVisible(True)
        self._input.setFocus()
