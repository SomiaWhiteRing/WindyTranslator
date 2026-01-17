# ui/main_window.py
import tkinter as tk
from tkinter import ttk, scrolledtext
import datetime
import os

# 导入同目录下的其他 UI 面板类 (假设它们稍后会被定义)
from . import easy_mode_panel
from . import pro_mode_panel
# 导入需要弹出的窗口 (用于类型提示或方法调用)
from . import rtp_dialog # 需要调用更新按钮文本
from . import config_dialogs # 可能需要引用
from . import dict_editor # 可能需要引用

class MainWindow:
    """应用程序的主窗口 UI 类。"""

    def __init__(self, root, app_controller, config):
        """
        初始化主窗口。

        Args:
            root (tk.Tk): Tkinter 根窗口。
            app_controller (RPGTranslatorApp): 应用控制器实例，用于事件回调。
            config (dict): 应用配置字典，用于初始化某些 UI 状态。
        """
        self.root = root
        self.app = app_controller # 保留对 App 控制器的引用
        self.config = config
        self.root.title("WindyTranslator")
        # 初始大小将在切换模式时设置
        # self.root.geometry("600x750") # 初始大小由模式决定

        # --- 创建主框架 ---
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)
        # **** Grid 配置: 让 main_frame 的列 0 和行 2 (日志区) 可以扩展 ****
        main_frame.columnconfigure(0, weight=1)
        main_frame.rowconfigure(2, weight=1) # 日志区所在行

        # --- 1. 游戏路径选择区域 ---
        path_frame = ttk.LabelFrame(main_frame, text="游戏路径", padding="5")
        # 让 path_frame 水平填充 (sticky="ew")
        path_frame.grid(row=0, column=0, sticky="ew", padx=0, pady=5)
        # **** Grid 配置: 让 path_frame 内的 Entry (列 0) 可以水平扩展 ****
        path_frame.columnconfigure(0, weight=1)

        self.game_path_entry = ttk.Entry(path_frame, textvariable=self.app.game_path, width=70)
        # 让 Entry 水平填充其单元格
        self.game_path_entry.grid(row=0, column=0, sticky="ew", padx=(0, 5), pady=5)

        self.browse_button = ttk.Button(path_frame, text="浏览...", command=self.app.browse_game_path)
        self.browse_button.grid(row=0, column=1, padx=5, pady=5) # 按钮不需要扩展

        # --- 2. 功能区 Notebook ---
        self.functions_notebook = ttk.Notebook(main_frame)
        # 让 Notebook 水平填充
        self.functions_notebook.grid(row=1, column=0, sticky="ew", padx=0, pady=5)
        # Notebook 本身不需要 weight，因为它的大小由内容决定或固定

        # 创建模式面板实例 (将 App 控制器传递给它们)
        # 轻松模式
        self.easy_mode_frame_container = ttk.Frame(self.functions_notebook, padding="10")
        self.easy_panel = easy_mode_panel.EasyModePanel(self.easy_mode_frame_container, self.app)
        self.functions_notebook.add(self.easy_mode_frame_container, text="轻松模式")

        # 专业模式
        self.pro_mode_frame_container = ttk.Frame(self.functions_notebook, padding="5")
        self.pro_panel = pro_mode_panel.ProModePanel(self.pro_mode_frame_container, self.app, self.config) # 专业模式需要配置来初始化
        self.functions_notebook.add(self.pro_mode_frame_container, text="专业模式")

        # --- 3. 日志区域 ---
        log_frame = ttk.LabelFrame(main_frame, text="操作日志", padding="5")
        # **** 让 log_frame 在主框架中双向填充 ****
        log_frame.grid(row=2, column=0, sticky="nsew", padx=0, pady=5)
        # **** Grid 配置: 让 log_frame 内部的 Text (行 0, 列 0) 可以双向扩展 ****
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)

        self.log_text = scrolledtext.ScrolledText(log_frame, wrap=tk.WORD, width=80, height=10, state=tk.DISABLED)
        # **** 让 log_text 填充其在 log_frame 中的单元格 ****
        self.log_text.grid(row=0, column=0, sticky="nsew")

        # 定义日志级别颜色标签
        self.log_text.tag_configure("normal", foreground="black")
        self.log_text.tag_configure("success", foreground="blue")
        self.log_text.tag_configure("error", foreground="red")
        self.log_text.tag_configure("warning", foreground="orange") # 添加 warning 级别
        self.log_text.tag_configure("debug", foreground="grey")   # 添加 debug 级别

        # --- 4. 状态栏 ---
        status_frame = ttk.Frame(main_frame, padding=(5, 2))
        # 让 status_frame 水平填充
        status_frame.grid(row=3, column=0, sticky="ew", padx=0, pady=(5, 0))
        # **** Grid 配置: 让 status_frame 内的 Label (列 0) 可以水平扩展 ****
        status_frame.columnconfigure(0, weight=1)

        self.status_var = tk.StringVar(value="就绪")
        status_label = ttk.Label(status_frame, textvariable=self.status_var, anchor=tk.W)
        # 让 status_label 水平填充
        status_label.grid(row=0, column=0, sticky="ew")

        # --- 保存控件引用，方便启用/禁用 ---
        self.all_controls = [
            self.browse_button,
            self.easy_panel.get_controls(), # EasyModePanel 需要提供获取其控件的方法
            self.pro_panel.get_controls()   # ProModePanel 需要提供获取其控件的方法
        ]
        # 扁平化控件列表
        self.flat_controls = self._flatten_controls(self.all_controls)

    # --- 公共方法 (供 App 调用) ---

    def add_log(self, message, level="normal"):
        """向日志区域添加带时间戳的消息。"""
        if not self.log_text.winfo_exists(): return # 防止窗口关闭后调用
        self.log_text.config(state=tk.NORMAL)
        timestamp = datetime.datetime.now().strftime("[%H:%M:%S] ")
        # 确保 level 是已定义的 tag
        log_level_tag = level if level in self.log_text.tag_names() else "normal"
        self.log_text.insert(tk.END, timestamp + message + "\n", log_level_tag)
        self.log_text.see(tk.END) # 滚动到底部
        self.log_text.config(state=tk.DISABLED)
        self.root.update_idletasks() # 确保界面刷新

    def update_status(self, message):
        """更新状态栏文本。"""
        if not self.root.winfo_exists(): return
        self.status_var.set(message)
        self.root.update_idletasks()

    def get_status(self):
        """获取当前状态栏文本。"""
        return self.status_var.get()

    def update_easy_status(self, message):
        """更新轻松模式面板的状态标签。"""
        if hasattr(self, 'easy_panel') and self.easy_panel.winfo_exists():
            self.easy_panel.update_status(message)
        self.root.update_idletasks()


    def update_easy_progress(self, value):
        """更新轻松模式面板的进度条。"""
        if hasattr(self, 'easy_panel') and self.easy_panel.winfo_exists():
            self.easy_panel.update_progress(value)
        self.root.update_idletasks()

    def set_controls_enabled(self, enabled):
        """启用或禁用窗口中的主要交互控件。"""
        state = tk.NORMAL if enabled else tk.DISABLED
        for control in self.flat_controls:
             # 检查控件是否存在且有 state 属性
            if control and hasattr(control, 'winfo_exists') and control.winfo_exists() and hasattr(control, 'configure'):
                try:
                    # 特殊处理 Notebook 标签页切换
                    if isinstance(control, ttk.Notebook):
                         for i in range(len(control.tabs())):
                              tab_state = state if enabled else 'disabled' # Notebook tab 用 'disabled'
                              try: # 防止切换过程中 tab ID 失效
                                   control.tab(i, state=tab_state)
                              except tk.TclError:
                                   pass # 忽略无效 tab ID 错误
                    else:
                        control.configure(state=state)
                except tk.TclError as e:
                     # 忽略设置状态时可能发生的错误（例如控件已被销毁）
                     print(f"设置控件状态时出错: {e} (控件: {control})")
                     pass

    def get_current_mode(self):
        """获取当前选中的 Notebook 标签页对应的模式 ('easy' 或 'pro')。"""
        try:
            selected_tab_index = self.functions_notebook.index(self.functions_notebook.select())
            return 'easy' if selected_tab_index == 0 else 'pro'
        except Exception:
             # 如果 Notebook 还没完全加载好或发生错误
            return self.config.get('selected_mode', 'easy') # 返回配置中的模式

    def switch_to_mode(self, mode):
        """切换到指定的模式标签页。"""
        target_frame = self.easy_mode_frame_container if mode == 'easy' else self.pro_mode_frame_container
        try:
            self.functions_notebook.select(target_frame)
        except tk.TclError as e:
             print(f"切换模式时出错 (可能控件尚未就绪): {e}")
             # 可以尝试延迟执行
             # self.root.after(50, lambda: self.switch_to_mode(mode))


    def update_rtp_button_text(self):
        """更新专业模式面板上的 RTP 选择按钮文本。"""
        if hasattr(self, 'pro_panel') and self.pro_panel.winfo_exists():
            self.pro_panel.update_rtp_button_text() # 调用 ProModePanel 的方法

    def show_file_selection_dialog(self, title, prompt, file_list):
        """
        弹出一个简单的列表选择对话框。

        Args:
            title (str): 对话框标题。
            prompt (str): 提示信息。
            file_list (list): 要显示的文件名列表。

        Returns:
            str: 用户选择的文件名，如果取消则返回 None。
        """
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.geometry("400x300")
        dialog.transient(self.root) # 依附于主窗口
        dialog.grab_set() # 模式对话框

        ttk.Label(dialog, text=prompt).pack(pady=10)

        listbox_frame = ttk.Frame(dialog)
        listbox_frame.pack(pady=10, padx=10, fill=tk.BOTH, expand=True)

        scrollbar = ttk.Scrollbar(listbox_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        listbox = tk.Listbox(listbox_frame, yscrollcommand=scrollbar.set, width=50)
        listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        for item in file_list:
            listbox.insert(tk.END, item)

        scrollbar.config(command=listbox.yview)

        selected_value = tk.StringVar()

        def on_select():
            if listbox.curselection():
                selected_value.set(listbox.get(listbox.curselection()))
            dialog.destroy()

        def on_cancel():
            selected_value.set("") # 表示取消
            dialog.destroy()

        button_frame = ttk.Frame(dialog)
        button_frame.pack(pady=10)
        ttk.Button(button_frame, text="选择", command=on_select).pack(side=tk.LEFT, padx=10)
        ttk.Button(button_frame, text="取消", command=on_cancel).pack(side=tk.LEFT, padx=10)

        dialog.protocol("WM_DELETE_WINDOW", on_cancel) # 处理关闭按钮

        # 等待对话框关闭
        self.root.wait_window(dialog)

        result = selected_value.get()
        return result if result else None

    
    # --- 新增: 更新 Pro 面板修正按钮状态的中继方法 ---
    def update_fix_fallback_button_state(self, enabled):
        """
        更新专业模式面板上的 '修正回退' 按钮的状态。
        此方法由 App 控制器调用。
        """
        # 检查 ProModePanel 实例是否存在且控件仍然有效
        if hasattr(self, 'pro_panel') and self.pro_panel.winfo_exists():
            # 调用 ProModePanel 实例上的方法来实际更新按钮状态
            self.pro_panel.update_fix_fallback_button_state(enabled)
        else:
            # 如果 ProModePanel 不可用（例如窗口正在关闭），则记录日志或忽略
            print("尝试更新修正回退按钮状态，但 ProModePanel 不可用。")

    # --- 内部辅助方法 ---

    def _flatten_controls(self, controls_list):
        """递归地将嵌套的控件列表/元组展平成单个列表。"""
        flat_list = []
        for item in controls_list:
            if isinstance(item, (list, tuple)):
                flat_list.extend(self._flatten_controls(item))
            elif item is not None:
                flat_list.append(item)
        return flat_list
