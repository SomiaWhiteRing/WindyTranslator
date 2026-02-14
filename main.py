# main.py
"""WindyTranslator 入口点。"""

import json
import logging
import sys
import os
import datetime

from core.utils.file_system import get_application_path, get_executable_dir


class JsonLogFormatter(logging.Formatter):
    """结构化 JSON 日志格式化器。

    每条日志输出为单行 JSON 对象，包含时间戳、级别、模块、消息等字段，
    便于日志聚合工具解析和检索。
    """

    def format(self, record: logging.LogRecord) -> str:
        """将 LogRecord 格式化为 JSON 字符串。"""
        entry = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "funcName": record.funcName,
            "lineno": record.lineno,
        }
        if record.exc_info and record.exc_info[1] is not None:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def setup_logging() -> None:
    """配置全局日志记录。

    输出三路日志：
    - 控制台：人类可读的文本格式（INFO 级别）
    - 文本日志文件：人类可读格式（DEBUG 级别）
    - JSON 日志文件：结构化 JSON 格式（DEBUG 级别），便于工具解析
    """
    text_format = "%(asctime)s - %(levelname)s - %(name)s - %(message)s"
    log_level = logging.INFO

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(logging.Formatter(text_format))
    root_logger.addHandler(console_handler)

    log_dir = os.path.join(get_executable_dir(), "logs")
    if not os.path.exists(log_dir):
        try:
            os.makedirs(log_dir)
        except OSError:
            print(f"警告：无法创建日志目录 {log_dir}")
            log_dir = None

    if log_dir:
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

        # --- 文本日志文件 ---
        text_log_file = os.path.join(log_dir, f"app_{timestamp}.log")
        try:
            file_handler = logging.FileHandler(text_log_file, mode='w', encoding='utf-8')
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(logging.Formatter(text_format))
            root_logger.addHandler(file_handler)
            print(f"日志将记录到: {text_log_file}")
        except Exception as e:
            print(f"警告：无法配置日志文件处理器: {e}")

        # --- JSON 结构化日志文件 ---
        json_log_file = os.path.join(log_dir, f"app_{timestamp}.jsonl")
        try:
            json_handler = logging.FileHandler(json_log_file, mode='w', encoding='utf-8')
            json_handler.setLevel(logging.DEBUG)
            json_handler.setFormatter(JsonLogFormatter())
            root_logger.addHandler(json_handler)
            print(f"JSON 日志将记录到: {json_log_file}")
        except Exception as e:
            print(f"警告：无法配置 JSON 日志处理器: {e}")

    logging.info("日志系统已配置。")


if __name__ == "__main__":
    setup_logging()

    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtGui import QIcon
    from app import RPGTranslatorApp

    qt_app = QApplication(sys.argv)
    qt_app.setApplicationName("WindyTranslator")

    # 设置窗口图标
    try:
        icon_path = os.path.join(get_application_path(), "assets", "icon.ico")
        if os.path.exists(icon_path):
            qt_app.setWindowIcon(QIcon(icon_path))
    except Exception as e:
        logging.debug(f"设置窗口图标失败: {e}")

    try:
        app = RPGTranslatorApp()
    except Exception as e:
        logging.exception("初始化应用程序时发生致命错误。")
        from PyQt6.QtWidgets import QMessageBox
        QMessageBox.critical(None, "启动失败", f"应用程序初始化失败:\n{e}")
        sys.exit(1)

    app.main_window.show()

    logging.info("启动主事件循环...")
    try:
        exit_code = qt_app.exec()
    except KeyboardInterrupt:
        logging.info("收到退出信号 (KeyboardInterrupt)。")
        exit_code = 0
    finally:
        app.shutdown()
        logging.info("应用程序已退出。")

    sys.exit(exit_code)
