#!/bin/sh
set -e

# See packaging/preinstall.sh.
dpkg-maintscript-helper mv_conffile \
    /etc/rocketchat-tray/config.ini /etc/rocketchat-tray/config.conf \
    0.0.40~ -- "$@"

echo "rocketchat-tray: Bitte /etc/rocketchat-tray/config.conf mit der Server-URL konfigurieren (oder das die Nutzer selbst in den App-Einstellungen tun lassen)."
echo "rocketchat-tray: Falls das Tray-Icon unter GNOME nicht erscheint, die Erweiterung 'AppIndicator and KStatusNotifierItem Support' aktivieren (gnome-extensions enable appindicatorsupport@rgcjonas.gmail.com)."

exit 0
