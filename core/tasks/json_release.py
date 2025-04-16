# core/tasks/json_release.py
import os
import re
import json
import logging
from core.utils import file_system, text_processing

log = logging.getLogger(__name__)

def _apply_translations_to_file(file_path, translations):
    """
    将加载的翻译应用到单个 StringScripts txt 文件。
    与原脚本的 process_translation_file 函数逻辑类似。

    Args:
        file_path (str): 要处理的 StringScripts txt 文件路径。
        translations (dict): 加载的翻译字典 {original_key: translated_text}。

    Returns:
        int: 成功应用到此文件的翻译条目数量。
        int: 在此文件中找到 key 但未应用（例如值为空）的数量。
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

# --- 主任务函数 ---
def run_release_json(game_path, works_dir, message_queue):
    """
    将翻译后的 JSON 文件内容写回到 StringScripts 目录。

    Args:
        game_path (str): 游戏根目录路径。
        works_dir (str): Works 工作目录的根路径。
        message_queue (queue.Queue): 用于向主线程发送消息的队列。
                 注意：此函数需要处理多 JSON 文件选择，这部分交互逻辑
                 理想上应由 App 层处理，Task 层只接收最终选定的文件路径。
                 但为了简化，暂时将选择逻辑放入 Task，通过队列通信获取选择结果。
                 更好的做法是 App 层弹出选择框，然后将选择结果传给 Task。
                 **我们将采用后一种方式：假设 App 层处理选择，Task 接收路径。**
        selected_json_path (str): 由 App 层确定的、用户选择的翻译 JSON 文件路径。
    """
    # --- 修改：移除选择逻辑，假设由 App 层传入 selected_json_path ---
    # --- (如果选择逻辑复杂，App 层可能需要同步等待 Task 完成文件查找，然后弹窗，再把结果传回)
    # --- 为了简化，我们这里查找文件，把列表传给 App，让 App 处理选择并再次调用 Task，
    # --- 或者 App 直接调用 Task 时传入已选路径。采用后者。
    # --- 因此，增加一个参数 selected_json_path

    # --- 模拟 App 层调用此 Task 前已经完成了文件选择 ---
    # --- App 层需要先扫描 translated 目录找到 JSON 文件列表 ---
    game_folder_name = text_processing.sanitize_filename(os.path.basename(game_path))
    if not game_folder_name: game_folder_name = "UntitledGame"
    work_game_dir = os.path.join(works_dir, game_folder_name)
    translated_dir = os.path.join(work_game_dir, "translated")

    # --- 由 App 传入的已选文件路径 ---
    # selected_json_path = ... # 这个值需要从 App 传入

    # --- 新增：函数签名接收路径 ---
    def run_release_json_internal(game_path, selected_json_path, message_queue):
        try:
            message_queue.put(("status", "正在应用翻译到 StringScripts..."))
            message_queue.put(("log", ("normal", "步骤 6: 开始释放 JSON 文件到 StringScripts...")))

            string_scripts_path = os.path.join(game_path, "StringScripts")
            if not os.path.isdir(string_scripts_path):
                message_queue.put(("error", f"未找到 StringScripts 目录: {string_scripts_path}"))
                message_queue.put(("status", "释放 JSON 失败"))
                message_queue.put(("done", None))
                return

            if not selected_json_path or not os.path.exists(selected_json_path):
                message_queue.put(("error", f"指定的翻译 JSON 文件无效或不存在: {selected_json_path}"))
                message_queue.put(("status", "释放 JSON 失败"))
                message_queue.put(("done", None))
                return

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
            log.info(f"开始遍历并更新 StringScripts 目录: {string_scripts_path}")
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

    # 返回内部函数，App 层需要先获取 JSON 列表，让用户选择，然后调用这个返回的函数
    # 或者直接修改 run_release_json 的签名接收 selected_json_path
    # 我们选择后者，简化调用流程
    # 所以上面的 run_release_json 签名需要改成 def run_release_json(game_path, works_dir, selected_json_path, message_queue):
    # 这里为了演示分离，保留内部函数结构，但实际使用时直接用修改后的签名

    # return run_release_json_internal # 返回内部函数给 App 调用 (方式一)

# --- 主任务函数 (修改后，直接接收路径) ---
def run_release_json(game_path, works_dir, selected_json_path, message_queue):
    """
    执行 JSON 文件的翻译写回流程 (已由 App 层确定 JSON 路径)。

    Args:
        game_path (str): 游戏根目录路径。
        works_dir (str): Works 目录路径 (用于日志或状态)。
        selected_json_path (str): 用户选择的翻译 JSON 文件完整路径。
        message_queue (queue.Queue): 用于向主线程发送消息的队列。
    """
    try:
        message_queue.put(("status", "正在应用翻译到 StringScripts..."))
        message_queue.put(("log", ("normal", "步骤 6: 开始释放 JSON 文件到 StringScripts...")))

        string_scripts_path = os.path.join(game_path, "StringScripts")
        if not os.path.isdir(string_scripts_path):
            message_queue.put(("error", f"未找到 StringScripts 目录: {string_scripts_path}"))
            message_queue.put(("status", "释放 JSON 失败"))
            message_queue.put(("done", None))
            return

        if not selected_json_path or not os.path.exists(selected_json_path):
            message_queue.put(("error", f"指定的翻译 JSON 文件无效或不存在: {selected_json_path}"))
            message_queue.put(("status", "释放 JSON 失败"))
            message_queue.put(("done", None))
            return

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
        log.info(f"开始遍历并更新 StringScripts 目录: {string_scripts_path}")
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