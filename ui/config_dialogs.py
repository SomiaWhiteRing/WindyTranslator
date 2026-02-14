# ui/config_dialogs.py
"""API 配置对话框 — 世界观字典配置 & 翻译配置。"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from PyQt6.QtCore import Qt, QMetaObject, Q_ARG
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QLineEdit, QComboBox, QPushButton, QCheckBox,
    QTextEdit, QSpinBox, QGroupBox, QMessageBox,
    QSplitter, QWidget,
)

from core.api_clients import gemini, deepseek
from core.config import DEFAULT_WORLD_DICT_CONFIG, DEFAULT_TRANSLATE_CONFIG

if TYPE_CHECKING:
    from app import RPGTranslatorApp

log = logging.getLogger(__name__)

PROVIDER_GEMINI = 'gemini'
PROVIDER_OPENAI = 'openai'


class WorldDictConfigWindow(QDialog):
    """世界观字典配置对话框。"""

    def __init__(self, parent: QWidget, app: RPGTranslatorApp, world_dict_config: dict) -> None:
        super().__init__(parent)
        self._app = app
        self._config = world_dict_config
        self._initializing = True
        self._connection_tested_ok = False

        self._provider_display_map = {
            PROVIDER_GEMINI: "Google Gemini 原生",
            PROVIDER_OPENAI: "OpenAI 兼容端点",
        }
        self._provider_value_map = {v: k for k, v in self._provider_display_map.items()}

        self.setWindowTitle("世界观字典配置")
        self.resize(820, 560)
        self.setModal(True)

        self._init_ui()
        self._update_provider_ui(initial=True)

        # 初始状态判断
        minimal_ready = bool(self._api_key_edit.text().strip())
        if self._get_provider() == PROVIDER_OPENAI:
            minimal_ready = minimal_ready and bool(self._api_url_edit.text().strip())
        self._connection_tested_ok = minimal_ready
        if self._connection_tested_ok:
            self._set_status("配置已加载", "green")
        else:
            self._set_status("请输入必要信息并测试连接", "red")
        self._save_btn.setEnabled(self._connection_tested_ok)

        self._initializing = False
        self._update_connection_signature()

    def _init_ui(self) -> None:
        layout = QGridLayout(self)
        layout.setColumnStretch(1, 1)
        layout.setColumnStretch(2, 1)

        provider_value = (self._config.get("provider") or PROVIDER_GEMINI).lower()
        if provider_value not in self._provider_display_map:
            provider_value = PROVIDER_GEMINI

        row = 0

        # 供应商
        layout.addWidget(QLabel("模型供应商:"), row, 0)
        self._provider_combo = QComboBox()
        self._provider_combo.addItems(list(self._provider_display_map.values()))
        self._provider_combo.setCurrentText(self._provider_display_map[provider_value])
        self._provider_combo.currentTextChanged.connect(self._on_provider_changed)
        layout.addWidget(self._provider_combo, row, 1, 1, 2)
        row += 1

        # API Key
        self._api_key_label = QLabel("API Key:")
        layout.addWidget(self._api_key_label, row, 0)
        key_widget = QWidget()
        key_layout = QHBoxLayout(key_widget)
        key_layout.setContentsMargins(0, 0, 0, 0)
        self._api_key_edit = QLineEdit(self._config.get("api_key", ""))
        self._api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._api_key_edit.textChanged.connect(self._on_config_change)
        key_layout.addWidget(self._api_key_edit)
        self._show_key_cb = QCheckBox("显示")
        self._show_key_cb.toggled.connect(
            lambda checked: self._api_key_edit.setEchoMode(
                QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
            )
        )
        key_layout.addWidget(self._show_key_cb)
        layout.addWidget(key_widget, row, 1, 1, 2)
        row += 1

        # API URL
        self._api_url_label = QLabel("API 基础地址:")
        layout.addWidget(self._api_url_label, row, 0)
        self._api_url_edit = QLineEdit(self._config.get("api_url", ""))
        self._api_url_edit.textChanged.connect(self._on_config_change)
        layout.addWidget(self._api_url_edit, row, 1, 1, 2)
        self._api_url_row = row
        row += 1

        # 模型名称
        layout.addWidget(QLabel("模型名称:"), row, 0)
        self._model_combo = QComboBox()
        self._model_combo.setEditable(True)
        self._model_combo.addItems([
            "gemini-2.5-pro-preview-05-06",
            "gemini-1.5-pro-latest",
            "gemini-1.5-flash-latest",
            "gemini-pro",
        ])
        self._model_combo.setCurrentText(
            self._config.get("model", DEFAULT_WORLD_DICT_CONFIG["model"])
        )
        self._model_combo.currentTextChanged.connect(self._on_config_change)
        layout.addWidget(self._model_combo, row, 1, 1, 2)
        row += 1

        # OpenAI 参数行
        self._openai_params_widget = QWidget()
        params_layout = QHBoxLayout(self._openai_params_widget)
        params_layout.setContentsMargins(0, 0, 0, 0)
        params_layout.addWidget(QLabel("温度 (0-2):"))
        self._temp_edit = QLineEdit(
            str(self._config.get("openai_temperature", DEFAULT_WORLD_DICT_CONFIG["openai_temperature"]))
        )
        self._temp_edit.setFixedWidth(60)
        self._temp_edit.textChanged.connect(self._on_config_change)
        params_layout.addWidget(self._temp_edit)
        params_layout.addWidget(QLabel("最大 tokens:"))
        max_tokens_cfg = self._config.get("openai_max_tokens", DEFAULT_WORLD_DICT_CONFIG["openai_max_tokens"])
        self._max_tokens_edit = QLineEdit("" if max_tokens_cfg in (None, "") else str(max_tokens_cfg))
        self._max_tokens_edit.setFixedWidth(80)
        self._max_tokens_edit.textChanged.connect(self._on_config_change)
        params_layout.addWidget(self._max_tokens_edit)
        params_layout.addWidget(QLabel("(留空使用服务端默认)"))
        params_layout.addStretch()
        layout.addWidget(self._openai_params_widget, row, 0, 1, 3)
        self._openai_params_row = row
        row += 1

        # Prompt 区域
        splitter = QSplitter(Qt.Orientation.Horizontal)
        char_group = QGroupBox("人物提取 Prompt")
        char_layout = QVBoxLayout(char_group)
        self._char_prompt_text = QTextEdit()
        self._char_prompt_text.setPlainText(
            self._config.get("character_prompt_template", DEFAULT_WORLD_DICT_CONFIG["character_prompt_template"])
        )
        self._char_prompt_text.textChanged.connect(self._on_config_change)
        char_layout.addWidget(self._char_prompt_text)
        splitter.addWidget(char_group)

        entity_group = QGroupBox("事物提取 Prompt")
        entity_layout = QVBoxLayout(entity_group)
        self._entity_prompt_text = QTextEdit()
        self._entity_prompt_text.setPlainText(
            self._config.get("entity_prompt_template", DEFAULT_WORLD_DICT_CONFIG["entity_prompt_template"])
        )
        self._entity_prompt_text.textChanged.connect(self._on_config_change)
        entity_layout.addWidget(self._entity_prompt_text)
        splitter.addWidget(entity_group)

        layout.addWidget(splitter, row, 0, 1, 3)
        layout.setRowStretch(row, 1)
        row += 1

        # 状态标签
        self._status_label = QLabel("请输入配置并测试连接")
        layout.addWidget(self._status_label, row, 0, 1, 3)
        row += 1

        # 按钮行
        btn_layout = QHBoxLayout()
        self._enable_base_dict_cb = QCheckBox("启用基础字典")
        self._enable_base_dict_cb.setChecked(self._config.get("enable_base_dictionary", True))
        btn_layout.addWidget(self._enable_base_dict_cb)
        edit_base_btn = QPushButton("编辑基础字典")
        edit_base_btn.clicked.connect(self._open_base_dict_editor)
        btn_layout.addWidget(edit_base_btn)
        btn_layout.addStretch()
        self._test_btn = QPushButton("测试连接")
        self._test_btn.clicked.connect(self._test_connection)
        btn_layout.addWidget(self._test_btn)
        self._save_btn = QPushButton("保存")
        self._save_btn.clicked.connect(self._save_config)
        btn_layout.addWidget(self._save_btn)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout, row, 0, 1, 3)

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _get_provider(self) -> str:
        display = self._provider_combo.currentText()
        return self._provider_value_map.get(display, PROVIDER_GEMINI)

    def _on_provider_changed(self) -> None:
        self._update_provider_ui()
        self._on_config_change()

    def _update_provider_ui(self, initial: bool = False) -> None:
        is_gemini = self._get_provider() == PROVIDER_GEMINI
        self._api_url_label.setVisible(not is_gemini)
        self._api_url_edit.setVisible(not is_gemini)
        self._openai_params_widget.setVisible(not is_gemini)
        self._api_key_label.setText("Gemini API Key:" if is_gemini else "OpenAI API Key:")
        title_suffix = "(Gemini)" if is_gemini else "(OpenAI 兼容)"
        self.setWindowTitle(f"世界观字典配置 {title_suffix}")
        if not initial:
            self._set_status("供应商已切换，请重新测试连接", "orange")

    def _open_base_dict_editor(self) -> None:
        self.setEnabled(False)
        editor = self._app.open_base_dict_editor(parent_for_editor=self)
        if editor:
            editor.exec()
        self.setEnabled(True)
        self.activateWindow()

    def _current_connection_signature(self) -> tuple:
        provider = self._get_provider()
        return (
            provider,
            self._api_key_edit.text().strip(),
            self._api_url_edit.text().strip() if provider == PROVIDER_OPENAI else "",
            self._model_combo.currentText().strip(),
        )

    def _update_connection_signature(self) -> None:
        self._last_sig = self._current_connection_signature()

    def _on_config_change(self) -> None:
        if self._initializing:
            return
        if getattr(self, '_last_sig', None) != self._current_connection_signature():
            self._connection_tested_ok = False
            self._save_btn.setEnabled(False)
            self._set_status("关键配置已修改，请重新测试连接", "orange")
            self._last_sig = self._current_connection_signature()

    def _set_status(self, message: str, color: str) -> None:
        self._status_label.setText(message)
        self._status_label.setStyleSheet(f"color: {color};")

    def _test_connection(self) -> None:
        provider = self._get_provider()
        api_key = self._api_key_edit.text().strip()
        model = self._model_combo.currentText().strip()
        api_url = self._api_url_edit.text().strip()

        if not api_key:
            QMessageBox.critical(self, "错误", "请输入 API Key")
            return
        if provider == PROVIDER_OPENAI and not api_url:
            QMessageBox.critical(self, "错误", "请输入 OpenAI 兼容 API 的基础地址")
            return
        if not model:
            QMessageBox.critical(self, "错误", "请选择或输入模型名称")
            return

        self._set_status("正在测试连接...", "blue")
        self._test_btn.setEnabled(False)
        self._save_btn.setEnabled(False)

        def _thread_func() -> None:
            try:
                if provider == PROVIDER_GEMINI:
                    client = gemini.GeminiClient(api_key)
                else:
                    client = deepseek.DeepSeekClient(base_url=api_url, api_key=api_key)
                success, message = client.test_connection(model)
                QMetaObject.invokeMethod(
                    self, "_on_test_result",
                    Qt.ConnectionType.QueuedConnection,
                    Q_ARG(bool, success), Q_ARG(str, message),
                )
            except Exception as exc:
                QMetaObject.invokeMethod(
                    self, "_on_test_result",
                    Qt.ConnectionType.QueuedConnection,
                    Q_ARG(bool, False), Q_ARG(str, f"测试时发生错误: {exc}"),
                )

        threading.Thread(target=_thread_func, daemon=True).start()

    def _on_test_result(self, success: bool, message: str) -> None:
        self._test_btn.setEnabled(True)
        if success:
            self._connection_tested_ok = True
            self._update_connection_signature()
            self._set_status("连接成功!", "green")
            self._save_btn.setEnabled(True)
            QMessageBox.information(self, "成功", message)
        else:
            self._connection_tested_ok = False
            self._set_status("连接失败", "red")
            self._save_btn.setEnabled(False)
            QMessageBox.critical(self, "连接失败", message)

    def _save_config(self) -> None:
        if not self._connection_tested_ok:
            QMessageBox.warning(self, "无法保存", "请先成功测试连接后再保存。")
            return

        temp_text = self._temp_edit.text().strip()
        if temp_text:
            try:
                openai_temperature = float(temp_text)
            except ValueError:
                QMessageBox.critical(self, "错误", "温度必须是数字。")
                return
        else:
            openai_temperature = DEFAULT_WORLD_DICT_CONFIG["openai_temperature"]

        max_tokens_text = self._max_tokens_edit.text().strip()
        openai_max_tokens = None
        if max_tokens_text:
            try:
                openai_max_tokens = int(max_tokens_text)
            except ValueError:
                QMessageBox.critical(self, "错误", "最大 tokens 必须是整数。")
                return

        self._config["provider"] = self._get_provider()
        self._config["api_key"] = self._api_key_edit.text().strip()
        self._config["api_url"] = self._api_url_edit.text().strip()
        self._config["model"] = self._model_combo.currentText().strip()
        self._config["openai_temperature"] = openai_temperature
        self._config["openai_max_tokens"] = openai_max_tokens
        self._config["character_prompt_template"] = self._char_prompt_text.toPlainText().strip()
        self._config["entity_prompt_template"] = self._entity_prompt_text.toPlainText().strip()
        self._config["enable_base_dictionary"] = self._enable_base_dict_cb.isChecked()
        self._config.setdefault(
            "character_dict_filename", DEFAULT_WORLD_DICT_CONFIG["character_dict_filename"]
        )
        self._config.setdefault(
            "entity_dict_filename", DEFAULT_WORLD_DICT_CONFIG["entity_dict_filename"]
        )

        self._app.save_config()
        log.info("世界观字典配置已更新。")
        self.accept()


class TranslateConfigWindow(QDialog):
    """翻译 JSON (OpenAI 兼容 API) 配置对话框。"""

    def __init__(self, parent: QWidget, app: RPGTranslatorApp, translate_config: dict) -> None:
        super().__init__(parent)
        self._app = app
        self._config = translate_config
        self._initializing = True
        self._connection_tested_ok = False

        self.setWindowTitle("翻译JSON文件配置 (OpenAI兼容 API)")
        self.resize(600, 580)
        self.setModal(True)

        self._init_ui()

        self._initializing = False
        if self._config.get("api_key") and self._config.get("api_url"):
            self._set_status("配置已加载，请测试连接", "orange")
        else:
            self._set_status("请输入 API URL 和 Key 并测试连接", "red")

    def _init_ui(self) -> None:
        layout = QGridLayout(self)
        layout.setColumnStretch(1, 1)

        row = 0

        # API URL
        layout.addWidget(QLabel("API URL:"), row, 0)
        self._api_url_edit = QLineEdit(self._config.get("api_url", DEFAULT_TRANSLATE_CONFIG["api_url"]))
        self._api_url_edit.textChanged.connect(self._on_config_change)
        layout.addWidget(self._api_url_edit, row, 1, 1, 3)
        row += 1

        # API Key
        layout.addWidget(QLabel("API Key:"), row, 0)
        key_widget = QWidget()
        key_layout = QHBoxLayout(key_widget)
        key_layout.setContentsMargins(0, 0, 0, 0)
        self._api_key_edit = QLineEdit(self._config.get("api_key", ""))
        self._api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._api_key_edit.textChanged.connect(self._on_config_change)
        key_layout.addWidget(self._api_key_edit)
        show_key_cb = QCheckBox("显示")
        show_key_cb.toggled.connect(
            lambda checked: self._api_key_edit.setEchoMode(
                QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
            )
        )
        key_layout.addWidget(show_key_cb)
        layout.addWidget(key_widget, row, 1, 1, 3)
        row += 1

        # 模型名称
        layout.addWidget(QLabel("模型名称:"), row, 0)
        self._model_combo = QComboBox()
        self._model_combo.setEditable(True)
        self._model_combo.addItems([
            "deepseek-chat", "deepseek-coder",
            "gpt-3.5-turbo", "gpt-4", "gpt-4-turbo-preview", "gpt-4o",
            "moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k",
        ])
        self._model_combo.setCurrentText(self._config.get("model", DEFAULT_TRANSLATE_CONFIG["model"]))
        self._model_combo.currentTextChanged.connect(self._on_config_change)
        layout.addWidget(self._model_combo, row, 1, 1, 3)
        row += 1

        # Spinbox 行
        spin_widget = QWidget()
        spin_layout = QHBoxLayout(spin_widget)
        spin_layout.setContentsMargins(0, 0, 0, 0)
        spin_layout.addWidget(QLabel("批次大小:"))
        self._batch_spin = QSpinBox()
        self._batch_spin.setRange(1, 100)
        self._batch_spin.setValue(self._config.get("batch_size", DEFAULT_TRANSLATE_CONFIG["batch_size"]))
        spin_layout.addWidget(self._batch_spin)
        spin_layout.addWidget(QLabel("上文行数:"))
        self._context_spin = QSpinBox()
        self._context_spin.setRange(0, 50)
        self._context_spin.setValue(self._config.get("context_lines", DEFAULT_TRANSLATE_CONFIG["context_lines"]))
        spin_layout.addWidget(self._context_spin)
        spin_layout.addWidget(QLabel("并发数:"))
        self._concur_spin = QSpinBox()
        self._concur_spin.setRange(1, 256)
        self._concur_spin.setValue(self._config.get("concurrency", DEFAULT_TRANSLATE_CONFIG["concurrency"]))
        spin_layout.addWidget(self._concur_spin)
        spin_layout.addStretch()
        layout.addWidget(spin_widget, row, 0, 1, 4)
        row += 1

        # 语言选择行
        lang_widget = QWidget()
        lang_layout = QHBoxLayout(lang_widget)
        lang_layout.setContentsMargins(0, 0, 0, 0)
        lang_layout.addWidget(QLabel("源语言:"))
        self._source_lang_combo = QComboBox()
        self._source_lang_combo.addItems([
            "日语", "英语", "简体中文", "繁体中文", "韩语", "俄语", "法语", "德语", "西班牙语", "自动检测"
        ])
        self._source_lang_combo.setCurrentText(
            self._config.get("source_language", DEFAULT_TRANSLATE_CONFIG["source_language"])
        )
        lang_layout.addWidget(self._source_lang_combo)
        lang_layout.addWidget(QLabel("目标语言:"))
        self._target_lang_combo = QComboBox()
        self._target_lang_combo.addItems([
            "简体中文", "繁体中文", "英语", "日语", "韩语", "俄语", "法语", "德语", "西班牙语"
        ])
        self._target_lang_combo.setCurrentText(
            self._config.get("target_language", DEFAULT_TRANSLATE_CONFIG["target_language"])
        )
        lang_layout.addWidget(self._target_lang_combo)
        lang_layout.addStretch()
        layout.addWidget(lang_widget, row, 0, 1, 4)
        row += 1

        # Prompt 模板
        prompt_group = QGroupBox("Prompt 模板")
        prompt_layout = QVBoxLayout(prompt_group)
        self._prompt_text = QTextEdit()
        self._prompt_text.setPlainText(
            self._config.get("prompt_template", DEFAULT_TRANSLATE_CONFIG["prompt_template"])
        )
        self._prompt_text.textChanged.connect(self._on_config_change)
        prompt_layout.addWidget(self._prompt_text)
        layout.addWidget(prompt_group, row, 0, 1, 4)
        layout.setRowStretch(row, 1)
        row += 1

        # 状态标签
        self._status_label = QLabel("请先测试连接")
        layout.addWidget(self._status_label, row, 0, 1, 4)
        row += 1

        # 按钮行
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        self._test_btn = QPushButton("测试连接")
        self._test_btn.clicked.connect(self._test_connection)
        btn_layout.addWidget(self._test_btn)
        self._save_btn = QPushButton("保存")
        self._save_btn.setEnabled(False)
        self._save_btn.clicked.connect(self._save_config)
        btn_layout.addWidget(self._save_btn)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout, row, 0, 1, 4)

    def _set_status(self, message: str, color: str) -> None:
        self._status_label.setText(message)
        self._status_label.setStyleSheet(f"color: {color};")

    def _on_config_change(self) -> None:
        if self._initializing:
            return
        self._connection_tested_ok = False
        self._save_btn.setEnabled(False)
        self._set_status("配置已修改，请重新测试连接", "orange")

    def _test_connection(self) -> None:
        api_url = self._api_url_edit.text().strip()
        api_key = self._api_key_edit.text().strip()
        model = self._model_combo.currentText().strip()

        if not api_key or not api_url:
            QMessageBox.critical(self, "错误", "请输入 API URL 和 API Key")
            return
        if not model:
            QMessageBox.critical(self, "错误", "请输入模型名称")
            return

        self._set_status("正在测试连接...", "blue")
        self._test_btn.setEnabled(False)
        self._save_btn.setEnabled(False)

        def _thread_func() -> None:
            try:
                client = deepseek.DeepSeekClient(base_url=api_url, api_key=api_key)
                success, message = client.test_connection(model)
                QMetaObject.invokeMethod(
                    self, "_on_test_result",
                    Qt.ConnectionType.QueuedConnection,
                    Q_ARG(bool, success), Q_ARG(str, message),
                )
            except Exception as exc:
                QMetaObject.invokeMethod(
                    self, "_on_test_result",
                    Qt.ConnectionType.QueuedConnection,
                    Q_ARG(bool, False), Q_ARG(str, f"测试时发生错误: {exc}"),
                )

        threading.Thread(target=_thread_func, daemon=True).start()

    def _on_test_result(self, success: bool, message: str) -> None:
        self._test_btn.setEnabled(True)
        if success:
            self._connection_tested_ok = True
            self._set_status("连接成功!", "green")
            self._save_btn.setEnabled(True)
            QMessageBox.information(self, "成功", message)
        else:
            self._connection_tested_ok = False
            self._set_status("连接失败", "red")
            self._save_btn.setEnabled(False)
            QMessageBox.critical(self, "连接失败", message)

    def _save_config(self) -> None:
        if not self._connection_tested_ok:
            QMessageBox.warning(self, "无法保存", "请先成功测试连接后再保存。")
            return

        self._config["api_url"] = self._api_url_edit.text().strip()
        self._config["api_key"] = self._api_key_edit.text().strip()
        self._config["model"] = self._model_combo.currentText().strip()
        self._config["batch_size"] = self._batch_spin.value()
        self._config["context_lines"] = self._context_spin.value()
        self._config["concurrency"] = self._concur_spin.value()
        self._config["source_language"] = self._source_lang_combo.currentText()
        self._config["target_language"] = self._target_lang_combo.currentText()
        self._config["prompt_template"] = self._prompt_text.toPlainText().strip()
        self._config.setdefault("max_retries", DEFAULT_TRANSLATE_CONFIG["max_retries"])

        self._app.save_config()
        log.info("翻译配置已更新。")
        self.accept()
