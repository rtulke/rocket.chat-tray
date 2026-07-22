#!/usr/bin/env bash
set -euo pipefail

# Bumps the app version, commits, pushes to main, then tags and pushes a
# release. The tag push triggers .github/workflows/release.yml, which builds
# .deb packages for Debian 12/13 + Ubuntu 24.04/26.04 and publishes a GitHub
# Release with them attached.
#
# Usage: packaging/release.sh <version> "<commit message>"
# Example: packaging/release.sh 0.0.39 "Fix presence sync clobbering external status changes"

if [ $# -lt 2 ]; then
    echo "Usage: $0 <version> \"<commit message>\"" >&2
    echo "Example: $0 0.0.39 \"Fix presence sync clobbering external status changes\"" >&2
    exit 1
fi

VERSION="$1"
shift
MESSAGE="$*"

if ! [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "Version must look like X.Y.Z, got: $VERSION" >&2
    exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [ "$BRANCH" != "main" ]; then
    echo "Refusing to release from branch '$BRANCH' (must be on main)." >&2
    exit 1
fi

# This repo's commits must be authored as rt@debian.sh, never the sandbox's
# global turo@naz.ch default -- see packaging/README or ask Claude why.
IDENTITY_EMAIL="$(git config --local user.email || true)"
if [ "$IDENTITY_EMAIL" != "rt@debian.sh" ]; then
    echo "Local git identity for this repo is '${IDENTITY_EMAIL:-<unset>}', expected 'rt@debian.sh'." >&2
    echo "Fix with:" >&2
    echo "  git config --local user.name \"Robert Tulke\"" >&2
    echo "  git config --local user.email rt@debian.sh" >&2
    exit 1
fi

echo "==> Bumping version to $VERSION"
sed -i "s/^__version__ = \".*\"/__version__ = \"$VERSION\"/" rocketchat_tray/__init__.py
sed -i "s/^version = \".*\"/version = \"$VERSION\"/" pyproject.toml

# Stage the version bump plus any already-modified tracked files. Deliberately
# `add -u` (tracked/modified only), not `add -A`: new untracked files must be
# staged explicitly first so nothing unintended (stray scratch files, etc.)
# gets swept into the release commit.
git add -u
git add rocketchat_tray/__init__.py pyproject.toml

if git diff --cached --quiet; then
    echo "Nothing to commit (version already at $VERSION and no other staged changes)." >&2
    exit 1
fi

echo
echo "==> About to commit:"
git diff --cached --stat
echo
echo "Commit message: $MESSAGE"
echo
read -r -p "Commit, push to main, and release v$VERSION? [y/N] " CONFIRM
if [ "$CONFIRM" != "y" ] && [ "$CONFIRM" != "Y" ]; then
    echo "Aborted." >&2
    exit 1
fi

echo "==> Committing"
git commit -m "$MESSAGE"

echo "==> Pushing main"
git push origin main

echo "==> Tagging v$VERSION"
git tag -a "v$VERSION" -m "v$VERSION: $MESSAGE"

# Pushed as its own, separate push (not bundled with the main push above):
# GitHub silently skips firing Actions push events for more than a few refs
# pushed at once, so tags need to go out on their own to reliably trigger
# the release workflow.
echo "==> Pushing tag v$VERSION (triggers the release build)"
git push origin "v$VERSION"

echo
echo "Done. Watch the build here:"
echo "  https://github.com/rtulke/rocket.chat-tray/actions"
echo "Release (with .deb downloads) will appear here once it finishes (~5-10 min):"
echo "  https://github.com/rtulke/rocket.chat-tray/releases/tag/v$VERSION"
