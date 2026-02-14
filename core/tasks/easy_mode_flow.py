# core/tasks/easy_mode_flow.py
"""轻松模式总控任务 — 使用流水线引擎按顺序执行翻译步骤。

通过 EasyFlowPipeline 替代硬编码循环，支持：
- 子任务失败检测（MessageBroker 错误标记）
- 检查点持久化（中断后从断点恢复）
- 可配置的步骤列表（易于扩展/重排）
"""

from __future__ import annotations

import os
import logging
from typing import TYPE_CHECKING

from . import (
    initialize, rename, export, json_creation,
    dict_generation, translate, json_release, import_task,
)
from .easy_pipeline import PipelineStep, CheckpointManager, EasyFlowPipeline
from core.utils import text_processing

if TYPE_CHECKING:
    from core.message_broker import MessageBroker

log = logging.getLogger(__name__)


def run_easy_flow(
    game_path: str,
    program_dir: str,
    works_dir: str,
    rtp_options: dict,
    export_encoding: str,
    import_encoding: str,
    world_dict_config: dict,
    translate_config: dict,
    rewrite_rtp_fix: bool,
    broker: MessageBroker,
) -> None:
    """按顺序执行轻松模式下的所有翻译步骤。

    函数签名与原版完全一致，保持 TaskDispatcher 兼容性。
    内部使用 EasyFlowPipeline 引擎替代硬编码循环。

    Args:
        game_path: 游戏路径。
        program_dir: 程序根目录。
        works_dir: Works 目录。
        rtp_options: RTP 选择。
        export_encoding: 导出编码。
        import_encoding: 导入编码。
        world_dict_config: Gemini 配置。
        translate_config: DeepSeek 配置。
        rewrite_rtp_fix: 是否进行 RTP 修正。
        broker: 类型化消息代理。
    """
    # --- 计算检查点目录 ---
    game_subfolder = (
        text_processing.sanitize_filename(os.path.basename(game_path))
        or "UntitledGame"
    )
    checkpoint_dir = os.path.join(works_dir, game_subfolder)

    # --- 释放 JSON 所需的路径（惰性求值） ---
    def _release_json_args() -> list:
        """构造释放 JSON 步骤的参数（延迟到执行时求值）。"""
        return [
            game_path,
            works_dir,
            os.path.join(
                works_dir,
                game_subfolder,
                "translated",
                "translation_translated.json",
            ),
            broker,
        ]

    # --- 释放/导入步骤的跳过条件 ---
    def _should_skip_release() -> bool:
        """检查翻译 JSON 是否存在，不存在则跳过释放和导入步骤。"""
        json_path = os.path.join(
            works_dir,
            game_subfolder,
            "translated",
            "translation_translated.json",
        )
        if not os.path.exists(json_path):
            broker.warning(
                f"未找到预期的翻译文件 "
                f"'{os.path.basename(json_path)}'，"
                f"将跳过释放和导入步骤。"
            )
            return True
        return False

    # --- 定义步骤列表 ---
    steps: list[PipelineStep] = [
        PipelineStep(
            step_id="initialize",
            display_name="初始化",
            func=initialize.run_initialize,
            args=[game_path, rtp_options, broker],
        ),
        PipelineStep(
            step_id="export",
            display_name="导出文本",
            func=export.run_export,
            args=[game_path, export_encoding, broker],
        ),
        PipelineStep(
            step_id="rename",
            display_name="重写文件名",
            func=rename.run_rename,
            args=[game_path, program_dir, rewrite_rtp_fix, broker],
        ),
        PipelineStep(
            step_id="create_json",
            display_name="制作JSON文件",
            func=json_creation.run_create_json,
            args=[game_path, works_dir, broker],
        ),
        PipelineStep(
            step_id="generate_dictionary",
            display_name="生成世界观字典",
            func=dict_generation.run_generate_dictionary,
            args=[game_path, works_dir, world_dict_config, broker],
        ),
        PipelineStep(
            step_id="translate",
            display_name="翻译JSON文件",
            func=translate.run_translate,
            args=[game_path, works_dir, translate_config, world_dict_config, broker],
        ),
        PipelineStep(
            step_id="release_json",
            display_name="释放JSON文件",
            func=json_release.run_release_json,
            args=_release_json_args,
            skip_condition=_should_skip_release,
        ),
        PipelineStep(
            step_id="import",
            display_name="导入文本",
            func=import_task.run_import,
            args=[game_path, import_encoding, broker],
            skip_condition=_should_skip_release,
        ),
    ]

    # --- 构建并执行流水线 ---
    checkpoint_mgr = CheckpointManager(checkpoint_dir)
    pipeline = EasyFlowPipeline(steps, checkpoint_mgr, broker)

    log.info("开始执行轻松模式翻译流程...")
    broker.status("轻松模式启动...")
    broker.easy_status("轻松模式启动...")

    try:
        pipeline.run()
    except Exception as e:
        log.exception("轻松模式流程发生意外错误。")
        broker.error(f"轻松模式流程发生严重错误: {e}")
        broker.status("轻松模式中止")
        broker.easy_status("轻松模式执行失败")
