# ui/signals.py
"""信号桥接，将 MessageBroker 的队列消息转换为 pyqtSignal。

替代 Tkinter 的 root.after(100, _process_messages) 轮询模式，
实现事件驱动消息传递。

架构：
    MessageBroker.put(msg)
      → QueuePollerThread 轮询队列
        → TaskSignalBridge.emit(信号)
          → MainWindow.on_xxx() 槽函数（UI 线程）
"""

from __future__ import annotations

import queue
import logging
from typing import Any

from PyQt6.QtCore import QObject, QThread, pyqtSignal

log = logging.getLogger(__name__)


class TaskSignalBridge(QObject):
    """类型化的 Qt 信号桥接器。

    每种消息类型对应一个 pyqtSignal，UI 层通过 connect() 订阅。
    信号在 UI 线程中被接收，无需手动 thread-safety 处理。
    """

    # --- 信号定义 ---
    log_received = pyqtSignal(str, str)        # (level, text)
    status_received = pyqtSignal(str)           # (text,)
    success_received = pyqtSignal(str)          # (text,)
    error_received = pyqtSignal(str)            # (text,)
    warning_received = pyqtSignal(str)          # (text,)
    progress_received = pyqtSignal(float)       # (value,)
    easy_status_received = pyqtSignal(str)      # (text,)
    done_received = pyqtSignal(object)          # (content,) — 可能是 None 或复杂元组
    task_finished = pyqtSignal()                # 任务线程结束，通知主线程恢复 UI 状态


class QueuePollerThread(QThread):
    """后台轮询线程，从 queue.Queue 读取消息并通过信号桥发射。

    相比 QTimer 轮询，QThread + queue.get(timeout) 的方式
    在空闲时几乎零 CPU 开销，消息到达时响应也更及时。
    """

    def __init__(
        self,
        message_queue: queue.Queue[Any],
        bridge: TaskSignalBridge,
        parent: QObject | None = None,
    ) -> None:
        """初始化轮询线程。

        Args:
            message_queue: 与 MessageBroker 共享的消息队列。
            bridge: 信号桥接器实例。
            parent: Qt 父对象。
        """
        super().__init__(parent)
        self._queue = message_queue
        self._bridge = bridge
        self._running = True

    def run(self) -> None:
        """线程主循环：阻塞式读取队列，解析消息类型并发射对应信号。"""
        while self._running:
            try:
                message = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue

            try:
                msg_type, content = message
                self._dispatch_signal(msg_type, content)
            except (ValueError, TypeError) as e:
                log.warning(f"无法解析消息格式: {message!r} — {e}")
            except Exception as e:
                log.exception(f"处理消息时出错: {e}")
            finally:
                self._queue.task_done()

    def _dispatch_signal(self, msg_type: str, content: Any) -> None:
        """根据消息类型发射对应的 Qt 信号。"""
        match msg_type:
            case "log":
                level, text = content
                self._bridge.log_received.emit(level, text)
            case "status":
                self._bridge.status_received.emit(content)
            case "success":
                self._bridge.success_received.emit(content)
            case "error":
                self._bridge.error_received.emit(content)
            case "warning":
                self._bridge.warning_received.emit(content)
            case "progress":
                self._bridge.progress_received.emit(float(content))
            case "easy_status":
                self._bridge.easy_status_received.emit(content)
            case "done":
                self._bridge.done_received.emit(content)
            case _:
                log.warning(f"未知的消息类型: {msg_type}")

    def stop(self) -> None:
        """请求线程停止。"""
        self._running = False
