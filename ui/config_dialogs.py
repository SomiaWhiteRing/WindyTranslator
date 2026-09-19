# ui/config_dialogs.py
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import threading
import logging

# 导入 API 客户端模块用于连接测试
from core.api_clients import gemini, deepseek
# 导入默认配置以获取默认 Prompt 值
from core.config import DEFAULT_WORLD_DICT_CONFIG, DEFAULT_TRANSLATE_CONFIG
from ui.model_selector import ModelSelector

log = logging.getLogger(__name__)

PROVIDER_GEMINI = 'gemini'
PROVIDER_OPENAI = 'openai'

# --- 世界观字典配置窗口 ---

class WorldDictConfigWindow(tk.Toplevel):
    """世界观字典配置对话框。"""

    def __init__(self, parent, app_controller, world_dict_config):
        """初始化配置窗口并绑定 UI 控件。"""
        super().__init__(parent)
        self.app = app_controller
        self.config = world_dict_config  # 直接引用配置字典

        self.initializing = True
        self.connection_tested_ok = False

        self.provider_display_map = {
            PROVIDER_GEMINI: "Google Gemini 原生",
            PROVIDER_OPENAI: "OpenAI 兼容端点",
        }
        self.provider_value_map = {label: value for value, label in self.provider_display_map.items()}

        provider_value = (self.config.get("provider") or PROVIDER_GEMINI).lower()
        if provider_value not in self.provider_display_map:
            provider_value = PROVIDER_GEMINI

        self.provider_var = tk.StringVar(value=provider_value)
        self.provider_display_var = tk.StringVar(value=self.provider_display_map[provider_value])
        self.api_key_var = tk.StringVar(value=self.config.get("api_key", ""))
        self.api_url_var = tk.StringVar(value=self.config.get("api_url", ""))
        default_model = DEFAULT_WORLD_DICT_CONFIG["model"] if provider_value == PROVIDER_GEMINI else ""
        self.model_var = tk.StringVar(value=self.config.get("model", default_model))
        self.openai_temp_var = tk.StringVar(value=str(self.config.get("openai_temperature", DEFAULT_WORLD_DICT_CONFIG["openai_temperature"])))
        max_tokens_cfg = self.config.get("openai_max_tokens", DEFAULT_WORLD_DICT_CONFIG["openai_max_tokens"])
        self.openai_max_tokens_var = tk.StringVar(value="" if max_tokens_cfg in (None, "") else str(max_tokens_cfg))
        self.enable_base_dict_var = tk.BooleanVar(value=self.config.get("enable_base_dictionary", True))
        self.show_key_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="请输入配置并测试连接")

        self.title("世界观字典配置")
        self.geometry("820x560")
        self.transient(parent)
        self.grab_set()

        frame = ttk.Frame(self, padding="10")
        frame.pack(fill=tk.BOTH, expand=True)
        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(2, weight=1)

        row_idx = 0

        ttk.Label(frame, text="模型供应商:").grid(row=row_idx, column=0, padx=5, pady=5, sticky="w")
        self.provider_combo = ttk.Combobox(
            frame,
            textvariable=self.provider_display_var,
            values=list(self.provider_display_map.values()),
            state='readonly',
            width=40,
        )
        self.provider_combo.grid(row=row_idx, column=1, columnspan=2, padx=5, pady=5, sticky="ew")
        self.provider_combo.bind("<<ComboboxSelected>>", self._on_provider_combo_selected)
        row_idx += 1

        self.api_key_label = ttk.Label(frame, text="API Key:")
        self.api_key_label.grid(row=row_idx, column=0, padx=5, pady=5, sticky="w")
        key_frame = ttk.Frame(frame)
        key_frame.grid(row=row_idx, column=1, columnspan=2, padx=5, pady=5, sticky="ew")
        key_frame.columnconfigure(0, weight=1)
        self.api_key_entry = ttk.Entry(key_frame, textvariable=self.api_key_var, width=50, show="*")
        self.api_key_entry.grid(row=0, column=0, sticky="ew")
        self.key_toggle_button = ttk.Checkbutton(key_frame, text="显示", variable=self.show_key_var, command=self._toggle_key_visibility)
        self.key_toggle_button.grid(row=0, column=1, padx=(5, 0))
        row_idx += 1

        self.api_url_row = row_idx
        self.api_url_label = ttk.Label(frame, text="API 基础地址:")
        self.api_url_label.grid(row=row_idx, column=0, padx=5, pady=5, sticky="w")
        self.api_url_entry = ttk.Entry(frame, textvariable=self.api_url_var, width=50)
        self.api_url_entry.grid(row=row_idx, column=1, columnspan=2, padx=5, pady=5, sticky="ew")
        row_idx += 1

        ttk.Label(frame, text="模型名称:").grid(row=row_idx, column=0, padx=5, pady=5, sticky="w")
        model_values = [
            "gemini-2.5-pro-preview-05-06",
            "gemini-1.5-pro-latest",
            "gemini-1.5-flash-latest",
            "gemini-pro",
        ]
        self.model_selector = ModelSelector(
            frame, self.api_url_var, self.api_key_var, self.model_var,
            status_callback=self._set_status,
            provider_var=self.provider_var, native_models=model_values,
        )
        self.model_selector.grid(row=row_idx, column=1, columnspan=2, padx=5, pady=5, sticky="ew")
        self.model_combobox = self.model_selector.combobox
        row_idx += 1

        self.openai_params_row = row_idx
        self.openai_params_frame = ttk.Frame(frame)
        self.openai_params_frame.grid(row=row_idx, column=0, columnspan=3, padx=5, pady=5, sticky="ew")
        self.openai_params_frame.columnconfigure(1, weight=1)
        self.openai_params_frame.columnconfigure(3, weight=1)
        ttk.Label(self.openai_params_frame, text="温度 (0-2):").grid(row=0, column=0, padx=5, pady=5, sticky="w")
        self.openai_temp_entry = ttk.Entry(self.openai_params_frame, textvariable=self.openai_temp_var, width=10)
        self.openai_temp_entry.grid(row=0, column=1, padx=5, pady=5, sticky="w")
        ttk.Label(self.openai_params_frame, text="最大 tokens:").grid(row=0, column=2, padx=5, pady=5, sticky="w")
        self.openai_max_tokens_entry = ttk.Entry(self.openai_params_frame, textvariable=self.openai_max_tokens_var, width=12)
        self.openai_max_tokens_entry.grid(row=0, column=3, padx=5, pady=5, sticky="w")
        ttk.Label(self.openai_params_frame, text="(留空使用服务端默认)").grid(row=0, column=4, padx=5, pady=5, sticky="w")
        row_idx += 1

        prompt_area_frame = ttk.Frame(frame)
        prompt_area_frame.grid(row=row_idx, column=0, columnspan=3, padx=5, pady=5, sticky="nsew")
        prompt_area_frame.columnconfigure(0, weight=1)
        prompt_area_frame.columnconfigure(1, weight=1)
        prompt_area_frame.rowconfigure(0, weight=1)
        frame.rowconfigure(row_idx, weight=1)
        row_idx += 1

        char_prompt_frame = ttk.LabelFrame(prompt_area_frame, text="人物提取 Prompt", padding="5")
        char_prompt_frame.grid(row=0, column=0, padx=(0, 5), pady=5, sticky="nsew")
        char_prompt_frame.rowconfigure(0, weight=1)
        char_prompt_frame.columnconfigure(0, weight=1)
        self.char_prompt_text = scrolledtext.ScrolledText(char_prompt_frame, wrap=tk.WORD, height=15)
        self.char_prompt_text.grid(row=0, column=0, sticky="nsew")
        self.char_prompt_text.insert(tk.END, self.config.get("character_prompt_template", DEFAULT_WORLD_DICT_CONFIG["character_prompt_template"]))
        self.char_prompt_text.edit_modified(False)

        entity_prompt_frame = ttk.LabelFrame(prompt_area_frame, text="事物提取 Prompt", padding="5")
        entity_prompt_frame.grid(row=0, column=1, padx=(5, 0), pady=5, sticky="nsew")
        entity_prompt_frame.rowconfigure(0, weight=1)
        entity_prompt_frame.columnconfigure(0, weight=1)
        self.entity_prompt_text = scrolledtext.ScrolledText(entity_prompt_frame, wrap=tk.WORD, height=15)
        self.entity_prompt_text.grid(row=0, column=0, sticky="nsew")
        self.entity_prompt_text.insert(tk.END, self.config.get("entity_prompt_template", DEFAULT_WORLD_DICT_CONFIG["entity_prompt_template"]))
        self.entity_prompt_text.edit_modified(False)

        self.status_label = ttk.Label(frame, textvariable=self.status_var, foreground="orange")
        self.status_label.grid(row=row_idx, column=0, columnspan=3, padx=5, pady=(10, 0), sticky="ew")
        row_idx += 1

        button_frame = ttk.Frame(frame)
        button_frame.grid(row=row_idx, column=0, columnspan=3, pady=10, sticky="e")

        self.enable_base_dict_checkbutton = ttk.Checkbutton(
            button_frame,
            text="启用基础字典",
            variable=self.enable_base_dict_var,
            command=self._on_config_change,
        )
        self.enable_base_dict_checkbutton.pack(side=tk.LEFT, padx=5)

        self.edit_base_dict_button = ttk.Button(button_frame, text="编辑基础字典", command=self._open_base_dict_editor_modal)
        self.edit_base_dict_button.pack(side=tk.LEFT, padx=5)

        self.test_button = ttk.Button(button_frame, text="测试连接", command=self._test_connection)
        self.test_button.pack(side=tk.LEFT, padx=5)

        self.save_button = ttk.Button(button_frame, text="保存", command=self._save_config)
        self.save_button.pack(side=tk.LEFT, padx=5)

        cancel_button = ttk.Button(button_frame, text="取消", command=self.destroy)
        cancel_button.pack(side=tk.LEFT, padx=5)

        self.provider_var.trace_add("write", self._on_provider_var_changed)
        self.api_key_var.trace_add("write", self._on_config_change)
        self.api_url_var.trace_add("write", self._on_config_change)
        self.model_var.trace_add("write", self._on_config_change)
        self.openai_temp_var.trace_add("write", self._on_config_change)
        self.openai_max_tokens_var.trace_add("write", self._on_config_change)
        self.char_prompt_text.bind("<<Modified>>", self._on_prompt_modified)
        self.entity_prompt_text.bind("<<Modified>>", self._on_prompt_modified)
        self.protocol("WM_DELETE_WINDOW", self.destroy)

        self.controls_to_manage = []  # 占位，稍后填充

        self._sync_provider_display()
        self._update_provider_ui(initial=True)

        minimal_ready = bool(self.api_key_var.get().strip())
        if self.provider_var.get() == PROVIDER_OPENAI:
            minimal_ready = minimal_ready and bool(self.api_url_var.get().strip())
        self.connection_tested_ok = minimal_ready
        if self.connection_tested_ok:
            self._set_status("配置已加载", "green")
        else:
            self._set_status("请输入必要信息并测试连接", "red")

        self.controls_to_manage = [
            self.provider_combo,
            self.api_key_entry,
            self.key_toggle_button,
            self.api_url_entry,
            self.model_combobox,
            self.openai_temp_entry,
            self.openai_max_tokens_entry,
            self.char_prompt_text,
            self.entity_prompt_text,
            self.enable_base_dict_checkbutton,
            self.edit_base_dict_button,
            self.test_button,
            self.save_button,
        ]

        self.initializing = False
        self._update_connection_signature()
        if self.connection_tested_ok:
            self.save_button.config(state=tk.NORMAL)
        else:
            self.save_button.config(state=tk.DISABLED)

    def _sync_provider_display(self):
        provider = self.provider_var.get()
        display_value = self.provider_display_map.get(provider, self.provider_display_map[PROVIDER_GEMINI])
        if self.provider_display_var.get() != display_value:
            self.provider_display_var.set(display_value)

    def _on_provider_combo_selected(self, event=None):
        display = self.provider_display_var.get()
        provider = self.provider_value_map.get(display, PROVIDER_GEMINI)
        if provider != self.provider_var.get():
            self.provider_var.set(provider)

    def _on_provider_var_changed(self, *args):
        self._sync_provider_display()
        self._update_provider_ui()
        self._on_config_change()

    def _update_provider_ui(self, initial=False):
        provider = self.provider_var.get()
        if provider == PROVIDER_GEMINI:
            self.title("世界观字典配置 (Gemini)")
            self.api_key_label.config(text="Gemini API Key:")
            self.api_url_label.grid_remove()
            self.api_url_entry.grid_remove()
            self.openai_params_frame.grid_remove()
        else:
            self.title("世界观字典配置 (OpenAI 兼容)")
            self.api_key_label.config(text="OpenAI API Key:")
            self.api_url_label.grid(row=self.api_url_row, column=0, padx=5, pady=5, sticky="w")
            self.api_url_entry.grid(row=self.api_url_row, column=1, columnspan=2, padx=5, pady=5, sticky="ew")
            self.openai_params_frame.grid(row=self.openai_params_row, column=0, columnspan=3, padx=5, pady=5, sticky="ew")
        if not initial:
            self._set_status("供应商已切换，请重新测试连接", "orange")

    def _open_base_dict_editor_modal(self):
        self._set_controls_enabled(False)
        editor_window = self.app.open_base_dict_editor(parent_for_editor=self)
        if editor_window:
            self.wait_window(editor_window)
        self._set_controls_enabled(True)
        self.focus_set()
        self.grab_set()

    def _set_controls_enabled(self, enabled):
        state = tk.NORMAL if enabled else tk.DISABLED
        for control in self.controls_to_manage:
            if not control or not control.winfo_exists():
                continue
            try:
                if isinstance(control, ttk.Combobox):
                    enabled_state = tk.NORMAL if control is self.model_combobox else 'readonly'
                    control.config(state=enabled_state if enabled else tk.DISABLED)
                elif isinstance(control, (tk.Entry, ttk.Entry, scrolledtext.ScrolledText, tk.Text, ttk.Spinbox)):
                    control.config(state=state)
                elif isinstance(control, (ttk.Button, ttk.Checkbutton)):
                    control.config(state=state)
            except tk.TclError:
                pass

    def _toggle_key_visibility(self):
        if self.show_key_var.get():
            self.api_key_entry.config(show="")
        else:
            self.api_key_entry.config(show="*")

    def _on_prompt_modified(self, event=None):
        if not self.initializing and event:
            widget = event.widget
            if widget.edit_modified():
                widget.edit_modified(False)
                self._on_config_change()

    def _current_connection_signature(self):
        provider = self.provider_var.get()
        api_key = self.api_key_var.get().strip()
        model = self.model_var.get().strip()
        api_url = self.api_url_var.get().strip() if provider == PROVIDER_OPENAI else ""
        return (provider, api_key, api_url, model)

    def _update_connection_signature(self):
        self._last_connection_signature = self._current_connection_signature()

    def _on_config_change(self, *args):
        if self.initializing:
            return
        current_signature = self._current_connection_signature()
        if getattr(self, '_last_connection_signature', None) != current_signature:
            self.connection_tested_ok = False
            if self.save_button.winfo_exists():
                self.save_button.config(state=tk.DISABLED)
            self._set_status("关键配置已修改，请重新测试连接", "orange")
            self._last_connection_signature = current_signature

    def _set_status(self, message, color):
        if self.winfo_exists():
            self.status_var.set(message)
            self.status_label.config(foreground=color)

    def _test_connection(self):
        provider = self.provider_var.get()
        api_key = self.api_key_var.get().strip()
        model = self.model_var.get().strip()
        api_url = self.api_url_var.get().strip()
        provider_display = self.provider_display_map.get(provider, provider)

        if not api_key:
            messagebox.showerror("错误", f"请输入 {provider_display} Key", parent=self)
            return
        if provider == PROVIDER_OPENAI and not api_url:
            messagebox.showerror("错误", "请输入 OpenAI 兼容 API 的基础地址", parent=self)
            return
        if not model:
            messagebox.showerror("错误", "请选择或输入模型名称", parent=self)
            return

        self.model_selector.suspend_status_updates()
        self._set_status("正在测试连接...", "blue")
        self.test_button.config(state=tk.DISABLED)
        self.save_button.config(state=tk.DISABLED)

        thread = threading.Thread(
            target=self._test_connection_thread,
            args=(provider, api_url, api_key, model),
            daemon=True,
        )
        thread.start()

    def _test_connection_thread(self, provider, api_url, api_key, model):
        provider_display = self.provider_display_map.get(provider, provider)
        try:
            if provider == PROVIDER_GEMINI:
                client = gemini.GeminiClient(api_key)
            else:
                client = deepseek.DeepSeekClient(base_url=api_url, api_key=api_key)
            success, message = client.test_connection(model)
            self.after(0, lambda: self._test_connection_result(success, message))
        except ConnectionError as exc:
            self.after(0, lambda: self._test_connection_result(False, f"客户端初始化失败: {exc}"))
        except Exception as exc:
            log.exception("世界观字典连接测试线程发生意外错误。")
            self.after(0, lambda: self._test_connection_result(False, f"测试时发生意外错误: {exc}"))

    def _test_connection_result(self, success, message):
        if not self.winfo_exists():
            return
        self.test_button.config(state=tk.NORMAL)
        if success:
            self.connection_tested_ok = True
            self._update_connection_signature()
            self._set_status("连接成功!", "green")
            self.save_button.config(state=tk.NORMAL)
            messagebox.showinfo("成功", message, parent=self)
        else:
            self.connection_tested_ok = False
            self._set_status("连接失败", "red")
            self.save_button.config(state=tk.DISABLED)
            messagebox.showerror("连接失败", message, parent=self)

    def _save_config(self):
        if not self.connection_tested_ok:
            messagebox.showwarning("无法保存", "请先成功测试连接后再保存。", parent=self)
            return

        provider = self.provider_var.get()
        api_key = self.api_key_var.get().strip()
        api_url = self.api_url_var.get().strip()
        model = self.model_var.get().strip()

        temp_text = self.openai_temp_var.get().strip()
        if temp_text:
            try:
                openai_temperature = float(temp_text)
            except ValueError:
                messagebox.showerror("错误", "OpenAI 温度必须是数字。", parent=self)
                return
        else:
            openai_temperature = DEFAULT_WORLD_DICT_CONFIG["openai_temperature"]

        max_tokens_text = self.openai_max_tokens_var.get().strip()
        if max_tokens_text:
            try:
                openai_max_tokens = int(max_tokens_text)
            except ValueError:
                messagebox.showerror("错误", "OpenAI 最大 tokens 必须是整数。", parent=self)
                return
        else:
            openai_max_tokens = None

        self.config["provider"] = provider
        self.config["api_key"] = api_key
        self.config["api_url"] = api_url
        self.config["model"] = model
        self.config["openai_temperature"] = openai_temperature
        self.config["openai_max_tokens"] = openai_max_tokens

        self.config["character_prompt_template"] = self.char_prompt_text.get("1.0", tk.END).strip()
        self.config["entity_prompt_template"] = self.entity_prompt_text.get("1.0", tk.END).strip()
        self.config["enable_base_dictionary"] = self.enable_base_dict_var.get()
        self.config["character_dict_filename"] = self.config.get(
            "character_dict_filename",
            DEFAULT_WORLD_DICT_CONFIG["character_dict_filename"],
        )
        self.config["entity_dict_filename"] = self.config.get(
            "entity_dict_filename",
            DEFAULT_WORLD_DICT_CONFIG["entity_dict_filename"],
        )

        self.app.save_config()
        log.info("世界观字典配置已更新。")
        self.destroy()


