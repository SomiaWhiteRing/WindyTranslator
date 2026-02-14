# core/tasks/easy_pipeline.py
"""轻松模式流水线引擎 — 状态机 + 检查点机制。

将硬编码的步骤序列替换为可配置的 PipelineStep 列表，
支持通过 CheckpointManager 持久化进度，实现中断后从断点恢复。
通过 MessageBroker._error_flag 检测子任务失败，零侵入现有子任务代码。
"""

from __future__ import annotations

import json
import os
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from core.message_broker import MessageBroker

log = logging.getLogger(__name__)


@dataclass
class PipelineStep:
    """流水线中的单个步骤定义。

    Attributes:
        step_id: 步骤唯一标识符（英文，用于检查点持久化）。
        display_name: 步骤显示名称（中文，用于 UI 展示）。
        func: 步骤执行函数。
        args: 位置参数列表，或返回参数列表的惰性工厂函数。
        skip_condition: 可选的跳过条件函数，返回 True 则跳过此步骤。
    """

    step_id: str
    display_name: str
    func: Callable[..., None]
    args: list[Any] | Callable[[], list[Any]]
    skip_condition: Callable[[], bool] | None = None


@dataclass
class CheckpointData:
    """检查点持久化数据。"""

    last_completed_step_index: int
    last_completed_step_id: str
    total_steps: int
    timestamp: str
    step_history: list[dict[str, str]] = field(default_factory=list)


