# core/tasks/json_release.py
import os
import re
import json
import logging
import shutil # <--- 新增导入
from core.utils import file_system, text_processing

log = logging.getLogger(__name__)

def _apply_translations_to_file(file_path, translations):
    """
    将加载的翻译应用到单个 StringScripts txt 文件。
    (函数内容保持不变)
    """
    applied_count = 0
    skipped_count = 0
    new_lines = []

    try:
        # 读取文件内容，使用 UTF-8
        with open(file_path, 'r', encoding='utf-8', errors='replace') as file:
            lines = file.readlines()
    except FileNotFoundError:
        log.error(f"读取文件失败: {file_path} 未找到。")
        return 0, 0
    except Exception as e:
        log.error(f"读取文件 {os.path.basename(file_path)} 时出错: {e}")
        return 0, 0

    i = 0
    while i < len(lines):
        line = lines[i]
        match = re.match(r'#(.+)#', line.strip())
        if match:
            title = match.group(1)
            new_lines.append(line) # 保留标题行
            i += 1
            if title in ['Message', 'Choice']:
                message_block = ""
                block_start_line_num = i + 1 # 行号从 1 开始
                while i < len(lines) and not lines[i].strip() == '##':
                    message_block += lines[i]
                    i += 1

                message_key = message_block.rstrip('\n')

                if message_key in translations:
                    translated_message = translations[message_key]
                    if translated_message is not None and translated_message != "": # 确保翻译非空
                        # 保持原始块的尾部换行符状态
                        if message_block.endswith('\n') and not translated_message.endswith('\n'):
                            new_lines.append(translated_message + '\n')
                        elif not message_block.endswith('\n') and translated_message.endswith('\n'):
                            new_lines.append(translated_message.rstrip('\n'))
                        else:
                            new_lines.append(translated_message)
                        applied_count += 1
                        log.debug(f"应用翻译到 {os.path.basename(file_path)} (行 {block_start_line_num}): '{message_key[:30]}...'")
                    else:
                        # 翻译为空或 None，保留原文
                        new_lines.append(message_block)
                        skipped_count += 1
                        log.warning(f"在文件 {os.path.basename(file_path)} (行 {block_start_line_num}) 找到 key '{message_key[:30]}...' 的翻译，但值为空，已跳过。")
                else:
                    # 未找到翻译，保留原文
                    new_lines.append(message_block)

                # 添加 '##' 行（如果存在）
                if i < len(lines) and lines[i].strip() == '##':
                    new_lines.append(lines[i])
                    i += 1 # 跳过 '##'

            elif title == 'EventName':
                 # 保留 EventName 下的内容行（通常是原始事件名）
                 if i < len(lines):
                      new_lines.append(lines[i])
                      i+=1
            else: # 其他单行内容
                if i < len(lines):
                    content_key = lines[i].strip()
                    line_to_append = lines[i] # 默认保留原始行（包括换行符）

                    if content_key in translations:
                        translated_content = translations[content_key]
                        if translated_content is not None and translated_content != "":
                            # 对于单行，通常我们期望它后面自带换行符
                            new_lines.append(translated_content.rstrip('\n') + '\n')
                            applied_count += 1
                            log.debug(f"应用翻译到 {os.path.basename(file_path)} (行 {i+1}): '{content_key[:30]}...'")
                        else:
                             new_lines.append(line_to_append)
                             skipped_count += 1
                             log.warning(f"在文件 {os.path.basename(file_path)} (行 {i+1}) 找到 key '{content_key[:30]}...' 的翻译，但值为空，已跳过。")
                    else:
                        new_lines.append(line_to_append)
                    i += 1 # 移动到下一行
                else:
                     log.warning(f"在文件 {os.path.basename(file_path)} 中，标题 #{title}# 后面没有内容行。")
        else:
            # 非标题行，原样保留
            new_lines.append(line)
            i += 1

    # --- 写回文件 ---
    try:
        with open(file_path, 'w', encoding='utf-8') as file_out:
            file_out.writelines(new_lines)
        return applied_count, skipped_count
    except Exception as e:
        log.error(f"写入文件失败: {file_path} - {e}")
        return 0, skipped_count # 返回 0 表示写入失败