# --- DeepSeek (翻译) 配置窗口 ---
class TranslateConfigWindow(tk.Toplevel):
    """翻译 JSON (DeepSeek/OpenAI 兼容) 配置对话框。"""

    def __init__(self, parent, app_controller, translate_config):
        """
        初始化 DeepSeek 配置窗口。

        Args:
            parent (tk.Widget): 父窗口。
            app_controller (RPGTranslatorApp): 应用控制器实例。
            translate_config (dict): 当前的翻译配置字典 (来自 app.config)。
                                     窗口将直接修改这个字典。
        """
        super().__init__(parent)
        self.app = app_controller
        self.config = translate_config # 直接引用配置字典

        # --- 状态标志 ---
        self.initializing = True
        self.connection_tested_ok = False

        # --- 窗口设置 ---
        self.title("翻译JSON文件配置 (OpenAI兼容 API)")
        self.geometry("780x700")
        self.transient(parent)
        self.grab_set()

        # --- 框架 ---
        frame = ttk.Frame(self, padding="10")
        frame.pack(fill=tk.BOTH, expand=True)
        frame.columnconfigure(1, weight=1) # 让输入框等可扩展

        # --- 控件变量 ---
        # 注意：获取默认值时，应从导入的 DEFAULT_TRANSLATE_CONFIG 获取
        from core.config import DEFAULT_TRANSLATE_CONFIG # 导入默认翻译配置
        self.api_url_var = tk.StringVar(value=self.config.get("api_url", DEFAULT_TRANSLATE_CONFIG["api_url"]))
        self.api_key_var = tk.StringVar(value=self.config.get("api_key", ""))
        self.model_var = tk.StringVar(value=self.config.get("model", DEFAULT_TRANSLATE_CONFIG["model"]))
        self.batch_var = tk.IntVar(value=self.config.get("batch_size", DEFAULT_TRANSLATE_CONFIG["batch_size"]))
        self.context_var = tk.IntVar(value=self.config.get("context_lines", DEFAULT_TRANSLATE_CONFIG["context_lines"]))
        self.concur_var = tk.IntVar(value=self.config.get("concurrency", DEFAULT_TRANSLATE_CONFIG["concurrency"]))
        self.retry_failed_items_var = tk.BooleanVar(value=self.config.get("retry_failed_items_only", DEFAULT_TRANSLATE_CONFIG["retry_failed_items_only"]))
        self.source_lang_var = tk.StringVar(value=self.config.get("source_language", DEFAULT_TRANSLATE_CONFIG["source_language"]))
        self.target_lang_var = tk.StringVar(value=self.config.get("target_language", DEFAULT_TRANSLATE_CONFIG["target_language"]))
        self.show_key_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="请先测试连接")

        # --- 创建控件 ---
        row_idx = 0

        # API URL
        ttk.Label(frame, text="API URL:").grid(row=row_idx, column=0, padx=5, pady=5, sticky="w")
        self.api_url_entry = ttk.Entry(frame, textvariable=self.api_url_var, width=60)
        self.api_url_entry.grid(row=row_idx, column=1, columnspan=3, padx=5, pady=5, sticky="ew") # 跨更多列
        row_idx += 1

        # API Key
        ttk.Label(frame, text="API Key:").grid(row=row_idx, column=0, padx=5, pady=5, sticky="w")
        key_frame = ttk.Frame(frame)
        key_frame.grid(row=row_idx, column=1, columnspan=3, padx=5, pady=5, sticky="ew")
        key_frame.columnconfigure(0, weight=1)
        self.api_key_entry = ttk.Entry(key_frame, textvariable=self.api_key_var, width=50, show="*")
        self.api_key_entry.grid(row=0, column=0, sticky="ew")
        key_toggle_button = ttk.Checkbutton(key_frame, text="显示", variable=self.show_key_var, command=self._toggle_key_visibility)
        key_toggle_button.grid(row=0, column=1, padx=(5, 0))
        row_idx += 1

        # Model Name
        ttk.Label(frame, text="模型名称:").grid(row=row_idx, column=0, padx=5, pady=5, sticky="w")
        self.model_selector = ModelSelector(
            frame, self.api_url_var, self.api_key_var, self.model_var,
            status_callback=self._set_status,
        )
        self.model_selector.grid(row=row_idx, column=1, columnspan=3, padx=5, pady=5, sticky="ew")
        self.model_combobox = self.model_selector.combobox
        row_idx += 1

        # Spinboxes in a subframe for better layout
        spinbox_frame = ttk.Frame(frame)
        spinbox_frame.grid(row=row_idx, column=0, columnspan=4, padx=0, pady=5, sticky="w")

        # Batch Size
        ttk.Label(spinbox_frame, text="每批最多条数:").pack(side=tk.LEFT, padx=(5, 2))
        self.batch_spinbox = ttk.Spinbox(spinbox_frame, from_=1, to=100, textvariable=self.batch_var, width=5)
        self.batch_spinbox.pack(side=tk.LEFT, padx=(0, 10))

        # Context Lines
        ttk.Label(spinbox_frame, text="上文行数:").pack(side=tk.LEFT, padx=(5, 2))
        self.context_spinbox = ttk.Spinbox(spinbox_frame, from_=0, to=50, textvariable=self.context_var, width=5)
        self.context_spinbox.pack(side=tk.LEFT, padx=(0, 10))

        # Concurrency
        ttk.Label(spinbox_frame, text="并发数:").pack(side=tk.LEFT, padx=(5, 2))
        self.concur_spinbox = ttk.Spinbox(spinbox_frame, from_=1, to=256, textvariable=self.concur_var, width=5) # 增加并发上限示例
        self.concur_spinbox.pack(side=tk.LEFT, padx=(0, 5))
        row_idx += 1

        # Language Selection
        lang_frame = ttk.Frame(frame)
        lang_frame.grid(row=row_idx, column=0, columnspan=4, padx=0, pady=5, sticky="ew")
        lang_frame.columnconfigure(1, weight=1)
        lang_frame.columnconfigure(3, weight=1)

        # Source Language
        ttk.Label(lang_frame, text="源语言:").grid(row=0, column=0, padx=5, sticky="w")
        source_lang_combo = ttk.Combobox(lang_frame, textvariable=self.source_lang_var, values=[
            "日语", "英语", "简体中文", "繁体中文", "韩语", "俄语", "法语", "德语", "西班牙语", "自动检测"
        ], width=15, state="readonly")
        source_lang_combo.grid(row=0, column=1, padx=5, sticky="ew")

        # Target Language
        ttk.Label(lang_frame, text="目标语言:").grid(row=0, column=2, padx=(10, 5), sticky="w")
        target_lang_combo = ttk.Combobox(lang_frame, textvariable=self.target_lang_var, values=[
            "简体中文", "繁体中文", "英语", "日语", "韩语", "俄语", "法语", "德语", "西班牙语"
        ], width=15, state="readonly")
        target_lang_combo.grid(row=0, column=3, padx=5, sticky="ew")
        row_idx += 1

        # Both message roles are configurable; no hidden translation policy.
        prompt_frame = ttk.LabelFrame(frame, text="提示词", padding="5")
        prompt_frame.grid(row=row_idx, column=0, columnspan=4, padx=5, pady=5, sticky="nsew")
        frame.rowconfigure(row_idx, weight=1)
        prompt_frame.columnconfigure(0, weight=1)
        prompt_frame.rowconfigure(0, weight=1)

        prompt_tabs = ttk.Notebook(prompt_frame)
        prompt_tabs.grid(row=0, column=0, sticky="nsew")
        self.prompt_editors = {}
        for key, title, description in (
            ("system_prompt", "核心翻译提示词", "翻译原则、人物表达和输出要求均可编辑。请保留 JSON 输出和控制标记约定。"),
            ("user_prompt_template", "输入模板", "可用变量：{source_language}、{target_language}、{character_glossary_section}、{entity_glossary_section}、{context_section}、{batch_text}。"),
        ):
            page = ttk.Frame(prompt_tabs, padding=5)
            page.columnconfigure(0, weight=1)
            page.rowconfigure(1, weight=1)
            prompt_tabs.add(page, text=title)
            ttk.Label(page, text=description, wraplength=700).grid(row=0, column=0, sticky="w", pady=(0, 5))
            editor = scrolledtext.ScrolledText(page, wrap=tk.WORD, height=12)
            editor.grid(row=1, column=0, sticky="nsew")
            editor.insert(tk.END, self.config.get(key, DEFAULT_TRANSLATE_CONFIG[key]))
            editor.edit_modified(False)
            self.prompt_editors[key] = editor
            ttk.Button(page, text="恢复此页默认提示词", command=lambda field=key: self._reset_prompt(field)).grid(row=2, column=0, sticky="e", pady=(5, 0))
        row_idx += 1

        # Status Label
        self.status_label = ttk.Label(frame, textvariable=self.status_var, foreground="orange")
        self.status_label.grid(row=row_idx, column=0, columnspan=4, padx=5, pady=(10, 0), sticky="ew")
        row_idx += 1

        # Buttons
        footer = ttk.Frame(frame)
        footer.grid(row=row_idx, column=0, columnspan=4, pady=10, sticky="ew")
        ttk.Checkbutton(
            footer, text="新版回退重试（实验性）",
            variable=self.retry_failed_items_var,
        ).pack(side=tk.LEFT, padx=(5, 0))
        help_icon = ttk.Label(footer, text="ⓘ", cursor="question_arrow", takefocus=True)
        help_icon.pack(side=tk.LEFT, padx=(4, 5))
        tooltip = None

        def show_tooltip(_event):
            nonlocal tooltip
            if tooltip is not None:
                return
            tooltip = tk.Toplevel(self)
            tooltip.overrideredirect(True)
            tk.Label(
                tooltip,
                text=(
                    "开启：每轮保存成功项，仅重试失败项，并附上具体错误和上次译文。\n"
                    "关闭：批次全部通过后保存；校验失败时整批重试，仍失败则拆批。\n\n"
                    "此开关只控制内容重试策略，不改变提示词配置。\n"
                    "两种模式均支持已完成批次续跑、长文分段和接口兼容处理；\n"
                    "达到预算、额度不足或持续失败时暂停并保留已保存进度。"
                ),
                justify=tk.LEFT, wraplength=360, padx=10, pady=8,
                background="#ffffe1", foreground="#202020",
                relief=tk.SOLID, borderwidth=1,
            ).pack()
            tooltip.update_idletasks()
            x = min(help_icon.winfo_rootx(), tooltip.winfo_screenwidth() - tooltip.winfo_reqwidth() - 8)
            y = help_icon.winfo_rooty() - tooltip.winfo_reqheight() - 6
            tooltip.geometry(f"+{max(0, x)}+{max(0, y)}")

        def hide_tooltip(_event):
            nonlocal tooltip
            if tooltip is not None:
                tooltip.destroy()
                tooltip = None

        help_icon.bind("<Enter>", show_tooltip)
        help_icon.bind("<Leave>", hide_tooltip)
        help_icon.bind("<FocusIn>", show_tooltip)
        help_icon.bind("<FocusOut>", hide_tooltip)
        help_icon.bind("<Escape>", hide_tooltip)

        button_frame = ttk.Frame(footer)
        button_frame.pack(side=tk.RIGHT)

        self.test_button = ttk.Button(button_frame, text="测试连接", command=self._test_connection)
        self.test_button.pack(side=tk.LEFT, padx=5)
        self.save_button = ttk.Button(button_frame, text="保存", command=self._save_config, state=tk.DISABLED)
        self.save_button.pack(side=tk.LEFT, padx=5)
        cancel_button = ttk.Button(button_frame, text="取消", command=self.destroy)
        cancel_button.pack(side=tk.LEFT, padx=5)

        # --- Bind Events ---
        self.api_url_var.trace_add("write", self._on_config_change)
        self.api_key_var.trace_add("write", self._on_config_change)
        self.model_var.trace_add("write", self._on_config_change)
        # Spinbox 变化也触发检查
        self.batch_spinbox.bind("<FocusOut>", self._on_config_change)
        self.batch_spinbox.bind("<KeyRelease>", self._on_config_change) # 用 KeyRelease 更即时
        self.context_spinbox.bind("<FocusOut>", self._on_config_change)
        self.context_spinbox.bind("<KeyRelease>", self._on_config_change)
        self.concur_spinbox.bind("<FocusOut>", self._on_config_change)
        self.concur_spinbox.bind("<KeyRelease>", self._on_config_change)
        # Combobox 选择变化
        source_lang_combo.bind("<<ComboboxSelected>>", self._on_config_change)
        target_lang_combo.bind("<<ComboboxSelected>>", self._on_config_change)
        # Prompt 文本变化
        for editor in self.prompt_editors.values():
            editor.bind("<<Modified>>", self._on_prompt_modified)
        self.protocol("WM_DELETE_WINDOW", self.destroy)

        # --- Initialization Complete ---
        self.initializing = False
        if self.api_key_var.get() and self.api_url_var.get():
            self._set_status("配置已加载，请测试连接", "orange")
        else:
            self._set_status("请输入 API URL 和 Key 并测试连接", "red")

    def _toggle_key_visibility(self):
        """Toggle API Key visibility."""
        if self.show_key_var.get():
            self.api_key_entry.config(show="")
        else:
            self.api_key_entry.config(show="*")

    def _on_prompt_modified(self, event=None):
        """Handle Prompt text modification."""
        editor = event.widget if event else None
        if not self.initializing and editor and editor.edit_modified():
            editor.edit_modified(False)
            self._on_config_change()

    def _reset_prompt(self, key):
        editor = self.prompt_editors[key]
        editor.delete("1.0", tk.END)
        editor.insert(tk.END, DEFAULT_TRANSLATE_CONFIG[key])

    def _on_config_change(self, *args):
        """仅连接参数变化时撤销已通过的连接检查。"""
        if self.initializing or not args:
            return
        connection_variables = (self.api_url_var, self.api_key_var, self.model_var)
        if str(args[0]) not in {str(variable) for variable in connection_variables}:
            return
        self.connection_tested_ok = False
        self.save_button.config(state=tk.DISABLED)
        self._set_status("连接配置已修改，请重新测试连接", "orange")

    def _set_status(self, message, color):
        """Update status label."""
        if self.winfo_exists():
             self.status_var.set(message)
             self.status_label.config(foreground=color)

    def _test_connection(self):
        """Start connection test thread."""
        api_url = self.api_url_var.get().strip()
        api_key = self.api_key_var.get().strip()
        model = self.model_var.get().strip()
        if not api_key or not api_url:
            messagebox.showerror("错误", "请输入 API URL 和 API Key", parent=self)
            return
        if not model:
             messagebox.showerror("错误", "请输入模型名称", parent=self)
             return

        self.model_selector.suspend_status_updates()
        self._set_status("正在测试连接...", "blue")
        self.test_button.config(state=tk.DISABLED)
        self.save_button.config(state=tk.DISABLED)

        thread = threading.Thread(target=self._test_connection_thread, args=(api_url, api_key, model), daemon=True)
        thread.start()

    def _test_connection_thread(self, api_url, api_key, model):
        """Run connection test in a thread."""
        try:
            client = deepseek.DeepSeekClient(base_url=api_url, api_key=api_key)
            success, message = client.test_connection(model)
            self.after(0, lambda: self._test_connection_result(success, message))
        except ConnectionError as e:
             self.after(0, lambda: self._test_connection_result(False, f"客户端初始化失败: {e}"))
        except Exception as e:
            log.exception("翻译API连接测试线程发生意外错误。")
            self.after(0, lambda: self._test_connection_result(False, f"测试时发生意外错误: {e}"))

    def _test_connection_result(self, success, message):
        """Handle connection test result."""
        if not self.winfo_exists(): return
        self.test_button.config(state=tk.NORMAL)
        if success:
            self.connection_tested_ok = True
            self._set_status("连接成功!", "green")
            self.save_button.config(state=tk.NORMAL)
            messagebox.showinfo("成功", message, parent=self)
        else:
            self.connection_tested_ok = False
            self._set_status("连接失败", "red")
            self.save_button.config(state=tk.DISABLED)
            messagebox.showerror("连接失败", message, parent=self)

    def _save_config(self):
        """Save configuration and close window."""
        if not self.connection_tested_ok:
            messagebox.showwarning("无法保存", "请先成功测试连接后再保存。", parent=self)
            return

        # 获取 Spinbox 的值，并进行基本的类型检查和范围限制 (可选但推荐)
        try:
            batch_size = int(self.batch_var.get())
            if not (1 <= batch_size <= 100): batch_size = 10 # 恢复默认
        except ValueError: batch_size = 10
        try:
            context_lines = int(self.context_var.get())
            if not (0 <= context_lines <= 50): context_lines = 10
        except ValueError: context_lines = 10
        try:
            concurrency = int(self.concur_var.get())
            if not (1 <= concurrency <= 256): concurrency = 16
        except ValueError: concurrency = 16


        # Update the config dictionary directly
        self.config["api_url"] = self.api_url_var.get().strip()
        self.config["api_key"] = self.api_key_var.get().strip()
        self.config["model"] = self.model_var.get().strip()
        self.config["batch_size"] = batch_size
        self.config["context_lines"] = context_lines
        self.config["concurrency"] = concurrency
        self.config["retry_failed_items_only"] = self.retry_failed_items_var.get()
        self.config["source_language"] = self.source_lang_var.get()
        self.config["target_language"] = self.target_lang_var.get()
        for obsolete in ("prompt_template", "max_context_chars", "max_batch_chars", "max_tokens", "max_task_tokens"):
            self.config.pop(obsolete, None)
        for key, editor in self.prompt_editors.items():
            self.config[key] = editor.get("1.0", tk.END).strip()
        # 确保 max_retries 也被保存 (如果之前没有，从默认值添加)
        from core.config import DEFAULT_TRANSLATE_CONFIG
        self.config["max_retries"] = self.config.get("max_retries", DEFAULT_TRANSLATE_CONFIG["max_retries"])

        # Notify app to save the entire config
        self.app.save_config()
        log.info("翻译配置已更新。")
        self.destroy()


