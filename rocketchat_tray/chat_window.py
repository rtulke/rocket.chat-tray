from __future__ import annotations

import html
import logging
import re
import threading
from datetime import datetime

import requests
from PySide6.QtCore import Qt, QSize, QUrl, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap, QTextDocument
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

from .i18n import tr

logger = logging.getLogger(__name__)

HISTORY_COUNT = 10
AVATAR_SIZE = 32
SIDEBAR_AVATAR_SIZE = 20
STATUS_DOT_SIZE = 10
_HTTP_TIMEOUT = 10

# Matches rocketchat_tray/resources/icons/bubble-*.svg exactly, so the DM
# sidebar's status dots read as the same colour language as the tray icon.
_STATUS_COLORS = {
    "online": "#2ecc71",
    "away": "#f39c12",
    "busy": "#e74c3c",
    "offline": "#9e9e9e",
}

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


def fetch_room_avatar(server_url: str, rid: str, verify_ssl: bool = True) -> bytes | None:
    """Same contract as fetch_avatar above, but for a channel/group's own
    avatar -- confirmed live against the real server: /avatar/room/<rid> is
    unauthenticated and always returns something (a custom-uploaded image or
    a generated placeholder SVG), same as /avatar/<username>."""
    try:
        response = requests.get(f"{server_url}/avatar/room/{rid}", verify=verify_ssl, timeout=_HTTP_TIMEOUT)
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Konnte Raum-Avatar fuer %s nicht laden: %s", rid, exc)
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


