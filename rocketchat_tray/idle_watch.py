from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtDBus import QDBus, QDBusConnection, QDBusInterface

logger = logging.getLogger(__name__)

IDLE_MONITOR_SERVICE = "org.gnome.Mutter.IdleMonitor"
IDLE_MONITOR_PATH = "/org/gnome/Mutter/IdleMonitor/Core"
IDLE_MONITOR_INTERFACE = "org.gnome.Mutter.IdleMonitor"
POLL_INTERVAL_MS = 15_000
IDLE_THRESHOLD_MS = 5 * 60 * 1000  # 5 minutes


class IdleWatcher(QObject):
    """Polls GNOME's (Mutter) idle monitor over D-Bus to detect OS-level
    inactivity.

    Deliberately polling GetIdletime() rather than using IdleMonitor's native
    AddIdleWatch()/WatchFired push mechanism: WatchFired is a *directed*
    D-Bus signal sent only back to the exact connection that registered the
    watch (confirmed by monitoring the bus live), and AddIdleWatch's uint64
    argument can't be marshalled from Python via PySide6 (no QVariant
    exposed to Python, see PYSIDE-1904 — the same limitation notifier.py
    works around for Notify() by shelling out to gdbus). Making that call via
    a throwaway gdbus subprocess doesn't help here since the reply signal
    would then go back to that subprocess's connection, not ours. Polling
    the argument-less, natively-working GetIdletime() call sidesteps all of
    that, and this feature only needs minute-scale precision anyway.
    """

    became_idle = Signal()
    became_active = Signal()

    def __init__(self, idle_threshold_ms: int = IDLE_THRESHOLD_MS, parent=None):
        super().__init__(parent)
        self._threshold_ms = idle_threshold_ms
        self._is_idle = False
        self._enabled = False

        bus = QDBusConnection.sessionBus()
        self._interface = QDBusInterface(IDLE_MONITOR_SERVICE, IDLE_MONITOR_PATH, IDLE_MONITOR_INTERFACE, bus)

        self._timer = QTimer(self)
        self._timer.setInterval(POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._poll)

    @property
    def is_idle(self) -> bool:
        return self._enabled and self._is_idle

    def set_enabled(self, enabled: bool) -> None:
        if enabled == self._enabled:
            return
        if enabled and not self._interface.isValid():
            logger.warning("GNOME-Idle-Monitor nicht verfuegbar; Leerlauferkennung bleibt aus")
            return
        self._enabled = enabled
        if enabled:
            self._is_idle = False
            self._timer.start()
            self._poll()
        else:
            self._timer.stop()
            if self._is_idle:
                self._is_idle = False
                self.became_active.emit()

    def _poll(self) -> None:
        reply = self._interface.call(QDBus.CallMode.Block, "GetIdletime")
        args = reply.arguments()
        if not args:
            logger.warning("GetIdletime fehlgeschlagen: %s", reply.errorMessage())
            return
        idle_ms = args[0]
        now_idle = idle_ms >= self._threshold_ms
        if now_idle and not self._is_idle:
            self._is_idle = True
            self.became_idle.emit()
        elif not now_idle and self._is_idle:
            self._is_idle = False
            self.became_active.emit()
