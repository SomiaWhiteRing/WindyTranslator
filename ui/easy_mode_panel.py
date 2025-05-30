# ui/easy_mode_panel.py
import tkinter as tk
from tkinter import ttk

class EasyModePanel(ttk.Frame):
    """轻松模式的 UI 面板类。"""

    def __init__(self, parent, app_controller):
        """
        初始化轻松模式面板。

        Args:
            parent (tk.Widget): 父容器 (通常是 Notebook 中的一个 Frame)。
            app_controller (RPGTranslatorApp): 应用控制器实例。
        """
        super().__init__(parent, padding="10")
        self.app = app_controller
        self.pack(fill=tk.BOTH, expand=True) # 让面板填充父容器

        # --- 控件变量 ---
        self.progress_var = tk.DoubleVar(value=0.0)
        self.status_var = tk.StringVar(value="选择游戏目录后点击“开始翻译”")

        # --- 创建控件 ---

        # 按钮容器 (用于居中或水平排列)
        button_container = ttk.Frame(self)
        # 使用 pack 让容器居中，并添加一些垂直间距
        button_container.pack(pady=20)

        # Gemini 配置按钮
        self.gemini_config_button = ttk.Button(
            button_container,
            text="字典API配置",
            command=lambda: self.app.start_task('configure_gemini'), # 调用 App 的方法
            width=15
        )
        self.gemini_config_button.pack(side=tk.LEFT, padx=10)

        # Deepseek 配置按钮
        self.deepseek_config_button = ttk.Button(
            button_container,
            text="翻译API配置",
            command=lambda: self.app.start_task('configure_deepseek'), # 调用 App 的方法
            width=15
        )
        self.deepseek_config_button.pack(side=tk.LEFT, padx=10)

        # 开始翻译按钮
        self.start_button = ttk.Button(
            button_container,
            text="开始翻译",
            command=lambda: self.app.start_task('easy_flow', mode='easy'), # 调用 App 的方法，指定任务名和模式
            width=15
        )
        self.start_button.pack(side=tk.LEFT, padx=10)

        # 开始游戏按钮
        self.start_game_button = ttk.Button(
            button_container,
            text="开始游戏",
            command=lambda: self.app.start_task('start_game'), # 调用 App 的方法
            width=15
        )
        self.start_game_button.pack(side=tk.LEFT, padx=10)

        # 进度条
        self.progressbar = ttk.Progressbar(
            self,
            variable=self.progress_var,
            maximum=100,
            mode='determinate' # 'determinate' 或 'indeterminate'
        )
        self.progressbar.pack(fill=tk.X, padx=10, pady=(20, 5)) # 增加与按钮的间距

        # 状态标签
        self.status_label = ttk.Label(
            self,
            textvariable=self.status_var,
            anchor=tk.CENTER # 文本居中显示
        )
        self.status_label.pack(fill=tk.X, padx=10, pady=(0, 10))

        # --- 保存控件引用 ---
        self.controls = [
            self.gemini_config_button,
            self.deepseek_config_button,
            self.start_button,
            self.start_game_button,
            # Progressbar 和 Label 通常不需要禁用，但如果需要也可以加入
            # self.progressbar,
            # self.status_label
        ]

    # --- 公共方法 (供 MainWindow 或 App 调用) ---

    def get_controls(self):
        """返回此面板上的可交互控件列表。"""
        return self.controls

    def update_status(self, message):
        """更新状态标签的文本。"""
        self.status_var.set(message)

    def update_progress(self, value):
        """更新进度条的值 (0 到 100)。"""
        if 0 <= value <= 100:
            self.progress_var.set(value)
        else:
            # 可以处理异常值，例如设置为不确定模式
            # self.progressbar.config(mode='indeterminate')
            print(f"无效的进度值: {value}")


    # 可以添加其他方法，例如重置状态
    def reset_state(self):
        """重置进度条和状态标签到初始状态。"""
        self.progress_var.set(0.0)
        self.status_var.set("选择游戏目录后点击“开始翻译”")