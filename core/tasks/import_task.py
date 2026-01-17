# core/tasks/import_task.py
import os
import logging
from core.external import rpgrewriter
from core.utils.engine_detection import detect_game_engine

log = logging.getLogger(__name__)

# --- 主任务函数 ---
def run_import(game_path, import_encoding, message_queue):
    """
    执行将 StringScripts 文本导入回游戏文件的流程。

    Args:
        game_path (str): 游戏根目录路径。
        import_encoding (str): 导入时使用的写入编码代号 (如 "936")。
        message_queue (queue.Queue): 用于向主线程发送消息的队列。
    """
    try:
        detected = detect_game_engine(game_path)
        if detected and detected.engine == "vxace":
            message_queue.put(("status", "正在导入文本 (VX Ace)..."))
            message_queue.put(("log", ("normal", "步骤 7(VX Ace): 从 StringScripts 写回 rvdata2...")))
            try:
                from core.engines import vxace

                modified_files = vxace.import_from_string_scripts(game_path, message_queue)
                message_queue.put(("success", f"VX Ace 导入完成：更新了 {modified_files} 个数据文件。"))
                message_queue.put(("status", "文本导入完成(VX Ace)"))
            except Exception as e:
                log.exception("VX Ace 导入失败。")
                message_queue.put(("error", f"VX Ace 导入失败: {e}"))
                message_queue.put(("status", "导入文本失败"))
            finally:
                message_queue.put(("done", None))
            return

        message_queue.put(("status", f"正在导入文本 (编码: {import_encoding})..."))
        message_queue.put(("log", ("normal", f"步骤 7: 开始导入文本 (写入编码: {import_encoding})...")))

        lmt_path = os.path.join(game_path, "RPG_RT.lmt")
        string_scripts_path = os.path.join(game_path, "StringScripts")

        if not os.path.exists(lmt_path):
            message_queue.put(("error", f"未找到 RPG_RT.lmt 文件: {lmt_path}"))
            message_queue.put(("status", "导入文本失败"))
            message_queue.put(("done", None))
            return
        if not os.path.isdir(string_scripts_path):
            message_queue.put(("error", f"未找到 StringScripts 目录: {string_scripts_path}，无法导入。"))
            message_queue.put(("status", "导入文本失败"))
            message_queue.put(("done", None))
            return

        # 执行导入命令
        return_code, stdout, stderr = rpgrewriter.import_text_command(lmt_path, import_encoding)

        if return_code == 0:
            message_queue.put(("log", ("success", "RPGRewriter 导入命令成功完成。")))
            message_queue.put(("success", "文本已从 StringScripts 文件夹导入到游戏中。"))
            message_queue.put(("status", "文本导入完成"))
            message_queue.put(("done", None))
        else:
            message_queue.put(("error", f"文本导入失败 (RPGRewriter 退出码: {return_code})。"))
            if stderr:
                 message_queue.put(("log", ("error", f"RPGRewriter 错误信息: {stderr}")))
            # 检查 stdout 是否包含有用的信息，如 "Failures:"
            if stdout and "Failures:" in stdout:
                 message_queue.put(("log", ("error", f"RPGRewriter 输出包含导入失败信息，请检查其日志或输出。")))
            message_queue.put(("status", "文本导入失败"))
            message_queue.put(("done", None))

    except Exception as e:
        log.exception("导入文本任务执行期间发生意外错误。")
        message_queue.put(("error", f"导入文本过程中发生严重错误: {e}"))
        message_queue.put(("status", "导入文本失败"))
        message_queue.put(("done", None))
