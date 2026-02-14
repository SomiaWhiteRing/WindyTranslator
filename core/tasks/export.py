# core/tasks/export.py
from __future__ import annotations

import os
import re
import shutil
import logging
import time
from typing import TYPE_CHECKING

from core.external import rpgrewriter
from core.utils import file_system
from core.utils.engine_detection import detect_game_engine

if TYPE_CHECKING:
    from core.message_broker import MessageBroker

log = logging.getLogger(__name__)

# --- 导出文本任务 ---
def run_export(game_path: str, export_encoding: str, broker: MessageBroker) -> None:
    """执行导出文本到 StringScripts 文件夹的流程。"""
    try:
        detected = detect_game_engine(game_path)
        if detected and detected.engine == "vxace":
            broker.status("正在导出文本 (VX Ace)...")
            broker.log("步骤 1(VX Ace): 导出文本到 StringScripts...")
            try:
                from core.engines import vxace

                vxace.export_to_string_scripts(game_path, broker)
                broker.success("VX Ace 文本导出完成。")
                broker.status("文本导出完成(VX Ace)")
            except Exception as e:
                log.exception("VX Ace 导出失败。")
                broker.error(f"VX Ace 导出失败: {e}")
                broker.status("导出文本失败")
            finally:
                broker.done()
            return

        broker.status(f"正在导出文本 (编码: {export_encoding})...")
        broker.log(f"步骤 1: 开始导出文本 (读取编码: {export_encoding})...")

        lmt_path = os.path.join(game_path, "RPG_RT.lmt")
        if not os.path.exists(lmt_path):
            broker.error(f"未找到 RPG_RT.lmt 文件: {lmt_path}")
            broker.status("导出文本失败")
            broker.done()
            return

        # 临时目录用于存放导出过程中出错的地图文件
        temp_problem_dir = os.path.join(game_path, "_temp_problem_files")
        # 先尝试清理可能残留的临时目录
        if os.path.exists(temp_problem_dir):
            log.warning(f"发现残留的临时问题文件目录，将尝试清理: {temp_problem_dir}")
            if not file_system.safe_remove(temp_problem_dir):
                 broker.error(f"无法清理残留的临时目录: {temp_problem_dir}，导出可能受影响。")
        # 创建新的临时目录
        if not file_system.ensure_dir_exists(temp_problem_dir):
             broker.error(f"无法创建临时目录: {temp_problem_dir}")
             broker.status("导出文本失败")
             broker.done()
             return

        problem_files = []
        export_successful = False
        max_attempts = 50
        attempts = 0

        while not export_successful and attempts < max_attempts:
            attempts += 1
            broker.log(f"导出尝试 #{attempts}/{max_attempts}...")

            string_scripts_path_cleanup = os.path.join(game_path, "StringScripts")
            if os.path.exists(string_scripts_path_cleanup):
                log.info(f"尝试导出前清理旧的 StringScripts 目录: {string_scripts_path_cleanup}")
                if not file_system.safe_remove(string_scripts_path_cleanup):
                    broker.warning(f"清理旧的 StringScripts 目录失败，导出可能包含旧文件: {string_scripts_path_cleanup}")
                else:
                    log.info("旧的 StringScripts 目录已清理。")

            return_code, stdout, stderr = rpgrewriter.export_text_command(lmt_path, export_encoding)

            if return_code == 0:
                export_successful = True
                broker.log("RPGRewriter 导出命令成功完成！", "success")
                break

            else:
                broker.log(f"RPGRewriter 导出命令失败 (退出码: {return_code})。", "error")
                if stderr: broker.log(f"错误信息: {stderr}", "error")

                if "IndexOutOfRange" in stderr or "OutOfRange" in stderr or "Index was outside the bounds" in stderr:
                    map_pattern = r"Extracting\s+(Map\d+\.lmu)"
                    maps_found = re.findall(map_pattern, stdout)

                    if maps_found:
                        problem_map_name = maps_found[-1]
                        problem_map_path = os.path.join(game_path, problem_map_name)
                        target_move_path = os.path.join(temp_problem_dir, problem_map_name)

                        if os.path.exists(problem_map_path):
                            log.warning(f"检测到潜在问题文件: {problem_map_name} (基于 RPGRewriter 输出和错误)。")
                            if file_system.safe_move(problem_map_path, target_move_path):
                                broker.log(f"已将问题文件 {problem_map_name} 暂时移至: {temp_problem_dir}，将重试导出。", "warning")
                                if problem_map_name not in problem_files:
                                    problem_files.append(problem_map_name)
                                time.sleep(0.1)
                                continue
                            else:
                                broker.error(f"移动问题文件 {problem_map_name} 失败，停止导出。")
                                break
                        else:
                            log.error(f"RPGRewriter 报告了问题文件 {problem_map_name}，但在游戏目录中找不到它。")
                            broker.error(f"无法定位问题文件 {problem_map_name}，停止导出。")
                            break
                    else:
                        broker.error("导出失败，但无法从 RPGRewriter 输出确定具体问题文件，停止尝试。")
                        break
                else:
                    broker.error("导出失败，遇到未知或无法恢复的 RPGRewriter 错误，停止尝试。")
                    break

        # --- 导出循环结束 ---
        string_scripts_path = os.path.join(game_path, "StringScripts")
        backup_path = os.path.join(game_path, "StringScripts_Origin")
        export_final_status = "失败"

        if export_successful:
            if os.path.exists(string_scripts_path):

                try:
                    broker.log("正在备份原始 StringScripts 到 StringScripts_Origin...")
                    if os.path.exists(backup_path):
                        log.info(f"发现已存在的备份目录，将先删除: {backup_path}")
                        if not file_system.safe_remove(backup_path):
                             broker.warning(f"删除旧的 StringScripts_Origin 备份失败: {backup_path}，将尝试覆盖。")

                    shutil.copytree(string_scripts_path, backup_path)
                    broker.log("成功备份到 StringScripts_Origin。", "success")
                except Exception as backup_err:
                    log.exception("备份 StringScripts 到 StringScripts_Origin 失败。")
                    broker.warning(f"警告：导出成功，但备份原始 StringScripts 到 StringScripts_Origin 失败: {backup_err}")

                try:
                    file_count = sum(len(files) for _, _, files in os.walk(string_scripts_path))
                    broker.log(f"文本导出成功完成，生成 StringScripts 目录，共 {file_count} 个文件。", "success")
                    export_final_status = "成功"
                    if problem_files:
                         export_final_status = "部分成功"
                         broker.log(f"有 {len(problem_files)} 个地图文件在导出过程中被暂时移出: {', '.join(problem_files)}", "warning")
                         broker.success(f"文本导出部分完成。共 {file_count} 个文件。有 {len(problem_files)} 个地图文件未能导出。（原始文件已备份至 StringScripts_Origin）")
                    else:
                        broker.success("文本导出成功完成。（原始文件已备份至 StringScripts_Origin）")

                except Exception as count_err:
                    log.error(f"统计 StringScripts 文件数量时出错: {count_err}")
                    broker.log("文本导出命令成功，但统计结果文件时出错。", "warning")
                    export_final_status = "可能成功(统计失败)"
                    broker.success("文本导出过程已完成（结果文件统计失败）。（原始文件备份状态请见日志）")
            else:
                 broker.error("RPGRewriter 命令返回成功，但未找到 StringScripts 目录。")
                 export_final_status = "失败(目录未生成)"
        else:
             broker.error("文本导出未能成功完成。")
             export_final_status = "失败"

        # --- 清理：移回问题文件 ---
        if problem_files:
             broker.log("正在将导出的问题文件移回原位...")
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

             broker.log(f"已尝试移回 {len(problem_files)} 个文件，成功 {moved_back_count} 个。")
             if move_back_failed:
                  broker.error(f"以下文件移回失败: {', '.join(move_back_failed)}")

        # --- 清理：删除空的临时目录 ---
        try:
             if os.path.exists(temp_problem_dir) and not os.listdir(temp_problem_dir):
                 file_system.safe_remove(temp_problem_dir)
                 broker.log("已清理空的临时问题文件目录。")
             elif os.path.exists(temp_problem_dir):
                 broker.log(f"临时问题文件目录非空，已保留: {temp_problem_dir}", "warning")
        except Exception as rmdir_err:
             log.error(f"清理临时目录时出错: {temp_problem_dir} - {rmdir_err}")


        broker.status(f"文本导出{export_final_status}")
        broker.done()

    except Exception as e:
        log.exception("导出文本任务执行期间发生意外错误。")
        broker.error(f"导出文本过程中发生严重错误: {e}")
        broker.status("导出文本失败")
        broker.done()
        # 尝试在异常情况下也清理临时文件
        try:
            temp_dir = os.path.join(game_path, "_temp_problem_files")
            if os.path.exists(temp_dir):
                log.info("尝试在异常处理中清理临时文件...")
                file_system.safe_remove(temp_dir)
        except Exception as final_cleanup_err:
            log.error(f"异常处理中清理临时目录失败: {final_cleanup_err}")
