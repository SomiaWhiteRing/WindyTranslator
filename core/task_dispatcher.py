# core/task_dispatcher.py
"""后台任务调度器。

用注册表模式替代 app.py 中 start_task() 的 15+ elif 分支，
将任务名称 → 可调用对象的映射集中管理。
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
from typing import Any, Callable, TYPE_CHECKING

from core.models.enums import TaskName
from core.message_broker import MessageBroker

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 任务注册表：TaskName → 可调用对象
# 仅包含真正需要后台线程执行的任务，UI 动作不在此注册
# ---------------------------------------------------------------------------

def _build_task_registry() -> dict[TaskName, Callable[..., None]]:
    """延迟导入并构建任务注册表，避免循环导入。"""
    from core.tasks import (
        initialize, rename, export, json_creation,
        dict_generation, translate, json_release, import_task,
        easy_mode_flow, apply_base_dictionary,
    )
    return {
        TaskName.INITIALIZE: initialize.run_initialize,
        TaskName.RENAME: rename.run_rename,
        TaskName.EXPORT: export.run_export,
        TaskName.CREATE_JSON: json_creation.run_create_json,
        TaskName.GENERATE_DICTIONARY: dict_generation.run_generate_dictionary,
        TaskName.TRANSLATE: translate.run_translate,
        TaskName.RELEASE_JSON: json_release.run_release_json,
        TaskName.IMPORT: import_task.run_import,
        TaskName.EASY_FLOW: easy_mode_flow.run_easy_flow,
        TaskName.APPLY_BASE_DICTIONARY: apply_base_dictionary.run_apply_base_dictionary,
    }


class TaskDispatcher:
    """后台任务调度器，封装线程管理和任务生命周期。

    职责：
    - 维护任务注册表（TaskName → 函数）
    - 在后台线程中执行任务
    - 管理 is_processing 状态
    - 处理编辑器回调注册表
    """

    def __init__(self, broker: MessageBroker) -> None:
        """初始化任务调度器。

        Args:
            broker: 类型化消息代理。
        """
        self._broker = broker
        self._is_processing = False
        self._current_thread: threading.Thread | None = None
        self._task_registry: dict[TaskName, Callable[..., None]] = _build_task_registry()
        self._editor_callbacks: dict[str, Any] = {}
        # 任务完成后的回调（由 AppController 设置，用于更新 UI 状态）
        self._on_task_finished: Callable[[], None] | None = None

    @property
    def is_processing(self) -> bool:
        """当前是否有后台任务在运行。"""
        return self._is_processing

    @property
    def task_registry(self) -> dict[TaskName, Callable[..., None]]:
        """任务注册表。"""
        return self._task_registry

    def set_on_task_finished(self, callback: Callable[[], None]) -> None:
        """设置任务完成后的回调。

        Args:
            callback: 无参回调函数，在任务线程结束后于主线程调用。
        """
        self._on_task_finished = callback

    def register_editor_callback(self, task_id: str, editor_instance: Any) -> None:
        """注册编辑器回调。

        Args:
            task_id: 唯一任务 ID。
            editor_instance: 编辑器窗口实例。
        """
        self._editor_callbacks[task_id] = editor_instance
        log.info(f"已注册编辑器回调, ID: {task_id}")

    def pop_editor_callback(self, task_id: str) -> Any | None:
        """弹出并返回编辑器回调实例。

        Args:
            task_id: 唯一任务 ID。

        Returns:
            编辑器实例，若不存在则返回 None。
        """
        return self._editor_callbacks.pop(task_id, None)

    def has_editor_callback(self, task_id: str) -> bool:
        """检查是否存在指定的编辑器回调。"""
        return task_id in self._editor_callbacks

    def resolve_task_func(self, task_name: TaskName) -> Callable[..., None] | None:
        """根据任务名称查找对应的任务函数。

        Args:
            task_name: 任务名称枚举。

        Returns:
            任务函数，若未注册则返回 None。
        """
        return self.task_registry.get(task_name)

    def dispatch(
        self,
        task_name: TaskName,
        args: list[Any],
        kwargs: dict[str, Any] | None = None,
        on_thread_done: Callable[[], None] | None = None,
    ) -> bool:
        """在后台线程中调度执行任务。

        Args:
            task_name: 任务名称枚举。
            args: 传递给任务函数的位置参数。
            kwargs: 传递给任务函数的关键字参数。
            on_thread_done: 线程结束后在主线程执行的额外回调（可选）。

        Returns:
            是否成功启动任务。False 表示当前有任务在运行或任务未注册。
        """
        if self._is_processing:
            log.warning(f"尝试调度任务 '{task_name}' 但当前有任务在运行。")
            return False

        task_func = self.resolve_task_func(task_name)
        if task_func is None:
            log.error(f"未注册的任务: {task_name}")
            return False

        kwargs = kwargs or {}
        task_id_for_callback = kwargs.get("task_id_for_callback")

        self._is_processing = True

        def wrapper() -> None:
            task_start_time = time.time()
            try:
                task_func(*args, **kwargs)
            except Exception as e:
                log.exception(f"任务 '{task_name}' 执行期间发生未捕获的严重错误。")
                self._broker.error(
                    f"任务 '{task_name}' 失败: {traceback.format_exc()}"
                )
                self._broker.status(f"{task_name} 执行失败")
                # 编辑器回调通过 broker.done() 通知，由主线程的 _on_done 处理
                if task_id_for_callback and task_id_for_callback in self._editor_callbacks:
                    error_msg = f"任务 '{task_name}' 执行时发生意外错误: {e}"
                    log.info(f"任务 {task_id_for_callback} (异常结束) 通过 done 信号通知编辑器。")
                    self._broker.done(task_id=task_id_for_callback, success=False, message=error_msg)
            finally:
                elapsed = time.time() - task_start_time
                log.info(
                    f"任务 '{task_name}' 线程执行完毕，耗时: {elapsed:.2f} 秒。"
                )
                self._is_processing = False
                self._current_thread = None
                # 触发完成回调
                if self._on_task_finished:
                    self._on_task_finished()
                if on_thread_done:
                    on_thread_done()

        self._current_thread = threading.Thread(target=wrapper, daemon=True)
        self._current_thread.start()
        log.info(f"任务 '{task_name}' 已在后台线程中启动。")
        return True
