# core/message_broker.py
"""类型化的消息代理，封装 task → UI 的通信。

包装 queue.Queue，提供类型安全的便捷方法，替代裸元组协议。
同时保留 put_raw() 用于向后兼容，允许渐进式迁移。
"""

from __future__ import annotations

import queue
from typing import Any

from core.models.enums import LogLevel


class MessageBroker:
    """后台任务与 UI 之间的类型化消息代理。

    每个便捷方法对应一种消息类型，内部仍通过 queue.Queue
    发送元组消息，确保与现有 app.py 的 _process_messages() 兼容。
    """

    def __init__(self, message_queue: queue.Queue[Any]) -> None:
        """初始化消息代理。

        Args:
            message_queue: 底层的消息队列（与 app.py 共享）。
        """
        self._queue = message_queue
        self._error_flag: bool = False

    # ------------------------------------------------------------------
    # 类型化便捷方法
    # ------------------------------------------------------------------

    def log(self, text: str, level: LogLevel | str = LogLevel.NORMAL) -> None:
        """发送日志消息。

        Args:
            text: 日志文本。
            level: 日志级别（LogLevel 枚举或字符串）。
        """
        level_str = level.value if isinstance(level, LogLevel) else level
        self._queue.put(("log", (level_str, text)))

    def status(self, text: str) -> None:
        """更新状态栏文本。"""
        self._queue.put(("status", text))

    def progress(self, value: float) -> None:
        """更新进度条（轻松模式）。

        Args:
            value: 进度值（0.0 ~ 100.0）。
        """
        self._queue.put(("progress", value))

    def easy_status(self, text: str) -> None:
        """更新轻松模式状态标签。"""
        self._queue.put(("easy_status", text))

    def success(self, text: str) -> None:
        """发送成功消息。"""
        self._queue.put(("success", text))

    def error(self, text: str) -> None:
        """发送错误消息。同时设置错误标记，供流水线引擎检测子任务失败。"""
        self._error_flag = True
        self._queue.put(("error", text))

    def has_error(self) -> bool:
        """检查自上次重置以来是否有错误发生。"""
        return self._error_flag

    def reset_error_flag(self) -> None:
        """重置错误标记（在每个子步骤执行前调用）。"""
        self._error_flag = False

    def warning(self, text: str) -> None:
        """发送警告消息（顶层，非 log 子类型）。"""
        self._queue.put(("warning", text))

    def done(
        self,
        task_id: str | None = None,
        success: bool | None = None,
        message: str | None = None,
    ) -> None:
        """发送任务完成信号。

        Args:
            task_id: 回调任务 ID（用于编辑器回调场景）。
            success: 任务是否成功。
            message: 回调消息。
        """
        if task_id is not None and success is not None:
            self._queue.put(("done", (task_id, (success, message or ""))))
        else:
            self._queue.put(("done", None))

    # ------------------------------------------------------------------
    # 向后兼容
    # ------------------------------------------------------------------

    def put_raw(self, msg_tuple: tuple[str, Any]) -> None:
        """直接发送原始元组消息（向后兼容）。

        用于尚未迁移的代码，或需要发送自定义消息格式的场景。
        """
        self._queue.put(msg_tuple)

    @property
    def queue(self) -> queue.Queue[Any]:
        """访问底层队列（向后兼容，供 app.py 的 _process_messages 使用）。"""
        return self._queue
