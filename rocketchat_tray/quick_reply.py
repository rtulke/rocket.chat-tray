from __future__ import annotations

import html
import logging
import re
import threading
from datetime import datetime

import requests
from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QPixmap, QTextDocument
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

from .i18n import tr

logger = logging.getLogger(__name__)

HISTORY_COUNT = 10
AVATAR_SIZE = 32
_HTTP_TIMEOUT = 10

# Rocket.Chat splits message history across three endpoints by room type
# (room.t: "d"/"c"/"p", matching deeplink.py's room_type) -- chat.postMessage
# below, by contrast, is one single endpoint that works for all of them.
_HISTORY_ENDPOINT_BY_TYPE = {
    "d": "im.history",
    "c": "channels.history",
    "p": "groups.history",
}

# mailto is matched separately since it's conventionally "mailto:addr", not
# "mailto://addr" -- both still end up recognised either way.
_URL_RE = re.compile(r'((?:https?|ftps?|ssh|run)://[^\s<>"]+|mailto:[^\s<>"]+)')
_URL_TRAILING_PUNCT = ".,;:!?)]}'\""


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


def fetch_avatar(server_url: str, username: str, verify_ssl: bool = True) -> bytes | None:
    """Rocket.Chat's /avatar/<username> is unauthenticated and always
    returns *something* -- a real uploaded/initials avatar (JPEG) for a
    known user, or a generated placeholder (SVG) otherwise -- confirmed
    live against the real server, both load fine via QPixmap.loadFromData.
    None only on an actual network failure."""
    try:
        response = requests.get(f"{server_url}/avatar/{username}", verify=verify_ssl, timeout=_HTTP_TIMEOUT)
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Konnte Avatar fuer %s nicht laden: %s", username, exc)
        return None
    return response.content


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


def _linkify(text: str) -> str:
    """HTML-escapes `text` while turning ssh/ftp(s)/run/mailto/http(s) URLs
    into clickable <a> tags and preserving line breaks. Escaping and link
    detection have to happen together, not escape-then-regex: html.escape
    would otherwise mangle "&" inside query strings before the URL regex
    (or the browser) ever sees them."""
    parts = _URL_RE.split(text)
    out = []
    for i, part in enumerate(parts):
        if i % 2 == 1:  # odd indices are the regex's capture group: a URL
            trail = ""
            while part and part[-1] in _URL_TRAILING_PUNCT:
                trail = part[-1] + trail
                part = part[:-1]
            escaped = html.escape(part, quote=True)
            out.append(f'<a href="{escaped}">{escaped}</a>{html.escape(trail)}')
        else:
            out.append(html.escape(part))
    return "".join(out).replace("\n", "<br>")


def _format_timestamp(ts: str) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%H:%M")
    except ValueError:
        return ""


class QuickReplyDialog(QDialog):
    """Small native reply window opened from the tray's "Verpasste
    Nachrichten" submenu -- shows the last few messages (with sender
    avatar/name/time, wrapped text, and clickable links) for context and
    lets the user send a reply via Rocket.Chat's REST API directly,
    without needing a browser. All network I/O (history/avatar fetch,
    send) runs on background threads; only the signal handlers below touch
    widgets, same threading pattern as RoomOpener/PresenceCoordinator
    elsewhere in this app."""

    _history_loaded = Signal(list, dict)  # messages, {username: avatar_bytes}
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
        self._known_avatars: set[str] = set()  # usernames already registered as document resources

        self.setWindowTitle(tr("quick_reply.title", sender=title))
        self.setModal(False)
        self.resize(420, 360)

        self._history_view = QTextBrowser()
        self._history_view.setOpenExternalLinks(True)
        self._history_view.setHtml(html.escape(tr("quick_reply.loading")))

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
        avatars: dict[str, bytes] = {}
        for message in messages:
            username = message.get("u", {}).get("username")
            if username and username not in self._known_avatars and username not in avatars:
                data = fetch_avatar(self._server_url, username, self._verify_ssl)
                if data:
                    avatars[username] = data
        self._history_loaded.emit(messages, avatars)

    def _format_message(self, message: dict) -> str:
        sender = message.get("u", {}).get("username", "?")
        text = _linkify(message.get("msg", ""))
        time_str = _format_timestamp(message.get("ts", ""))
        avatar_src = f"avatar:{sender}" if sender in self._known_avatars else ""
        avatar_html = (
            f'<img src="{avatar_src}" width="{AVATAR_SIZE}" height="{AVATAR_SIZE}">' if avatar_src else ""
        )
        return (
            '<table cellspacing="0" cellpadding="0" style="margin-bottom:10px;"><tr>'
            f'<td valign="top" width="{AVATAR_SIZE + 8}">{avatar_html}</td>'
            f'<td valign="top"><b>{html.escape(sender)}</b> '
            f'<span style="color:#888888;">{html.escape(time_str)}</span><br>{text}</td>'
            "</tr></table>"
        )

    def _display_history(self, messages: list[dict], avatars: dict[str, bytes]) -> None:
        document = self._history_view.document()
        for username, data in avatars.items():
            pixmap = QPixmap()
            if pixmap.loadFromData(data):
                document.addResource(QTextDocument.ResourceType.ImageResource, QUrl(f"avatar:{username}"), pixmap)
                self._known_avatars.add(username)

        if not messages:
            self._history_view.setHtml(html.escape(tr("quick_reply.no_history")))
            return
        self._history_view.setHtml("".join(self._format_message(m) for m in messages))
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
