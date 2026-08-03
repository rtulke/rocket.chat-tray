#!/bin/sh
set -e

# See packaging/preinstall.sh.
dpkg-maintscript-helper mv_conffile \
    /etc/rocketchat-tray/config.ini /etc/rocketchat-tray/config.conf \
    0.0.40~ -- "$@"

echo "rocketchat-tray: Bitte /etc/rocketchat-tray/config.conf mit der Server-URL konfigurieren (oder das die Nutzer selbst in den App-Einstellungen tun lassen)."
echo "rocketchat-tray: Falls das Tray-Icon unter GNOME nicht erscheint, die Erweiterung 'AppIndicator and KStatusNotifierItem Support' aktivieren (gnome-extensions enable appindicatorsupport@rgcjonas.gmail.com)."

# Ask an already-running instance (from before this upgrade) to relaunch
# itself with the just-installed version -- see
# rocketchat_tray/single_instance.py's SingleInstanceGuard, which listens
# on this same socket both to prevent double-launches and, here, for this
# restart request. Uses the system python3 (a declared package dependency,
# always present), not the app's own bundled venv, since this only needs
# the stdlib socket module. Silent no-op if nothing is listening (fresh
# install, or no session currently running it).
python3 - <<'PYEOF' 2>/dev/null || true
import socket
try:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(1)
    s.connect("/tmp/rocketchat-tray")
    s.sendall(b"restart")
    s.close()
except OSError:
    pass
PYEOF

exit 0
