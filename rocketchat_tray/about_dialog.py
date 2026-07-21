from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from . import __version__
from .i18n import tr
from .resources import icon_path

APP_NAME = "Rocket.Chat Tray"
LICENSE_NAME = "MIT License"
REPO_URL = "https://github.com/rtulke/rocket.chat-tray/"


class AboutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("about.title", app_name=APP_NAME))

        icon_label = QLabel()
        icon_label.setPixmap(
            QPixmap(str(icon_path("online"))).scaled(
                64, 64, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
            )
        )
        icon_label.setAlignment(Qt.AlignmentFlag.AlignTop)

        text_label = QLabel(
            f"<b>{APP_NAME}</b><br>"
            f"Version {__version__}<br><br>"
            f"{tr('about.license_label', license=LICENSE_NAME)}<br>"
            f'<a href="{REPO_URL}">{REPO_URL}</a>'
        )
        text_label.setTextFormat(Qt.TextFormat.RichText)
        text_label.setOpenExternalLinks(True)
        text_label.setWordWrap(True)

        content_row = QHBoxLayout()
        content_row.addWidget(icon_label)
        content_row.addSpacing(14)
        content_row.addWidget(text_label, stretch=1)

        ok_button = QPushButton(tr("common.ok"))
        ok_button.setDefault(True)
        ok_button.clicked.connect(self.accept)

        button_row = QHBoxLayout()
        button_row.addStretch()
        button_row.addWidget(ok_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)
        layout.addLayout(content_row)
        layout.addLayout(button_row)
