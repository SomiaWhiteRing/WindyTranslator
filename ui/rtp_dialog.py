# ui/rtp_dialog.py
"""RTP 选择对话框。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout,
    QPushButton, QCheckBox, QLabel, QWidget,
)

if TYPE_CHECKING:
    from app import RPGTranslatorApp


class RTPSelectionWindow(QDialog):
    """RTP 选择对话框窗口。"""

    def __init__(self, parent: QWidget, app: RPGTranslatorApp, rtp_config: dict) -> None:
        super().__init__(parent)
        self._app = app
        self._rtp_config = rtp_config

        self.setWindowTitle("选择RTP")
        self.setFixedSize(250, 200)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("选择要安装的RTP文件:"))

        self._checkboxes: dict[str, QCheckBox] = {}
        rtp_items = [
            ('2000', "RPG Maker 2000", True),
            ('2000en', "RPG Maker 2000 (英文版)", False),
            ('2003', "RPG Maker 2003", False),
            ('2003steam', "RPG Maker 2003 (Steam版)", False),
        ]
        for key, label, default in rtp_items:
            cb = QCheckBox(label)
            cb.setChecked(self._rtp_config.get(key, default))
            self._checkboxes[key] = cb
            layout.addWidget(cb)

        confirm_btn = QPushButton("确定")
        confirm_btn.clicked.connect(self._on_confirm)
        layout.addWidget(confirm_btn)

    def _on_confirm(self) -> None:
        """保存选择并关闭。"""
        for key, cb in self._checkboxes.items():
            self._rtp_config[key] = cb.isChecked()
        self._app.save_config()
        self._app.main_window.update_rtp_button_text()
        self.accept()
