# Rocket.Chat Tray

We decided against using the Rocket.Chat Electron desktop client [Rocket.Chat](https://www.rocket.chat/). because it wasn’t very stable. However, we still wanted desktop notifications and the ability to manage our status directly from GNOME 3.

This GNOME 3 system tray client displays your live Rocket.Chat presence—online, away, busy, or offline—as a colour-coded tray icon. It stays synchronised in real time, including when your status is changed through another client, such as the web or mobile app.

- Colour-coded tray icon that mirrors your actual Rocket.Chat presence, kept in sync in real time — including status changes made elsewhere (the web client, mobile)
- Desktop notifications on new messages, with a click-to-open-that-chat action
- Configurable notification sound, picked from your system's own GNOME/freedesktop sound theme (or the app's bundled chime)
- Optional automatic "Away" after a configurable period of system inactivity
- Password stored in the GNOME keyring, never on disk in plain text
- Server URL and login can be changed any time from the Settings dialog — no config file editing required after initial setup

## Supported systems

Prebuilt `.deb` packages are published for:

| Distribution | Version |
|---|---|
| Debian | 12 (bookworm), 13 (trixie) |
| Ubuntu | 24.04 LTS, 26.04 LTS |

Requires GNOME Shell with the [AppIndicator and KStatusNotifierItem Support](https://extensions.gnome.org/extension/615/appindicator-support/) extension — without it, GNOME Shell doesn't display tray icons from any application. Ubuntu's GNOME usually has this enabled by default; on other distributions install `gnome-shell-extension-appindicator` and enable it once:

```bash
gnome-extensions enable appindicatorsupport@rgcjonas.gmail.com
```

## Installation

Download the `.deb` matching your distribution from the [Releases](https://github.com/rtulke/rocket.chat-tray/releases) page and install it:

```bash
curl -LO https://github.com/rtulke/rocket.chat-tray/releases/latest/download/rocketchat-tray_<version>_<distro>_amd64.deb
sudo apt install ./rocketchat-tray_<version>_<distro>_amd64.deb
```

`<distro>` is one of `debian12`, `debian13`, `ubuntu2404`, `ubuntu2604` — use the package built for your actual system. The package bundles its own Python environment (PySide6 included), so it doesn't depend on distro-specific Qt packages that don't exist everywhere.

After installing, set the server URL once for all users:

```bash
sudo nano /etc/rocketchat-tray/config.ini
```

Then either log out/in (autostart picks it up) or run `rocketchat-tray` directly.

## Configuration

**Server URL** — resolved in this order:

1. A per-user override set from the app's own Settings dialog (⚙ → Server), stored in `~/.config/rocketchat-tray/settings.json` under `server.url_override`. Once set, this always wins.
2. The fleet-wide default in `/etc/rocketchat-tray/config.ini` (`[server] url = ...`), managed by whoever deployed the package.

Changing the URL from Settings takes effect immediately — no restart needed — and prompts you to log in to the new server right away.

**Login** — the username is taken from the OS session (`$USER`); the password is entered once and stored in the GNOME keyring. Use "Anmeldung zurücksetzen…" in Settings (or the tray menu's "Passwort erneut eingeben…", shown automatically after a failed login) to re-enter it at any time.

**Everything else** (notifications, sound, presence/idle behaviour) is configured from the Settings dialog, reachable from the tray icon's context menu.

## Development

```bash
git clone https://github.com/rtulke/rocket.chat-tray.git
cd rocket.chat-tray
python3 -m venv .venv
.venv/bin/pip install -e .

# Admin config is required even for local runs:
sudo mkdir -p /etc/rocketchat-tray
sudo cp packaging/config.ini.example /etc/rocketchat-tray/config.ini
sudo nano /etc/rocketchat-tray/config.ini   # set server_url to a real server

.venv/bin/rocketchat-tray
```

The first run shows a login dialog (username prefilled, enter your password); the tray icon should then appear grey, turning green/orange/red as your Rocket.Chat presence changes.

### Building the `.deb` locally

```bash
sudo apt install python3-venv curl
bash packaging/build-venv.sh ./stage      # bundles PySide6 etc. into ./stage
# install nfpm: https://nfpm.goreleaser.com/install/
PKG_VERSION=$(python3 -c "import re; print(re.search(r'__version__ = \"([^\"]+)\"', open('rocketchat_tray/__init__.py').read()).group(1))")
nfpm package -f packaging/nfpm.yaml -p deb -t rocketchat-tray_${PKG_VERSION}_amd64.deb
```

Build this on the same distribution/version you intend to install on — the bundled virtualenv references that system's own `python3`, so it isn't portable across differing Python versions (see the comment header in `packaging/build-venv.sh`). CI (`.github/workflows/release.yml`) builds and verifies one package per supported distro this way on every push, and publishes them to GitHub Releases on a `vX.Y.Z` tag push.

## License

[MIT](LICENSE)
