# tests/test_message_broker.py
"""消息代理测试。"""

import queue

import pytest

from core.message_broker import MessageBroker
from core.models.enums import LogLevel


class TestMessageBroker:
    """MessageBroker 各便捷方法测试。"""

    def test_log_default_level(self, broker: MessageBroker, message_queue: queue.Queue) -> None:
        broker.log("hello")
        msg_type, payload = message_queue.get_nowait()
        assert msg_type == "log"
        assert payload == ("normal", "hello")

    def test_log_with_enum_level(self, broker: MessageBroker, message_queue: queue.Queue) -> None:
        broker.log("warn text", LogLevel.WARNING)
        msg_type, payload = message_queue.get_nowait()
        assert msg_type == "log"
        assert payload == ("warning", "warn text")

    def test_log_with_string_level(self, broker: MessageBroker, message_queue: queue.Queue) -> None:
        broker.log("err", "error")
        _, payload = message_queue.get_nowait()
        assert payload == ("error", "err")

    def test_status(self, broker: MessageBroker, message_queue: queue.Queue) -> None:
        broker.status("processing...")
        msg_type, payload = message_queue.get_nowait()
        assert msg_type == "status"
        assert payload == "processing..."

    def test_progress(self, broker: MessageBroker, message_queue: queue.Queue) -> None:
        broker.progress(42.5)
        msg_type, payload = message_queue.get_nowait()
        assert msg_type == "progress"
        assert payload == 42.5

    def test_easy_status(self, broker: MessageBroker, message_queue: queue.Queue) -> None:
        broker.easy_status("step 2")
        msg_type, payload = message_queue.get_nowait()
        assert msg_type == "easy_status"
        assert payload == "step 2"

    def test_success(self, broker: MessageBroker, message_queue: queue.Queue) -> None:
        broker.success("done!")
        msg_type, payload = message_queue.get_nowait()
        assert msg_type == "success"

    def test_error(self, broker: MessageBroker, message_queue: queue.Queue) -> None:
        broker.error("fail")
        msg_type, payload = message_queue.get_nowait()
        assert msg_type == "error"

    def test_warning(self, broker: MessageBroker, message_queue: queue.Queue) -> None:
        broker.warning("caution")
        msg_type, payload = message_queue.get_nowait()
        assert msg_type == "warning"

    def test_done_simple(self, broker: MessageBroker, message_queue: queue.Queue) -> None:
        broker.done()
        msg_type, payload = message_queue.get_nowait()
        assert msg_type == "done"
        assert payload is None

    def test_done_with_callback(self, broker: MessageBroker, message_queue: queue.Queue) -> None:
        broker.done(task_id="t1", success=True, message="ok")
        msg_type, payload = message_queue.get_nowait()
        assert msg_type == "done"
        assert payload == ("t1", (True, "ok"))

    def test_done_with_callback_no_message(self, broker: MessageBroker, message_queue: queue.Queue) -> None:
        broker.done(task_id="t2", success=False)
        _, payload = message_queue.get_nowait()
        assert payload == ("t2", (False, ""))

    def test_put_raw(self, broker: MessageBroker, message_queue: queue.Queue) -> None:
        broker.put_raw(("custom", {"key": "val"}))
        msg_type, payload = message_queue.get_nowait()
        assert msg_type == "custom"
        assert payload == {"key": "val"}

    def test_queue_property(self, broker: MessageBroker, message_queue: queue.Queue) -> None:
        assert broker.queue is message_queue

    # --- 错误标记 ---

    def test_error_sets_flag(self, broker: MessageBroker, message_queue: queue.Queue) -> None:
        assert not broker.has_error()
        broker.error("fail")
        assert broker.has_error()

    def test_reset_error_flag(self, broker: MessageBroker, message_queue: queue.Queue) -> None:
        broker.error("fail")
        broker.reset_error_flag()
        assert not broker.has_error()

    def test_has_error_default_false(self, broker: MessageBroker, message_queue: queue.Queue) -> None:
        assert not broker.has_error()
