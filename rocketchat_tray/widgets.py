from __future__ import annotations

from PySide6.QtCore import Property, QEasingCurve, QPropertyAnimation, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPalette
from PySide6.QtWidgets import QAbstractButton

# App-wide QPushButton look: applied once via QApplication.setStyleSheet(),
# so it reaches every button, including ones we don't build ourselves (e.g.
# QMessageBox's/QInputDialog's standard OK/Cancel), not just our own dialogs.
# Three things this overrides from the native GTK/Adwaita-derived Qt style:
#   1. The special highlighted border+gradient a style draws around whichever
#      button is setDefault(True) (confirmed live: renders as a thick
#      palette(Highlight)-coloured ring -- the Yaru theme's accent orange on
#      the user's real desktop) -- QPushButton:default below pins it back to
#      the same plain border as every other button.
#   2. Sharp corners -- border-radius rounds them.
#   3. Standard-icon buttons (QMessageBox/QInputDialog assign one via
#      style().standardIcon()) -- icon-size: 0px collapses any assigned icon
#      to nothing without needing to touch each call site individually.
BUTTON_STYLE = """
QPushButton {
    border: 1px solid #c0c0c0;
    border-radius: 6px;
    padding: 6px 18px;
    min-width: 84px;
    min-height: 22px;
    background-color: palette(button);
    icon-size: 0px 0px;
}
QPushButton:hover {
    background-color: #ececec;
}
QPushButton:pressed {
    background-color: #dcdcdc;
}
QPushButton:default {
    border: 1px solid #c0c0c0;
}
QPushButton:focus {
    outline: none;
}
QPushButton:disabled {
    color: #9e9e9e;
    border-color: #d8d8d8;
}
"""


class ToggleSwitch(QAbstractButton):
    """Modern pill-shaped on/off switch. Drop-in replacement for QCheckBox —
    same isChecked()/setChecked()/toggled() API — used instead of a plain
    checkbox for a more modern settings-UI look."""

    _TRACK_W = 44
    _TRACK_H = 24
    _MARGIN = 2

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._offset = float(self._MARGIN)
        self._anim = QPropertyAnimation(self, b"offset", self)
        self._anim.setDuration(150)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self.toggled.connect(self._animate_to_state)

    def _animate_to_state(self, checked: bool) -> None:
        end = float(self._TRACK_W - self._TRACK_H + self._MARGIN) if checked else float(self._MARGIN)
        self._anim.stop()
        self._anim.setStartValue(self._offset)
        self._anim.setEndValue(end)
        self._anim.start()

    def getOffset(self) -> float:
        return self._offset

    def setOffset(self, value: float) -> None:
        self._offset = value
        self.update()

    offset = Property(float, getOffset, setOffset)

    def sizeHint(self) -> QSize:
        return QSize(self._TRACK_W, self._TRACK_H)

    def hitButton(self, pos) -> bool:
        return self.rect().contains(pos)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)

        track_rect = QRectF(0, (self.height() - self._TRACK_H) / 2, self._TRACK_W, self._TRACK_H)
        # ON uses the system's actual accent colour (palette Highlight) so
        # the switch matches whatever GTK/GNOME accent the user has
        # configured. OFF/disabled deliberately do NOT use palette roles:
        # QPalette.ColorRole.Mid resolves to pure white (#ffffff, identical
        # to the dialog background) under this Qt style, making an
        # unchecked switch completely invisible -- confirmed live, not a
        # guess. Fixed neutral greys sidestep that regardless of theme.
        if not self.isEnabled():
            track_color = QColor("#d0d0d0")
        elif self.isChecked():
            track_color = self.palette().color(QPalette.ColorRole.Highlight)
        else:
            track_color = QColor("#9e9e9e")
        painter.setBrush(track_color)
        painter.drawRoundedRect(track_rect, self._TRACK_H / 2, self._TRACK_H / 2)

        thumb_d = self._TRACK_H - 2 * self._MARGIN
        painter.setBrush(QColor("#ffffff"))
        painter.drawEllipse(QRectF(self._offset, track_rect.top() + self._MARGIN, thumb_d, thumb_d))
