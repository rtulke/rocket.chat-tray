from __future__ import annotations

import getpass
import logging

import keyring
import keyring.errors
import requests
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
)

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
        raise LoginError(f"Server nicht erreichbar ({server_url}): {exc}") from exc

    if response.status_code == 401:
        raise LoginError("invalid_credentials")
    if response.status_code != 200:
        raise LoginError(f"Unerwartete Server-Antwort ({response.status_code})")

    try:
        body = response.json()
    except ValueError as exc:
        raise LoginError("Unerwartete Server-Antwort (kein JSON)") from exc

    if body.get("status") != "success":
        raise LoginError(body.get("message", "Anmeldung fehlgeschlagen"))

    data = body["data"]
    return data["authToken"], data["userId"]


class LoginDialog(QDialog):
    """First-run / re-auth dialog. Username is read-only (comes from the OS);
    the password is only written to the keyring after a successful login."""

    def __init__(self, server_url: str, verify_ssl: bool, parent=None):
        super().__init__(parent)
        self._server_url = server_url
        self._verify_ssl = verify_ssl

        self.setWindowTitle("Rocket.Chat Tray — Anmelden")
        self.setModal(True)

        self._username_field = QLineEdit(current_username())
        self._username_field.setReadOnly(True)
        self._password_field = QLineEdit()
        self._password_field.setEchoMode(QLineEdit.EchoMode.Password)

        form = QFormLayout()
        form.addRow("Benutzername:", self._username_field)
        form.addRow("Passwort:", self._password_field)

        self._status_label = QLabel(f"Server: {server_url}")
        self._status_label.setWordWrap(True)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._buttons.accepted.connect(self._attempt_login)
        self._buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self._status_label)
        layout.addLayout(form)
        layout.addWidget(self._buttons)

        self._password_field.setFocus()

    def _attempt_login(self) -> None:
        password = self._password_field.text()
        if not password:
            return
        self._buttons.setEnabled(False)
        self.setCursor(Qt.CursorShape.WaitCursor)
        try:
            rest_login(self._server_url, current_username(), password, self._verify_ssl)
        except LoginError as exc:
            self.unsetCursor()
            self._buttons.setEnabled(True)
            message = (
                "Benutzername oder Passwort falsch."
                if str(exc) == "invalid_credentials"
                else f"Anmeldung fehlgeschlagen: {exc}"
            )
            QMessageBox.warning(self, "Anmeldung fehlgeschlagen", message)
            self._password_field.selectAll()
            self._password_field.setFocus()
            return

        set_stored_password(password)
        self.unsetCursor()
        self.accept()