# --- 主任务函数 (修改后) ---
def run_release_json(game_path, works_dir, selected_json_path, message_queue):
    """
    将翻译后的 JSON 文件内容写回到 StringScripts 目录。
    在应用翻译前，会先从 StringScripts_Origin 恢复 StringScripts。

    Args:
        game_path (str): 游戏根目录路径。
        works_dir (str): Works 目录路径 (用于日志或状态)。
        selected_json_path (str): 用户选择的翻译 JSON 文件完整路径。
        message_queue (queue.Queue): 用于向主线程发送消息的队列。
    """
    try:
        message_queue.put(("status", "准备应用翻译..."))
        message_queue.put(("log", ("normal", "步骤 6: 开始释放 JSON 文件到 StringScripts...")))

        string_scripts_path = os.path.join(game_path, "StringScripts")
        backup_path = os.path.join(game_path, "StringScripts_Origin") # <--- 定义备份路径

        # --- 新增：检查并从 StringScripts_Origin 恢复 StringScripts ---
        message_queue.put(("log", ("normal", "检查原始备份 StringScripts_Origin...")))
        if not os.path.isdir(backup_path):
            message_queue.put(("error", f"错误：未找到原始脚本备份目录 StringScripts_Origin: {backup_path}"))
            message_queue.put(("error", "无法应用翻译。请确保已成功执行过一次导出操作以生成备份。"))
            message_queue.put(("status", "释放 JSON 失败"))
            message_queue.put(("done", None))
            return
        else:
             message_queue.put(("log", ("normal", f"找到备份目录 StringScripts_Origin，准备恢复...")))

        try:
            # 先删除当前 StringScripts (如果存在)
            if os.path.exists(string_scripts_path):
                 message_queue.put(("log", ("normal", f"正在删除现有的 StringScripts 目录...")))
                 if not file_system.safe_remove(string_scripts_path):
                      message_queue.put(("error", f"错误：无法删除现有的 StringScripts 目录: {string_scripts_path}"))
                      message_queue.put(("status", "释放 JSON 失败"))
                      message_queue.put(("done", None))
                      return
                 else:
                      message_queue.put(("log", ("normal", "现有的 StringScripts 目录已删除。")))

            # 从备份复制
            message_queue.put(("log", ("normal", f"正在从 StringScripts_Origin 恢复到 StringScripts...")))
            shutil.copytree(backup_path, string_scripts_path)
            message_queue.put(("log", ("success", "成功从 StringScripts_Origin 恢复 StringScripts 目录。")))

        except Exception as restore_err:
            log.exception(f"从 StringScripts_Origin 恢复 StringScripts 失败。")
            message_queue.put(("error", f"错误：从 StringScripts_Origin 恢复 StringScripts 时出错: {restore_err}"))
            message_queue.put(("status", "释放 JSON 失败"))
            message_queue.put(("done", None))
            return
        # --- 恢复结束 ---

        # 检查恢复后的 StringScripts 目录是否存在
        if not os.path.isdir(string_scripts_path):
            message_queue.put(("error", f"严重错误：恢复 StringScripts 后目录仍不存在: {string_scripts_path}"))
            message_queue.put(("status", "释放 JSON 失败"))
            message_queue.put(("done", None))
            return

        # 检查选择的 JSON 文件
        if not selected_json_path or not os.path.exists(selected_json_path):
            message_queue.put(("error", f"指定的翻译 JSON 文件无效或不存在: {selected_json_path}"))
            message_queue.put(("status", "释放 JSON 失败"))
            message_queue.put(("done", None))
            return

        message_queue.put(("status", "正在加载翻译并应用...")) # 更新状态
        message_queue.put(("log", ("normal", f"使用翻译文件: {selected_json_path}")))

        # --- 加载翻译 JSON ---
        try:
            with open(selected_json_path, 'r', encoding='utf-8') as f_json:
                translations = json.load(f_json)
            message_queue.put(("log", ("normal", f"已加载 {len(translations)} 个翻译条目。")))
        except Exception as e:
            log.exception(f"加载翻译 JSON 文件失败: {selected_json_path} - {e}")
            message_queue.put(("error", f"加载翻译 JSON 文件失败: {e}"))
            message_queue.put(("status", "释放 JSON 失败"))
            message_queue.put(("done", None))
            return

        # --- 遍历 StringScripts 并应用翻译 ---
        total_applied = 0
        total_skipped = 0
        processed_files = 0
        log.info(f"开始遍历并更新已恢复的 StringScripts 目录: {string_scripts_path}")
        message_queue.put(("log", ("normal", "开始将翻译写回 StringScripts 文件...")))
        for root, _, files in os.walk(string_scripts_path):
            for file in files:
                if file.lower().endswith('.txt'):
                    file_path = os.path.join(root, file)
                    log.debug(f"处理文件: {file_path}")
                    applied, skipped = _apply_translations_to_file(file_path, translations)
                    total_applied += applied
                    total_skipped += skipped
                    processed_files += 1

        message_queue.put(("log", ("success", f"已处理 {processed_files} 个文件，应用了 {total_applied} 个翻译条目，跳过了 {total_skipped} 个空翻译。")))
        message_queue.put(("success", f"JSON 文件释放完成。应用 {total_applied} 翻译，跳过 {total_skipped}。"))
        message_queue.put(("status", "释放 JSON 完成"))
        message_queue.put(("done", None))

    except Exception as e:
        log.exception("释放 JSON 文件任务执行期间发生意外错误。")
        message_queue.put(("error", f"释放 JSON 文件过程中发生严重错误: {e}"))
        message_queue.put(("status", "释放 JSON 失败"))
        message_queue.put(("done", None))