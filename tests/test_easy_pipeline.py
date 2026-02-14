# tests/test_easy_pipeline.py
"""轻松模式流水线引擎测试。"""

from __future__ import annotations

import json
import os
import queue

import pytest

from core.message_broker import MessageBroker
from core.tasks.easy_pipeline import (
    CheckpointData,
    CheckpointManager,
    EasyFlowPipeline,
    PipelineStep,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def message_queue() -> queue.Queue:
    return queue.Queue()


@pytest.fixture()
def broker(message_queue: queue.Queue) -> MessageBroker:
    return MessageBroker(message_queue)


@pytest.fixture()
def checkpoint_dir(tmp_path: os.PathLike) -> str:
    d = os.path.join(str(tmp_path), "Works", "TestGame")
    os.makedirs(d, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# TestCheckpointManager
# ---------------------------------------------------------------------------

class TestCheckpointManager:
    """检查点管理器测试。"""

    def test_load_no_file(self, checkpoint_dir: str) -> None:
        """文件不存在时返回 None。"""
        mgr = CheckpointManager(checkpoint_dir)
        assert mgr.load() is None

    def test_save_and_load(self, checkpoint_dir: str) -> None:
        """保存后能正确加载。"""
        mgr = CheckpointManager(checkpoint_dir)
        history = [{"step_id": "init", "display_name": "初始化", "status": "completed", "timestamp": "T"}]
        mgr.save(0, "init", 3, history)

        data = mgr.load()
        assert data is not None
        assert data.last_completed_step_index == 0
        assert data.last_completed_step_id == "init"
        assert data.total_steps == 3
        assert len(data.step_history) == 1

    def test_clear(self, checkpoint_dir: str) -> None:
        """清除后文件不存在。"""
        mgr = CheckpointManager(checkpoint_dir)
        mgr.save(0, "init", 3, [])
        assert mgr.exists()
        mgr.clear()
        assert not mgr.exists()

    def test_clear_no_file(self, checkpoint_dir: str) -> None:
        """清除不存在的文件不报错。"""
        mgr = CheckpointManager(checkpoint_dir)
        mgr.clear()  # 不应抛异常

    def test_exists(self, checkpoint_dir: str) -> None:
        """exists() 正确反映文件状态。"""
        mgr = CheckpointManager(checkpoint_dir)
        assert not mgr.exists()
        mgr.save(0, "init", 3, [])
        assert mgr.exists()

    def test_load_invalid_json(self, checkpoint_dir: str) -> None:
        """JSON 格式无效时返回 None。"""
        mgr = CheckpointManager(checkpoint_dir)
        with open(mgr.checkpoint_path, "w", encoding="utf-8") as f:
            f.write("not valid json{{{")
        assert mgr.load() is None

    def test_load_missing_keys(self, checkpoint_dir: str) -> None:
        """JSON 缺少必要字段时返回 None。"""
        mgr = CheckpointManager(checkpoint_dir)
        with open(mgr.checkpoint_path, "w", encoding="utf-8") as f:
            json.dump({"only_one_key": 1}, f)
        assert mgr.load() is None

    def test_checkpoint_path(self, checkpoint_dir: str) -> None:
        """检查点路径正确。"""
        mgr = CheckpointManager(checkpoint_dir)
        assert mgr.checkpoint_path == os.path.join(
            checkpoint_dir, "easy_flow_checkpoint.json"
        )


# ---------------------------------------------------------------------------
# 辅助：构造测试用步骤
# ---------------------------------------------------------------------------

def _make_step(
    step_id: str,
    display_name: str = "",
    func: object = None,
    args: list | None = None,
    skip_condition: object = None,
) -> PipelineStep:
    """构造测试用 PipelineStep。"""
    return PipelineStep(
        step_id=step_id,
        display_name=display_name or step_id,
        func=func or (lambda: None),
        args=args if args is not None else [],
        skip_condition=skip_condition,
    )


def _noop_step(broker: MessageBroker) -> None:
    """成功的空操作子任务（模拟 broker.done()）。"""
    broker.done()


def _failing_step(broker: MessageBroker) -> None:
    """失败的子任务。"""
    broker.error("模拟失败")
    broker.done()


def _exception_step(broker: MessageBroker) -> None:
    """抛出异常的子任务。"""
    raise RuntimeError("模拟异常")


# ---------------------------------------------------------------------------
# TestEasyFlowPipeline
# ---------------------------------------------------------------------------

class TestEasyFlowPipeline:
    """流水线执行引擎测试。"""

    def test_all_steps_succeed(self, broker: MessageBroker, checkpoint_dir: str) -> None:
        """所有步骤成功完成，检查点被清除。"""
        call_order: list[str] = []

        def step_a(b: MessageBroker) -> None:
            call_order.append("a")
            b.done()

        def step_b(b: MessageBroker) -> None:
            call_order.append("b")
            b.done()

        steps = [
            _make_step("a", "步骤A", func=step_a, args=[broker]),
            _make_step("b", "步骤B", func=step_b, args=[broker]),
        ]
        mgr = CheckpointManager(checkpoint_dir)
        pipeline = EasyFlowPipeline(steps, mgr, broker)
        pipeline.run()

        assert call_order == ["a", "b"]
        assert not mgr.exists()  # 全部成功后检查点被清除

    def test_step_failure_aborts(self, broker: MessageBroker, checkpoint_dir: str) -> None:
        """步骤失败时中止流水线，后续步骤不执行。"""
        call_order: list[str] = []

        def step_ok(b: MessageBroker) -> None:
            call_order.append("ok")
            b.done()

        def step_fail(b: MessageBroker) -> None:
            call_order.append("fail")
            b.error("出错了")
            b.done()

        def step_never(b: MessageBroker) -> None:
            call_order.append("never")
            b.done()

        steps = [
            _make_step("ok", func=step_ok, args=[broker]),
            _make_step("fail", func=step_fail, args=[broker]),
            _make_step("never", func=step_never, args=[broker]),
        ]
        mgr = CheckpointManager(checkpoint_dir)
        pipeline = EasyFlowPipeline(steps, mgr, broker)
        pipeline.run()

        assert call_order == ["ok", "fail"]
        # 检查点指向最后成功的步骤（索引 0）
        data = mgr.load()
        assert data is not None
        assert data.last_completed_step_index == 0
        assert data.last_completed_step_id == "ok"

    def test_first_step_failure(self, broker: MessageBroker, checkpoint_dir: str) -> None:
        """第一步就失败时，不保存检查点（无成功步骤）。"""
        steps = [
            _make_step("fail", func=_failing_step, args=[broker]),
            _make_step("never", func=_noop_step, args=[broker]),
        ]
        mgr = CheckpointManager(checkpoint_dir)
        pipeline = EasyFlowPipeline(steps, mgr, broker)
        pipeline.run()

        # 第一步失败，i=0，i>0 为 False，不保存检查点
        assert not mgr.exists()

    def test_exception_in_step(self, broker: MessageBroker, checkpoint_dir: str) -> None:
        """步骤抛出异常时被捕获，视为失败。"""
        call_order: list[str] = []

        def step_after(b: MessageBroker) -> None:
            call_order.append("after")
            b.done()

        steps = [
            _make_step("boom", func=_exception_step, args=[broker]),
            _make_step("after", func=step_after, args=[broker]),
        ]
        mgr = CheckpointManager(checkpoint_dir)
        pipeline = EasyFlowPipeline(steps, mgr, broker)
        pipeline.run()

        assert call_order == []  # 异常步骤后中止

    def test_checkpoint_resume(self, broker: MessageBroker, checkpoint_dir: str) -> None:
        """从检查点恢复，跳过已完成的步骤。"""
        call_order: list[str] = []

        def step_a(b: MessageBroker) -> None:
            call_order.append("a")
            b.done()

        def step_b(b: MessageBroker) -> None:
            call_order.append("b")
            b.done()

        def step_c(b: MessageBroker) -> None:
            call_order.append("c")
            b.done()

        steps = [
            _make_step("a", func=step_a, args=[broker]),
            _make_step("b", func=step_b, args=[broker]),
            _make_step("c", func=step_c, args=[broker]),
        ]

        # 手动写入检查点：步骤 a（索引 0）已完成
        mgr = CheckpointManager(checkpoint_dir)
        mgr.save(0, "a", 3, [{"step_id": "a", "display_name": "a", "status": "completed", "timestamp": "T"}])

        pipeline = EasyFlowPipeline(steps, mgr, broker)
        pipeline.run()

        # 应从步骤 b 开始
        assert call_order == ["b", "c"]
        assert not mgr.exists()  # 全部成功后清除

    def test_checkpoint_mismatch_total_steps(self, broker: MessageBroker, checkpoint_dir: str) -> None:
        """检查点步骤数不匹配时从头开始。"""
        call_order: list[str] = []

        def step_a(b: MessageBroker) -> None:
            call_order.append("a")
            b.done()

        steps = [_make_step("a", func=step_a, args=[broker])]

        # 检查点记录了 5 步，但当前只有 1 步
        mgr = CheckpointManager(checkpoint_dir)
        mgr.save(2, "x", 5, [])

        pipeline = EasyFlowPipeline(steps, mgr, broker)
        pipeline.run()

        assert call_order == ["a"]  # 从头开始

    def test_checkpoint_mismatch_step_id(self, broker: MessageBroker, checkpoint_dir: str) -> None:
        """检查点 step_id 不匹配时从头开始。"""
        call_order: list[str] = []

        def step_a(b: MessageBroker) -> None:
            call_order.append("a")
            b.done()

        def step_b(b: MessageBroker) -> None:
            call_order.append("b")
            b.done()

        steps = [
            _make_step("a", func=step_a, args=[broker]),
            _make_step("b", func=step_b, args=[broker]),
        ]

        # 检查点说索引 0 是 "wrong_id"，但实际是 "a"
        mgr = CheckpointManager(checkpoint_dir)
        mgr.save(0, "wrong_id", 2, [])

        pipeline = EasyFlowPipeline(steps, mgr, broker)
        pipeline.run()

        assert call_order == ["a", "b"]  # 从头开始

    def test_skip_condition(self, broker: MessageBroker, checkpoint_dir: str) -> None:
        """跳过条件为 True 时跳过步骤。"""
        call_order: list[str] = []

        def step_a(b: MessageBroker) -> None:
            call_order.append("a")
            b.done()

        def step_skipped(b: MessageBroker) -> None:
            call_order.append("skipped")
            b.done()

        def step_c(b: MessageBroker) -> None:
            call_order.append("c")
            b.done()

        steps = [
            _make_step("a", func=step_a, args=[broker]),
            _make_step("skip_me", "跳过步骤", func=step_skipped, args=[broker],
                       skip_condition=lambda: True),
            _make_step("c", func=step_c, args=[broker]),
        ]
        mgr = CheckpointManager(checkpoint_dir)
        pipeline = EasyFlowPipeline(steps, mgr, broker)
        pipeline.run()

        assert call_order == ["a", "c"]

    def test_lazy_args(self, broker: MessageBroker, checkpoint_dir: str) -> None:
        """惰性参数工厂在执行时求值。"""
        call_order: list[str] = []
        lazy_called = [False]

        def step_with_lazy(val: str, b: MessageBroker) -> None:
            call_order.append(val)
            b.done()

        def lazy_factory() -> list:
            lazy_called[0] = True
            return ["lazy_value", broker]

        steps = [
            _make_step("lazy", func=step_with_lazy, args=lazy_factory),
        ]
        assert not lazy_called[0]  # 构造时未调用

        mgr = CheckpointManager(checkpoint_dir)
        pipeline = EasyFlowPipeline(steps, mgr, broker)
        pipeline.run()

        assert lazy_called[0]  # 执行时调用
        assert call_order == ["lazy_value"]

    def test_progress_reporting(self, broker: MessageBroker, message_queue: queue.Queue, checkpoint_dir: str) -> None:
        """验证进度报告消息。"""
        steps = [
            _make_step("a", func=_noop_step, args=[broker]),
            _make_step("b", func=_noop_step, args=[broker]),
        ]
        mgr = CheckpointManager(checkpoint_dir)
        pipeline = EasyFlowPipeline(steps, mgr, broker)
        pipeline.run()

        # 收集所有 progress 消息
        progress_values: list[float] = []
        while not message_queue.empty():
            msg_type, content = message_queue.get_nowait()
            if msg_type == "progress":
                progress_values.append(content)

        assert 50.0 in progress_values  # 步骤 1/2
        assert 100.0 in progress_values  # 步骤 2/2 或最终

    def test_empty_pipeline(self, broker: MessageBroker, checkpoint_dir: str) -> None:
        """空步骤列表直接完成。"""
        mgr = CheckpointManager(checkpoint_dir)
        pipeline = EasyFlowPipeline([], mgr, broker)
        pipeline.run()  # 不应抛异常
        assert not mgr.exists()


# ---------------------------------------------------------------------------
# TestMessageBrokerErrorFlag
# ---------------------------------------------------------------------------

class TestMessageBrokerErrorFlag:
    """MessageBroker 错误标记测试。"""

    def test_has_error_default_false(self, broker: MessageBroker) -> None:
        """初始状态无错误。"""
        assert not broker.has_error()

    def test_error_sets_flag(self, broker: MessageBroker) -> None:
        """调用 error() 后标记为 True。"""
        broker.error("test error")
        assert broker.has_error()

    def test_reset_error_flag(self, broker: MessageBroker) -> None:
        """重置后标记恢复为 False。"""
        broker.error("test error")
        assert broker.has_error()
        broker.reset_error_flag()
        assert not broker.has_error()

    def test_multiple_errors(self, broker: MessageBroker) -> None:
        """多次 error() 后标记仍为 True。"""
        broker.error("error 1")
        broker.error("error 2")
        assert broker.has_error()

    def test_reset_then_error(self, broker: MessageBroker) -> None:
        """重置后再次 error() 能正确设置。"""
        broker.error("first")
        broker.reset_error_flag()
        assert not broker.has_error()
        broker.error("second")
        assert broker.has_error()
