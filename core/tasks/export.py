# core/tasks/export.py
import os
import re
import shutil
import logging
import time # 用于短暂等待
from core.external import rpgrewriter
from core.utils import file_system

log = logging.getLogger(__name__)

# --- 导出文本任务 ---
def run_export(game_path, export_encoding, message_queue):
    """
    执行导出文本到 StringScripts 文件夹的流程。
    包含处理 RPGRewriter 导出失败时移动问题地图文件的逻辑。

    Args:
        game_path (str): 游戏根目录路径。
        export_encoding (str): 导出时使用的读取编码代号 (如 "932")。
        message_queue (queue.Queue): 用于向主线程发送消息的队列。
    """
    try:
        message_queue.put(("status", f"正在导出文本 (编码: {export_encoding})..."))
        message_queue.put(("log", ("normal", f"步骤 2: 开始导出文本 (读取编码: {export_encoding})...")))

        lmt_path = os.path.join(game_path, "RPG_RT.lmt")
        if not os.path.exists(lmt_path):
            message_queue.put(("error", f"未找到 RPG_RT.lmt 文件: {lmt_path}"))
            message_queue.put(("status", "导出文本失败"))
            message_queue.put(("done", None))
            return

        # 临时目录用于存放导出过程中出错的地图文件
        temp_problem_dir = os.path.join(game_path, "_temp_problem_files")
        # 先尝试清理可能残留的临时目录
        if os.path.exists(temp_problem_dir):
            log.warning(f"发现残留的临时问题文件目录，将尝试清理: {temp_problem_dir}")
            if not file_system.safe_remove(temp_problem_dir):
                 message_queue.put(("error", f"无法清理残留的临时目录: {temp_problem_dir}，导出可能受影响。"))
                 # 可以选择停止或继续
        # 创建新的临时目录
        if not file_system.ensure_dir_exists(temp_problem_dir):
             message_queue.put(("error", f"无法创建临时目录: {temp_problem_dir}"))
             message_queue.put(("status", "导出文本失败"))
             message_queue.put(("done", None))
             return

        problem_files = [] # 记录移动到临时目录的文件名
        export_successful = False
        max_attempts = 50 # 最多尝试移动 50 个问题文件
        attempts = 0

        while not export_successful and attempts < max_attempts:
            attempts += 1
            message_queue.put(("log", ("normal", f"导出尝试 #{attempts}/{max_attempts}...")))

            # 执行导出命令
            return_code, stdout, stderr = rpgrewriter.export_text_command(lmt_path, export_encoding)

            if return_code == 0:
                export_successful = True
                message_queue.put(("log", ("success", "RPGRewriter 导出命令成功完成！")))
                break # 成功则跳出循环

            else:
                message_queue.put(("log", ("error", f"RPGRewriter 导出命令失败 (退出码: {return_code})。")))
                if stderr: message_queue.put(("log", ("error", f"错误信息: {stderr}")))

                # 检查是否是已知的可恢复错误 (如 IndexOutOfRange)
                # 注意：错误信息文本可能随 RPGRewriter 版本变化
                if "IndexOutOfRange" in stderr or "OutOfRange" in stderr or "Index was outside the bounds" in stderr:
                    # 尝试从 stdout 中提取最后处理的地图文件
                    map_pattern = r"Extracting\s+(Map\d+\.lmu)" # 增加对空格的处理
                    maps_found = re.findall(map_pattern, stdout)

                    if maps_found:
                        # 最后一个匹配到的地图文件通常是出问题的那个
                        problem_map_name = maps_found[-1]
                        problem_map_path = os.path.join(game_path, problem_map_name)
                        target_move_path = os.path.join(temp_problem_dir, problem_map_name)

                        if os.path.exists(problem_map_path):
                            log.warning(f"检测到潜在问题文件: {problem_map_name} (基于 RPGRewriter 输出和错误)。")
                            if file_system.safe_move(problem_map_path, target_move_path):
                                message_queue.put(("log", ("warning", f"已将问题文件 {problem_map_name} 暂时移至: {temp_problem_dir}，将重试导出。")))
                                if problem_map_name not in problem_files:
                                    problem_files.append(problem_map_name)
                                time.sleep(0.1) # 短暂等待，可能有助于文件系统同步
                                continue # 继续循环，重试导出
                            else:
                                message_queue.put(("error", f"移动问题文件 {problem_map_name} 失败，停止导出。"))
                                break # 移动失败，无法继续
                        else:
                            # 理论上 RPGRewriter 报告正在处理的文件应该存在
                            log.error(f"RPGRewriter 报告了问题文件 {problem_map_name}，但在游戏目录中找不到它。")
                            message_queue.put(("error", f"无法定位问题文件 {problem_map_name}，停止导出。"))
                            break
                    else:
                        # 无法从输出确定是哪个文件
                        message_queue.put(("error", "导出失败，但无法从 RPGRewriter 输出确定具体问题文件，停止尝试。"))
                        break
                else:
                    # 未知错误，停止尝试
                    message_queue.put(("error", "导出失败，遇到未知或无法恢复的 RPGRewriter 错误，停止尝试。"))
                    break

        # --- 导出循环结束 ---
        string_scripts_path = os.path.join(game_path, "StringScripts")
        export_final_status = "失败"

        if export_successful:
            if os.path.exists(string_scripts_path):
                try:
                    # 统计文件数量
                    file_count = sum(len(files) for _, _, files in os.walk(string_scripts_path))
                    message_queue.put(("log", ("success", f"文本导出成功完成，生成 StringScripts 目录，共 {file_count} 个文件。")))
                    export_final_status = "成功"
                    if problem_files:
                         export_final_status = "部分成功"
                         message_queue.put(("log", ("warning", f"有 {len(problem_files)} 个地图文件在导出过程中被暂时移出: {', '.join(problem_files)}")))
                         message_queue.put(("success", f"文本导出部分完成。共 {file_count} 个文件。有 {len(problem_files)} 个地图文件未能导出。"))
                    else:
                        message_queue.put(("success", "文本导出成功完成。"))

                except Exception as count_err:
                    log.error(f"统计 StringScripts 文件数量时出错: {count_err}")
                    message_queue.put(("log", ("warning", "文本导出命令成功，但统计结果文件时出错。")))
                    # 即使统计失败，导出本身可能还是成功的
                    export_final_status = "可能成功(统计失败)"
                    message_queue.put(("success", "文本导出过程已完成（结果文件统计失败）。"))
            else:
                 message_queue.put(("error", "RPGRewriter 命令返回成功，但未找到 StringScripts 目录。"))
                 export_final_status = "失败(目录未生成)"
        else:
             message_queue.put(("error", "文本导出未能成功完成。"))
             export_final_status = "失败"

        # --- 清理：移回问题文件 ---
        if problem_files:
             message_queue.put(("log", ("normal", "正在将导出的问题文件移回原位...")))
             moved_back_count = 0
             move_back_failed = []
             for filename in problem_files:
                 source = os.path.join(temp_problem_dir, filename)
                 destination = os.path.join(game_path, filename)
                 if os.path.exists(source):
                     if file_system.safe_move(source, destination):
                         moved_back_count += 1
                     else:
                         move_back_failed.append(filename)
                         log.error(f"移回问题文件失败: {filename}")
                 else:
                     log.warning(f"尝试移回问题文件，但源文件不存在: {source}")

             message_queue.put(("log", ("normal", f"已尝试移回 {len(problem_files)} 个文件，成功 {moved_back_count} 个。")))
             if move_back_failed:
                  message_queue.put(("error", f"以下文件移回失败: {', '.join(move_back_failed)}"))

        # --- 清理：删除空的临时目录 ---
        try:
             if os.path.exists(temp_problem_dir) and not os.listdir(temp_problem_dir):
                 file_system.safe_remove(temp_problem_dir)
                 message_queue.put(("log", ("normal", "已清理空的临时问题文件目录。")))
             elif os.path.exists(temp_problem_dir):
                 # 如果目录非空（移回失败或其他原因），保留它供用户检查
                 message_queue.put(("log", ("warning", f"临时问题文件目录非空，已保留: {temp_problem_dir}")))
        except Exception as rmdir_err:
             log.error(f"清理临时目录时出错: {temp_problem_dir} - {rmdir_err}")

        message_queue.put(("status", f"文本导出{export_final_status}"))
        message_queue.put(("done", None))

    except Exception as e:
        log.exception("导出文本任务执行期间发生意外错误。")
        message_queue.put(("error", f"导出文本过程中发生严重错误: {e}"))
        message_queue.put(("status", "导出文本失败"))
        message_queue.put(("done", None))
        # 尝试在异常情况下也清理临时文件
        try:
            temp_dir = os.path.join(game_path, "_temp_problem_files")
            if os.path.exists(temp_dir):
                log.info("尝试在异常处理中清理临时文件...")
                # 简单起见，直接删除整个目录，不再尝试移回
                file_system.safe_remove(temp_dir)
        except Exception as final_cleanup_err:
            log.error(f"异常处理中清理临时目录失败: {final_cleanup_err}")