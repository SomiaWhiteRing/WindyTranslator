# tests/test_task_dispatcher.py
"""任务调度器测试。"""

import queue
import threading
import time

import pytest

from core.message_broker import MessageBroker
from core.models.enums import TaskName
from core.task_dispatcher import TaskDispatcher


class TestTaskDispatcher:
    """TaskDispatcher 核心功能测试。"""

    @pytest.fixture()
    def dispatcher(self, broker: MessageBroker) -> TaskDispatcher:
        d = TaskDispatcher(broker)
        # 注入一个简单的测试注册表，避免导入真实 task 模块
        d._task_registry = {}
        return d

    def _register_noop(self, dispatcher: TaskDispatcher, task_name: TaskName) -> list[bool]:
        """注册一个记录调用的空任务，返回调用记录列表。"""
        called: list[bool] = []

        def noop(*args: object, **kwargs: object) -> None:
            called.append(True)

        dispatcher._task_registry[task_name] = noop
        return called

    # --- is_processing ---

    def test_initial_not_processing(self, dispatcher: TaskDispatcher) -> None:
        assert dispatcher.is_processing is False

    # --- resolve_task_func ---

    def test_resolve_registered_task(self, dispatcher: TaskDispatcher) -> None:
        self._register_noop(dispatcher, TaskName.EXPORT)
        assert dispatcher.resolve_task_func(TaskName.EXPORT) is not None

    def test_resolve_unregistered_task(self, dispatcher: TaskDispatcher) -> None:
        assert dispatcher.resolve_task_func(TaskName.EXPORT) is None

    # --- dispatch ---

    def test_dispatch_runs_task(self, dispatcher: TaskDispatcher) -> None:
        called = self._register_noop(dispatcher, TaskName.EXPORT)
        ok = dispatcher.dispatch(TaskName.EXPORT, [])
        assert ok is True
        # 等待后台线程完成
        time.sleep(0.3)
        assert called == [True]
        assert dispatcher.is_processing is False

    def test_dispatch_rejects_when_busy(self, dispatcher: TaskDispatcher) -> None:
        """当前有任务运行时，新调度应返回 False。"""
        event = threading.Event()

        def blocking(*args: object, **kwargs: object) -> None:
            event.wait(timeout=2)

        dispatcher._task_registry[TaskName.EXPORT] = blocking
        dispatcher.dispatch(TaskName.EXPORT, [])
        assert dispatcher.is_processing is True

        # 第二次调度应被拒绝
        ok = dispatcher.dispatch(TaskName.EXPORT, [])
        assert ok is False

        event.set()  # 释放阻塞
        time.sleep(0.3)

    def test_dispatch_unregistered_returns_false(self, dispatcher: TaskDispatcher) -> None:
        ok = dispatcher.dispatch(TaskName.EXPORT, [])
        assert ok is False

    def test_dispatch_passes_args(self, dispatcher: TaskDispatcher) -> None:
        received: list[tuple] = []

        def capture(*args: object, **kwargs: object) -> None:
            received.append((args, kwargs))

        dispatcher._task_registry[TaskName.RENAME] = capture
        dispatcher.dispatch(TaskName.RENAME, ["a", "b"], {"x": 1})
        time.sleep(0.3)
        assert len(received) == 1
        assert received[0][0] == ("a", "b")
        assert received[0][1] == {"x": 1}

    def test_on_task_finished_callback(self, dispatcher: TaskDispatcher) -> None:
        finished: list[bool] = []
        dispatcher.set_on_task_finished(lambda: finished.append(True))
        self._register_noop(dispatcher, TaskName.EXPORT)
        dispatcher.dispatch(TaskName.EXPORT, [])
        time.sleep(0.3)
        assert finished == [True]

    # --- 编辑器回调 ---

    def test_editor_callback_lifecycle(self, dispatcher: TaskDispatcher) -> None:
        mock_editor = object()
        dispatcher.register_editor_callback("task_1", mock_editor)
        assert dispatcher.has_editor_callback("task_1") is True
        assert dispatcher.pop_editor_callback("task_1") is mock_editor
        assert dispatcher.has_editor_callback("task_1") is False

    def test_pop_nonexistent_callback(self, dispatcher: TaskDispatcher) -> None:
        assert dispatcher.pop_editor_callback("no_such") is None

    # --- 异常处理 ---

    def test_dispatch_handles_task_exception(
        self, dispatcher: TaskDispatcher, message_queue: queue.Queue
    ) -> None:
        """任务抛出异常时，调度器应捕获并通过 broker 发送错误消息。"""

        def failing(*args: object, **kwargs: object) -> None:
            raise RuntimeError("boom")

        dispatcher._task_registry[TaskName.IMPORT] = failing
        dispatcher.dispatch(TaskName.IMPORT, [])
        time.sleep(0.3)

        # 应该有错误消息在队列中
        messages = []
        while not message_queue.empty():
            messages.append(message_queue.get_nowait())
        error_msgs = [m for m in messages if m[0] == "error"]
        assert len(error_msgs) >= 1
        assert "boom" in error_msgs[0][1]
        assert dispatcher.is_processing is False
