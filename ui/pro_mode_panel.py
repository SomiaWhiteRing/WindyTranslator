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
        self.write_log_var = tk.BooleanVar(value=pro_settings.get('write_log_rename', True))
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

        # --- 1. 重写文件名 ---
        row_frame_1 = create_row(self, "1. 重写文件名", "非ASCII文件名转Unicode")
        self.rename_button = ttk.Button(row_frame_1, text="执行", width=button_width,
                                       command=lambda: self.app.start_task('rename'))
        self.rename_button.pack(side=tk.RIGHT, padx=padx_val)
        self.log_checkbutton = ttk.Checkbutton(row_frame_1, text="输出日志", variable=self.write_log_var,
                                               command=self._save_settings)
        self.log_checkbutton.pack(side=tk.RIGHT, padx=padx_val)
        all_controls_list.extend([self.rename_button, self.log_checkbutton])
        row_idx += 1

        # --- 2. 导出文本 ---
        row_frame_2 = create_row(self, "2. 导出文本", "导出文本到 StringScripts")
        self.export_button = ttk.Button(row_frame_2, text="执行", width=button_width,
                                       command=lambda: self.app.start_task('export'))
        self.export_button.pack(side=tk.RIGHT, padx=padx_val)
        # 编码控件组合 (也使用 pack)
        encoding_frame_export = ttk.Frame(row_frame_2)
        encoding_frame_export.pack(side=tk.RIGHT, padx=padx_val)
        self.export_encoding_combo = ttk.Combobox(encoding_frame_export, textvariable=self.export_encoding_var,
                                             values=self.encoding_display_values, state="readonly", width=config_control_width - 2)
        ttk.Label(encoding_frame_export, text="编码:").pack(side=tk.LEFT, padx=(0, 2))
        self.export_encoding_combo.pack(side=tk.LEFT)
        self.export_encoding_combo.bind("<<ComboboxSelected>>", self._on_encoding_change)
        self._set_combobox_value(self.export_encoding_combo, self.export_encoding_var.get())
        all_controls_list.extend([self.export_button, self.export_encoding_combo])
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
        row_frame_5 = create_row(self, "5. 翻译JSON文件", "DeepSeek API 翻译 JSON")
        self.translate_button = ttk.Button(row_frame_5, text="执行", width=button_width,
                                          command=lambda: self.app.start_task('translate'))
        self.translate_button.pack(side=tk.RIGHT, padx=padx_val)
        self.deepseek_config_button = ttk.Button(row_frame_5, text="配置", width=config_button_width,
                                              command=lambda: self.app.start_task('configure_deepseek'))
        self.deepseek_config_button.pack(side=tk.RIGHT, padx=padx_val)
        self.fix_fallback_button = ttk.Button(row_frame_5, text="修正回退", width=button_width + 2,
                                              command=lambda: self.app.start_task('fix_fallback'),
                                              state=tk.DISABLED) # <--- 初始禁用
        self.fix_fallback_button.pack(side=tk.RIGHT, padx=padx_val) # <--- 添加按钮到布局
        all_controls_list.extend([self.translate_button, self.deepseek_config_button])
        row_idx += 1

        # --- 6. 释放JSON文件 ---
        row_frame_6 = create_row(self, "6. 释放JSON文件", "翻译后 JSON 释放到 StringScripts")
        self.release_json_button = ttk.Button(row_frame_6, text="执行", width=button_width,
                                             command=lambda: self.app.start_task('release_json'))
        self.release_json_button.pack(side=tk.RIGHT, padx=padx_val)
        all_controls_list.append(self.release_json_button)
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

    # --- 新增: 更新修正回退按钮状态的方法 ---
    def update_fix_fallback_button_state(self, enabled):
        """根据传入的状态更新 '修正回退' 按钮的可用性。"""
        new_state = tk.NORMAL if enabled else tk.DISABLED
        if hasattr(self, 'fix_fallback_button') and self.fix_fallback_button.winfo_exists():
            try:
                self.fix_fallback_button.config(state=new_state)
            except tk.TclError:
                # 忽略控件可能已销毁的错误
                pass

    # ... (update_rtp_button_text, _set_combobox_value, _on_encoding_change, _save_settings 方法保持不变, 但注意 _save_settings 中获取 Combobox 值的方式可能需要调整，因为现在是通过 self 实例属性访问) ...
    def update_rtp_button_text(self):
        """根据当前配置更新 RTP 选择按钮的文本。由 App 层调用。"""
        pro_settings = self.config.get('pro_mode_settings', {})
        rtp_opts = pro_settings.get('rtp_options', {})
        selected_rtps = [name for name, selected in rtp_opts.items() if selected]

        if not selected_rtps:
            self.rtp_button_text.set("RTP选择: 无")
        elif len(selected_rtps) == 1:
            name_map = {'2000': '2000', '2000en': '2000en', '2003': '2003', '2003steam': '2003steam'}
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
            'write_log_rename': self.write_log_var.get(),
        }
        self.app.save_pro_mode_settings(settings_to_save)