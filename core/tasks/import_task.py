# core/tasks/import_task.py
from __future__ import annotations

import os
import logging
from typing import TYPE_CHECKING

from core.external import rpgrewriter
from core.utils.engine_detection import detect_game_engine

if TYPE_CHECKING:
    from core.message_broker import MessageBroker

log = logging.getLogger(__name__)

# --- 主任务函数 ---
def run_import(game_path: str, import_encoding: str, broker: MessageBroker) -> None:
    """执行将 StringScripts 文本导入回游戏文件的流程。"""
    try:
        detected = detect_game_engine(game_path)
        if detected and detected.engine == "vxace":
            broker.status("正在导入文本 (VX Ace)...")
            broker.log("步骤 7(VX Ace): 从 StringScripts 写回 rvdata2...")
            try:
                from core.engines import vxace

                modified_files = vxace.import_from_string_scripts(game_path, broker)
                broker.success(f"VX Ace 导入完成：更新了 {modified_files} 个数据文件。")
                broker.status("文本导入完成(VX Ace)")
            except Exception as e:
                log.exception("VX Ace 导入失败。")
                broker.error(f"VX Ace 导入失败: {e}")
                broker.status("导入文本失败")
            finally:
                broker.done()
            return

        broker.status(f"正在导入文本 (编码: {import_encoding})...")
        broker.log(f"步骤 7: 开始导入文本 (写入编码: {import_encoding})...")

        lmt_path = os.path.join(game_path, "RPG_RT.lmt")
        string_scripts_path = os.path.join(game_path, "StringScripts")

        if not os.path.exists(lmt_path):
            broker.error(f"未找到 RPG_RT.lmt 文件: {lmt_path}")
            broker.status("导入文本失败")
            broker.done()
            return
        if not os.path.isdir(string_scripts_path):
            broker.error(f"未找到 StringScripts 目录: {string_scripts_path}，无法导入。")
            broker.status("导入文本失败")
            broker.done()
            return

        # 执行导入命令
        return_code, stdout, stderr = rpgrewriter.import_text_command(lmt_path, import_encoding)

        if return_code == 0:
            broker.log("RPGRewriter 导入命令成功完成。", "success")
            broker.success("文本已从 StringScripts 文件夹导入到游戏中。")
            broker.status("文本导入完成")
            broker.done()
        else:
            broker.error(f"文本导入失败 (RPGRewriter 退出码: {return_code})。")
            if stderr:
                 broker.log(f"RPGRewriter 错误信息: {stderr}", "error")
            if stdout and "Failures:" in stdout:
                 broker.log("RPGRewriter 输出包含导入失败信息，请检查其日志或输出。", "error")
            broker.status("文本导入失败")
            broker.done()

    except Exception as e:
        log.exception("导入文本任务执行期间发生意外错误。")
        broker.error(f"导入文本过程中发生严重错误: {e}")
        broker.status("导入文本失败")
        broker.done()
