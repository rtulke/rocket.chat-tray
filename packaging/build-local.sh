#!/usr/bin/env bash
# Builds one distro's .deb locally, in a container matching that distro
# exactly -- the same steps .github/workflows/release.yml runs, just
# runnable on your own machine first so a broken build/packaging step (or a
# real code issue) is caught before spending a version bump + tag push on
# it. Also runs the same "install it like a real user would" verify pass
# CI does, in a second, fresh container -- catches missing/broken
# dependencies without needing to actually install anything on your system.
#
# Builds from your CURRENT working tree, uncommitted changes included (not
# from the last commit) -- that's the point: test before you even commit.
#
# Usage: packaging/build-local.sh [distro]
# distro: debian12 | debian13 | ubuntu2404 (default) | ubuntu2604
#
# Recommended flow: build-local.sh (defaults to ubuntu2404) -> install/test
# the resulting dist/*.deb yourself -> only once that looks right,
# packaging/release.sh to tag and trigger the full 4-distro remote build.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DISTRO="${1:-ubuntu2404}"
NFPM_VERSION="2.47.0"

case "$DISTRO" in
    debian12) IMAGE="debian:12" ;;
    debian13) IMAGE="debian:13" ;;
    ubuntu2404) IMAGE="ubuntu:24.04" ;;
    ubuntu2604) IMAGE="ubuntu:26.04" ;;
    *)
        echo "Unknown distro '$DISTRO'. Use one of: debian12 debian13 ubuntu2404 ubuntu2604" >&2
        exit 1
        ;;
esac

ENGINE="${CONTAINER_ENGINE:-}"
if [ -z "$ENGINE" ]; then
    if command -v docker >/dev/null 2>&1; then
        ENGINE=docker
    elif command -v podman >/dev/null 2>&1; then
        ENGINE=podman
    else
        echo "Neither docker nor podman found; install one or set CONTAINER_ENGINE." >&2
        exit 1
    fi
fi

# Modern apt drops privileges to a "_apt" sandbox user for its own
# downloads by default -- that setgroups()/setuid() dance fails under
# rootless podman's user-namespace mapping (confirmed live: "setgroups 65534
# failed - Operation not permitted", apt exit 100) even though the
# container itself runs fine otherwise. Harmless to disable inside a
# throwaway build container we already trust completely.
# Skipping recommended packages too: confirmed live that pulling them in
# (fontconfig-config, ca-certificates' font-related transitive deps, etc.
# rode in as Recommends of otherwise-unrelated packages here) triggers a
# *different* rootless-podman/NFS-backing-store issue -- their postinst
# scripts chown/chmod paths like /usr/local/share/fonts, which fails
# ("Invalid argument" / "Value too large for defined data type") under this
# setup's user-namespace + NFS overlay combination, and dpkg then aborts
# the whole install. None of them are needed for a bare build toolchain or
# for verifying rocketchat-tray's own actual dependencies resolve.
APT_OPTS="-o APT::Sandbox::User=root -o APT::Install-Recommends=false"

# This network requires an outbound HTTP(S) proxy -- rootless podman's
# default networking (slirp4netns) doesn't inherit the host's proxy or
# routing setup on its own, and DNS here resolves archive.ubuntu.com to
# IPv6-only addresses that then hang rather than fail fast (confirmed live:
# apt sat for 10+ minutes with no error instead of erroring out). Passing
# the host's proxy env vars through is necessary but NOT sufficient on its
# own for apt specifically -- confirmed live that apt's http transport
# method ignores plain http_proxy/https_proxy env vars here and still hangs
# the same way; it needs the proxy spelled out via explicit
# Acquire::http(s)::Proxy options instead (curl, used later for the nfpm
# download, does honour the env vars normally, so those are still passed
# through for its sake). Everything here is only forwarded/added if
# actually set on the host, so this is a no-op on a network that doesn't
# need a proxy.
PROXY_ENV_ARGS=()
for var in http_proxy https_proxy no_proxy HTTP_PROXY HTTPS_PROXY NO_PROXY; do
    if [ -n "${!var:-}" ]; then
        PROXY_ENV_ARGS+=(-e "$var=${!var}")
    fi
done
if [ -n "${http_proxy:-}" ]; then
    APT_OPTS="$APT_OPTS -o Acquire::http::Proxy=$http_proxy"
fi
if [ -n "${https_proxy:-}" ]; then
    APT_OPTS="$APT_OPTS -o Acquire::https::Proxy=$https_proxy"
fi

