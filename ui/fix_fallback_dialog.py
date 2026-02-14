# ui/fix_fallback_dialog.py
"""修正翻译回退项对话框。"""

from __future__ import annotations

import csv
import json
import os
import logging

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout,
    QPushButton, QTableWidget, QTableWidgetItem,
    QHeaderView, QMessageBox, QAbstractItemView, QWidget,
)

from core.utils import file_system

log = logging.getLogger(__name__)


class FixFallbackDialog(QDialog):
    """用于修正翻译回退项的对话框。"""

    def __init__(
        self,
        parent: QWidget,
        app_controller: object,
        fallback_csv_path: str,
        translated_json_path: str,
    ) -> None:
        super().__init__(parent)
        self._app = app_controller
        self._csv_path = fallback_csv_path
        self._json_path = translated_json_path

        self.setWindowTitle("修正翻译回退项")
        self.resize(950, 600)
        self.setModal(True)

        self._headers = ["原文", "最终尝试结果", "修正译文"]
        self._init_ui()
        self._load_data()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)

        # --- 表格 ---
        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(self._headers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        header = self._table.horizontalHeader()
        if header:
            header.setStretchLastSection(True)
            header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
            for col in range(3):
                header.resizeSection(col, 300)
        layout.addWidget(self._table)

        # --- 按钮行 ---
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        cancel_btn = QPushButton("关闭")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)
        save_btn = QPushButton("保存修正并关闭")
        save_btn.clicked.connect(self._save_corrections)
        btn_layout.addWidget(save_btn)
        layout.addLayout(btn_layout)

    def _load_data(self) -> None:
        """从 fallback_corrections.csv 加载数据。"""
        if not os.path.exists(self._csv_path):
            QMessageBox.warning(self, "未找到文件", "未找到回退修正文件。")
            self.reject()
            return

        try:
            with open(self._csv_path, 'r', encoding='utf-8-sig', newline='') as f:
                reader = csv.reader(f, quoting=csv.QUOTE_ALL)
                file_header = next(reader, None)
                if not file_header or len(file_header) < 2:
                    raise ValueError("CSV 文件表头无效或列数不足。")

                has_correction_col = len(file_header) >= 3 and file_header[2].strip() == "修正译文"

                for row_data in reader:
                    if not row_data or len(row_data) < 2:
                        continue
                    original = row_data[0]
                    last_attempt = row_data[1]
                    correction = row_data[2] if has_correction_col and len(row_data) > 2 else ""

                    row_idx = self._table.rowCount()
                    self._table.insertRow(row_idx)
                    # 原文和最终尝试结果设为只读
                    orig_item = QTableWidgetItem(original)
                    orig_item.setFlags(orig_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    self._table.setItem(row_idx, 0, orig_item)

                    attempt_item = QTableWidgetItem(last_attempt)
                    attempt_item.setFlags(attempt_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    self._table.setItem(row_idx, 1, attempt_item)

                    self._table.setItem(row_idx, 2, QTableWidgetItem(correction))

            log.info(f"已从 {self._csv_path} 加载 {self._table.rowCount()} 条回退项。")
        except Exception as e:
            log.exception(f"加载回退修正文件失败: {self._csv_path}")
            QMessageBox.critical(self, "加载失败", f"无法加载回退修正文件:\n{e}")
            self.reject()

    def _save_corrections(self) -> None:
        """收集修正，更新 JSON 文件，并重写 CSV 文件。"""
        # 1. 收集修正
        corrections: dict[str, str] = {}
        for row_idx in range(self._table.rowCount()):
            correction_item = self._table.item(row_idx, 2)
            correction = correction_item.text().strip() if correction_item else ""
            if correction:
                orig_item = self._table.item(row_idx, 0)
                original_key = orig_item.text() if orig_item else ""
                if original_key:
                    corrections[original_key] = correction

        if not corrections:
            QMessageBox.information(self, "无需保存", "没有检测到任何修正。")
            return

        log.info(f"准备保存 {len(corrections)} 条修正...")

        # 2. 读取原始 CSV
        try:
            with open(self._csv_path, 'r', encoding='utf-8-sig', newline='') as f:
                original_csv = list(csv.reader(f, quoting=csv.QUOTE_ALL))
        except Exception as e:
            QMessageBox.critical(self, "错误", f"读取原始回退文件时出错:\n{e}")
            return

        if not original_csv:
            QMessageBox.critical(self, "错误", "原始回退文件为空或无效。")
            return

        csv_header = original_csv[0]

        # 3. 读取并更新 JSON
        try:
            with open(self._json_path, 'r', encoding='utf-8') as f:
                json_data = json.load(f)

            update_count = 0
            for key, value in corrections.items():
                if key in json_data:
                    json_data[key] = value
                    update_count += 1
                else:
                    log.warning(f"JSON 中未找到 Key: {key}")

            log.info(f"已将 {update_count} 条修正应用到 JSON 数据。")
        except Exception as e:
            QMessageBox.critical(self, "错误", f"读取或更新 JSON 文件时出错:\n{e}")
            return

        # 4. 过滤 CSV（移除已修正的行）
        corrected_keys = set(corrections.keys())
        new_csv = [csv_header]
        for row in original_csv[1:]:
            if not row:
                continue
            if row[0] not in corrected_keys:
                new_csv.append(row)

        # 5. 保存文件
        try:
            file_system.ensure_dir_exists(os.path.dirname(self._json_path))
            with open(self._json_path, 'w', encoding='utf-8') as f:
                json.dump(json_data, f, ensure_ascii=False, indent=4)

            file_system.ensure_dir_exists(os.path.dirname(self._csv_path))
            with open(self._csv_path, 'w', newline='', encoding='utf-8-sig') as f:
                writer = csv.writer(f, quoting=csv.QUOTE_ALL)
                writer.writerows(new_csv)

            QMessageBox.information(
                self, "保存成功",
                f"成功应用了 {len(corrections)} 条修正。\nJSON 文件已更新，剩余回退项已保存。"
            )

            # 通知 App 更新 UI 状态
            if hasattr(self._app, '_check_and_update_ui_states'):
                self._app._check_and_update_ui_states()

            self.accept()
        except Exception as e:
            log.exception(f"保存修正文件时出错: {e}")
            QMessageBox.critical(self, "保存失败", f"保存文件时出错:\n{e}")
