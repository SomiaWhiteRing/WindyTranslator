# ui/main_window.py
"""主窗口。"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QTextCharFormat
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLineEdit, QPushButton, QTabWidget, QTextEdit,
    QLabel, QGroupBox, QInputDialog, QComboBox,
)

from ui.easy_mode_panel import EasyModePanel
from ui.pro_mode_panel import ProModePanel

if TYPE_CHECKING:
    from app import RPGTranslatorApp

log = logging.getLogger(__name__)

# 日志级别 → 颜色映射
_LOG_COLORS: dict[str, QColor] = {
    "normal": QColor("black"),
    "success": QColor("blue"),
    "error": QColor("red"),
    "warning": QColor("orange"),
    "debug": QColor("grey"),
}

# 过滤器选项：显示名称 → 包含的级别集合
_FILTER_OPTIONS: dict[str, set[str] | None] = {
    "全部": None,  # None 表示不过滤
    "错误": {"error"},
    "警告 + 错误": {"warning", "error"},
    "成功": {"success"},
    "调试": {"debug"},
}


@dataclass(slots=True)
class LogEntry:
    """单条日志记录。"""

    timestamp: str
    level: str
    message: str


class MainWindow(QMainWindow):
    """应用程序主窗口。"""

    # 日志条目上限，超过后丢弃最早的条目
    _MAX_LOG_ENTRIES = 5000

    def __init__(self, app: RPGTranslatorApp, config: dict) -> None:
        super().__init__()
        self._app = app
        self._config = config
        self._log_entries: list[LogEntry] = []

        self.setWindowTitle("WindyTranslator")
        self.resize(750, 700)

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(10, 10, 10, 10)

        # --- 1. 游戏路径选择区域 ---
        path_group = QGroupBox("游戏路径")
        path_layout = QHBoxLayout(path_group)
        self.game_path_edit = QLineEdit()
        self.game_path_edit.setReadOnly(True)
        path_layout.addWidget(self.game_path_edit)
        self.browse_btn = QPushButton("浏览...")
        self.browse_btn.setFixedWidth(80)
        self.browse_btn.clicked.connect(self._app.browse_game_path)
        path_layout.addWidget(self.browse_btn)
        main_layout.addWidget(path_group)

        # --- 2. 功能区 TabWidget ---
        self.tab_widget = QTabWidget()

        self.easy_panel = EasyModePanel(self._app)
        self.tab_widget.addTab(self.easy_panel, "轻松模式")

        self.pro_panel = ProModePanel(self._app, self._config)
        self.tab_widget.addTab(self.pro_panel, "专业模式")

        main_layout.addWidget(self.tab_widget)

        # --- 3. 日志区域 ---
        log_group = QGroupBox("操作日志")
        log_layout = QVBoxLayout(log_group)

        # 过滤器行
        filter_layout = QHBoxLayout()
        filter_layout.addWidget(QLabel("日志过滤:"))
        self.log_filter_combo = QComboBox()
        self.log_filter_combo.addItems(list(_FILTER_OPTIONS.keys()))
        self.log_filter_combo.setFixedWidth(140)
        self.log_filter_combo.currentTextChanged.connect(self._on_filter_changed)
        filter_layout.addWidget(self.log_filter_combo)
        self.clear_log_btn = QPushButton("清空日志")
        self.clear_log_btn.setFixedWidth(80)
        self.clear_log_btn.clicked.connect(self._clear_log)
        filter_layout.addWidget(self.clear_log_btn)
        filter_layout.addStretch()
        log_layout.addLayout(filter_layout)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        log_layout.addWidget(self.log_text)
        main_layout.addWidget(log_group, stretch=1)

        # --- 4. 状态栏 ---
        self.status_label = QLabel("就绪")
        main_layout.addWidget(self.status_label)

        # --- 收集所有需要禁用的控件 ---
        self._flat_controls: list[QWidget] = [
            self.browse_btn,
            *self.easy_panel.get_controls(),
            *self.pro_panel.get_controls(),
        ]

    # ------------------------------------------------------------------
    # 公共方法（供 AppController 调用）
    # ------------------------------------------------------------------

    def set_game_path_text(self, path: str) -> None:
        """更新路径输入框的显示文本。"""
        self.game_path_edit.setText(path)

    def add_log(self, message: str, level: str = "normal") -> None:
        """向日志区域追加带时间戳和颜色的消息。

        消息同时存储到内部列表，支持按级别过滤重新渲染。
        """
        timestamp = datetime.datetime.now().strftime("[%H:%M:%S]")
        entry = LogEntry(timestamp=timestamp, level=level, message=message)
        self._log_entries.append(entry)

        # 超过上限时丢弃最早的条目
        if len(self._log_entries) > self._MAX_LOG_ENTRIES:
            self._log_entries = self._log_entries[-self._MAX_LOG_ENTRIES:]

        # 如果当前过滤器允许此级别，直接追加到显示区域
        if self._entry_matches_filter(entry):
            self._append_entry_to_display(entry)

    def update_status(self, message: str) -> None:
        """更新状态栏文本。"""
        self.status_label.setText(message)

    def get_status(self) -> str:
        """获取当前状态栏文本。"""
        return self.status_label.text()

    def update_easy_status(self, message: str) -> None:
        """更新轻松模式面板的状态标签。"""
        self.easy_panel.update_status(message)

    def update_easy_progress(self, value: float) -> None:
        """更新轻松模式面板的进度条。"""
        self.easy_panel.update_progress(value)

    def set_controls_enabled(self, enabled: bool) -> None:
        """启用或禁用窗口中的主要交互控件。"""
        for control in self._flat_controls:
            control.setEnabled(enabled)
        # 禁用/启用 Tab 切换
        for i in range(self.tab_widget.count()):
            self.tab_widget.setTabEnabled(i, enabled)

    def get_current_mode(self) -> str:
        """获取当前选中的标签页对应的模式。"""
        return 'easy' if self.tab_widget.currentIndex() == 0 else 'pro'

    def switch_to_mode(self, mode: str) -> None:
        """切换到指定的模式标签页。"""
        self.tab_widget.setCurrentIndex(0 if mode == 'easy' else 1)

    def update_rtp_button_text(self) -> None:
        """更新专业模式面板上的 RTP 选择按钮文本。"""
        self.pro_panel.update_rtp_button_text()

    def update_fix_fallback_button_state(self, enabled: bool) -> None:
        """更新专业模式面板上的修正回退按钮状态。"""
        self.pro_panel.update_fix_fallback_button_state(enabled)

    def show_file_selection_dialog(
        self, title: str, prompt: str, file_list: list[str]
    ) -> str | None:
        """弹出列表选择对话框，返回选中的文件名或 None。"""
        item, ok = QInputDialog.getItem(
            self, title, prompt, file_list, 0, False
        )
        return item if ok else None

    # ------------------------------------------------------------------
    # 内部方法：日志过滤
    # ------------------------------------------------------------------

    def _get_active_filter(self) -> set[str] | None:
        """获取当前选中的过滤级别集合。None 表示显示全部。"""
        filter_name = self.log_filter_combo.currentText()
        return _FILTER_OPTIONS.get(filter_name)

    def _entry_matches_filter(self, entry: LogEntry) -> bool:
        """检查日志条目是否匹配当前过滤器。"""
        allowed = self._get_active_filter()
        if allowed is None:
            return True
        return entry.level in allowed

    def _append_entry_to_display(self, entry: LogEntry) -> None:
        """将单条日志条目追加到 QTextEdit 显示区域。"""
        fmt = QTextCharFormat()
        fmt.setForeground(_LOG_COLORS.get(entry.level, _LOG_COLORS["normal"]))
        cursor = self.log_text.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(f"{entry.timestamp} {entry.message}\n", fmt)
        self.log_text.setTextCursor(cursor)
        self.log_text.ensureCursorVisible()

    def _rerender_log(self) -> None:
        """根据当前过滤器重新渲染整个日志区域。"""
        self.log_text.clear()
        for entry in self._log_entries:
            if self._entry_matches_filter(entry):
                self._append_entry_to_display(entry)

    def _on_filter_changed(self, _text: str) -> None:
        """过滤器下拉框变更时重新渲染日志。"""
        self._rerender_log()

    def _clear_log(self) -> None:
        """清空所有日志条目和显示区域。"""
        self._log_entries.clear()
        self.log_text.clear()