def run_command(
    server_url: str, auth_token: str, user_id: str, rid: str, command: str, params: str, verify_ssl: bool = True,
) -> tuple[bool, str]:
    """Slash commands (e.g. "/shrug", "/status away") are NOT processed by
    chat.postMessage -- confirmed live: posting text starting with "/"
    there just sends it as a literal message. Rocket.Chat has a separate
    endpoint for actually executing a registered command."""
    try:
        response = requests.post(
            f"{server_url}/api/v1/commands.run",
            json={"command": command, "params": params, "roomId": rid},
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


def fetch_channels(server_url: str, auth_token: str, user_id: str, verify_ssl: bool = True) -> list[dict]:
    """Public channels + private groups the user has joined. Each entry:
    {"rid", "name", "room_type": "c"|"p"}, sorted by name. Best-effort per
    endpoint -- e.g. a user with no private groups still gets their
    channels even if groups.list 403s for some permission reason."""
    headers = {"X-Auth-Token": auth_token, "X-User-Id": user_id}
    channels: list[dict] = []
    try:
        response = requests.get(
            f"{server_url}/api/v1/channels.list.joined", headers=headers, verify=verify_ssl, timeout=_HTTP_TIMEOUT,
        )
        response.raise_for_status()
        for c in response.json().get("channels", []):
            channels.append({"rid": c["_id"], "name": c.get("name", "?"), "room_type": "c"})
    except (requests.RequestException, ValueError, KeyError) as exc:
        logger.warning("Konnte Channel-Liste nicht laden: %s", exc)
    try:
        response = requests.get(
            f"{server_url}/api/v1/groups.list", headers=headers, verify=verify_ssl, timeout=_HTTP_TIMEOUT,
        )
        response.raise_for_status()
        for g in response.json().get("groups", []):
            channels.append({"rid": g["_id"], "name": g.get("name", "?"), "room_type": "p"})
    except (requests.RequestException, ValueError, KeyError) as exc:
        logger.warning("Konnte Gruppen-Liste nicht laden: %s", exc)
    channels.sort(key=lambda c: c["name"].lower())
    return channels


def fetch_direct_messages(
    server_url: str, auth_token: str, user_id: str, own_username: str, verify_ssl: bool = True,
) -> list[dict]:
    """1:1 DM rooms, each: {"rid", "username", "user_id"} (the *other*
    participant; for the self-notes room, own_username/user_id). Sorted
    by username. im.list conveniently returns both "usernames" and
    "uids" in matching order, so the other participant's user id -- needed
    to match live presence pushes (which identify the user by id, not
    name) to the right sidebar row -- comes for free without a second
    lookup per contact. Presence *status* itself is deliberately not
    fetched here -- see fetch_user_status, called separately per contact
    so the sidebar can render immediately and fill in status dots as they
    resolve."""
    headers = {"X-Auth-Token": auth_token, "X-User-Id": user_id}
    dms: list[dict] = []
    try:
        response = requests.get(
            f"{server_url}/api/v1/im.list", headers=headers, verify=verify_ssl, timeout=_HTTP_TIMEOUT,
        )
        response.raise_for_status()
        for room in response.json().get("ims", []):
            usernames = room.get("usernames", [])
            uids = room.get("uids", [])
            other_index = next((i for i, u in enumerate(usernames) if u != own_username), None)
            if other_index is not None and other_index < len(uids):
                username, other_id = usernames[other_index], uids[other_index]
            else:
                username, other_id = own_username, user_id  # self-notes room
            dms.append({"rid": room["_id"], "username": username, "user_id": other_id})
    except (requests.RequestException, ValueError, KeyError) as exc:
        logger.warning("Konnte Direktnachrichten-Liste nicht laden: %s", exc)
    dms.sort(key=lambda d: d["username"].lower())
    return dms


def fetch_user_status(server_url: str, auth_token: str, user_id: str, username: str, verify_ssl: bool = True) -> str:
    """Best-effort; "offline" (the neutral/unknown-looking dot) on any
    failure rather than raising, since this drives a decorative sidebar
    icon, not anything essential."""
    try:
        response = requests.get(
            f"{server_url}/api/v1/users.info", params={"username": username},
            headers={"X-Auth-Token": auth_token, "X-User-Id": user_id}, verify=verify_ssl, timeout=_HTTP_TIMEOUT,
        )
        response.raise_for_status()
        return response.json().get("user", {}).get("status", "offline")
    except (requests.RequestException, ValueError):
        return "offline"


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


def _format_message(message: dict, known_avatars: set[str]) -> str:
    sender = message.get("u", {}).get("username", "?")
    text = _linkify(message.get("msg", ""))
    time_str = _format_timestamp(message.get("ts", ""))
    avatar_src = f"avatar:{sender}" if sender in known_avatars else ""
    avatar_html = f'<img src="{avatar_src}" width="{AVATAR_SIZE}" height="{AVATAR_SIZE}">' if avatar_src else ""
    return (
        '<table cellspacing="0" cellpadding="0" style="margin-bottom:10px;"><tr>'
        f'<td valign="top" width="{AVATAR_SIZE + 8}">{avatar_html}</td>'
        f'<td valign="top"><b>{html.escape(sender)}</b> '
        f'<span style="color:#888888;">{html.escape(time_str)}</span><br>{text}</td>'
        "</tr></table>"
    )


def _status_dot_icon(status: str) -> QIcon:
    """A bare status dot, centred on a SIDEBAR_AVATAR_SIZE canvas so it lines
    up with _avatar_status_icon's dot position -- used as the fallback for a
    DM contact whose avatar hasn't resolved yet (or failed to load)."""
    pixmap = QPixmap(SIDEBAR_AVATAR_SIZE, SIDEBAR_AVATAR_SIZE)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(_STATUS_COLORS.get(status, _STATUS_COLORS["offline"])))
    offset = (SIDEBAR_AVATAR_SIZE - STATUS_DOT_SIZE) // 2
    painter.drawEllipse(offset, offset, STATUS_DOT_SIZE, STATUS_DOT_SIZE)
    painter.end()
    return QIcon(pixmap)


def _centered_avatar_canvas(pixmap: QPixmap) -> tuple[QPixmap, QPainter]:
    scaled = pixmap.scaled(
        SIDEBAR_AVATAR_SIZE, SIDEBAR_AVATAR_SIZE, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
    )
    canvas = QPixmap(SIDEBAR_AVATAR_SIZE, SIDEBAR_AVATAR_SIZE)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    x = (SIDEBAR_AVATAR_SIZE - scaled.width()) // 2
    y = (SIDEBAR_AVATAR_SIZE - scaled.height()) // 2
    painter.drawPixmap(x, y, scaled)
    return canvas, painter