class CheckpointManager:
    """检查点管理器 — 将流水线进度持久化到 JSON 文件。"""

    CHECKPOINT_FILENAME = "easy_flow_checkpoint.json"

    def __init__(self, checkpoint_dir: str) -> None:
        """初始化检查点管理器。

        Args:
            checkpoint_dir: 检查点文件所在目录（Works/{game_subfolder}/）。
        """
        self._checkpoint_path = os.path.join(
            checkpoint_dir, self.CHECKPOINT_FILENAME
        )

    @property
    def checkpoint_path(self) -> str:
        """检查点文件的完整路径。"""
        return self._checkpoint_path

    def load(self) -> CheckpointData | None:
        """加载检查点数据。如果文件不存在或格式无效，返回 None。"""
        if not os.path.exists(self._checkpoint_path):
            return None
        try:
            with open(self._checkpoint_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return CheckpointData(
                last_completed_step_index=data["last_completed_step_index"],
                last_completed_step_id=data["last_completed_step_id"],
                total_steps=data["total_steps"],
                timestamp=data["timestamp"],
                step_history=data.get("step_history", []),
            )
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            log.warning(f"检查点文件格式无效，将忽略: {e}")
            return None

    def save(
        self,
        step_index: int,
        step_id: str,
        total_steps: int,
        step_history: list[dict[str, str]],
    ) -> None:
        """保存检查点数据。"""
        data = {
            "last_completed_step_index": step_index,
            "last_completed_step_id": step_id,
            "total_steps": total_steps,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "step_history": step_history,
        }
        try:
            os.makedirs(os.path.dirname(self._checkpoint_path), exist_ok=True)
            with open(self._checkpoint_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            log.info(f"检查点已保存: 步骤 {step_index} ({step_id})")
        except Exception as e:
            log.error(f"保存检查点失败: {e}")

    def clear(self) -> None:
        """删除检查点文件（流水线全部成功完成后调用）。"""
        if os.path.exists(self._checkpoint_path):
            try:
                os.remove(self._checkpoint_path)
                log.info("检查点文件已清除。")
            except Exception as e:
                log.warning(f"清除检查点文件失败: {e}")

    def exists(self) -> bool:
        """检查点文件是否存在。"""
        return os.path.exists(self._checkpoint_path)


class EasyFlowPipeline:
    """轻松模式流水线执行引擎。

    线性执行一系列 PipelineStep，支持：
    - 通过 MessageBroker._error_flag 检测子任务失败
    - 检查点持久化，支持从失败步骤恢复
    - 进度报告（progress / easy_status / log）
    """

    def __init__(
        self,
        steps: list[PipelineStep],
        checkpoint_mgr: CheckpointManager,
        broker: MessageBroker,
    ) -> None:
        """初始化流水线。

        Args:
            steps: 步骤定义列表。
            checkpoint_mgr: 检查点管理器。
            broker: 类型化消息代理。
        """
        self._steps = steps
        self._checkpoint = checkpoint_mgr
        self._broker = broker
        self._step_history: list[dict[str, str]] = []

    def run(self) -> None:
        """执行流水线。从检查点恢复或从头开始。"""
        total = len(self._steps)
        start_index = self._resolve_start_index()

        if start_index > 0:
            self._broker.log(
                f"从检查点恢复：跳过已完成的前 {start_index} 个步骤，"
                f"从步骤 {start_index + 1}/{total} 开始。",
                "success",
            )
            self._broker.easy_status(
                f"从步骤 {start_index + 1}/{total} 恢复执行..."
            )

        for i in range(start_index, total):
            step = self._steps[i]
            step_num = i + 1

            # --- 检查跳过条件 ---
            if step.skip_condition and step.skip_condition():
                self._broker.log(
                    f"步骤 {step_num}/{total} '{step.display_name}' "
                    f"已跳过（条件不满足）。",
                    "warning",
                )
                self._record_step(step, "skipped")
                self._checkpoint.save(
                    i, step.step_id, total, self._step_history
                )
                continue

            # --- 执行步骤 ---
            self._broker.status(
                f"({step_num}/{total}) 正在执行: {step.display_name}..."
            )
            self._broker.easy_status(
                f"({step_num}/{total}) {step.display_name}"
            )
            self._broker.log(
                f"--- 轻松模式步骤 {step_num}/{total}: "
                f"{step.display_name} ---"
            )

            success = self._execute_step(step)

            if success:
                progress = (step_num / total) * 100
                self._broker.progress(progress)
                self._broker.log(
                    f"步骤 '{step.display_name}' 完成。", "success"
                )
                self._record_step(step, "completed")
                self._checkpoint.save(
                    i, step.step_id, total, self._step_history
                )
            else:
                self._broker.log(
                    f"步骤 '{step.display_name}' 失败，轻松模式中止。",
                    "error",
                )
                self._broker.easy_status(
                    f"在步骤 {step_num}/{total} "
                    f"'{step.display_name}' 处失败"
                )
                self._record_step(step, "failed")
                # 检查点指向最后成功的步骤
                if i > 0:
                    prev = self._steps[i - 1]
                    self._checkpoint.save(
                        i - 1, prev.step_id, total, self._step_history
                    )
                return  # 中止流水线，不清除检查点

        # --- 全部成功 ---
        self._checkpoint.clear()
        self._broker.progress(100.0)
        self._broker.success("轻松模式所有步骤已成功完成。")
        self._broker.easy_status("翻译流程完成！")
        self._broker.status("轻松模式翻译流程完成！")

    def _execute_step(self, step: PipelineStep) -> bool:
        """执行单个步骤，通过 broker 错误标记检测成功/失败。

        Returns:
            True 表示步骤成功，False 表示失败。
        """
        self._broker.reset_error_flag()

        # 解析参数（支持惰性工厂函数）
        args = step.args() if callable(step.args) else step.args

        try:
            step.func(*args)
        except Exception as e:
            log.exception(
                f"步骤 '{step.display_name}' 执行时抛出异常: {e}"
            )
            self._broker.error(
                f"步骤 '{step.display_name}' 发生严重错误: {e}"
            )
            return False

        # 检查子任务是否通过 broker.error() 报告了错误
        return not self._broker.has_error()

    def _resolve_start_index(self) -> int:
        """根据检查点确定起始步骤索引。"""
        checkpoint = self._checkpoint.load()
        if checkpoint is None:
            return 0

        # 验证检查点与当前步骤列表的兼容性
        if checkpoint.total_steps != len(self._steps):
            log.warning(
                f"检查点步骤数 ({checkpoint.total_steps}) 与当前 "
                f"({len(self._steps)}) 不匹配，从头开始。"
            )
            return 0

        # 验证 step_id 匹配
        idx = checkpoint.last_completed_step_index
        if idx < 0 or idx >= len(self._steps):
            log.warning(f"检查点索引 ({idx}) 超出范围，从头开始。")
            return 0

        expected_id = self._steps[idx].step_id
        if checkpoint.last_completed_step_id != expected_id:
            log.warning(
                f"检查点步骤 ID 不匹配 "
                f"('{checkpoint.last_completed_step_id}' vs "
                f"'{expected_id}')，从头开始。"
            )
            return 0

        # 恢复历史记录
        self._step_history = list(checkpoint.step_history)

        # 从下一个步骤开始
        return idx + 1

    def _record_step(self, step: PipelineStep, status: str) -> None:
        """记录步骤执行结果到历史。"""
        self._step_history.append({
            "step_id": step.step_id,
            "display_name": step.display_name,
            "status": status,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
