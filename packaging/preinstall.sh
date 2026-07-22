#!/bin/sh
set -e

# /etc/rocketchat-tray/config.ini was renamed to config.conf (more
# conventional for a Linux /etc config file) starting with 0.0.40.
# dpkg-maintscript-helper's mv_conffile must be called identically from
# preinst, postinst, and prerm for the rename to be tracked correctly --
# it preserves the admin's edits and only prompts if both old and new
# files were independently modified.
dpkg-maintscript-helper mv_conffile \
    /etc/rocketchat-tray/config.ini /etc/rocketchat-tray/config.conf \
    0.0.40~ -- "$@"

exit 0
