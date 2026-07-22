#!/bin/sh
set -e

# See packaging/preinstall.sh -- must call the same mv_conffile here too,
# it's how dpkg-maintscript-helper is designed to be used (preinst +
# postinst + prerm, identical arguments).
dpkg-maintscript-helper mv_conffile \
    /etc/rocketchat-tray/config.ini /etc/rocketchat-tray/config.conf \
    0.0.40~ -- "$@"

exit 0
