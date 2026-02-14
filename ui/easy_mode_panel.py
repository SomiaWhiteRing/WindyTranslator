# ui/easy_mode_panel.py
"""轻松模式面板 — 一键翻译流程的简化界面。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout,
    QPushButton, QProgressBar, QLabel,
)

if TYPE_CHECKING:
    from app import RPGTranslatorApp

class EasyModePanel(QWidget):
    """轻松模式面板：配置按钮、一键翻译、进度条、状态标签。"""

    def __init__(self, app: RPGTranslatorApp, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._app = app
        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)

        # --- 按钮行 ---
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        self.gemini_config_btn = QPushButton("字典API配置")
        self.gemini_config_btn.setFixedWidth(130)
        self.gemini_config_btn.clicked.connect(lambda: self._app.start_task('configure_gemini'))
        btn_layout.addWidget(self.gemini_config_btn)

        self.deepseek_config_btn = QPushButton("翻译API配置")
        self.deepseek_config_btn.setFixedWidth(130)
        self.deepseek_config_btn.clicked.connect(lambda: self._app.start_task('configure_deepseek'))
        btn_layout.addWidget(self.deepseek_config_btn)

        self.start_btn = QPushButton("开始翻译")
        self.start_btn.setFixedWidth(130)
        self.start_btn.clicked.connect(lambda: self._app.start_task('easy_flow', mode='easy'))
        btn_layout.addWidget(self.start_btn)

        self.start_game_btn = QPushButton("开始游戏")
        self.start_game_btn.setFixedWidth(130)
        self.start_game_btn.clicked.connect(lambda: self._app.start_task('start_game'))
        btn_layout.addWidget(self.start_game_btn)

        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        layout.addSpacing(20)

        # --- 进度条 ---
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        # --- 状态标签 ---
        self.status_label = QLabel('选择游戏目录后点击"开始翻译"')
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.status_label)

        layout.addStretch()

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    def get_controls(self) -> list[QWidget]:
        """返回需要在任务运行时禁用的控件列表。"""
        return [
            self.gemini_config_btn,
            self.deepseek_config_btn,
            self.start_btn,
            self.start_game_btn,
        ]

    def update_status(self, message: str) -> None:
        """更新状态标签文本。"""
        self.status_label.setText(message)

    def update_progress(self, value: float) -> None:
        """更新进度条（0~100）。"""
        clamped = max(0, min(100, int(value)))
        self.progress_bar.setValue(clamped)

    def reset_state(self) -> None:
        """重置进度条和状态标签到初始状态。"""
        self.progress_bar.setValue(0)
        self.status_label.setText('选择游戏目录后点击"开始翻译"')
