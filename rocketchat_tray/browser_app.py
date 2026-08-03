from __future__ import annotations

import json
import logging
from pathlib import Path
from urllib.parse import urlsplit

import requests
import websocket
from PySide6.QtCore import QProcess

logger = logging.getLogger(__name__)

# Fixed, dedicated port + profile for the app-mode browser instance this app
# launches and controls -- deliberately NOT the user's regular Chrome
# profile/instance, both so we don't interfere with their normal browsing
# and because a second process pointed at an already-running *undebugged*
# profile mostly ignores its command-line flags (including
# --remote-debugging-port), so debugging only works reliably if we own the
# profile outright from the start.
DEBUG_PORT = 9445
PROFILE_DIR = Path.home() / ".config" / "rocketchat-tray" / "chrome-app-profile"
_HTTP_TIMEOUT = 2.0


def _cdp_get(path: str) -> object | None:
    try:
        response = requests.get(f"http://127.0.0.1:{DEBUG_PORT}{path}", timeout=_HTTP_TIMEOUT)
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.debug("CDP GET %s fehlgeschlagen: %s", path, exc)
        return None


def _cdp_mutate(path: str) -> bool:
    # /json/new and /json/activate/* used to accept GET, but Chrome 111+
    # requires PUT for them (a bare GET was a CSRF vector: any webpage could
    # trigger it via e.g. <img src="http://localhost:PORT/json/new?evil">).
    # Try PUT first (current Chrome/Chromium); a 405 means an older browser
    # that still expects GET.
    try:
        response = requests.put(f"http://127.0.0.1:{DEBUG_PORT}{path}", timeout=_HTTP_TIMEOUT)
        if response.status_code == 405:
            response = requests.get(f"http://127.0.0.1:{DEBUG_PORT}{path}", timeout=_HTTP_TIMEOUT)
        if not response.ok:
            logger.info("CDP-Aufruf %s -> HTTP %s: %s", path, response.status_code, response.text[:200])
        return response.ok
    except requests.RequestException as exc:
        logger.info("CDP-Aufruf %s fehlgeschlagen: %s", path, exc)
        return False


def _find_matching_target(origin: str) -> dict | None:
    targets = _cdp_get("/json/list")
    if not targets:
        logger.info("CDP /json/list lieferte keine Ziele (Browser laeuft, aber keine Tabs/Fenster gemeldet)")
        return None
    logger.info(
        "CDP /json/list: %s",
        [{"type": t.get("type"), "url": t.get("url")} for t in targets],
    )
    pages = [t for t in targets if t.get("type") == "page"]
    for target in pages:
        if target.get("url", "").startswith(origin):
            logger.info("Passendes Ziel gefunden (Origin-Treffer): %s", target.get("url"))
            return target
    # Our dedicated profile only ever hosts one site, so any leftover page
    # target (e.g. sitting on a transient about:blank) is still the right
    # window to reuse rather than spawning a second one.
    if pages:
        logger.info("Kein Origin-Treffer, verwende erstes 'page'-Ziel: %s", pages[0].get("url"))
        return pages[0]
    logger.info("Keine Ziele vom Typ 'page' in der Liste (vorhandene Typen: %s)", {t.get("type") for t in targets})
    return None


def _navigate_target(target: dict, url: str) -> bool:
    ws_url = target.get("webSocketDebuggerUrl")
    if not ws_url:
        logger.info("Ziel %s hat keine webSocketDebuggerUrl, kann nicht navigieren", target.get("id"))
        return False
    try:
        # Chrome 111+ rejects incoming CDP WebSocket connections that carry
        # a browser-style Origin header (a DNS-rebinding hardening measure)
        # unless it's been explicitly allow-listed via a launch flag --
        # confirmed live: every navigate attempt got a 403 and silently
        # fell back to opening a new tab instead of reusing the window.
        # suppress_origin skips sending that header entirely, which Chrome
        # accepts (this is how real CDP tooling like Puppeteer/Selenium
        # connects too) -- simpler and less invasive than loosening
        # Chrome's origin allow-list via --remote-allow-origins.
        ws = websocket.create_connection(ws_url, timeout=_HTTP_TIMEOUT, suppress_origin=True)
        try:
            ws.send(json.dumps({"id": 1, "method": "Page.navigate", "params": {"url": url}}))
            reply = ws.recv()
            logger.info("Page.navigate Antwort: %s", reply)
        finally:
            ws.close()
        return True
    except (OSError, ValueError, websocket.WebSocketException) as exc:
        logger.info("Page.navigate ueber %s fehlgeschlagen: %s", ws_url, exc)
        return False


def _launch_new_instance(browser_path: str, url: str) -> bool:
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    args = [
        f"--app={url}",
        f"--remote-debugging-port={DEBUG_PORT}",
        f"--user-data-dir={PROFILE_DIR}",
    ]
    logger.info("Starte neue Browser-Instanz: %s %s", browser_path, " ".join(args))
    return bool(QProcess.startDetached(browser_path, args))


def open_in_app_window(browser_path: str, url: str) -> bool:
    """Opens `url` in a dedicated, isolated Chromium profile, reusing and
    navigating an already-open window from a previous call instead of
    always spawning a new one. Plain `chromium --app=URL` alone doesn't do
    that on its own -- Chrome only auto-reuses/focuses windows for
    *installed* PWAs, not ad-hoc --app invocations of an arbitrary URL
    (confirmed live: without installing Rocket.Chat as a PWA, which this
    project deliberately doesn't require the user to do, Chrome opened a
    brand new window on every single call). This drives that reuse
    ourselves via Chrome's DevTools Protocol debugging port instead."""
    origin = f"{urlsplit(url).scheme}://{urlsplit(url).netloc}"

    if _cdp_get("/json/version") is not None:
        logger.info("Bestehende Browser-Instanz auf Port %s gefunden, versuche Fenster wiederzuverwenden", DEBUG_PORT)
        target = _find_matching_target(origin)
        if target and _navigate_target(target, url) and _cdp_mutate(f"/json/activate/{target['id']}"):
            logger.info("Fenster erfolgreich wiederverwendet und navigiert")
            return True
        # Instance is running but reuse failed for some reason (window
        # closed between the liveness check and here, CDP hiccup, no page
        # target yet) -- still avoid spawning a second *process*, just open
        # a fresh tab in the one that's already running.
        logger.info("Wiederverwendung fehlgeschlagen, oeffne stattdessen neuen Tab in bestehender Instanz")
        return _cdp_mutate(f"/json/new?{url}")

    logger.info("Keine laufende Browser-Instanz auf Port %s gefunden", DEBUG_PORT)
    return _launch_new_instance(browser_path, url)
