# ui/config_dialogs.py
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import threading
import logging

# 导入 API 客户端模块用于连接测试
from core.api_clients import gemini, deepseek

log = logging.getLogger(__name__)

# --- Gemini (世界观字典) 配置窗口 ---
class WorldDictConfigWindow(tk.Toplevel):
    """世界观字典 (Gemini) 配置对话框。"""

    def __init__(self, parent, app_controller, world_dict_config):
        """
        初始化 Gemini 配置窗口。

        Args:
            parent (tk.Widget): 父窗口。
            app_controller (RPGTranslatorApp): 应用控制器实例。
            world_dict_config (dict): 当前的世界观字典配置字典 (来自 app.config)。
                                     窗口将直接修改这个字典。
        """
        super().__init__(parent)
        self.app = app_controller
        self.config = world_dict_config # 直接引用配置字典

        # --- 状态标志 ---
        self.initializing = True # 防止初始化期间触发 on_change
        self.connection_tested_ok = False # 标记连接测试是否通过

        # --- 窗口设置 ---
        self.title("世界观字典配置 (Gemini)")
        self.geometry("600x550")
        self.transient(parent)
        self.grab_set()

        # --- 框架 ---
        frame = ttk.Frame(self, padding="10")
        frame.pack(fill=tk.BOTH, expand=True)
        frame.columnconfigure(1, weight=1) # 让输入框和文本框可以扩展

        # --- 控件变量 ---
        self.api_key_var = tk.StringVar(value=self.config.get("api_key", ""))
        self.model_var = tk.StringVar(value=self.config.get("model", "gemini-1.5-pro-latest")) # 默认使用最新 Pro
        self.show_key_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="请先测试连接")

        # --- 创建控件 ---
        row_idx = 0

        # API Key
        ttk.Label(frame, text="Gemini API Key:").grid(row=row_idx, column=0, padx=5, pady=5, sticky="w")
        key_frame = ttk.Frame(frame) # 使用 Frame 容纳输入框和显示按钮
        key_frame.grid(row=row_idx, column=1, columnspan=2, padx=5, pady=5, sticky="ew")
        key_frame.columnconfigure(0, weight=1)
        self.api_key_entry = ttk.Entry(key_frame, textvariable=self.api_key_var, width=50, show="*")
        self.api_key_entry.grid(row=0, column=0, sticky="ew")
        key_toggle_button = ttk.Checkbutton(key_frame, text="显示", variable=self.show_key_var, command=self._toggle_key_visibility)
        key_toggle_button.grid(row=0, column=1, padx=(5, 0))
        row_idx += 1

        # Model Name
        ttk.Label(frame, text="模型名称:").grid(row=row_idx, column=0, padx=5, pady=5, sticky="w")
        model_combobox = ttk.Combobox(frame, textvariable=self.model_var, values=[
            "gemini-1.5-pro-latest", # 推荐
            "gemini-pro",            # 旧版稳定
            "gemini-1.0-pro",        # 明确版本
            # "gemini-1.5-flash-latest", # 如果需要 Flash 版本
        ], width=48)
        model_combobox.grid(row=row_idx, column=1, columnspan=2, padx=5, pady=5, sticky="ew")
        row_idx += 1

        # Prompt Template
        prompt_frame = ttk.LabelFrame(frame, text="Prompt 模板", padding="5")
        prompt_frame.grid(row=row_idx, column=0, columnspan=3, padx=5, pady=5, sticky="nsew")
        frame.rowconfigure(row_idx, weight=1) # 让 Prompt 区域可以垂直扩展
        prompt_frame.columnconfigure(0, weight=1)
        prompt_frame.rowconfigure(0, weight=1)

        self.prompt_text = scrolledtext.ScrolledText(prompt_frame, wrap=tk.WORD, height=15)
        self.prompt_text.grid(row=0, column=0, sticky="nsew")
        self.prompt_text.insert(tk.END, self.config.get("prompt", "")) # 加载初始 Prompt
        self.prompt_text.edit_modified(False) # 初始化修改状态
        row_idx += 1

        # 状态标签
        self.status_label = ttk.Label(frame, textvariable=self.status_var, foreground="orange")
        self.status_label.grid(row=row_idx, column=0, columnspan=3, padx=5, pady=(10, 0), sticky="ew")
        row_idx += 1

        # 按钮区域
        button_frame = ttk.Frame(frame)
        button_frame.grid(row=row_idx, column=0, columnspan=3, pady=10, sticky="e")

        self.test_button = ttk.Button(button_frame, text="测试连接", command=self._test_connection)
        self.test_button.pack(side=tk.LEFT, padx=5)
        self.save_button = ttk.Button(button_frame, text="保存", command=self._save_config, state=tk.DISABLED) # 初始禁用
        self.save_button.pack(side=tk.LEFT, padx=5)
        cancel_button = ttk.Button(button_frame, text="取消", command=self.destroy)
        cancel_button.pack(side=tk.LEFT, padx=5)

        # --- 绑定事件 ---
        self.api_key_var.trace_add("write", self._on_config_change)
        self.model_var.trace_add("write", self._on_config_change)
        # 使用 bind 监听 Text 控件的变化比 trace 更可靠
        self.prompt_text.bind("<<Modified>>", self._on_prompt_modified)
        self.protocol("WM_DELETE_WINDOW", self.destroy) # 处理关闭按钮

        # --- 初始化完成 ---
        self.initializing = False
        # 检查初始状态
        if self.api_key_var.get():
            self._set_status("配置已加载，请测试连接", "orange")
        else:
            self._set_status("请输入 API Key 并测试连接", "red")

    def _toggle_key_visibility(self):
        """切换 API Key 的显示状态。"""
        if self.show_key_var.get():
            self.api_key_entry.config(show="")
        else:
            self.api_key_entry.config(show="*")

    def _on_prompt_modified(self, event=None):
        """处理 Prompt 文本框的修改事件。"""
        # edit_modified() 会触发此回调，需要重置标志位以避免无限循环
        # 并且仅在非初始化阶段响应
        if not self.initializing and self.prompt_text.edit_modified():
            self.prompt_text.edit_modified(False) # 重置标志位
            self._on_config_change()

    def _on_config_change(self, *args):
        """配置发生变化时的处理。"""
        if self.initializing: return
        self.connection_tested_ok = False
        self.save_button.config(state=tk.DISABLED)
        self._set_status("配置已更改，请重新测试连接", "orange")

    def _set_status(self, message, color):
        """更新状态标签的文本和颜色。"""
        if self.winfo_exists(): # 检查窗口是否存在
             self.status_var.set(message)
             self.status_label.config(foreground=color)

    def _test_connection(self):
        """启动 Gemini 连接测试线程。"""
        api_key = self.api_key_var.get().strip()
        model = self.model_var.get().strip()
        if not api_key:
            messagebox.showerror("错误", "请输入 Gemini API Key", parent=self)
            return
        if not model:
            messagebox.showerror("错误", "请选择或输入模型名称", parent=self)
            return

        self._set_status("正在测试连接...", "blue")
        self.test_button.config(state=tk.DISABLED)
        self.save_button.config(state=tk.DISABLED)

        # 使用线程执行测试
        thread = threading.Thread(target=self._test_connection_thread, args=(api_key, model), daemon=True)
        thread.start()

    def _test_connection_thread(self, api_key, model):
        """在线程中执行实际的 Gemini API 连接测试。"""
        try:
            client = gemini.GeminiClient(api_key) # 初始化客户端
            success, message = client.test_connection(model) # 调用客户端的测试方法
            # 使用 after 在主线程更新 UI
            self.after(0, lambda: self._test_connection_result(success, message))
        except ConnectionError as e: # 捕获客户端初始化失败
             self.after(0, lambda: self._test_connection_result(False, f"客户端初始化失败: {e}"))
        except Exception as e: # 捕获其他意外错误
            log.exception("Gemini 连接测试线程发生意外错误。")
            self.after(0, lambda: self._test_connection_result(False, f"测试时发生意外错误: {e}"))

    def _test_connection_result(self, success, message):
        """处理连接测试结果，更新 UI。"""
        if not self.winfo_exists(): return # 窗口已关闭
        self.test_button.config(state=tk.NORMAL)
        if success:
            self.connection_tested_ok = True
            self._set_status("连接成功!", "green")
            self.save_button.config(state=tk.NORMAL) # 测试成功才允许保存
            messagebox.showinfo("成功", message, parent=self)
        else:
            self.connection_tested_ok = False
            self._set_status("连接失败", "red")
            self.save_button.config(state=tk.DISABLED)
            messagebox.showerror("连接失败", message, parent=self)

    def _save_config(self):
        """保存配置到 App 持有的字典并关闭窗口。"""
        if not self.connection_tested_ok:
            messagebox.showwarning("无法保存", "请先成功测试连接后再保存。", parent=self)
            return

        # 更新 App 持有的配置字典
        self.config["api_key"] = self.api_key_var.get().strip()
        self.config["model"] = self.model_var.get().strip()
        self.config["prompt"] = self.prompt_text.get("1.0", tk.END).strip()

        # 通知 App 保存整个配置到文件
        self.app.save_config()
        log.info("世界观字典配置已更新。")
        self.destroy() # 关闭窗口


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
        self.title("翻译JSON文件配置 (DeepSeek/OpenAI)")
        self.geometry("600x580") # 增加高度以容纳 Prompt
        self.transient(parent)
        self.grab_set()

        # --- 框架 ---
        frame = ttk.Frame(self, padding="10")
        frame.pack(fill=tk.BOTH, expand=True)
        frame.columnconfigure(1, weight=1) # 让输入框等可扩展

        # --- 控件变量 ---
        self.api_url_var = tk.StringVar(value=self.config.get("api_url", "https://api.deepseek.com/v1")) # 默认 DeepSeek 官方
        self.api_key_var = tk.StringVar(value=self.config.get("api_key", ""))
        self.model_var = tk.StringVar(value=self.config.get("model", "deepseek-chat")) # 默认 DeepSeek Chat
        self.batch_var = tk.IntVar(value=self.config.get("batch_size", 10))
        self.context_var = tk.IntVar(value=self.config.get("context_lines", 10))
        self.concur_var = tk.IntVar(value=self.config.get("concurrency", 16))
        self.source_lang_var = tk.StringVar(value=self.config.get("source_language", "日语"))
        self.target_lang_var = tk.StringVar(value=self.config.get("target_language", "简体中文"))
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
        model_combobox = ttk.Combobox(frame, textvariable=self.model_var, values=[
            "deepseek-chat", "deepseek-coder", # DeepSeek 官方模型示例
            "gpt-3.5-turbo", "gpt-4", "gpt-4-turbo-preview", # OpenAI 模型示例
            "moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k", # Moonshot 模型示例
            # 添加其他你可能使用的 OpenAI 兼容模型
        ], width=48)
        model_combobox.grid(row=row_idx, column=1, columnspan=3, padx=5, pady=5, sticky="ew")
        row_idx += 1

        # Spinboxes in a subframe for better layout
        spinbox_frame = ttk.Frame(frame)
        spinbox_frame.grid(row=row_idx, column=0, columnspan=4, padx=0, pady=5, sticky="w")

        # Batch Size
        ttk.Label(spinbox_frame, text="批次大小:").pack(side=tk.LEFT, padx=(5, 2))
        self.batch_spinbox = ttk.Spinbox(spinbox_frame, from_=1, to=100, textvariable=self.batch_var, width=5)
        self.batch_spinbox.pack(side=tk.LEFT, padx=(0, 10))

        # Context Lines
        ttk.Label(spinbox_frame, text="上文行数:").pack(side=tk.LEFT, padx=(5, 2))
        self.context_spinbox = ttk.Spinbox(spinbox_frame, from_=0, to=50, textvariable=self.context_var, width=5)
        self.context_spinbox.pack(side=tk.LEFT, padx=(0, 10))

        # Concurrency
        ttk.Label(spinbox_frame, text="并发数:").pack(side=tk.LEFT, padx=(5, 2))
        self.concur_spinbox = ttk.Spinbox(spinbox_frame, from_=1, to=64, textvariable=self.concur_var, width=5)
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

        # Prompt Template
        prompt_frame = ttk.LabelFrame(frame, text="Prompt 模板", padding="5")
        prompt_frame.grid(row=row_idx, column=0, columnspan=4, padx=5, pady=5, sticky="nsew")
        frame.rowconfigure(row_idx, weight=1)
        prompt_frame.columnconfigure(0, weight=1)
        prompt_frame.rowconfigure(0, weight=1)

        self.prompt_text = scrolledtext.ScrolledText(prompt_frame, wrap=tk.WORD, height=8) # 减少默认高度
        self.prompt_text.grid(row=0, column=0, sticky="nsew")
        self.prompt_text.insert(tk.END, self.config.get("prompt_template", ""))
        self.prompt_text.edit_modified(False)
        row_idx += 1

        # Status Label
        self.status_label = ttk.Label(frame, textvariable=self.status_var, foreground="orange")
        self.status_label.grid(row=row_idx, column=0, columnspan=4, padx=5, pady=(10, 0), sticky="ew")
        row_idx += 1

        # Buttons
        button_frame = ttk.Frame(frame)
        button_frame.grid(row=row_idx, column=0, columnspan=4, pady=10, sticky="e")

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
        self.batch_spinbox.bind("<KeyRelease>", self._on_config_change)
        self.context_spinbox.bind("<FocusOut>", self._on_config_change)
        self.context_spinbox.bind("<KeyRelease>", self._on_config_change)
        self.concur_spinbox.bind("<FocusOut>", self._on_config_change)
        self.concur_spinbox.bind("<KeyRelease>", self._on_config_change)
        # Combobox 选择变化
        source_lang_combo.bind("<<ComboboxSelected>>", self._on_config_change)
        target_lang_combo.bind("<<ComboboxSelected>>", self._on_config_change)
        # Prompt 文本变化
        self.prompt_text.bind("<<Modified>>", self._on_prompt_modified)
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
        if not self.initializing and self.prompt_text.edit_modified():
            self.prompt_text.edit_modified(False)
            self._on_config_change()

    def _on_config_change(self, *args):
        """Handle configuration changes."""
        if self.initializing: return
        self.connection_tested_ok = False
        if hasattr(self, 'save_button') and self.save_button.winfo_exists():
             self.save_button.config(state=tk.DISABLED)
        self._set_status("配置已更改，请重新测试连接", "orange")

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
            log.exception("DeepSeek 连接测试线程发生意外错误。")
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

        # Update the config dictionary directly
        self.config["api_url"] = self.api_url_var.get().strip()
        self.config["api_key"] = self.api_key_var.get().strip()
        self.config["model"] = self.model_var.get().strip()
        self.config["batch_size"] = self.batch_var.get()
        self.config["context_lines"] = self.context_var.get()
        self.config["concurrency"] = self.concur_var.get()
        self.config["source_language"] = self.source_lang_var.get()
        self.config["target_language"] = self.target_lang_var.get()
        self.config["prompt_template"] = self.prompt_text.get("1.0", tk.END).strip()

        # Notify app to save the entire config
        self.app.save_config()
        log.info("翻译配置已更新。")
        self.destroy()