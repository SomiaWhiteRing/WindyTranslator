# main.py
import tkinter as tk
from tkinter import ttk, messagebox
import logging
import sys
import os
import datetime

# 导入主应用程序类
from app import RPGTranslatorApp

def setup_logging():
    """配置全局日志记录。"""
    log_format = "%(asctime)s - %(levelname)s - %(name)s - %(message)s"
    log_level = logging.INFO # 默认级别，可以设为 DEBUG 获取更详细信息
    # log_level = logging.DEBUG

    # 获取根 logger
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # 清除可能存在的默认处理器 (例如某些库会自动添加)
    # for handler in root_logger.handlers[:]:
    #     root_logger.removeHandler(handler)

    # 创建控制台处理器
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    console_formatter = logging.Formatter(log_format)
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)

    # (可选) 创建文件处理器，将日志写入文件
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    if not os.path.exists(log_dir):
        try:
            os.makedirs(log_dir)
        except OSError:
            print(f"警告：无法创建日志目录 {log_dir}")
            log_dir = None # 创建失败则不记录到文件

    if log_dir:
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        log_file = os.path.join(log_dir, f"app_{timestamp}.log")
        try:
            file_handler = logging.FileHandler(log_file, mode='w', encoding='utf-8') # 追加模式
            file_handler.setLevel(logging.DEBUG) # 文件记录更详细的 DEBUG 级别
            file_formatter = logging.Formatter(log_format)
            file_handler.setFormatter(file_formatter)
            root_logger.addHandler(file_handler)
            print(f"日志将记录到: {log_file}")
        except Exception as e:
            print(f"警告：无法配置日志文件处理器: {e}")

    logging.info("日志系统已配置。")


if __name__ == "__main__":
    # 配置日志
    setup_logging()

    # 创建 Tkinter 根窗口
    root = tk.Tk()

    # (可选) 设置主题样式
    # 需要确保 'Breeze' 主题已安装 (例如通过 pip install ttkthemes)
    # 或者使用 Tkinter 自带的主题 'clam', 'alt', 'default', 'classic'
    style = ttk.Style(root)
    available_themes = style.theme_names()
    logging.debug(f"可用主题: {available_themes}")
    preferred_themes = ['breeze', 'clam', 'vista', 'xpnative'] # 尝试使用的主题顺序
    selected_theme = 'default' # 默认主题
    for theme in preferred_themes:
        if theme in available_themes:
            try:
                style.theme_use(theme)
                selected_theme = theme
                logging.info(f"使用主题: {selected_theme}")
                break
            except tk.TclError:
                logging.warning(f"尝试使用主题 '{theme}' 失败。")
                continue
    if selected_theme == 'default':
         logging.info("使用默认 Tkinter 主题。")


    # 实例化主应用程序
    # 将 root 传递给 App
    try:
        app = RPGTranslatorApp(root)
    except Exception as e:
        logging.exception("初始化应用程序时发生致命错误。")
        # 尝试用 messagebox 显示错误，如果 Tkinter 还可用的话
        try:
            messagebox.showerror("启动失败", f"应用程序初始化失败:\n{e}")
        except:
            pass # 如果连 messagebox 都无法显示，也没办法了
        sys.exit(1) # 退出程序

    # 启动 Tkinter 事件循环
    logging.info("启动 Tkinter 主事件循环...")
    try:
        root.mainloop()
    except KeyboardInterrupt:
        logging.info("收到退出信号 (KeyboardInterrupt)。")
        # 可以在这里调用 app 的清理方法（如果需要）
        # if 'app' in locals() and hasattr(app, '_on_close'):
        #    app._on_close() # 手动触发关闭处理
    finally:
        logging.info("应用程序已退出。")