# ui/dict_editor.py
"""世界观字典编辑器窗口 — QDialog + QTableWidget 实现。"""

from __future__ import annotations

import csv
import os
import logging
from typing import TYPE_CHECKING

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout,
    QPushButton, QTabWidget, QWidget,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QMessageBox, QAbstractItemView,
)

from core.utils import file_system, text_processing
from core.utils.dictionary_manager import (
    BASE_CHARACTER_DICT_PATH, BASE_ENTITY_DICT_PATH,
    BASE_CHARACTER_HEADERS, BASE_ENTITY_HEADERS,
)
from core.config import DEFAULT_WORLD_DICT_CONFIG

if TYPE_CHECKING:
    from app import RPGTranslatorApp

log = logging.getLogger(__name__)


class DictEditorWindow(QDialog):
    """世界观字典编辑器窗口（支持人物和事物词典 Tab）。"""

    def __init__(
        self,
        parent: QWidget,
        app_controller: RPGTranslatorApp,
        works_dir: str,
        game_path: str | None = None,
        is_base_dict: bool = False,
    ) -> None:
        super().__init__(parent)
        self._app = app_controller
        self._is_base_dict = is_base_dict
        self._game_path = game_path
        self._is_applying_base_dict = False

        # --- 确定文件路径和表头 ---
        if self._is_base_dict:
            self._char_path = BASE_CHARACTER_DICT_PATH
            self._entity_path = BASE_ENTITY_DICT_PATH
            self._char_headers = list(BASE_CHARACTER_HEADERS)
            self._entity_headers = list(BASE_ENTITY_HEADERS)
            self._work_dir = os.path.dirname(BASE_CHARACTER_DICT_PATH)
            title_prefix = "基础字典编辑器"
        else:
            if not game_path:
                QMessageBox.critical(self, "错误", "编辑游戏特定字典时必须提供游戏路径。")
                self.reject()
                return
            game_folder = text_processing.sanitize_filename(os.path.basename(game_path)) or "UntitledGame"
            self._work_dir = os.path.join(works_dir, game_folder)
            world_dict_config = self._app.config.get('world_dict_config', DEFAULT_WORLD_DICT_CONFIG)
            char_fn = world_dict_config.get("character_dict_filename", DEFAULT_WORLD_DICT_CONFIG["character_dict_filename"])
            entity_fn = world_dict_config.get("entity_dict_filename", DEFAULT_WORLD_DICT_CONFIG["entity_dict_filename"])
            self._char_path = os.path.join(self._work_dir, char_fn)
            self._entity_path = os.path.join(self._work_dir, entity_fn)
            self._char_headers = ['原文', '译文', '对应原名', '性别', '年龄', '性格', '口吻', '描述']
            self._entity_headers = ['原文', '译文', '类别', '描述']
            safe_name = text_processing.sanitize_filename(os.path.basename(game_path or '')) or '未命名游戏'
            title_prefix = f"游戏字典编辑器 - {safe_name}"

        self.setWindowTitle(title_prefix)
        self.resize(1100, 700)
        self.setModal(False)

        if not file_system.ensure_dir_exists(self._work_dir):
            QMessageBox.critical(self, "错误", f"无法创建工作目录: {self._work_dir}")
            self.reject()
            return

        self._init_ui()
        self._load_data()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)

        # --- Tab 控件 ---
        self._tab_widget = QTabWidget()

        self._char_table = self._create_table(self._char_headers)
        self._tab_widget.addTab(self._char_table, "人物词典")

        self._entity_table = self._create_table(self._entity_headers)
        self._tab_widget.addTab(self._entity_table, "事物词典")

        layout.addWidget(self._tab_widget)

        # --- 按钮行 ---
        btn_layout = QHBoxLayout()
        self._add_btn = QPushButton("添加行")
        self._add_btn.clicked.connect(self._add_row)
        btn_layout.addWidget(self._add_btn)

        self._delete_btn = QPushButton("删除选中行")
        self._delete_btn.setEnabled(False)
        self._delete_btn.clicked.connect(self._delete_selected_rows)
        btn_layout.addWidget(self._delete_btn)

        btn_layout.addStretch()

        # 应用基础字典按钮（仅游戏字典模式）
        if not self._is_base_dict:
            self._apply_base_btn = QPushButton("应用基础字典")
            self._apply_base_btn.clicked.connect(self._on_apply_base_dict)
            btn_layout.addWidget(self._apply_base_btn)

        self._save_btn = QPushButton("保存全部")
        self._save_btn.clicked.connect(self._on_save_clicked)
        btn_layout.addWidget(self._save_btn)

        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.close)
        btn_layout.addWidget(close_btn)

        layout.addLayout(btn_layout)

        # 选中变化时更新删除按钮状态
        self._char_table.itemSelectionChanged.connect(self._update_delete_btn)
        self._entity_table.itemSelectionChanged.connect(self._update_delete_btn)
        self._tab_widget.currentChanged.connect(self._update_delete_btn)

    def _create_table(self, headers: list[str]) -> QTableWidget:
        """创建并配置一个 QTableWidget。"""
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        header = table.horizontalHeader()
        if header:
            header.setStretchLastSection(True)
            header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        return table

    # ------------------------------------------------------------------
    # 数据加载/保存
    # ------------------------------------------------------------------

    def _load_data(self) -> None:
        self._load_table(self._char_table, self._char_path, self._char_headers)
        self._load_table(self._entity_table, self._entity_path, self._entity_headers)
        self._update_delete_btn()

    def _load_table(self, table: QTableWidget, path: str, headers: list[str]) -> None:
        table.setRowCount(0)
        num_cols = len(headers)

        if not os.path.exists(path):
            log.warning(f"字典文件未找到，将创建空文件: {path}")
            self._create_empty_file(path, headers)
            return

        try:
            with open(path, 'r', encoding='utf-8-sig', newline='') as f:
                reader = csv.reader(f, quoting=csv.QUOTE_ALL)
                next(reader, None)  # 跳过表头
                for row_data in reader:
                    if not row_data:
                        continue
                    # 补齐或截断列数
                    if len(row_data) < num_cols:
                        row_data = list(row_data) + [''] * (num_cols - len(row_data))
                    elif len(row_data) > num_cols:
                        row_data = row_data[:num_cols]
                    row_idx = table.rowCount()
                    table.insertRow(row_idx)
                    for col_idx, value in enumerate(row_data):
                        table.setItem(row_idx, col_idx, QTableWidgetItem(value))
            log.info(f"已从 {path} 加载 {table.rowCount()} 行数据。")
        except Exception as e:
            log.exception(f"加载字典文件失败: {path}")
            QMessageBox.critical(self, "加载失败", f"无法加载字典文件:\n{path}\n{e}")

    def _save_table(self, table: QTableWidget, path: str, headers: list[str]) -> bool:
        num_cols = len(headers)
        data = [headers]
        for row_idx in range(table.rowCount()):
            row = []
            for col_idx in range(num_cols):
                item = table.item(row_idx, col_idx)
                row.append(item.text() if item else "")
            data.append(row)
        try:
            with open(path, 'w', encoding='utf-8-sig', newline='') as f:
                writer = csv.writer(f, quoting=csv.QUOTE_ALL)
                writer.writerows(data)
            log.info(f"字典数据已保存到: {path}")
            return True
        except Exception as e:
            log.exception(f"保存字典文件失败: {path}")
            QMessageBox.critical(self, "保存失败", f"无法保存字典文件:\n{path}\n{e}")
            return False

    def _save_all(self) -> bool:
        char_ok = self._save_table(self._char_table, self._char_path, self._char_headers)
        entity_ok = self._save_table(self._entity_table, self._entity_path, self._entity_headers)
        if char_ok and entity_ok:
            if not self._is_base_dict:
                self._app.log_message("世界观字典已全部保存。", "success")
        return char_ok and entity_ok

    def _create_empty_file(self, path: str, headers: list[str]) -> None:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='utf-8-sig', newline='') as f:
                writer = csv.writer(f, quoting=csv.QUOTE_ALL)
                writer.writerow(headers)
            log.info(f"已创建空的字典文件: {path}")
        except Exception as e:
            log.exception(f"创建空字典文件失败: {path}")
            QMessageBox.critical(self, "错误", f"无法创建字典文件:\n{path}\n{e}")

    # ------------------------------------------------------------------
    # UI 操作
    # ------------------------------------------------------------------

    def _get_active_table(self) -> QTableWidget:
        return self._char_table if self._tab_widget.currentIndex() == 0 else self._entity_table

    def _add_row(self) -> None:
        table = self._get_active_table()
        row_idx = table.rowCount()
        table.insertRow(row_idx)
        for col in range(table.columnCount()):
            table.setItem(row_idx, col, QTableWidgetItem(""))
        table.selectRow(row_idx)
        table.scrollToItem(table.item(row_idx, 0))

    def _delete_selected_rows(self) -> None:
        table = self._get_active_table()
        selected = table.selectionModel().selectedRows()
        if not selected:
            return
        reply = QMessageBox.question(
            self, "确认删除",
            f"确定要删除选中的 {len(selected)} 行吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            for idx in sorted(selected, key=lambda i: i.row(), reverse=True):
                table.removeRow(idx.row())
        self._update_delete_btn()

    def _update_delete_btn(self) -> None:
        table = self._get_active_table()
        self._delete_btn.setEnabled(bool(table.selectionModel().selectedRows()))

    def _on_save_clicked(self) -> None:
        if self._save_all():
            self._app.log_message("字典数据已手动保存。", "success")

    # ------------------------------------------------------------------
    # 应用基础字典
    # ------------------------------------------------------------------

    def _on_apply_base_dict(self) -> None:
        if self._is_base_dict or self._is_applying_base_dict:
            return
        if not self._save_all():
            QMessageBox.warning(self, "保存失败", "应用基础字典前未能成功保存当前更改，操作已取消。")
            return

        self._is_applying_base_dict = True
        self._set_controls_enabled(False)
        original_title = self.windowTitle()
        self.setWindowTitle(f"{original_title} - 正在应用基础字典...")

        self._app.start_task_for_editor_callback(
            task_name='apply_base_dictionary_manual',
            game_path=self._game_path,
            editor_instance=self,
        )

    def handle_apply_base_dict_result(self, success: bool, message: str) -> None:
        """由 AppController 在任务完成后调用。"""
        title = self.windowTitle()
        if title.endswith(" - 正在应用基础字典..."):
            self.setWindowTitle(title.replace(" - 正在应用基础字典...", ""))

        self._load_data()
        self._set_controls_enabled(True)
        self._is_applying_base_dict = False

        if success:
            QMessageBox.information(self, "操作完成", f"应用基础字典已完成。\n{message}")
        else:
            QMessageBox.critical(self, "操作失败", f"应用基础字典时发生错误。\n{message}")

        self.activateWindow()
        self.raise_()

    def _set_controls_enabled(self, enabled: bool) -> None:
        for w in [self._add_btn, self._delete_btn, self._save_btn, self._tab_widget]:
            w.setEnabled(enabled)
        if hasattr(self, '_apply_base_btn'):
            self._apply_base_btn.setEnabled(enabled)
        # 表格编辑触发器
        triggers = (
            QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed
            if enabled
            else QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self._char_table.setEditTriggers(triggers)
        self._entity_table.setEditTriggers(triggers)

