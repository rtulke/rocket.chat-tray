#!/bin/bash
# Builds a self-contained virtualenv with rocketchat-tray and all of its
# Python dependencies (PySide6 included) staged at
# <stage-dir>/opt/rocketchat-tray/venv, so the resulting .deb needs only a
# bare `python3` on the target system -- no distro-specific
# python3-pyside6.* package, which doesn't exist on every distro this
# project targets (e.g. Ubuntu 24.04, Debian 12).
#
# Must be run inside a container/chroot matching the EXACT target distro:
# the venv's bin/python3 is a symlink to *that* system's python3 binary,
# and pip-installed compiled extensions (PySide6's bundled Qt libraries)
# are tied to that interpreter's ABI. A venv built in a debian:12 container
# is only valid for installation on Debian 12 -- see .github/workflows/
# release.yml, which builds once per target distro for exactly this reason.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
STAGE_DIR="${1:?usage: build-venv.sh <stage-dir>}"
PREFIX="$STAGE_DIR/opt/rocketchat-tray"

rm -rf "$PREFIX"
python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install --no-cache-dir --upgrade pip wheel
"$PREFIX/venv/bin/pip" install --no-cache-dir "$REPO_ROOT"

# Trim build artifacts nfpm doesn't need to ship.
find "$PREFIX/venv" -name "__pycache__" -type d -prune -exec rm -rf {} +
rm -rf "$PREFIX/venv/share/doc" "$PREFIX/venv/share/man"

# pip's auto-generated console_scripts wrapper (from pyproject.toml's
# [project.scripts]) hardcodes this build's staging path in its shebang, so
# it would be broken once installed elsewhere -- unused dead weight, since
# packaging/rocketchat-tray (the actual /usr/bin/rocketchat-tray launcher)
# invokes `python3 -m rocketchat_tray.main` directly instead.
rm -f "$PREFIX/venv/bin/rocketchat-tray"
