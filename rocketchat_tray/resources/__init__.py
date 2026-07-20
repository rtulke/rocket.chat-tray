from pathlib import Path

_RESOURCES_DIR = Path(__file__).parent

# Matches Rocket.Chat's own presence states (see rc_client.STATUS_CODE_MAP).
ICON_NAMES = ("online", "away", "busy", "offline")

SOUND_PATH = _RESOURCES_DIR / "sounds" / "notify.wav"


def icon_path(name: str) -> Path:
    return _RESOURCES_DIR / "icons" / f"bubble-{name}.svg"
