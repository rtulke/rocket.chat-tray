from __future__ import annotations

import html
import logging
import re
import threading
from datetime import datetime

import requests
from PySide6.QtCore import Qt, QSize, QUrl, Signal
from PySide6.QtGui import QBrush, QColor, QIcon, QKeySequence, QPainter, QPixmap, QTextDocument
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .i18n import tr

logger = logging.getLogger(__name__)

HISTORY_COUNT = 10
AVATAR_SIZE = 32
SIDEBAR_AVATAR_SIZE = 20
STATUS_DOT_SIZE = 10
SIDEBAR_MIN_WIDTH = 140
SIDEBAR_MAX_AUTO_WIDTH = 420  # cap for the auto-fit-to-widest-name default; the user can still drag past it
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
    fetched here -- see fetch_user_details, called separately per contact
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


def fetch_user_details(server_url: str, auth_token: str, user_id: str, username: str, verify_ssl: bool = True) -> dict:
    """{"status", "name", "active"} for `username` -- "status" drives the
    sidebar presence dot, "name" is the LDAP-synced full display name
    (users.info's own "name" field, distinct from "username"), "active"
    is False for a deactivated account (confirmed live: this is what an
    LDAP-removed user looks like via this API -- Rocket.Chat deactivates
    rather than deletes on sync by default). Best-effort neutral defaults
    ("offline"/""/True) on any failure, since none of this is essential --
    worst case a contact just shows its username with no status dot."""
    try:
        response = requests.get(
            f"{server_url}/api/v1/users.info", params={"username": username},
            headers={"X-Auth-Token": auth_token, "X-User-Id": user_id}, verify=verify_ssl, timeout=_HTTP_TIMEOUT,
        )
        response.raise_for_status()
        user = response.json().get("user", {})
        return {"status": user.get("status", "offline"), "name": user.get("name") or "", "active": user.get("active", True)}
    except (requests.RequestException, ValueError):
        return {"status": "offline", "name": "", "active": True}


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


# A leading "```<language>\n" tag (Rocket.Chat's own convention, e.g.
# "```bash") is metadata, not code -- stripped from the rendered block and
# shown as a small label above it instead. Requires a trailing newline so a
# one-line snippet like "```echo hi```" isn't mistaken for a bare language tag.
_LANG_TAG_RE = re.compile(r"^[ \t]*([A-Za-z0-9_+-]{1,20})[ \t]*\n")
_CODE_BLOCK_STYLE = (
    "background-color:#f5f5f5;border:1px solid #dddddd;border-radius:4px;"
    "padding:6px 8px;margin:4px 0;font-family:monospace;white-space:pre-wrap;"
)


def _render_code_block(raw: str) -> str:
    language, body = "", raw
    match = _LANG_TAG_RE.match(raw)
    if match:
        language, body = match.group(1), raw[match.end():]
    body = body.rstrip("\n")
    label = f'<div style="color:#888888;font-size:11px;">{html.escape(language)}</div>' if language else ""
    return f'{label}<pre style="{_CODE_BLOCK_STYLE}">{html.escape(body)}</pre>'


def _render_message_body(text: str) -> str:
    """Renders a message's text, turning ```-fenced spans into monospace
    code blocks (see _render_code_block) and leaving everything else to
    _linkify as before. text.split("```") always alternates
    text/code/text/code/... starting with text, regardless of whether the
    fence count is even or odd -- so a message with an odd number of ```
    (an unclosed trailing fence) naturally has its last segment land on a
    code index and render as code through to the end of the message,
    auto-closing it exactly like the real Rocket.Chat client does, with no
    special-casing needed."""
    parts = text.split("```")
    out = []
    for i, part in enumerate(parts):
        if i % 2 == 1:
            out.append(_render_code_block(part))
        elif part:
            out.append(_linkify(part))
    return "".join(out)


def _format_timestamp(ts: str) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%H:%M")
    except ValueError:
        return ""


