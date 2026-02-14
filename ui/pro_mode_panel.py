# ui/pro_mode_panel.py
"""专业模式面板 — 分步操作的完整翻译流程界面。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QComboBox,
)

if TYPE_CHECKING:
    from app import RPGTranslatorApp


# 编码选项：(显示名, 代码页)
ENCODING_OPTIONS: list[tuple[str, str]] = [
    ("日语 (Shift-JIS)", "932"),
    ("中文简体 (GBK)", "936"),
    ("中文繁体 (Big5)", "950"),
    ("韩语 (EUC-KR)", "949"),
    ("泰语", "874"),
    ("拉丁语系 (西欧)", "1252"),
    ("东欧", "1250"),
    ("西里尔字母", "1251"),
]


def _encoding_display_values() -> list[str]:
    return [f"{name} - {code}" for name, code in ENCODING_OPTIONS]


def _code_from_display(display: str) -> str:
    """从 '日语 (Shift-JIS) - 932' 提取 '932'。"""
    if " - " in display:
        return display.rsplit(" - ", 1)[-1]
    return "932"


def _display_from_code(code: str) -> str:
    """根据编码代码找到对应的显示文本。"""
    for name, c in ENCODING_OPTIONS:
        if c == code:
            return f"{name} - {c}"
    return _encoding_display_values()[0]


class _StepRow(QWidget):
    """单行步骤控件：标题 + 描述 + 右侧按钮/下拉框。"""

    def __init__(self, title: str, description: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(5, 3, 5, 3)

        title_label = QLabel(title)
        title_label.setFixedWidth(110)
        self._layout.addWidget(title_label)

        desc_label = QLabel(description)
        self._layout.addWidget(desc_label)

        self._layout.addStretch()

    def add_widget(self, widget: QWidget) -> None:
        """在行右侧添加控件（按钮、下拉框等）。"""
        self._layout.addWidget(widget)


class ProModePanel(QWidget):
    """专业模式面板：8 个分步操作行。"""

    def __init__(self, app: RPGTranslatorApp, config: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._app = app
        self._config = config
        self._all_controls: list[QWidget] = []
        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)

        pro_settings = self._config.get('pro_mode_settings', {})

        # --- 0. 初始化 ---
        row0 = _StepRow("0. 初始化", "复制EasyRPG/RTP并转换编码")
        self.rtp_btn = QPushButton()
        self.rtp_btn.setFixedWidth(160)
        self.rtp_btn.clicked.connect(lambda: self._app.start_task('select_rtp'))
        row0.add_widget(self.rtp_btn)
        self.init_btn = QPushButton("执行")
        self.init_btn.setFixedWidth(70)
        self.init_btn.clicked.connect(lambda: self._app.start_task('initialize'))
        row0.add_widget(self.init_btn)
        layout.addWidget(row0)
        self._all_controls.extend([self.init_btn, self.rtp_btn])

        # --- 1. 导出文本 ---
        row1 = _StepRow("1. 导出文本", "导出文本到 StringScripts")
        enc_label_exp = QLabel("编码:")
        row1.add_widget(enc_label_exp)
        self.export_encoding_combo = QComboBox()
        self.export_encoding_combo.addItems(_encoding_display_values())
        self.export_encoding_combo.setCurrentText(
            _display_from_code(pro_settings.get('export_encoding', '932'))
        )
        self.export_encoding_combo.setFixedWidth(180)
        self.export_encoding_combo.currentTextChanged.connect(self._save_settings)
        row1.add_widget(self.export_encoding_combo)
        self.export_btn = QPushButton("执行")
        self.export_btn.setFixedWidth(70)
        self.export_btn.clicked.connect(lambda: self._app.start_task('export'))
        row1.add_widget(self.export_btn)
        layout.addWidget(row1)
        self._all_controls.extend([self.export_btn, self.export_encoding_combo])

        # --- 2. 重写文件名 ---
        row2 = _StepRow("2. 重写文件名", "非ASCII文件名转Unicode")
        self.rename_btn = QPushButton("执行")
        self.rename_btn.setFixedWidth(70)
        self.rename_btn.clicked.connect(lambda: self._app.start_task('rename'))
        row2.add_widget(self.rename_btn)
        layout.addWidget(row2)
        self._all_controls.append(self.rename_btn)

        # --- 3. 制作JSON文件 ---
        row3 = _StepRow("3. 制作JSON文件", "StringScripts 文本压缩为 JSON")
        self.create_json_btn = QPushButton("执行")
        self.create_json_btn.setFixedWidth(70)
        self.create_json_btn.clicked.connect(lambda: self._app.start_task('create_json'))
        row3.add_widget(self.create_json_btn)
        layout.addWidget(row3)
        self._all_controls.append(self.create_json_btn)

        # --- 4. 生成世界观字典 ---
        row4 = _StepRow("4. 生成世界观字典", "Gemini API 从 JSON 生成字典")
        self.edit_dict_btn = QPushButton("编辑字典")
        self.edit_dict_btn.setFixedWidth(90)
        self.edit_dict_btn.clicked.connect(lambda: self._app.start_task('edit_dictionary'))
        row4.add_widget(self.edit_dict_btn)
        self.gemini_config_btn = QPushButton("配置")
        self.gemini_config_btn.setFixedWidth(60)
        self.gemini_config_btn.clicked.connect(lambda: self._app.start_task('configure_gemini'))
        row4.add_widget(self.gemini_config_btn)
        self.gen_dict_btn = QPushButton("执行")
        self.gen_dict_btn.setFixedWidth(70)
        self.gen_dict_btn.clicked.connect(lambda: self._app.start_task('generate_dictionary'))
        row4.add_widget(self.gen_dict_btn)
        layout.addWidget(row4)
        self._all_controls.extend([self.gen_dict_btn, self.gemini_config_btn, self.edit_dict_btn])

        # --- 5. 翻译JSON文件 ---
        row5 = _StepRow("5. 翻译JSON文件", "OpenAI兼容 API 翻译 JSON")
        self.fix_fallback_btn = QPushButton("修正回退")
        self.fix_fallback_btn.setFixedWidth(90)
        self.fix_fallback_btn.setEnabled(False)
        self.fix_fallback_btn.clicked.connect(lambda: self._app.start_task('fix_fallback'))
        row5.add_widget(self.fix_fallback_btn)
        self.deepseek_config_btn = QPushButton("配置")
        self.deepseek_config_btn.setFixedWidth(60)
        self.deepseek_config_btn.clicked.connect(lambda: self._app.start_task('configure_deepseek'))
        row5.add_widget(self.deepseek_config_btn)
        self.translate_btn = QPushButton("执行")
        self.translate_btn.setFixedWidth(70)
        self.translate_btn.clicked.connect(lambda: self._app.start_task('translate'))
        row5.add_widget(self.translate_btn)
        layout.addWidget(row5)
        self._all_controls.extend([self.translate_btn, self.deepseek_config_btn])

        # --- 6. 释放JSON文件 ---
        row6 = _StepRow("6. 释放JSON文件", "翻译后 JSON 释放到 StringScripts")
        self.release_json_btn = QPushButton("执行")
        self.release_json_btn.setFixedWidth(70)
        self.release_json_btn.clicked.connect(lambda: self._app.start_task('release_json'))
        row6.add_widget(self.release_json_btn)
        layout.addWidget(row6)
        self._all_controls.append(self.release_json_btn)

        # --- 7. 导入文本 ---
        row7 = _StepRow("7. 导入文本", "StringScripts 文本导入游戏")
        enc_label_imp = QLabel("编码:")
        row7.add_widget(enc_label_imp)
        self.import_encoding_combo = QComboBox()
        self.import_encoding_combo.addItems(_encoding_display_values())
        self.import_encoding_combo.setCurrentText(
            _display_from_code(pro_settings.get('import_encoding', '936'))
        )
        self.import_encoding_combo.setFixedWidth(180)
        self.import_encoding_combo.currentTextChanged.connect(self._save_settings)
        row7.add_widget(self.import_encoding_combo)
        self.import_btn = QPushButton("执行")
        self.import_btn.setFixedWidth(70)
        self.import_btn.clicked.connect(lambda: self._app.start_task('import'))
        row7.add_widget(self.import_btn)
        layout.addWidget(row7)
        self._all_controls.extend([self.import_btn, self.import_encoding_combo])

        layout.addStretch()

        # 初始化 RTP 按钮文本
        self.update_rtp_button_text()

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    def get_controls(self) -> list[QWidget]:
        """返回需要在任务运行时禁用的控件列表。"""
        return list(self._all_controls)

    def update_rtp_button_text(self) -> None:
        """根据当前配置更新 RTP 选择按钮的文本。"""
        pro_settings = self._config.get('pro_mode_settings', {})
        rtp_opts = pro_settings.get('rtp_options', {})
        selected = [name for name, sel in rtp_opts.items() if sel]

        if not selected:
            self.rtp_btn.setText("RTP选择: 无")
        elif len(selected) == 1:
            self.rtp_btn.setText(f"RTP选择: {selected[0]}")
        else:
            self.rtp_btn.setText(f"RTP选择: {len(selected)}个")

    def update_fix_fallback_button_state(self, enabled: bool) -> None:
        """更新修正回退按钮的可用性。"""
        self.fix_fallback_btn.setEnabled(enabled)

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _save_settings(self) -> None:
        """将当前面板上的编码设置保存到 App 配置中。"""
        settings = {
            'export_encoding': _code_from_display(self.export_encoding_combo.currentText()),
            'import_encoding': _code_from_display(self.import_encoding_combo.currentText()),
            'rewrite_rtp_fix': self._config.get('pro_mode_settings', {}).get('rewrite_rtp_fix', True),
        }
        self._app.save_pro_mode_settings(settings)