def _room_avatar_icon(pixmap: QPixmap) -> QIcon:
    canvas, painter = _centered_avatar_canvas(pixmap)
    painter.end()
    return QIcon(canvas)


def _avatar_status_icon(avatar: QPixmap | None, status: str) -> QIcon:
    """A DM contact's sidebar icon: their avatar with a small status-colour
    dot in the bottom-right corner, so the photo and presence are both
    visible at a glance. Falls back to a bare dot (_status_dot_icon) until
    the avatar itself has resolved."""
    if avatar is None:
        return _status_dot_icon(status)
    canvas, painter = _centered_avatar_canvas(avatar)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(_STATUS_COLORS.get(status, _STATUS_COLORS["offline"])))
    painter.drawEllipse(SIDEBAR_AVATAR_SIZE - STATUS_DOT_SIZE, SIDEBAR_AVATAR_SIZE - STATUS_DOT_SIZE, STATUS_DOT_SIZE, STATUS_DOT_SIZE)
    painter.end()
    return QIcon(canvas)


_ROOM_ROLE = Qt.ItemDataRole.UserRole


class ChatWindow(QDialog):
    """Native chat window: a sidebar (joined channels/groups, direct
    messages with a live presence dot) on the left, and the previously
    standalone quick-reply view (avatar/name/time per message, linkified
    URLs, an input line -- now also understanding "/slash commands") on
    the right. Opened from the tray's "Verpasste Nachrichten" submenu
    (pre-selecting that room) or a dedicated "Chat" menu entry.

    Singleton by convention (see main.open_chat_window): only one room's
    stream-room-messages can be subscribed at a time (RocketChatWorker.
    set_active_room), so a second independent window would fight the
    first over which room is "live". All network I/O (history/avatar/
    sidebar/status fetch, send, commands) runs on background threads;
    only the signal handlers touch widgets, same threading pattern as
    RoomOpener/PresenceCoordinator elsewhere in this app. Live push
    (worker.room_message_received/user_status_changed) arrives already
    queued-connection-safe since RocketChatWorker is a QObject and these
    are bound-method connections -- see main.py's own worker wiring for
    the same pattern."""

    _channels_loaded = Signal(list)
    _dms_loaded = Signal(list)
    _status_resolved = Signal(str, str)  # username, status -- one at a time as each resolves
    _room_avatar_resolved = Signal(str, object)  # rid, avatar_bytes|None
    _dm_avatar_resolved = Signal(str, object)  # username, avatar_bytes|None
    _history_loaded = Signal(list, dict)  # messages, {username: avatar_bytes}
    _send_finished = Signal(bool, str)
    _live_avatar_ready = Signal(str, object, dict)  # username, avatar_bytes|None, message

    def __init__(
        self, server_url: str, auth_token: str, user_id: str, own_username: str, worker,
        initial_rid: str | None = None, initial_room_type: str | None = None, initial_title: str = "",
        verify_ssl: bool = True, parent=None,
    ):
        super().__init__(parent)
        self._server_url = server_url
        self._auth_token = auth_token
        self._user_id = user_id
        self._own_username = own_username
        self._worker = worker
        self._verify_ssl = verify_ssl
        self._rid = initial_rid
        self._room_type = initial_room_type
        self._known_avatars: set[str] = set()  # usernames already registered as document resources
        self._room_items: dict[str, QListWidgetItem] = {}  # rid -> its sidebar QListWidgetItem (channels/groups)
        self._dm_items: dict[str, QListWidgetItem] = {}  # username -> its sidebar QListWidgetItem
        self._dm_username_by_id: dict[str, str] = {}  # other-participant user_id -> username, for live presence
        self._dm_avatars: dict[str, QPixmap] = {}  # username -> resolved avatar, for re-compositing on status changes
        self._dm_status: dict[str, str] = {}  # username -> last-known status, for re-compositing on avatar arrival

        self.setWindowTitle(tr("chat_window.title", room=initial_title) if initial_title else tr("chat_window.title_generic"))
        self.setModal(False)
        self.resize(680, 440)

        # One continuous list for channels/groups/DMs (with in-list section
        # headers, see _make_header_item) rather than separate boxed
        # QListWidgets stacked on top of each other -- avoids the
        # look-like-several-mini-windows effect two independently-scrolling,
        # independently-bordered lists had.
        self._sidebar_list = QListWidget()
        self._sidebar_list.setMaximumWidth(220)
        self._sidebar_list.setIconSize(QSize(SIDEBAR_AVATAR_SIZE, SIDEBAR_AVATAR_SIZE))
        self._sidebar_list.itemClicked.connect(self._handle_sidebar_click)

        self._history_view = QTextBrowser()
        self._history_view.setOpenExternalLinks(True)
        self._history_view.setHtml(html.escape(tr("chat_window.select_room")) if not initial_rid else html.escape(tr("quick_reply.loading")))

        self._input = QLineEdit()
        self._input.setPlaceholderText(tr("quick_reply.placeholder"))
        self._input.returnPressed.connect(self._handle_send)

        self._send_button = QPushButton(tr("quick_reply.send"))
        self._send_button.setDefault(True)
        self._send_button.clicked.connect(self._handle_send)
        # QPushButton's own sizeHint tends to come out taller than QLineEdit's
        # under most styles (extra padding for the button "chrome") -- match
        # the input's height to the button's rather than the other way round,
        # so the input doesn't shrink text-wise, it just gains padding.
        self._input.setFixedHeight(self._send_button.sizeHint().height())

        self._status_label = QLabel()
        self._status_label.setVisible(False)

        input_row = QHBoxLayout()
        input_row.addWidget(self._input, stretch=1)
        input_row.addWidget(self._send_button)

        chat_side = QVBoxLayout()
        chat_side.addWidget(self._history_view, stretch=1)
        chat_side.addWidget(self._status_label)
        chat_side.addLayout(input_row)

        root = QHBoxLayout(self)
        root.addWidget(self._sidebar_list)
        root.addLayout(chat_side, stretch=1)

        self._channels_loaded.connect(self._display_channels)
        self._dms_loaded.connect(self._display_dms)
        self._status_resolved.connect(self._handle_status_resolved)
        self._room_avatar_resolved.connect(self._handle_room_avatar_resolved)
        self._dm_avatar_resolved.connect(self._handle_dm_avatar_resolved)
        self._history_loaded.connect(self._display_history)
        self._send_finished.connect(self._handle_send_finished)
        self._live_avatar_ready.connect(self._handle_live_avatar_ready)

        self._worker.room_message_received.connect(self._handle_live_message)
        self._worker.user_status_changed.connect(self._handle_presence_update)

        self._input.setFocus()
        threading.Thread(target=self._fetch_sidebar_worker, daemon=True).start()
        if self._rid:
            self._worker.set_active_room(self._rid)
            threading.Thread(target=self._fetch_history_worker, daemon=True).start()

    def closeEvent(self, event) -> None:
        self._worker.set_active_room(None)
        self._worker.room_message_received.disconnect(self._handle_live_message)
        self._worker.user_status_changed.disconnect(self._handle_presence_update)
        super().closeEvent(event)

    # --- sidebar population -------------------------------------------------
    #
    # Fetch order matters here: _fetch_sidebar_worker runs once per window
    # lifetime (kicked off from __init__ only, no live channel-list refresh
    # exists yet), and _display_channels() clears+rebuilds the *entire*
    # sidebar list -- safe only because it always runs before _display_dms()
    # populates the DM section below it. If a manual sidebar refresh is ever
    # added, this ordering assumption needs revisiting.

    def _fetch_sidebar_worker(self) -> None:
        channels = fetch_channels(self._server_url, self._auth_token, self._user_id, self._verify_ssl)
        self._channels_loaded.emit(channels)
        for c in channels:
            avatar_data = fetch_room_avatar(self._server_url, c["rid"], self._verify_ssl)
            self._room_avatar_resolved.emit(c["rid"], avatar_data)

        dms = fetch_direct_messages(self._server_url, self._auth_token, self._user_id, self._own_username, self._verify_ssl)
        self._dms_loaded.emit(dms)
        for dm in dms:
            status = fetch_user_status(self._server_url, self._auth_token, self._user_id, dm["username"], self._verify_ssl)
            self._status_resolved.emit(dm["username"], status)
            avatar_data = fetch_avatar(self._server_url, dm["username"], self._verify_ssl)
            self._dm_avatar_resolved.emit(dm["username"], avatar_data)

    def _add_sidebar_section(self, label: str, rooms: list[dict]) -> None:
        self._sidebar_list.addItem(_make_header_item(label))
        for c in rooms:
            item = QListWidgetItem(c["name"])
            item.setData(_ROOM_ROLE, (c["rid"], c["room_type"], c["name"]))
            self._sidebar_list.addItem(item)
            self._room_items[c["rid"]] = item
            if c["rid"] == self._rid:
                self._sidebar_list.setCurrentItem(item)

    def _display_channels(self, channels: list[dict]) -> None:
        self._sidebar_list.clear()
        self._room_items.clear()
        self._add_sidebar_section(tr("chat_window.channels_section"), [c for c in channels if c["room_type"] == "c"])
        self._add_sidebar_section(tr("chat_window.groups_section"), [c for c in channels if c["room_type"] == "p"])

    def _display_dms(self, dms: list[dict]) -> None:
        self._dm_items.clear()
        self._dm_username_by_id.clear()
        self._sidebar_list.addItem(_make_header_item(tr("chat_window.dms_section")))
        for dm in dms:
            item = QListWidgetItem(_status_dot_icon("offline"), dm["username"])
            item.setData(_ROOM_ROLE, (dm["rid"], "d", dm["username"]))
            self._sidebar_list.addItem(item)
            self._dm_items[dm["username"]] = item
            self._dm_username_by_id[dm["user_id"]] = dm["username"]
            if dm["rid"] == self._rid:
                self._sidebar_list.setCurrentItem(item)

    def _handle_room_avatar_resolved(self, rid: str, data: bytes | None) -> None:
        if not data:
            return
        item = self._room_items.get(rid)
        if not item:
            return
        pixmap = QPixmap()
        if pixmap.loadFromData(data):
            item.setIcon(_room_avatar_icon(pixmap))

    def _update_dm_icon(self, username: str) -> None:
        item = self._dm_items.get(username)
        if item:
            item.setIcon(_avatar_status_icon(self._dm_avatars.get(username), self._dm_status.get(username, "offline")))

    def _handle_dm_avatar_resolved(self, username: str, data: bytes | None) -> None:
        if data:
            pixmap = QPixmap()
            if pixmap.loadFromData(data):
                self._dm_avatars[username] = pixmap
        self._update_dm_icon(username)

    def _handle_status_resolved(self, username: str, status: str) -> None:
        self._dm_status[username] = status
        self._update_dm_icon(username)

    def _handle_presence_update(self, user_id: str, status: str) -> None:
        # Live presence pushes identify the user by id; _display_dms built
        # the id->username map from im.list's "uids" array (same order as
        # "usernames", no extra lookup needed) so this can update the right
        # sidebar row directly instead of only the currently-open DM.
        username = self._dm_username_by_id.get(user_id)
        if username:
            self._handle_status_resolved(username, status)

    def _handle_sidebar_click(self, item: QListWidgetItem) -> None:
        data = item.data(_ROOM_ROLE)
        if data is None:  # a non-selectable section header (see _make_header_item)
            return
        rid, room_type, title = data
        if rid == self._rid:
            return
        self.select_room(rid, room_type, title)

    def select_room(self, rid: str, room_type: str, title: str) -> None:
        """Public: also called from main.py when reusing an already-open
        window (see open_chat_window) instead of opening a second one."""
        self._rid = rid
        self._room_type = room_type
        self.setWindowTitle(tr("chat_window.title", room=title))
        self._worker.set_active_room(rid)
        self._history_view.setHtml(html.escape(tr("quick_reply.loading")))
        threading.Thread(target=self._fetch_history_worker, daemon=True).start()

    # --- message history ------------------------------------------------------

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

    def _register_avatar(self, username: str, data: bytes) -> bool:
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            return False
        self._history_view.document().addResource(QTextDocument.ResourceType.ImageResource, QUrl(f"avatar:{username}"), pixmap)
        self._known_avatars.add(username)
        return True

    def _display_history(self, messages: list[dict], avatars: dict[str, bytes]) -> None:
        for username, data in avatars.items():
            self._register_avatar(username, data)

        if not messages:
            self._history_view.setHtml(html.escape(tr("quick_reply.no_history")))
            return
        self._history_view.setHtml("".join(_format_message(m, self._known_avatars) for m in messages))
        scrollbar = self._history_view.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    # --- live push (from RocketChatWorker.room_message_received) --------------

    def _handle_live_message(self, message: dict) -> None:
        if message.get("rid") != self._rid:
            return
        sender = message.get("u", {}).get("username")
        if sender and sender not in self._known_avatars:
            threading.Thread(target=self._fetch_live_avatar, args=(sender, message), daemon=True).start()
        else:
            self._append_message(message)

    def _fetch_live_avatar(self, sender: str, message: dict) -> None:
        data = fetch_avatar(self._server_url, sender, self._verify_ssl)
        self._live_avatar_ready.emit(sender, data, message)

    def _handle_live_avatar_ready(self, sender: str, data: bytes | None, message: dict) -> None:
        if data:
            self._register_avatar(sender, data)
        self._append_message(message)

    def _append_message(self, message: dict) -> None:
        self._history_view.append(_format_message(message, self._known_avatars))
        scrollbar = self._history_view.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    # --- sending ----------------------------------------------------------

    def _handle_send(self) -> None:
        text = self._input.text().strip()
        if not text or not self._rid:
            return
        self._input.setEnabled(False)
        self._send_button.setEnabled(False)
        self._status_label.setVisible(False)
        if text.startswith("/"):
            parts = text[1:].split(maxsplit=1)
            command = parts[0] if parts else ""
            params = parts[1] if len(parts) > 1 else ""
            threading.Thread(target=self._command_worker, args=(command, params), daemon=True).start()
        else:
            threading.Thread(target=self._send_worker, args=(text,), daemon=True).start()

    def _send_worker(self, text: str) -> None:
        success, error = send_message(
            self._server_url, self._auth_token, self._user_id, self._rid, text, self._verify_ssl,
        )
        self._send_finished.emit(success, error)

    def _command_worker(self, command: str, params: str) -> None:
        success, error = run_command(
            self._server_url, self._auth_token, self._user_id, self._rid, command, params, self._verify_ssl,
        )
        self._send_finished.emit(success, error)

    def _handle_send_finished(self, success: bool, error: str) -> None:
        self._input.setEnabled(True)
        self._send_button.setEnabled(True)
        if success:
            self._input.clear()
            # Not relying solely on the live stream to show our own
            # just-sent message/command result: set_active_room()'s
            # subscription can take a few seconds to take effect after
            # opening/switching rooms (see RocketChatWorker docs), so a
            # message sent right away might not echo back live in time.
            # A plain REST refetch is simple and always correct; any
            # separate live push that already arrived in the meantime is
            # just superseded by it (this rebuilds the whole view).
            threading.Thread(target=self._fetch_history_worker, daemon=True).start()
        else:
            self._status_label.setText(tr("quick_reply.send_failed", error=error))
            self._status_label.setVisible(True)
        self._input.setFocus()


def _make_header_item(text: str) -> QListWidgetItem:
    """A non-clickable section heading (e.g. "Kanäle") living directly in
    the sidebar list, rather than a separate QLabel above a separate list
    box -- keeps channels/groups/DMs as one continuous scrollable list
    instead of looking like several stacked-but-separate mini windows."""
    item = QListWidgetItem(text)
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
    font = item.font()
    font.setBold(True)
    item.setFont(font)
    item.setForeground(QColor("#888888"))
    return item