# If $ENGINE is Podman (including "docker" being the podman-docker
# compatibility wrapper -- confirmed live that's the case here: /usr/bin/
# docker is a shell script that just execs podman), redirect its storage
# *root* away from wherever it defaults to (--runroot is left alone --
# confirmed live it's already under /run/user/<uid>, a local tmpfs, so it
# was never the problem here). The storage root defaults to somewhere
# under $HOME, which is NFS-mounted here -- confirmed live that installing
# a package whose dependency chain reaches fontconfig-config (anything
# pulling in GTK, e.g. rocketchat-tray's own libgtk-3-0 dependency) fails
# outright under that combination: dpkg postinst scripts chown/chmod paths
# like /usr/local/share/fonts, which errors ("Invalid argument") against
# the NFS-backed overlay's forced permission mask, and apt has no
# equivalent of --no-install-recommends for a hard Depends. A local
# (non-NFS) storage root sidesteps the whole problem. No-op if $ENGINE
# turns out to be real Docker (this is a Podman-only flag) or if storage
# already isn't NFS-backed. Kept short: podman's --runroot rejects paths
# over 50 characters, and while that limit is documented for --runroot
# specifically, --root gets the same short, fixed name for consistency.
ENGINE_ARGS=()
if "$ENGINE" --version 2>/dev/null | grep -qi podman; then
    PODMAN_LOCAL_STORAGE="${TMPDIR:-/tmp}/rct-podman-storage"
    mkdir -p "$PODMAN_LOCAL_STORAGE"
    ENGINE_ARGS=(--root "$PODMAN_LOCAL_STORAGE")
fi

VERSION=$(python3 -c "import re; print(re.search(r'__version__ = \"([^\"]+)\"', open('$REPO_ROOT/rocketchat_tray/__init__.py').read()).group(1))")
OUT_DIR="$REPO_ROOT/dist"
mkdir -p "$OUT_DIR"
DEB_NAME="rocketchat-tray_${VERSION}_${DISTRO}_amd64.deb"

echo "==> Building $DISTRO ($IMAGE) via $ENGINE -- version $VERSION"
echo "==> Working tree copied in as-is (uncommitted changes included)"

# The repo is bind-mounted read-only: the container works from an internal
# copy (rsync'd, .git/stage/dist excluded) so nothing it does -- writing
# packaging/build-venv.sh's ./stage output, pip caches, etc. -- ever touches
# your actual working directory. Only the final .deb crosses back out, via
# the separate read-write /out mount.
"$ENGINE" "${ENGINE_ARGS[@]}" run --rm \
    -v "$REPO_ROOT:/src:ro" \
    -v "$OUT_DIR:/out" \
    -e "APT_OPTS=$APT_OPTS" \
    "${PROXY_ENV_ARGS[@]}" \
    "$IMAGE" \
    bash -c '
        set -euo pipefail
        apt-get $APT_OPTS update -qq
        DEBIAN_FRONTEND=noninteractive apt-get $APT_OPTS install -y -qq \
            python3 python3-venv python3-pip curl ca-certificates rsync
        mkdir -p /build
        rsync -a --exclude=".git" --exclude="stage" --exclude="dist" --exclude=".venv" /src/ /build/
        cd /build
        bash packaging/build-venv.sh ./stage
        curl -sSL "https://github.com/goreleaser/nfpm/releases/download/v'"$NFPM_VERSION"'/nfpm_'"$NFPM_VERSION"'_Linux_x86_64.tar.gz" -o /tmp/nfpm.tar.gz
        # --no-same-owner: without it, tar tries to restore the archived
        # file'"'"'s original uid/gid (1001:1001 in nfpm'"'"'s release tarball),
        # which fails under this setup'"'"'s rootless-podman + NFS-backing-store
        # combination ("Cannot change ownership ... Invalid argument",
        # confirmed live) -- same root cause as the earlier apt postinst
        # chown failures, just hit by tar directly this time.
        tar -xzf /tmp/nfpm.tar.gz -C /usr/local/bin --no-same-owner nfpm
        chmod +x /usr/local/bin/nfpm
        PKG_VERSION='"$VERSION"' nfpm package -f packaging/nfpm.yaml -p deb -t "/out/'"$DEB_NAME"'"
    '

echo "==> Built: $OUT_DIR/$DEB_NAME"
echo "==> Verifying install (fresh $IMAGE container, real apt dependency resolution)"

"$ENGINE" "${ENGINE_ARGS[@]}" run --rm \
    -v "$OUT_DIR:/out:ro" \
    -e "APT_OPTS=$APT_OPTS" \
    "${PROXY_ENV_ARGS[@]}" \
    "$IMAGE" \
    bash -c '
        set -euo pipefail
        apt-get $APT_OPTS update -qq
        DEBIAN_FRONTEND=noninteractive apt-get $APT_OPTS install -y -qq "/out/'"$DEB_NAME"'"
        test -x /usr/bin/rocketchat-tray
        test -f /etc/rocketchat-tray/config.conf
        test -f /etc/xdg/autostart/rocketchat-tray.desktop
        /opt/rocketchat-tray/venv/bin/python3 -c "import rocketchat_tray; print(\"version:\", rocketchat_tray.__version__)"
        /opt/rocketchat-tray/venv/bin/python3 -c "from PySide6.QtWidgets import QApplication"
    '

echo
echo "==> $DISTRO build + install verify OK: $OUT_DIR/$DEB_NAME"
echo "    Install it yourself to test the actual app (needs a real display/D-Bus"
echo "    session, same limitation the CI verify job has):"
echo "      sudo dpkg -i $OUT_DIR/$DEB_NAME"
