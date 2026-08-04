from __future__ import annotations

import sys

import requests
import urllib3

from . import auth
from .config import AdminConfig, ConfigError, UserSettings
from .i18n import tr

_HTTP_TIMEOUT = 10


def _resolve_channel_param(target: str) -> str:
    """Rocket.Chat's chat.postMessage takes a "channel" address directly --
    "#name" for a channel/group, "@username" for a DM -- so unlike the tray
    app's own quick-reply flow, this never needs a separate room-id lookup.
    A bare name with neither prefix is assumed to be a username."""
    target = target.strip()
    if target.startswith("#") or target.startswith("@"):
        return target
    return f"@{target}"


def send(
    server_url: str, auth_token: str, user_id: str, channel: str, text: str, verify_ssl: bool = True,
) -> tuple[bool, str]:
    """Returns (success, error_message). error_message is "" on success."""
    try:
        response = requests.post(
            f"{server_url}/api/v1/chat.postMessage",
            json={"channel": channel, "text": text},
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


def main() -> int:
    """CLI testing helper -- not wired into the tray app itself. Sends a
    message to one or more users/channels from the command line, using the
    exact same server config (including any per-user override set from the
    Settings dialog) and stored keyring credentials as the tray app, under
    whichever OS user runs it. Useful for manually triggering a real
    notification while developing/testing the tray app itself."""
    if len(sys.argv) != 3:
        print(tr("cli.usage"), file=sys.stderr)
        return 2

    targets_arg, text = sys.argv[1], sys.argv[2]
    targets = [t for t in (part.strip() for part in targets_arg.split(",")) if t]
    if not targets:
        print(tr("cli.usage"), file=sys.stderr)
        return 2

    settings = UserSettings.load()
    try:
        admin_config = AdminConfig.load()
    except ConfigError as exc:
        print(tr("cli.config_error", error=exc), file=sys.stderr)
        return 1
    if settings.server_url_override:
        admin_config.server_url = settings.server_url_override
        admin_config.verify_ssl = settings.verify_ssl_override

    if not admin_config.verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    username = auth.current_username()
    password = auth.get_stored_password(username)
    if not password:
        print(tr("cli.no_password", username=username), file=sys.stderr)
        return 1

    try:
        auth_token, user_id = auth.rest_login(admin_config.server_url, username, password, admin_config.verify_ssl)
    except auth.LoginError as exc:
        print(tr("cli.login_failed", error=exc), file=sys.stderr)
        return 1

    exit_code = 0
    for target in targets:
        channel = _resolve_channel_param(target)
        ok, error = send(admin_config.server_url, auth_token, user_id, channel, text, admin_config.verify_ssl)
        if ok:
            print(tr("cli.sent_ok", channel=channel))
        else:
            print(tr("cli.send_failed", channel=channel, error=error), file=sys.stderr)
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
