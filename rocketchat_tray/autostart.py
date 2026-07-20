from __future__ import annotations

from pathlib import Path

# The package installs a system-wide autostart entry in
# /etc/xdg/autostart/rocketchat-tray.desktop, enabled by default for every
# user (see packaging/autostart.desktop) -- deliberately system-wide rather
# than per-user so an admin doesn't have to configure each account
# individually. A per-user file of the same name under ~/.config/autostart
# takes priority over it (standard XDG autostart search order) and, with
# Hidden=true, suppresses it entirely without touching /etc or needing
# root -- the same mechanism GNOME's own "Startup Applications"/Tweaks tool
# uses, so a user who edits it there sees the same effect (and vice versa).
_OVERRIDE_PATH = Path.home() / ".config" / "autostart" / "rocketchat-tray.desktop"

_HIDDEN_ENTRY = """[Desktop Entry]
Type=Application
Name=Rocket.Chat Tray
Exec=/usr/bin/rocketchat-tray
Hidden=true
"""


def is_enabled() -> bool:
    """Whether the app will autostart on login for the current user."""
    if not _OVERRIDE_PATH.exists():
        return True
    try:
        content = _OVERRIDE_PATH.read_text()
    except OSError:
        return True
    return "Hidden=true" not in content


def set_enabled(enabled: bool) -> None:
    if enabled:
        _OVERRIDE_PATH.unlink(missing_ok=True)
    else:
        _OVERRIDE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _OVERRIDE_PATH.write_text(_HIDDEN_ENTRY)
