from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from . import sounds
from .i18n import tr
from .widgets import ToggleSwitch

# Mimics GNOME's "boxed list" preferences style (see e.g. GNOME Settings, or
# Remmina's own preferences window): rows grouped into a single bordered,
# rounded card per block/section, instead of loose controls directly on the
# dialog background. Deliberately no border on individual rows -- only the
# block as a whole is boxed, rows within it are just stacked with spacing.
# QPalette.ColorRole.Mid resolves to pure white (identical to the dialog
# background) under this Qt style/theme -- confirmed live, not a guess --
# making anything drawn with it invisible, so the border uses a fixed light
# grey instead of a palette role (also keeps it subtle rather than
# dominating the dialog).
_CARD_STYLE = """
QFrame#settingsCard {
    background-color: palette(base);
    border: 1px solid #e0e0e0;
    border-radius: 10px;
}
"""
_SECTION_LABEL_STYLE = "font-weight: 600; color: palette(placeholder-text);"

ROW_MARGINS = (14, 10, 14, 10)


def _row(*widgets: QWidget) -> QWidget:
    row = QWidget()
    row.setObjectName("settingsRow")
    row.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    layout = QHBoxLayout(row)
    layout.setContentsMargins(*ROW_MARGINS)
    layout.setSpacing(10)
    for widget in widgets:
        layout.addWidget(widget)
    return row


def _label_row(label_text: str, control: QWidget) -> QWidget:
    label = QLabel(label_text)
    row = _row(label, control)
    row.layout().insertStretch(1)
    return row


def _boxed_list(rows: list[QWidget]) -> QFrame:
    card = QFrame()
    card.setObjectName("settingsCard")
    card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    card.setStyleSheet(_CARD_STYLE)
    layout = QVBoxLayout(card)
    layout.setContentsMargins(0, 4, 0, 4)
    layout.setSpacing(0)
    for row in rows:
        layout.addWidget(row)
    return card


def _section_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setStyleSheet(_SECTION_LABEL_STYLE)
    return label


class SettingsDialog(QDialog):
    def __init__(
        self, settings, current_server_url: str, current_verify_ssl: bool,
        current_autostart_enabled: bool,
        on_test_sound: Callable[[], None], on_update_server: Callable[[str, bool], None],
        on_reset_login: Callable[[], None], on_set_autostart: Callable[[bool], None], parent=None,
    ):
        super().__init__(parent)
        self._settings = settings
        self._on_test_sound = on_test_sound
        self._on_update_server = on_update_server
        self._on_set_autostart = on_set_autostart

        self.setWindowTitle(tr("settings.title"))
        self.setMinimumWidth(528)

        self._autostart_toggle = ToggleSwitch()
        self._autostart_toggle.setChecked(current_autostart_enabled)
        start_card = _boxed_list([
            _label_row(tr("settings.autostart"), self._autostart_toggle),
        ])

        self._blink_toggle = ToggleSwitch()
        self._blink_toggle.setChecked(settings.blink_enabled)
        self._sound_toggle = ToggleSwitch()
        self._sound_toggle.setChecked(settings.sound_enabled)
        self._tooltip_toggle = ToggleSwitch()
        self._tooltip_toggle.setChecked(settings.tooltip_enabled)
        self._idle_toggle = ToggleSwitch()
        self._idle_toggle.setChecked(settings.idle_detection_enabled)

        self._server_url_field = QLineEdit(current_server_url)
        self._verify_ssl_toggle = ToggleSwitch()
        self._verify_ssl_toggle.setChecked(current_verify_ssl)
        reset_login_button = QPushButton(tr("settings.reset_login"))
        reset_login_button.clicked.connect(on_reset_login)
        reset_login_row = _row(reset_login_button)
        reset_login_row.layout().insertStretch(0)

        server_card = _boxed_list([
            _row(QLabel(tr("settings.server_url")), self._server_url_field),  # no stretch: field fills the row
            _label_row(tr("settings.verify_ssl"), self._verify_ssl_toggle),
            reset_login_row,
        ])

        self._sound_choices = sounds.available_choices()
        self._sound_choice_combo = QComboBox()
        for key, label, _path in self._sound_choices:
            self._sound_choice_combo.addItem(label, userData=key)
        current_index = next(
            (i for i, (key, _l, _p) in enumerate(self._sound_choices) if key == settings.sound_choice), 0
        )
        self._sound_choice_combo.setCurrentIndex(current_index)

        self._volume_slider = QSlider(Qt.Orientation.Horizontal)
        self._volume_slider.setRange(0, 100)
        self._volume_slider.setValue(round(settings.sound_volume * 100))
        self._volume_slider.setMinimumWidth(140)

        test_button = QPushButton(tr("settings.test"))
        test_button.clicked.connect(self._handle_test_sound)

        def _update_sound_controls_enabled(enabled: bool) -> None:
            self._sound_choice_combo.setEnabled(enabled)
            self._volume_slider.setEnabled(enabled)
            test_button.setEnabled(enabled)

        self._sound_toggle.toggled.connect(_update_sound_controls_enabled)
        _update_sound_controls_enabled(settings.sound_enabled)

        notifications_card = _boxed_list([
            _label_row(tr("settings.blink"), self._blink_toggle),
            _label_row(tr("settings.sound_enabled"), self._sound_toggle),
            _label_row(tr("settings.sound_choice"), self._sound_choice_combo),
            _row(QLabel(tr("settings.volume")), self._volume_slider, test_button),
            _label_row(tr("settings.tooltip"), self._tooltip_toggle),
        ])
        presence_card = _boxed_list([
            _label_row(tr("settings.idle_away"), self._idle_toggle),
        ])

        save_button = QPushButton(tr("settings.save"))
        save_button.setDefault(True)
        save_button.clicked.connect(self._handle_accept)
        cancel_button = QPushButton(tr("common.cancel"))
        cancel_button.clicked.connect(self.reject)

        button_row = QHBoxLayout()
        button_row.addStretch()
        button_row.addWidget(cancel_button)
        button_row.addWidget(save_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(18)
        layout.addWidget(_section_label(tr("settings.section_start")))
        layout.addWidget(start_card)
        layout.addWidget(_section_label(tr("settings.section_server")))
        layout.addWidget(server_card)
        layout.addWidget(_section_label(tr("settings.section_notifications")))
        layout.addWidget(notifications_card)
        layout.addWidget(_section_label(tr("settings.section_presence")))
        layout.addWidget(presence_card)
        layout.addStretch()
        layout.addLayout(button_row)

    def _handle_test_sound(self) -> None:
        # Apply the current volume/choice first so "Test" previews exactly
        # what's about to be saved, even before clicking "Speichern".
        self._settings.sound_volume = self._volume_slider.value() / 100
        self._settings.sound_choice = self._sound_choice_combo.currentData()
        self._on_test_sound()

    def _handle_accept(self) -> None:
        self._on_set_autostart(self._autostart_toggle.isChecked())
        url = self._server_url_field.text().strip().rstrip("/")
        if url:
            self._on_update_server(url, self._verify_ssl_toggle.isChecked())
        self._settings.blink_enabled = self._blink_toggle.isChecked()
        self._settings.sound_enabled = self._sound_toggle.isChecked()
        self._settings.tooltip_enabled = self._tooltip_toggle.isChecked()
        self._settings.idle_detection_enabled = self._idle_toggle.isChecked()
        self._settings.sound_choice = self._sound_choice_combo.currentData()
        self._settings.sound_volume = self._volume_slider.value() / 100
        self._settings.save()
        self.accept()
