from __future__ import annotations

from pathlib import Path

from .resources import SOUND_PATH

# Where GNOME/freedesktop notification-relevant sound themes typically live.
# Checked in this order so Ubuntu's own Yaru theme wins over the generic
# freedesktop fallback when both provide the same name.
_SYSTEM_THEME_DIRS = [
    Path("/usr/share/sounds/gnome/default/alerts"),
    Path("/usr/share/sounds/Yaru/stereo"),
    Path("/usr/share/sounds/freedesktop/stereo"),
]
_EXTENSIONS = (".oga", ".ogg", ".wav")

# (key, label, filename stem to search for across _SYSTEM_THEME_DIRS).
# "click"/"string"/"swing"/"hum" are GNOME Settings' own alert-sound picker
# choices; the rest are the standard freedesktop sound theme's
# notification-relevant sounds. Curated to exclude non-notification content
# (ringtones, UI clicks, channel test tones, ...) also present in those themes.
_CATALOG = [
    ("click", "Klick"),
    ("string", "Zupfton"),
    ("swing", "Swing"),
    ("hum", "Summen"),
    ("message-new-instant", "Nachricht (kurz)"),
    ("message", "Nachricht"),
    ("bell", "Glocke"),
    ("complete", "Fertig-Ton"),
]

DEFAULT_CHOICE = "message-new-instant"


def _find(stem: str) -> Path | None:
    for directory in _SYSTEM_THEME_DIRS:
        for ext in _EXTENSIONS:
            candidate = directory / f"{stem}{ext}"
            if candidate.exists():
                return candidate
    return None


def available_choices() -> list[tuple[str, str, Path]]:
    """(key, label, path) for every sound that actually exists on this
    system. The app's own bundled chime is always included as a fallback
    that works even with no system sound theme installed."""
    choices = [("bundled", "Standard (App-eigen)", SOUND_PATH)]
    for key, label in _CATALOG:
        path = _find(key)
        if path:
            choices.append((key, label, path))
    return choices


def resolve(key: str) -> Path:
    for choice_key, _label, path in available_choices():
        if choice_key == key:
            return path
    return SOUND_PATH
