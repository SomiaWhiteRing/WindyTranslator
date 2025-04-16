# ui/rtp_dialog.py
import tkinter as tk
from tkinter import ttk

class RTPSelectionWindow(tk.Toplevel):
    """RTP 选择对话框窗口。"""

    def __init__(self, parent, app_controller, rtp_config):
        """
        初始化 RTP 选择窗口。

        Args:
            parent (tk.Widget): 父窗口 (通常是主窗口的 root)。
            app_controller (RPGTranslatorApp): 应用控制器实例，用于保存配置。
            rtp_config (dict): 当前的 RTP 配置字典 (来自 app.config['pro_mode_settings']['rtp_options'])。
                               窗口将直接修改这个字典。
        """
        super().__init__(parent)
        self.app = app_controller
        self.rtp_config = rtp_config # 直接引用 App 中的配置字典部分

        self.title("选择RTP")
        self.geometry("250x200") # 调整大小以适应内容
        self.transient(parent) # 依附于父窗口
        self.grab_set()        # 设为模式对话框
        self.resizable(False, False)

        # --- 控件变量 ---
        # 创建 BooleanVar 并与传入的 rtp_config 同步初始状态
        # 注意 key 与 pro_mode_settings 中的 rtp_options key 一致
        self.rtp_vars = {
            '2000': tk.BooleanVar(value=self.rtp_config.get('2000', True)), # 默认勾选 2000
            '2000en': tk.BooleanVar(value=self.rtp_config.get('2000en', False)),
            '2003': tk.BooleanVar(value=self.rtp_config.get('2003', False)),
            '2003steam': tk.BooleanVar(value=self.rtp_config.get('2003steam', False))
        }

        # --- 创建控件 ---
        frame = ttk.Frame(self, padding="10")
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frame, text="选择要安装的RTP文件:").pack(anchor=tk.W, pady=(0, 10))

        ttk.Checkbutton(frame, text="RPG Maker 2000", variable=self.rtp_vars['2000']).pack(anchor=tk.W, pady=2)
        ttk.Checkbutton(frame, text="RPG Maker 2000 (英文版)", variable=self.rtp_vars['2000en']).pack(anchor=tk.W, pady=2)
        ttk.Checkbutton(frame, text="RPG Maker 2003", variable=self.rtp_vars['2003']).pack(anchor=tk.W, pady=2)
        ttk.Checkbutton(frame, text="RPG Maker 2003 (Steam版)", variable=self.rtp_vars['2003steam']).pack(anchor=tk.W, pady=2)

        # 确定按钮
        confirm_button = ttk.Button(frame, text="确定", command=self._on_confirm)
        confirm_button.pack(anchor=tk.CENTER, pady=(15, 0)) # 增加顶部间距

        # 处理窗口关闭事件 (等同于取消)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

        # 尝试将窗口定位在主窗口内或附近 (可选)
        try:
            parent_x = parent.winfo_rootx()
            parent_y = parent.winfo_rooty()
            parent_width = parent.winfo_width()
            # 简单的定位逻辑：放在主窗口右上角附近
            win_x = parent_x + parent_width - self.winfo_reqwidth() - 50
            win_y = parent_y + 100
            self.geometry(f"+{win_x}+{win_y}")
        except:
            pass # 定位失败就算了

    def _on_confirm(self):
        """用户点击确定按钮的处理。"""
        # 将 BooleanVar 的当前值更新回传入的 rtp_config 字典
        for key, var in self.rtp_vars.items():
            self.rtp_config[key] = var.get()

        # 通知 App 保存整个配置（包含了已更新的 rtp_config）
        self.app.save_config()

        # 通知 App 更新 ProModePanel 上的按钮文本
        # (App 会调用 MainWindow 的方法，MainWindow 再调用 ProModePanel 的方法)
        self.app.main_window.update_rtp_button_text()

        self.destroy() # 关闭对话框

    def _on_cancel(self):
        """用户点击关闭按钮或按 Alt+F4 时的处理。"""
        # 不保存任何更改，直接关闭窗口
        self.destroy()