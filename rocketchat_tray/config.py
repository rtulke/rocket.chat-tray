from __future__ import annotations

import configparser
import copy
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from .sounds import DEFAULT_CHOICE as DEFAULT_SOUND_CHOICE

logger = logging.getLogger(__name__)

ADMIN_CONFIG_PATH = Path("/etc/rocketchat-tray/config.conf")
# Renamed from config.ini in 0.0.40 (.conf is the more conventional extension
# for a Linux /etc config file); the .deb's maintainer scripts migrate an
# existing installation's file via dpkg-maintscript-helper (see
# packaging/preinstall.sh), but this fallback also covers configs deployed
# by other means (config management, manual copies, ...).
LEGACY_ADMIN_CONFIG_PATH = Path("/etc/rocketchat-tray/config.ini")
USER_SETTINGS_PATH = Path.home() / ".config" / "rocketchat-tray" / "settings.json"

STATUS_OPTIONS = ("auto", "online", "away", "busy", "offline")

DEFAULT_SETTINGS = {
    "notifications": {
        "blink_enabled": True,
        "sound_enabled": False,
        "sound_volume": 0.7,
        "sound_choice": DEFAULT_SOUND_CHOICE,
        "tooltip_enabled": True,
    },
    "presence": {
        "forced_status": "auto",
        "idle_detection_enabled": True,
        "status_message": "",
    },
    "server": {
        "url_override": "",
        "verify_ssl_override": True,
    },
}


class ConfigError(Exception):
    """Raised when the admin-provisioned configuration is missing or invalid."""


@dataclass
class AdminConfig:
    """Not actually admin-immutable at runtime: the user can override both
    fields from the settings dialog (see main.update_server()), at which
    point this instance is mutated in place rather than replaced, since
    RocketChatWorker/PresenceCoordinator/RoomOpener all hold a reference to
    the same object and read server_url/verify_ssl fresh on each use."""

    server_url: str
    verify_ssl: bool = True

    @classmethod
    def load(cls, path: Path = ADMIN_CONFIG_PATH) -> "AdminConfig":
        if not path.exists() and LEGACY_ADMIN_CONFIG_PATH.exists():
            logger.warning(
                "%s nicht gefunden, verwende veraltetes %s -- bitte auf .conf umbenennen",
                path, LEGACY_ADMIN_CONFIG_PATH,
            )
            path = LEGACY_ADMIN_CONFIG_PATH
        if not path.exists():
            raise ConfigError(f"Admin-Konfiguration nicht gefunden: {path}")
        parser = configparser.ConfigParser()
        try:
            parser.read(path)
            server_url = parser.get("server", "url")
        except (configparser.Error, KeyError) as exc:
            raise ConfigError(f"Ungueltige Admin-Konfiguration in {path}: {exc}") from exc
        if not server_url or "REPLACE-ME" in server_url:
            raise ConfigError(
                f"{path} enthaelt noch eine Platzhalter-Server-URL; bitte vom Admin konfigurieren lassen"
            )
        verify_ssl = parser.getboolean("server", "verify_ssl", fallback=True)
        return cls(server_url=server_url.rstrip("/"), verify_ssl=verify_ssl)


class UserSettings:
    def __init__(self, data: dict, path: Path = USER_SETTINGS_PATH):
        self._path = path
        self._data = data

    @classmethod
    def load(cls, path: Path = USER_SETTINGS_PATH) -> "UserSettings":
        data = copy.deepcopy(DEFAULT_SETTINGS)
        if path.exists():
            try:
                loaded = json.loads(path.read_text())
                data["notifications"].update(loaded.get("notifications", {}))
                data["presence"].update(loaded.get("presence", {}))
                data["server"].update(loaded.get("server", {}))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Konnte %s nicht lesen, verwende Standardwerte: %s", path, exc)
        return cls(data, path=path)

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(self._data, indent=2))
        os.replace(tmp_path, self._path)

    @property
    def blink_enabled(self) -> bool:
        return self._data["notifications"]["blink_enabled"]

    @blink_enabled.setter
    def blink_enabled(self, value: bool) -> None:
        self._data["notifications"]["blink_enabled"] = value

    @property
    def sound_enabled(self) -> bool:
        return self._data["notifications"]["sound_enabled"]

    @sound_enabled.setter
    def sound_enabled(self, value: bool) -> None:
        self._data["notifications"]["sound_enabled"] = value

    @property
    def sound_volume(self) -> float:
        return self._data["notifications"]["sound_volume"]

    @sound_volume.setter
    def sound_volume(self, value: float) -> None:
        self._data["notifications"]["sound_volume"] = max(0.0, min(1.0, value))

    @property
    def sound_choice(self) -> str:
        return self._data["notifications"]["sound_choice"]

    @sound_choice.setter
    def sound_choice(self, value: str) -> None:
        self._data["notifications"]["sound_choice"] = value

    @property
    def tooltip_enabled(self) -> bool:
        return self._data["notifications"]["tooltip_enabled"]

    @tooltip_enabled.setter
    def tooltip_enabled(self, value: bool) -> None:
        self._data["notifications"]["tooltip_enabled"] = value

    @property
    def forced_status(self) -> str:
        return self._data["presence"]["forced_status"]

    @forced_status.setter
    def forced_status(self, value: str) -> None:
        if value not in STATUS_OPTIONS:
            raise ValueError(f"Unbekannter Status: {value!r}")
        self._data["presence"]["forced_status"] = value

    @property
    def idle_detection_enabled(self) -> bool:
        return self._data["presence"]["idle_detection_enabled"]

    @idle_detection_enabled.setter
    def idle_detection_enabled(self, value: bool) -> None:
        self._data["presence"]["idle_detection_enabled"] = value

    @property
    def status_message(self) -> str:
        return self._data["presence"]["status_message"]

    @status_message.setter
    def status_message(self, value: str) -> None:
        self._data["presence"]["status_message"] = value

    @property
    def server_url_override(self) -> str:
        """Empty string means "no override, use the admin-provisioned
        server_url from /etc/rocketchat-tray/config.conf"."""
        return self._data["server"]["url_override"]

    @server_url_override.setter
    def server_url_override(self, value: str) -> None:
        self._data["server"]["url_override"] = value

    @property
    def verify_ssl_override(self) -> bool:
        return self._data["server"]["verify_ssl_override"]

    @verify_ssl_override.setter
    def verify_ssl_override(self, value: bool) -> None:
        self._data["server"]["verify_ssl_override"] = value
