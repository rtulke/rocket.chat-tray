from __future__ import annotations

import getpass
import logging

import keyring
import keyring.errors
import requests
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from .i18n import tr

logger = logging.getLogger(__name__)

KEYRING_SERVICE = "rocketchat-tray"


class LoginError(Exception):
    """Raised when a Rocket.Chat login attempt fails. str(exc) is
    "invalid_credentials" for a bad username/password, otherwise a
    human-readable message."""


def current_username() -> str:
    return getpass.getuser()


def get_stored_password(username: str | None = None) -> str | None:
    return keyring.get_password(KEYRING_SERVICE, username or current_username())


def set_stored_password(password: str, username: str | None = None) -> None:
    keyring.set_password(KEYRING_SERVICE, username or current_username(), password)


def delete_stored_password(username: str | None = None) -> None:
    try:
        keyring.delete_password(KEYRING_SERVICE, username or current_username())
    except keyring.errors.PasswordDeleteError:
        pass


def rest_login(server_url: str, username: str, password: str, verify_ssl: bool = True) -> tuple[str, str]:
    """Log in via the Rocket.Chat REST API. Returns (auth_token, user_id)."""
    try:
        response = requests.post(
            f"{server_url}/api/v1/login",
            json={"user": username, "password": password},
            verify=verify_ssl,
            timeout=15,
        )
    except requests.RequestException as exc:
        raise LoginError(tr("auth.server_unreachable", url=server_url, error=exc)) from exc

    if response.status_code == 401:
        raise LoginError("invalid_credentials")
    if response.status_code != 200:
        raise LoginError(tr("auth.unexpected_response", status=response.status_code))

    try:
        body = response.json()
    except ValueError as exc:
        raise LoginError(tr("auth.unexpected_response_no_json")) from exc

    if body.get("status") != "success":
        raise LoginError(body.get("message", tr("auth.failed_title")))

    data = body["data"]
    return data["authToken"], data["userId"]


class LoginDialog(QDialog):
    """First-run / re-auth dialog. Username is read-only (comes from the OS);
    the password is only written to the keyring after a successful login."""

    def __init__(self, server_url: str, verify_ssl: bool, parent=None):
        super().__init__(parent)
        self._server_url = server_url
        self._verify_ssl = verify_ssl

        self.setWindowTitle(tr("auth.dialog_title"))
        self.setModal(True)

        self._username_field = QLineEdit(current_username())
        self._username_field.setReadOnly(True)
        self._password_field = QLineEdit()
        self._password_field.setEchoMode(QLineEdit.EchoMode.Password)

        form = QFormLayout()
        form.addRow(tr("auth.username_label"), self._username_field)
        form.addRow(tr("auth.password_label"), self._password_field)

        self._status_label = QLabel(tr("auth.server_label", url=server_url))
        self._status_label.setWordWrap(True)

        # Plain QPushButtons, not QDialogButtonBox standard buttons: under
        # GNOME's GTK/Adwaita Qt theme, standard Ok/Cancel buttons get
        # auto-assigned icons the user explicitly didn't want on any dialog.
        login_button = QPushButton(tr("auth.login_button"))
        login_button.setDefault(True)
        login_button.clicked.connect(self._attempt_login)
        self._password_field.returnPressed.connect(self._attempt_login)
        cancel_button = QPushButton(tr("common.cancel"))
        cancel_button.clicked.connect(self.reject)
        self._buttons = (login_button, cancel_button)

        button_row = QHBoxLayout()
        button_row.addStretch()
        button_row.addWidget(cancel_button)
        button_row.addWidget(login_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self._status_label)
        layout.addLayout(form)
        layout.addLayout(button_row)

        self._password_field.setFocus()

    def _attempt_login(self) -> None:
        password = self._password_field.text()
        if not password:
            return
        for button in self._buttons:
            button.setEnabled(False)
        self.setCursor(Qt.CursorShape.WaitCursor)
        try:
            rest_login(self._server_url, current_username(), password, self._verify_ssl)
        except LoginError as exc:
            self.unsetCursor()
            for button in self._buttons:
                button.setEnabled(True)
            message = (
                tr("auth.invalid_credentials")
                if str(exc) == "invalid_credentials"
                else tr("auth.failed_generic", error=exc)
            )
            QMessageBox.warning(self, tr("auth.failed_title"), message)
            self._password_field.selectAll()
            self._password_field.setFocus()
            return

        set_stored_password(password)
        self.unsetCursor()
        self.accept()