def _format_message(message: dict, known_avatars: set[str], known_names: dict[str, str], show_full_names: bool) -> str:
    username = message.get("u", {}).get("username", "?")
    display_name = (known_names.get(username) if show_full_names else None) or username
    text = _render_message_body(message.get("msg", ""))
    time_str = _format_timestamp(message.get("ts", ""))
    avatar_src = f"avatar:{username}" if username in known_avatars else ""
    avatar_html = f'<img src="{avatar_src}" width="{AVATAR_SIZE}" height="{AVATAR_SIZE}">' if avatar_src else ""
    return (
        '<table cellspacing="0" cellpadding="0" style="margin-bottom:10px;"><tr>'
        f'<td valign="top" width="{AVATAR_SIZE + 8}">{avatar_html}</td>'
        f'<td valign="top"><b>{html.escape(display_name)}</b> '
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


class _HistoryBrowser(QTextBrowser):
    """QTextBrowser's default Ctrl+C/context-menu "Copy" includes a U+FFFC
    object-replacement character for every inline avatar <img> in the
    selection -- confirmed live: Ctrl+A, Ctrl+C into a text editor showed a
    stray box glyph per message. Overriding the public copy() slot alone
    turned out to be not enough: confirmed via a simulated Ctrl+C key event
    that Qt's internal text control handles the standard Copy *shortcut*
    itself, writing straight to the clipboard without ever calling this
    object's (overridable) copy() -- so the keyboard path additionally
    needs its own keyPressEvent interception, calling copy() explicitly and
    consuming the event so Qt's own handling doesn't run afterwards and
    clobber the clipboard right back. The right-click context menu's own
    "Copy" entry turned out to have the exact same problem -- also
    confirmed by triggering it programmatically in a test and inspecting
    the resulting clipboard content -- so contextMenuEvent() below rewires
    that one entry to call copy() too."""

    def copy(self) -> None:
        # QTextCursor.selectedText() encodes embedded objects (the avatar
        # <img> tags) as U+FFFC and paragraph/line breaks as U+2029, not a
        # plain "\n" -- translate both before handing the text to the
        # clipboard.
        text = self.textCursor().selectedText().replace("\ufffc", "").replace("\u2029", "\n")
        QApplication.clipboard().setText(text)

    def keyPressEvent(self, event) -> None:
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copy()
            event.accept()
            return
        super().keyPressEvent(event)

    def _standard_context_menu_with_copy_fix(self):
        """Split out from contextMenuEvent() so the rewiring itself is
        directly testable without going through QMenu.exec()'s blocking
        modal loop (there's nothing to click in an offscreen/headless
        test)."""
        menu = self.createStandardContextMenu()
        for action in menu.actions():
            label = action.text().split("\t")[0].replace("&", "").strip().lower()
            if label == "copy":
                action.triggered.disconnect()
                action.triggered.connect(self.copy)
                break
        return menu

    def contextMenuEvent(self, event) -> None:
        self._standard_context_menu_with_copy_fix().exec(event.globalPos())



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
    _dm_details_resolved = Signal(str, dict)  # username, {"status","name","active"} -- one at a time as each resolves
    _room_avatar_resolved = Signal(str, object)  # rid, avatar_bytes|None
    _dm_avatar_resolved = Signal(str, object)  # username, avatar_bytes|None
    _history_loaded = Signal(list, dict, dict)  # messages, {username: avatar_bytes}, {username: full_name}
    _send_finished = Signal(bool, str)
    _live_avatar_ready = Signal(str, object, str, dict)  # username, avatar_bytes|None, full_name, message

    def __init__(
        self, server_url: str, auth_token: str, user_id: str, own_username: str, worker, settings,
        initial_rid: str | None = None, initial_room_type: str | None = None, initial_title: str = "",
        verify_ssl: bool = True, parent=None,
    ):
        super().__init__(parent)
        self._server_url = server_url
        self._auth_token = auth_token
        self._user_id = user_id
        self._own_username = own_username
        self._worker = worker
        self._settings = settings
        self._verify_ssl = verify_ssl
        self._rid = initial_rid
        self._room_type = initial_room_type
        self._known_avatars: set[str] = set()  # usernames already registered as document resources
        self._known_names: dict[str, str] = {}  # username -> resolved full name, for message-sender display
        self._room_items: dict[str, QListWidgetItem] = {}  # rid -> its sidebar QListWidgetItem (channels/groups)
        self._dm_items: dict[str, QListWidgetItem] = {}  # username -> its sidebar QListWidgetItem
        self._dm_username_by_id: dict[str, str] = {}  # other-participant user_id -> username, for live presence
        self._dm_avatars: dict[str, QPixmap] = {}  # username -> resolved avatar, for re-compositing on info changes
        self._dm_info: dict[str, dict] = {}  # username -> {"status","name","active"}, for re-compositing on avatar arrival

        self.setWindowTitle(tr("chat_window.title", room=initial_title) if initial_title else tr("chat_window.title_generic"))
        self.setModal(False)
        self.resize(680, 440)

        # One continuous list for channels/groups/DMs (with in-list section
        # headers, see _make_header_item) rather than separate boxed
        # QListWidgets stacked on top of each other -- avoids the
        # look-like-several-mini-windows effect two independently-scrolling,
        # independently-bordered lists had.
        self._sidebar_list = QListWidget()
        self._sidebar_list.setMinimumWidth(SIDEBAR_MIN_WIDTH)
        self._sidebar_list.setIconSize(QSize(SIDEBAR_AVATAR_SIZE, SIDEBAR_AVATAR_SIZE))
        self._sidebar_list.itemClicked.connect(self._handle_sidebar_click)
        # Becomes True the moment the user drags the splitter handle
        # themselves -- from then on _apply_auto_sidebar_width() backs off
        # and leaves their chosen width alone instead of fighting it every
        # time the sidebar content changes (a new DM's full name resolving
        # and being wider than anything seen so far, etc.).
        self._sidebar_user_resized = False

        self._history_view = _HistoryBrowser()
        self._history_view.setOpenExternalLinks(True)
        self._history_view.setHtml(html.escape(tr("chat_window.select_room")) if not initial_rid else html.escape(tr("quick_reply.loading")))

        self._input = QLineEdit()
        self._input.setPlaceholderText(tr("quick_reply.placeholder"))
        self._input.setTextMargins(6, 0, 6, 0)  # placeholder/typed text otherwise sits flush against the field's edge
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
        chat_side_widget = QWidget()
        chat_side_widget.setLayout(chat_side)

        # A QSplitter (not a plain QHBoxLayout) so the user can drag the
        # boundary themselves; setChildrenCollapsible(False) stops a drag
        # from ever hiding the sidebar entirely by accident.
        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.addWidget(self._sidebar_list)
        self._splitter.addWidget(chat_side_widget)
        self._splitter.setChildrenCollapsible(False)
        self._splitter.setStretchFactor(0, 0)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.splitterMoved.connect(self._handle_splitter_moved)

        root = QHBoxLayout(self)
        root.addWidget(self._splitter)

        self._channels_loaded.connect(self._display_channels)
        self._dms_loaded.connect(self._display_dms)
        self._dm_details_resolved.connect(self._handle_dm_details_resolved)
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
            details = fetch_user_details(self._server_url, self._auth_token, self._user_id, dm["username"], self._verify_ssl)
            self._dm_details_resolved.emit(dm["username"], details)
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
        self._apply_auto_sidebar_width()

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
        self._apply_auto_sidebar_width()

    def _handle_splitter_moved(self, pos: int, index: int) -> None:
        self._sidebar_user_resized = True

    def _compute_sidebar_width(self) -> int:
        """The widest current row label (channels/groups/DMs/headers) plus
        room for the icon and ~2 average character widths of breathing
        room, per the user's request -- clamped to a sane range so an
        empty/near-empty list or one freak long name doesn't produce a
        degenerate width."""
        metrics = self._sidebar_list.fontMetrics()
        widest_text = 0
        for i in range(self._sidebar_list.count()):
            widest_text = max(widest_text, metrics.horizontalAdvance(self._sidebar_list.item(i).text()))
        icon_and_gap = SIDEBAR_AVATAR_SIZE + 12
        two_chars = metrics.averageCharWidth() * 2
        scrollbar_allowance = self._sidebar_list.verticalScrollBar().sizeHint().width() + 8
        width = widest_text + icon_and_gap + two_chars + scrollbar_allowance
        return max(SIDEBAR_MIN_WIDTH, min(width, SIDEBAR_MAX_AUTO_WIDTH))

    def _apply_auto_sidebar_width(self) -> None:
        if self._sidebar_user_resized:
            return
        width = self._compute_sidebar_width()
        total = sum(self._splitter.sizes()) or (width + 400)
        self._splitter.setSizes([width, max(1, total - width)])

    def _handle_room_avatar_resolved(self, rid: str, data: bytes | None) -> None:
        if not data:
            return
        item = self._room_items.get(rid)
        if not item:
            return
        pixmap = QPixmap()
        if pixmap.loadFromData(data):
            item.setIcon(_room_avatar_icon(pixmap))

    def _update_dm_row(self, username: str) -> None:
        item = self._dm_items.get(username)
        if not item:
            return
        info = self._dm_info.get(username, {})
        status = info.get("status", "offline")
        active = info.get("active", True)
        display_name = (info.get("name") if self._settings.show_full_names else "") or username

        font = item.font()
        if active:
            item.setHidden(False)
            item.setText(display_name)
            item.setForeground(QBrush())  # clears any override back to the view's default text colour
            item.setIcon(_avatar_status_icon(self._dm_avatars.get(username), status))
            font.setItalic(False)
        else:
            # Deactivated (typically LDAP-removed) account -- either hidden
            # entirely or shown clearly greyed-out/labelled, per the user's
            # settings toggle, rather than looking identical to an active
            # colleague. No presence dot: a gone account has no meaningful
            # live status regardless of its last-known one.
            item.setHidden(self._settings.hide_deactivated_users)
            item.setText(tr("chat_window.deactivated_label", name=display_name))
            item.setForeground(QColor("#aaaaaa"))
            item.setIcon(_avatar_status_icon(self._dm_avatars.get(username), "offline"))
            font.setItalic(True)
        item.setFont(font)
        self._apply_auto_sidebar_width()

    def _handle_dm_avatar_resolved(self, username: str, data: bytes | None) -> None:
        if data:
            pixmap = QPixmap()
            if pixmap.loadFromData(data):
                self._dm_avatars[username] = pixmap
        self._update_dm_row(username)

    def _handle_dm_details_resolved(self, username: str, details: dict) -> None:
        self._dm_info[username] = details
        self._update_dm_row(username)

    def _handle_presence_update(self, user_id: str, status: str) -> None:
        # Live presence pushes identify the user by id; _display_dms built
        # the id->username map from im.list's "uids" array (same order as
        # "usernames", no extra lookup needed) so this can update the right
        # sidebar row directly instead of only the currently-open DM.
        # setdefault+update rather than a plain assignment: preserves the
        # name/active fields already resolved by _handle_dm_details_resolved
        # instead of clobbering them with a status-only dict.
        username = self._dm_username_by_id.get(user_id)
        if not username:
            return
        self._dm_info.setdefault(username, {"status": "offline", "name": "", "active": True})["status"] = status
        self._update_dm_row(username)

    def apply_settings(self) -> None:
        """Re-renders sidebar DM rows against already-cached data (no
        network I/O) after the settings dialog closes, so a show_full_names/
        hide_deactivated_users change while this window is already open
        takes effect immediately rather than only on the next room/window
        open. Called from main.py. The message view's own full-name display
        is deliberately not retroactively re-rendered here -- only future
        messages pick up the change -- to avoid needing to keep the raw
        message list around just for this."""
        for username in list(self._dm_items):
            self._update_dm_row(username)

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
        names: dict[str, str] = {}
        for message in messages:
            username = message.get("u", {}).get("username")
            if not username:
                continue
            if username not in self._known_avatars and username not in avatars:
                data = fetch_avatar(self._server_url, username, self._verify_ssl)
                if data:
                    avatars[username] = data
            if self._settings.show_full_names and username not in self._known_names and username not in names:
                name = fetch_user_details(self._server_url, self._auth_token, self._user_id, username, self._verify_ssl).get("name")
                if name:
                    names[username] = name
        self._history_loaded.emit(messages, avatars, names)

    def _register_avatar(self, username: str, data: bytes) -> bool:
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            return False
        self._history_view.document().addResource(QTextDocument.ResourceType.ImageResource, QUrl(f"avatar:{username}"), pixmap)
        self._known_avatars.add(username)
        return True

    def _display_history(self, messages: list[dict], avatars: dict[str, bytes], names: dict[str, str]) -> None:
        for username, data in avatars.items():
            self._register_avatar(username, data)
        self._known_names.update(names)

        if not messages:
            self._history_view.setHtml(html.escape(tr("quick_reply.no_history")))
            return
        self._history_view.setHtml(
            "".join(_format_message(m, self._known_avatars, self._known_names, self._settings.show_full_names) for m in messages)
        )
        scrollbar = self._history_view.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    # --- live push (from RocketChatWorker.room_message_received) --------------

    def _handle_live_message(self, message: dict) -> None:
        if message.get("rid") != self._rid:
            return
        sender = message.get("u", {}).get("username")
        needs_avatar = bool(sender) and sender not in self._known_avatars
        needs_name = bool(sender) and self._settings.show_full_names and sender not in self._known_names
        if needs_avatar or needs_name:
            threading.Thread(target=self._fetch_live_avatar, args=(sender, message), daemon=True).start()
        else:
            self._append_message(message)

    def _fetch_live_avatar(self, sender: str, message: dict) -> None:
        data = fetch_avatar(self._server_url, sender, self._verify_ssl) if sender not in self._known_avatars else None
        name = ""
        if self._settings.show_full_names and sender not in self._known_names:
            name = fetch_user_details(self._server_url, self._auth_token, self._user_id, sender, self._verify_ssl).get("name", "")
        self._live_avatar_ready.emit(sender, data, name, message)

    def _handle_live_avatar_ready(self, sender: str, data: bytes | None, name: str, message: dict) -> None:
        if data:
            self._register_avatar(sender, data)
        if name:
            self._known_names[sender] = name
        self._append_message(message)

    def _append_message(self, message: dict) -> None:
        self._history_view.append(_format_message(message, self._known_avatars, self._known_names, self._settings.show_full_names))
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
