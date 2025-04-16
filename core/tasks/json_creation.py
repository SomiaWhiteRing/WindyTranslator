# core/tasks/json_creation.py
import os
import re
import json
import logging
from core.utils import file_system, text_processing # 需要文件名清理

log = logging.getLogger(__name__)

def _extract_strings_from_file(file_path):
    """
    从单个 StringScripts txt 文件中提取需要翻译的字符串。
    与原脚本的 process_file 函数逻辑类似。

    Args:
        file_path (str): txt 文件路径。

    Returns:
        dict: 提取的字符串字典 {original_text: original_text}。
              使用原文作为 key 和 value，方便后续查找。
    """
    strings = {}
    try:
        with open(file_path, 'r', encoding='utf-8', errors='replace') as file:
            lines = file.readlines()

        i = 0
        while i < len(lines):
            line = lines[i]
            match = re.match(r'#(.+)#', line.strip())
            if match:
                title = match.group(1)
                i += 1 # 移动到标题下的第一行
                if title in ['Message', 'Choice']:
                    message_block = ""
                    # 记录消息块的起始和结束行（相对于当前i）
                    block_start_index = i
                    while i < len(lines) and not lines[i].strip() == '##':
                        message_block += lines[i]
                        i += 1
                    # 去除尾部换行符作为 Key
                    message_key = message_block.rstrip('\n')
                    if message_key: # 只有非空消息块才添加
                        # **** 对 Value 进行转换 ****
                        converted_value = text_processing.convert_half_to_full_katakana(message_key)
                        strings[message_key] = converted_value # Key 是原文, Value 是转换后的
                        if message_key != converted_value:
                             log.debug(f"JSON Value Converted (HW->FW Kata): '{message_key[:30]}...' -> '{converted_value[:30]}...'")
                    # i 此时指向 '##' 或文件末尾
                    if i < len(lines) and lines[i].strip() == '##':
                         i += 1 # 跳过 '##' 行
                         
                elif title == 'EventName':
                    # EventName 通常不需要翻译，原脚本逻辑是跳过，这里也跳过
                    # 但如果需要翻译，可以在这里添加逻辑
                    pass # 跳过 EventName 下的内容行
                    if i < len(lines): i+=1 # 指向下一行

                else: # 处理其他如 System/Terms 等单行内容
                    if i < len(lines):
                        content_key = lines[i].strip() # 原文作为 Key
                        if content_key:
                            # **** 对 Value 进行转换 ****
                            converted_value = text_processing.convert_half_to_full_katakana(content_key)
                            strings[content_key] = converted_value # Key 是原文, Value 是转换后的
                            if content_key != converted_value:
                                log.debug(f"JSON Value Converted (HW->FW Kata): '{content_key[:30]}...' -> '{converted_value[:30]}...'")
                        i += 1 # 移动到下一行（可能是下一个 #Title# 或文件末尾）
                    else:
                        # 标题后面没有内容了
                        log.warning(f"在文件 {os.path.basename(file_path)} 中，标题 #{title}# 后面没有内容行。")
                        # i 已经越界，循环会自然结束
            else:
                # 不是标题行，直接跳过
                i += 1
        return strings

    except FileNotFoundError:
        log.error(f"读取文件失败: {file_path} 未找到。")
        return {}
    except Exception as e:
        log.error(f"处理文件 {os.path.basename(file_path)} 时出错: {e}")
        return {}

# --- 主任务函数 ---
def run_create_json(game_path, works_dir, message_queue):
    """
    遍历 StringScripts 目录，提取文本并创建未翻译的 JSON 文件。

    Args:
        game_path (str): 游戏根目录路径。
        works_dir (str): Works 工作目录的根路径。
        message_queue (queue.Queue): 用于向主线程发送消息的队列。
    """
    try:
        message_queue.put(("status", "正在提取文本并创建 JSON..."))
        message_queue.put(("log", ("normal", "步骤 3: 开始创建未翻译的 JSON 文件...")))

        string_scripts_path = os.path.join(game_path, "StringScripts")
        if not os.path.isdir(string_scripts_path):
            message_queue.put(("error", f"未找到 StringScripts 目录: {string_scripts_path}，请先导出文本。"))
            message_queue.put(("status", "创建 JSON 失败"))
            message_queue.put(("done", None))
            return

        # --- 创建 Works 子目录 ---
        game_folder_name = text_processing.sanitize_filename(os.path.basename(game_path))
        if not game_folder_name: game_folder_name = "UntitledGame"
        work_game_dir = os.path.join(works_dir, game_folder_name)
        untranslated_dir = os.path.join(work_game_dir, "untranslated")
        # translated_dir = os.path.join(work_game_dir, "translated") # 在这里创建或在翻译步骤创建均可

        if not file_system.ensure_dir_exists(work_game_dir): raise OSError(f"无法创建目录: {work_game_dir}")
        if not file_system.ensure_dir_exists(untranslated_dir): raise OSError(f"无法创建目录: {untranslated_dir}")
        # if not file_system.ensure_dir_exists(translated_dir): raise OSError(f"无法创建目录: {translated_dir}")

        message_queue.put(("log", ("normal", f"将在以下目录创建 JSON: {untranslated_dir}")))

        # --- 遍历并提取字符串 ---
        all_strings = {}
        processed_file_count = 0
        log.info(f"开始扫描 StringScripts 目录: {string_scripts_path}")
        for root, _, files in os.walk(string_scripts_path):
            for file in files:
                if file.lower().endswith('.txt'):
                    file_path = os.path.join(root, file)
                    log.debug(f"处理文件: {file_path}")
                    file_strings = _extract_strings_from_file(file_path)
                    # 合并字典，如果 key 冲突，后面的会覆盖前面的（理论上不应有冲突）
                    all_strings.update(file_strings)
                    processed_file_count += 1

        total_strings = len(all_strings)
        message_queue.put(("log", ("normal", f"已处理 {processed_file_count} 个文件，提取到 {total_strings} 个唯一字符串。")))

        # --- 写入 JSON 文件 ---
        json_filename = "translation.json"
        json_path = os.path.join(untranslated_dir, json_filename)
        message_queue.put(("log", ("normal", f"正在将提取的字符串写入 JSON 文件: {json_path}")))

        try:
            with open(json_path, 'w', encoding='utf-8') as json_file:
                # indent=None 生成紧凑格式，减小文件大小
                # indent=4 生成易读格式
                json.dump(all_strings, json_file, ensure_ascii=False, indent=4)
            message_queue.put(("success", f"未翻译的 JSON 文件创建成功: {json_path}"))
            message_queue.put(("status", "创建 JSON 文件完成"))
            message_queue.put(("done", None))
        except Exception as write_err:
            log.exception(f"写入 JSON 文件失败: {json_path} - {write_err}")
            message_queue.put(("error", f"写入 JSON 文件失败: {write_err}"))
            message_queue.put(("status", "创建 JSON 失败"))
            message_queue.put(("done", None))

    except Exception as e:
        log.exception("创建 JSON 文件任务执行期间发生意外错误。")
        message_queue.put(("error", f"创建 JSON 文件过程中发生严重错误: {e}"))
        message_queue.put(("status", "创建 JSON 失败"))
        message_queue.put(("done", None))