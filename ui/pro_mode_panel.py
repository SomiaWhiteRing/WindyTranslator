# ui/pro_mode_panel.py
import tkinter as tk
from tkinter import ttk

class ProModePanel(ttk.Frame):
    def __init__(self, parent, app_controller, config):
        super().__init__(parent, padding="5")
        self.app = app_controller
        self.config = config
        self.pack(fill=tk.BOTH, expand=True)

        # **** Grid 配置: 主面板只有一列，让行容器可以扩展 ****
        self.columnconfigure(0, weight=1)

        pro_settings = self.config.setdefault('pro_mode_settings', {})

        # --- 控件变量 (不变) ---
        self.export_encoding_var = tk.StringVar(value=pro_settings.get('export_encoding', '932'))
        self.import_encoding_var = tk.StringVar(value=pro_settings.get('import_encoding', '936'))
        self.rtp_fix_check = tk.BooleanVar(value=pro_settings.get('rewrite_rtp_fix', True))
        self.auto_import_after_release_check = tk.BooleanVar(value=pro_settings.get('auto_import_after_release', False))
        self.export_scope = dict(pro_settings.get('export_scope', {}))
        self.rtp_button_text = tk.StringVar()

        # --- 编码选项列表 (不变) ---
        self.encoding_options = [
            ("日语 (Shift-JIS)", "932"), ("中文简体 (GBK)", "936"), ("中文繁体 (Big5)", "950"),
            ("韩语 (EUC-KR)", "949"), ("泰语", "874"), ("拉丁语系 (西欧)", "1252"),
            ("东欧", "1250"), ("西里尔字母", "1251")
        ]
        self.encoding_display_values = [f"{name} - {code}" for name, code in self.encoding_options]

        # --- 创建控件 (行容器 + pack 布局) ---
        all_controls_list = [] # 用于收集所有控件引用
        row_idx = 0
        pady_val = 3
        padx_val = 5
        button_width = 8
        config_button_width = 6
        config_control_width = 20

        # --- Helper function to create a row ---
        def create_row(parent_frame, description_title, description_text=None):
            row_frame = ttk.Frame(parent_frame)
            # **** 让行容器水平填充 ****
            row_frame.grid(row=row_idx, column=0, sticky="ew", pady=pady_val)

            title_label = ttk.Label(row_frame, text=description_title, width=16)
            title_label.pack(side=tk.LEFT, padx=(padx_val, 0))

            desc_label = ttk.Label(row_frame, text=description_text)
            desc_label.pack(side=tk.LEFT, padx=(padx_val, 0))

            # 返回行容器，用于向右侧添加按钮
            return row_frame

        # --- 0. 初始化 ---
        row_frame_0 = create_row(self, "0. 初始化", "复制EasyRPG/RTP并转换编码")
        # **** 使用 pack 从右向左添加按钮 ****
        self.init_button = ttk.Button(row_frame_0, text="执行", width=button_width,
                                     command=lambda: self.app.start_task('initialize'))
        self.init_button.pack(side=tk.RIGHT, padx=padx_val)
        self.rtp_button = ttk.Button(row_frame_0, textvariable=self.rtp_button_text, width=config_control_width,
                                      command=lambda: self.app.start_task('select_rtp'))
        self.rtp_button.pack(side=tk.RIGHT, padx=padx_val)
        self.update_rtp_button_text()
        all_controls_list.extend([self.init_button, self.rtp_button])
        row_idx += 1

        # --- 1. 导出文本 ---
        row_frame_1 = create_row(self, "1. 导出文本", "导出文本到 StringScripts")
        self.export_button = ttk.Button(row_frame_1, text="执行", width=button_width,
                                       command=lambda: self.app.start_task('export'))
        self.export_button.pack(side=tk.RIGHT, padx=padx_val)
        self.export_config_button = ttk.Button(
            row_frame_1, text="配置", width=config_button_width,
            command=self._show_export_settings,
        )
        self.export_config_button.pack(side=tk.RIGHT, padx=padx_val)
        all_controls_list.extend([self.export_button, self.export_config_button])
        row_idx += 1

        # --- 2. 重写文件名 ---
        row_frame_2 = create_row(self, "2. 重写文件名", "非ASCII文件名转Unicode")
        self.rename_button = ttk.Button(row_frame_2, text="执行", width=button_width,
                                       command=lambda: self.app.start_task('rename'))
        self.rename_button.pack(side=tk.RIGHT, padx=padx_val)
        # self.log_checkbutton = ttk.Checkbutton(row_frame_1, text="RTP修正", variable=self.rtp_fix_check,
        #                                        command=self._save_settings)
        # self.log_checkbutton.pack(side=tk.RIGHT, padx=padx_val)
        # all_controls_list.extend([self.rename_button, self.log_checkbutton])
        all_controls_list.extend([self.rename_button])
        row_idx += 1


        # --- 3. 制作JSON文件 ---
        row_frame_3 = create_row(self, "3. 制作JSON文件", "StringScripts 文本压缩为 JSON")
        self.create_json_button = ttk.Button(row_frame_3, text="执行", width=button_width,
                                            command=lambda: self.app.start_task('create_json'))
        self.create_json_button.pack(side=tk.RIGHT, padx=padx_val)
        all_controls_list.append(self.create_json_button)
        row_idx += 1

        # --- 4. 生成世界观字典 ---
        row_frame_4 = create_row(self, "4. 生成世界观字典", "Gemini API 从 JSON 生成字典")
        self.gen_dict_button = ttk.Button(row_frame_4, text="执行", width=button_width,
                                         command=lambda: self.app.start_task('generate_dictionary'))
        self.gen_dict_button.pack(side=tk.RIGHT, padx=padx_val)
        self.gemini_config_button = ttk.Button(row_frame_4, text="配置", width=config_button_width,
                                            command=lambda: self.app.start_task('configure_gemini'))
        self.gemini_config_button.pack(side=tk.RIGHT, padx=padx_val)
        self.edit_dict_button = ttk.Button(row_frame_4, text="编辑字典", width=button_width + 2,
                                          command=lambda: self.app.start_task('edit_dictionary'))
        self.edit_dict_button.pack(side=tk.RIGHT, padx=padx_val)
        all_controls_list.extend([self.gen_dict_button, self.gemini_config_button, self.edit_dict_button])
        row_idx += 1

        # --- 5. 翻译JSON文件 ---
        row_frame_5 = create_row(self, "5. 翻译JSON文件", "OpenAI兼容 API 翻译 JSON")
        self.translate_button = ttk.Button(row_frame_5, text="执行", width=button_width,
                                          command=lambda: self.app.start_task('translate'))
        self.translate_button.pack(side=tk.RIGHT, padx=padx_val)
        self.deepseek_config_button = ttk.Button(row_frame_5, text="配置", width=config_button_width,
                                              command=lambda: self.app.start_task('configure_deepseek'))
        self.deepseek_config_button.pack(side=tk.RIGHT, padx=padx_val)
        all_controls_list.extend([self.translate_button, self.deepseek_config_button])
        row_idx += 1

        # --- 6. 释放JSON文件 ---
        row_frame_6 = create_row(self, "6. 释放JSON文件", "翻译后 JSON 释放到 StringScripts")
        self.release_json_button = ttk.Button(row_frame_6, text="执行", width=button_width,
                                             command=lambda: self.app.start_task('release_json'))
        self.release_json_button.pack(side=tk.RIGHT, padx=padx_val)
        self.auto_import_after_release_checkbox = ttk.Checkbutton(
            row_frame_6,
            text="释放后自动导入",
            variable=self.auto_import_after_release_check,
            command=self._save_settings,
        )
        self.auto_import_after_release_checkbox.pack(side=tk.RIGHT, padx=padx_val)
        all_controls_list.extend([self.release_json_button, self.auto_import_after_release_checkbox])
        row_idx += 1

        # --- 7. 导入文本 ---
        row_frame_7 = create_row(self, "7. 导入文本", "StringScripts 文本导入游戏")
        self.import_button = ttk.Button(row_frame_7, text="执行", width=button_width,
                                       command=lambda: self.app.start_task('import'))
        self.import_button.pack(side=tk.RIGHT, padx=padx_val)
        # 编码控件组合
        encoding_frame_import = ttk.Frame(row_frame_7)
        encoding_frame_import.pack(side=tk.RIGHT, padx=padx_val)
        self.import_encoding_combo = ttk.Combobox(encoding_frame_import, textvariable=self.import_encoding_var,
                                             values=self.encoding_display_values, state="readonly", width=config_control_width - 2)
        ttk.Label(encoding_frame_import, text="编码:").pack(side=tk.LEFT, padx=(0, 2))
        self.import_encoding_combo.pack(side=tk.LEFT)
        self.import_encoding_combo.bind("<<ComboboxSelected>>", self._on_encoding_change)
        self._set_combobox_value(self.import_encoding_combo, self.import_encoding_var.get())
        all_controls_list.extend([self.import_button, self.import_encoding_combo])
        row_idx += 1

        # --- 保存所有按钮引用 ---
        self.all_controls = all_controls_list # 使用收集到的列表


    def get_controls(self):
        """返回此面板上的所有可交互控件列表。"""
        return self.all_controls

    # ... (update_rtp_button_text, _set_combobox_value, _on_encoding_change, _save_settings 方法保持不变, 但注意 _save_settings 中获取 Combobox 值的方式可能需要调整，因为现在是通过 self 实例属性访问) ...
    def update_rtp_button_text(self):
        """根据当前配置更新 RTP 选择按钮的文本。由 App 层调用。"""
        pro_settings = self.config.get('pro_mode_settings', {})
        rtp_opts = pro_settings.get('rtp_options', {})
        selected_rtps = [name for name, selected in rtp_opts.items() if selected]

        if not selected_rtps:
            self.rtp_button_text.set("RTP选择: 无")
        elif len(selected_rtps) == 1:
            name_map = {'2000': '2000', '2000en': '2000en', '2003': '2003', '2003steam': '2003steam', '2003zh_tw': '2003繁中'}
            display_name = name_map.get(selected_rtps[0], selected_rtps[0])
            self.rtp_button_text.set(f"RTP选择: {display_name}")
        else:
            self.rtp_button_text.set(f"RTP选择: {len(selected_rtps)}个")

    def _set_combobox_value(self, combobox, code_value):
        """根据编码代码设置 Combobox 的显示值。"""
        for display_value in self.encoding_display_values:
            if display_value.endswith(f" - {code_value}"):
                combobox.set(display_value)
                return
        combobox.set(self.encoding_display_values[0]) # 默认选第一个

    def _on_encoding_change(self, event=None):
        """当编码下拉框选择变化时保存设置。"""
        self._save_settings()

    def _show_export_settings(self):
        dialog = tk.Toplevel(self)
        dialog.title("导出设置")
        dialog.transient(self.winfo_toplevel())
        dialog.grab_set()
        dialog.resizable(False, False)
        style = ttk.Style(dialog)
        background = style.lookup("TFrame", "background") or dialog.cget("background")
        dialog.configure(background=background)
        content = tk.Frame(dialog, background=background)
        content.pack(fill=tk.BOTH, expand=True)

        encoding_frame = ttk.LabelFrame(content, text="文本编码", padding=10)
        encoding_frame.pack(fill=tk.X, padx=12, pady=(12, 6))
        export_code = self.export_encoding_var.get().split(' - ')[-1]
        encoding_var = tk.StringVar(value=next(
            value for value in self.encoding_display_values if value.endswith(f" - {export_code}")
        ))
        ttk.Combobox(
            encoding_frame, textvariable=encoding_var,
            values=self.encoding_display_values, state="readonly", width=28,
        ).pack(fill=tk.X)

        scope_frame = ttk.LabelFrame(content, text="导出范围", padding=10)
        scope_frame.pack(fill=tk.X, padx=12, pady=6)
        scope_background = style.lookup("TLabelframe", "background") or background
        scope_vars = {
            key: tk.BooleanVar(value=self.export_scope.get(key, default))
            for key, default in (
                ("game_text", True),
                ("game_title", True),
                ("map_names", False),
                ("map_event_names", False),
                ("switch_names", False),
                ("variable_names", False),
                ("common_event_names", False),
                ("troop_names", False),
            )
        }
        game_labels = (
            ("game_text", "游戏正文"),
            ("game_title", "游戏标题"),
        )
        engineering_labels = (
            ("map_names", "地图名称"),
            ("map_event_names", "地图事件名称"),
            ("switch_names", "开关名称"),
            ("variable_names", "变量名称"),
            ("common_event_names", "公共事件名称"),
            ("troop_names", "敌群名称"),
        )
        ttk.Label(scope_frame, text="游戏文本").grid(
            row=0, column=0, columnspan=2, sticky=tk.W, padx=6, pady=(0, 3)
        )
        for index, (key, label) in enumerate(game_labels):
            check = ttk.Checkbutton(scope_frame, text=label, variable=scope_vars[key])
            check.grid(row=1, column=index, sticky=tk.W, padx=6, pady=3)
            if key == "game_text":
                check.state(["disabled"])

        engineering_header = tk.Frame(scope_frame, background=scope_background)
        engineering_header.grid(row=2, column=0, columnspan=2, sticky=tk.W, padx=6, pady=(8, 3))
        tk.Label(engineering_header, text="工程文本", background=scope_background).pack(side=tk.LEFT)
        help_icon = tk.Canvas(
            engineering_header, width=16, height=16, highlightthickness=0,
            background=scope_background, cursor="hand2",
        )
        help_icon.pack(side=tk.LEFT, padx=(4, 0))
        help_icon.create_oval(1, 1, 15, 15, outline="#606060")
        help_icon.create_text(8, 8, text="?", fill="#404040")

        tooltip = None

        def show_tooltip(_event):
            nonlocal tooltip
            if tooltip is not None:
                return
            tooltip = tk.Toplevel(dialog)
            tooltip.overrideredirect(True)
            tooltip.configure(background="#ffffe1")
            tk.Label(
                tooltip,
                text="此类别的文本正常情况下仅会在RPG Maker 编辑器中出现，\n以游玩为目的时通常无需翻译。",
                background="#ffffe1", relief=tk.SOLID, borderwidth=1, padx=6, pady=4, justify=tk.LEFT, anchor=tk.W,
            ).pack()
            tooltip.geometry(f"+{help_icon.winfo_rootx() + help_icon.winfo_width() + 4}+{help_icon.winfo_rooty()}")

        def hide_tooltip(_event):
            nonlocal tooltip
            if tooltip is not None:
                tooltip.destroy()
                tooltip = None

        help_icon.bind("<Enter>", show_tooltip)
        help_icon.bind("<Leave>", hide_tooltip)

        for index, (key, label) in enumerate(engineering_labels):
            check = ttk.Checkbutton(scope_frame, text=label, variable=scope_vars[key])
            check.grid(row=3 + index // 2, column=index % 2, sticky=tk.W, padx=6, pady=3)

        button_frame = tk.Frame(content, background=background)
        button_frame.pack(fill=tk.X, padx=12, pady=(6, 12))

        def save():
            self.export_encoding_var.set(encoding_var.get())
            self.export_scope = {key: var.get() for key, var in scope_vars.items()}
            self.export_scope["game_text"] = True
            self._save_settings()
            dialog.destroy()

        ttk.Button(button_frame, text="确定", command=save).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(button_frame, text="取消", command=dialog.destroy).pack(side=tk.RIGHT)

    def _save_settings(self):
        """将当前面板上的设置保存到 App 配置中。"""
        # 现在可以直接通过实例属性访问 Combobox
        export_display = self.export_encoding_var.get()
        import_display = self.import_encoding_var.get()

        export_code = export_display.split(' - ')[-1] if ' - ' in export_display else '932'
        import_code = import_display.split(' - ')[-1] if ' - ' in import_display else '936'

        settings_to_save = {
            'export_encoding': export_code,
            'import_encoding': import_code,
            'export_scope': dict(self.export_scope),
            'rewrite_rtp_fix': self.rtp_fix_check.get(),
            'auto_import_after_release': self.auto_import_after_release_check.get(),
        }
        self.app.save_pro_mode_settings(settings_to_save)
