# core/tasks/json_creation.py
import os
import re
import json
import logging
from core.utils import file_system, text_processing # 需要文件名清理

log = logging.getLogger(__name__)

# 定义表示无脸图/默认发言人的特殊标识符
DEFAULT_SPEAKER_ID = "NARRATION" # 用于旁白或Page开始时无明确脸图的情况
SYSTEM_TEXT_SPEAKER_ID = "SYSTEM"  # 用于系统词条、非对话文本
ERASE_COMMAND_ID = "_ERASE_FACE_" # 用于内部标记Erase指令

# 正则表达式预编译
RE_MARKER_LINE = re.compile(r'#(.+)#')
# 新的思路：先检查是否是脸图指令行，再提取内容
RE_IS_FACE_GRAPHIC_LINE = re.compile(r"^\s*\{{2,}.*?Select Face Graphic:.*?\}{2,}\s*$", re.IGNORECASE)
RE_EXTRACT_FACE_GRAPHIC_CONTENT = re.compile(r"Select Face Graphic:(.*)", re.IGNORECASE) # 提取 "Select Face Graphic:" 之后的内容

RE_PAGE_SEPARATOR = re.compile(r"^(?:-{5,}Page\d+-{5,}|={5,}Page\d+={5,}|\*{5,}Entry\d+\*{5,})$", re.IGNORECASE)


def _parse_face_graphic_command_details(command_details_str):
    """
    解析 "Select Face Graphic:" 指令的冒号之后的内容。
    Args:
        command_details_str (str): 例如 "u30b7u30e7u30a6A, 7, Left, Flip Horizontal" 或 "Erase"
    Returns:
        str or None: 解析后的 speaker_id (例如 "u30b7u30e7u30a6A_7", ERASE_COMMAND_ID)，
                     如果无法解析，则返回 None。
    """
    details_lower = command_details_str.strip().lower()

    if details_lower == "erase":
        log.debug(f"    _parse_face_graphic_command_details: 解析到 'erase' -> {ERASE_COMMAND_ID}")
        return ERASE_COMMAND_ID

    parts = [p.strip() for p in command_details_str.split(',')]
    face_name_raw = parts[0]

    if not face_name_raw: # 脸图名为空，例如 ", 0"
        log.debug(f"    _parse_face_graphic_command_details: 脸图名为空 ('{command_details_str}') -> {ERASE_COMMAND_ID}")
        return ERASE_COMMAND_ID
    
    face_name = face_name_raw.strip('\'"') # 移除可能的引号

    # 再次检查 face_name 是否为 "Erase" (不区分大小写)
    if face_name.lower() == "erase":
        log.debug(f"    _parse_face_graphic_command_details: 解析到脸图名为 'erase' -> {ERASE_COMMAND_ID}")
        return ERASE_COMMAND_ID

    face_index_str = None
    if len(parts) > 1:
        face_index_str = parts[1]

    if face_index_str and face_index_str.isdigit():
        final_id = f"{face_name}_{face_index_str}"
        log.debug(f"    _parse_face_graphic_command_details: 解析到 '{command_details_str}' -> '{final_id}'")
        return final_id
    elif face_name: # 只有文件名，没有有效索引
        log.debug(f"    _parse_face_graphic_command_details: 解析到 '{command_details_str}' (只有文件名) -> '{face_name}'")
        return face_name
    
    log.warning(f"    _parse_face_graphic_command_details: 未能从 '{command_details_str}' 中解析出有效ID。")
    return None


def _extract_strings_from_file(file_path):
    """
    从单个 StringScripts txt 文件中提取需要翻译的字符串及其元数据。
    (此函数逻辑基本保持你提供的已修复版本)

    Args:
        file_path (str): txt 文件路径。

    Returns:
        dict: 提取的字符串字典。格式：
              {
                  "原文": {
                      "text_to_translate": "原文 (可能经过半角假名转换)",
                      "original_marker": "标记类型 (如 Message, Name)",
                      "speaker_id": "发言人标识 (脸图文件名_索引, 文件名, NARRATION, SYSTEM)"
                  },
                  ...
              }
    """
    strings_with_metadata = {}
    current_speaker_id = DEFAULT_SPEAKER_ID
    current_line_number_for_log = 0
    current_page_for_log = "Page_Unknown_Init" 

    log.debug(f"开始解析文件: '{os.path.basename(file_path)}', 初始 Speaker ID: '{current_speaker_id}'")

    try:
        with open(file_path, 'r', encoding='utf-8-sig', errors='replace') as file: # 使用 'utf-8-sig' 处理BOM
            lines = file.readlines()

        i = 0
        while i < len(lines):
            current_line_number_for_log = i + 1
            line_content = lines[i]
            line_content_stripped = line_content.strip()

            # 1. 检查是否是Page分隔符
            page_match = RE_PAGE_SEPARATOR.match(line_content_stripped)
            if page_match:
                current_page_for_log = page_match.group(0)
                previous_speaker_id = current_speaker_id
                current_speaker_id = DEFAULT_SPEAKER_ID
                log.debug(f"  [L{current_line_number_for_log}, {current_page_for_log}] Page分隔符. Speaker ID 从 '{previous_speaker_id}' 重置为 '{current_speaker_id}'.")
                i += 1
                continue

            # 2. 检查是否是脸图指令
            if RE_IS_FACE_GRAPHIC_LINE.match(line_content_stripped):
                extract_match = RE_EXTRACT_FACE_GRAPHIC_CONTENT.search(line_content_stripped)
                if extract_match:
                    command_details_dirty = extract_match.group(1)
                    command_details_cleaned = command_details_dirty.split('}', 1)[0].strip()
                    log.debug(f"  [L{current_line_number_for_log}, {current_page_for_log}] 识别到脸图指令. 待解析内容: '{command_details_cleaned}'")
                    
                    parsed_id = _parse_face_graphic_command_details(command_details_cleaned)
                    previous_speaker_id = current_speaker_id
                    if parsed_id == ERASE_COMMAND_ID:
                        current_speaker_id = DEFAULT_SPEAKER_ID
                        log.debug(f"    脸图指令 Erase. Speaker ID 从 '{previous_speaker_id}' 重置为 '{current_speaker_id}'.")
                    elif parsed_id:
                        current_speaker_id = parsed_id
                        log.debug(f"    脸图指令. Speaker ID 从 '{previous_speaker_id}' 更新为 '{current_speaker_id}'.")
                    else:
                        log.warning(f"    未能有效解析脸图指令细节: '{command_details_cleaned}'. Speaker ID ('{current_speaker_id}') 保持不变.")
                else:
                    log.warning(f"  [L{current_line_number_for_log}, {current_page_for_log}] 行 '{line_content_stripped[:50]}...' 疑似脸图指令但无法提取内容 (RE_EXTRACT_FACE_GRAPHIC_CONTENT 未匹配).")
                i += 1
                continue

            # 3. 检查是否是文本标记行
            marker_match = RE_MARKER_LINE.match(line_content_stripped)
            if marker_match:
                original_marker = marker_match.group(1)
                # 对话类与图文说明类（StringPicture）按多行块处理；其他标记按单行处理
                speaker_id_for_this_entry = current_speaker_id if original_marker in ['Message'] else SYSTEM_TEXT_SPEAKER_ID # Choice 文本也可能需要发言人ID，但RPG Maker 2000/2003的Choice通常不直接关联脸图，所以默认为SYSTEM
                log.debug(f"  [L{current_line_number_for_log}, {current_page_for_log}] 处理标记 '#{original_marker}#'. 使用 Speaker ID: '{speaker_id_for_this_entry}' (基于 current_speaker_id='{current_speaker_id}').")
                i += 1 
                
                if original_marker in ['Message', 'StringPicture']: # Message 与 StringPicture 都按多行块处理
                    message_block_lines = []
                    while i < len(lines) and not lines[i].strip() == '##':
                        message_block_lines.append(lines[i])
                        i += 1
                    
                    message_block_raw_text = "".join(message_block_lines)
                    message_key_as_original = message_block_raw_text.rstrip('\n') 
                    
                    if message_key_as_original:
                        text_to_translate_val = text_processing.convert_half_to_full_katakana(message_key_as_original)
                        strings_with_metadata[message_key_as_original] = {
                            "text_to_translate": text_to_translate_val,
                            "original_marker": original_marker,
                            "speaker_id": speaker_id_for_this_entry if original_marker == 'Message' else SYSTEM_TEXT_SPEAKER_ID
                        }
                        log.debug(f"    提取到 '{original_marker}' 块. 原文Key: '{message_key_as_original[:30].replace(chr(10),'/LF/') + ('...' if len(message_key_as_original)>30 else '')}'. Speaker: '{speaker_id_for_this_entry}'")
                        if message_key_as_original != text_to_translate_val:
                             log.debug(f"      半角假名已转换.")
                    else:
                        log.debug(f"    空的 '{original_marker}' 块被跳过.")
                        
                    if i < len(lines) and lines[i].strip() == '##': 
                         i += 1
                
                elif original_marker == 'EventName': 
                    if i < len(lines):
                        log.debug(f"    跳过 EventName: '{lines[i].strip()}' (内容在 L{i+1})")
                        i += 1 
                    else:
                        log.warning(f"    标记 #{original_marker}# (在 L{current_line_number_for_log}) 后没有内容行.")

                elif original_marker == 'Choice': # 特别的，Choice 虽然以系统文本归类，但其内部存在不止一行，需要为这一个标记制作多个条目
                    choice_lines = []
                    while i < len(lines) and not lines[i].strip() == '##':
                        choice_lines.append(lines[i])
                        i += 1
                    for choice_line in choice_lines:
                        choice_line_key = choice_line.strip()
                        if choice_line_key:
                            text_to_translate_val = text_processing.convert_half_to_full_katakana(choice_line_key)
                            strings_with_metadata[choice_line_key] = {
                                "text_to_translate": text_to_translate_val,
                                "original_marker": original_marker,
                                "speaker_id": speaker_id_for_this_entry 
                            }
                            log.debug(f"    提取到 Choice 标记 '{original_marker}'. 原文Key: '{choice_line_key[:30].replace(chr(10),'/LF/') + ('...' if len(choice_line_key)>30 else '')}'. Speaker: '{speaker_id_for_this_entry}' (内容来自 L{i+1})")
                            if choice_line_key != text_to_translate_val:
                                log.debug(f"      半角假名已转换.")
                    if i < len(lines) and lines[i].strip() == '##':
                        i += 1
                        
                else: # 其他所有标记都视为单行系统文本
                    if i < len(lines): 
                        single_line_key = lines[i].strip()
                        if single_line_key:
                            text_to_translate_val = text_processing.convert_half_to_full_katakana(single_line_key)
                            # 对于非Message类，speaker_id 固定为 SYSTEM_TEXT_SPEAKER_ID
                            strings_with_metadata[single_line_key] = {
                                "text_to_translate": text_to_translate_val,
                                "original_marker": original_marker,
                                "speaker_id": SYSTEM_TEXT_SPEAKER_ID 
                            }
                            log.debug(f"    提取到单行标记 '{original_marker}'. 原文Key: '{single_line_key[:30].replace(chr(10),'/LF/') + ('...' if len(single_line_key)>30 else '')}'. Speaker: '{SYSTEM_TEXT_SPEAKER_ID}' (内容来自 L{i+1})")
                            if single_line_key != text_to_translate_val:
                                log.debug(f"      半角假名已转换.")
                        else:
                            log.debug(f"    标记 '{original_marker}' (在 L{current_line_number_for_log}) 下内容为空行 (L{i+1})，跳过.")
                        i += 1 
                    else:
                        log.warning(f"    标记 #{original_marker}# (在 L{current_line_number_for_log}) 后没有内容行.")
            else:
                i += 1 
        
        log.debug(f"完成文件解析: '{os.path.basename(file_path)}'. 共提取 {len(strings_with_metadata)} 条数据.")
        return strings_with_metadata

    except FileNotFoundError:
        log.error(f"读取文件失败: {file_path} 未找到。")
        return {}
    except Exception as e:
        log.exception(f"处理文件 '{os.path.basename(file_path)}' (行 ~{current_line_number_for_log}) 时发生严重错误: {e}")
        return {}


# --- 主任务函数 ---
def run_create_json(game_path, works_dir, message_queue):
    """
    遍历 StringScripts 目录，提取文本及其元数据，并按文件名组织创建未翻译的 JSON 文件。
    """
    try:
        message_queue.put(("status", "正在提取文本并创建 JSON (按文件组织)...")) # 状态消息更新
        message_queue.put(("log", ("normal", "步骤 3: 开始创建未翻译的 JSON 文件 (按文件组织)..."))) # 日志消息更新

        string_scripts_path = os.path.join(game_path, "StringScripts")
        if not os.path.isdir(string_scripts_path):
            message_queue.put(("error", f"未找到 StringScripts 目录: {string_scripts_path}，请先导出文本。"))
            message_queue.put(("status", "创建 JSON 失败"))
            message_queue.put(("done", None))
            return

        game_folder_name = text_processing.sanitize_filename(os.path.basename(game_path))
        if not game_folder_name: game_folder_name = "UntitledGame"
        work_game_dir = os.path.join(works_dir, game_folder_name)
        untranslated_dir = os.path.join(work_game_dir, "untranslated")
        
        if not file_system.ensure_dir_exists(work_game_dir): 
            raise OSError(f"无法创建或访问游戏特定工作目录: {work_game_dir}")
        if not file_system.ensure_dir_exists(untranslated_dir): 
            raise OSError(f"无法创建或访问 untranslated 目录: {untranslated_dir}")
        
        message_queue.put(("log", ("normal", f"将在以下目录创建 JSON: {untranslated_dir}")))

        # *** 主要改动点：创建按文件名组织的顶层字典 ***
        file_organized_data = {} 
        processed_file_count = 0
        total_extracted_entries_across_all_files = 0 # 用于统计总条目数

        log.info(f"开始扫描 StringScripts 目录并按文件组织数据: {string_scripts_path}")
        for root_dir, _, files_in_dir in os.walk(string_scripts_path):
            for file_name in files_in_dir:
                if file_name.lower().endswith('.txt'):
                    file_path = os.path.join(root_dir, file_name)
                    file_key_in_json = os.path.relpath(file_path, string_scripts_path)

                    message_queue.put(("log", ("debug", f"正在解析文件: {file_key_in_json}"))) 
                    
                    # 调用 _extract_strings_from_file 获取该文件的所有文本和元数据
                    data_from_single_file = _extract_strings_from_file(file_path)
                    
                    if data_from_single_file: # 只有当文件中有数据时才添加
                        file_organized_data[file_key_in_json] = data_from_single_file
                        total_extracted_entries_across_all_files += len(data_from_single_file)
                        log.debug(f"文件 '{file_key_in_json}' 解析完成，提取 {len(data_from_single_file)} 条。")
                    else:
                        log.debug(f"文件 '{file_key_in_json}' 未提取到任何可翻译条目。")
                    processed_file_count += 1
        
        message_queue.put(("log", ("normal", f"已处理 {processed_file_count} 个文件。总共提取到 {total_extracted_entries_across_all_files} 个原文条目。")))

        if not file_organized_data: # 如果没有任何文件包含可翻译内容
            message_queue.put(("log", ("warning", "未从任何文件中提取到文本条目。生成的 JSON 文件将为空对象。")))
        
        json_filename = "translation.json" # 输出文件名保持不变
        json_path = os.path.join(untranslated_dir, json_filename)
        message_queue.put(("log", ("normal", f"正在将按文件组织的文本及元数据写入 JSON 文件: {json_path}")))

        try:
            with open(json_path, 'w', encoding='utf-8') as json_file:
                json.dump(file_organized_data, json_file, ensure_ascii=False, indent=4) # 写入新的组织结构
            message_queue.put(("success", f"按文件组织的未翻译 JSON 文件创建成功: {json_path}"))
            message_queue.put(("status", "创建 JSON 文件完成"))
            message_queue.put(("done", None))
        except Exception as write_err:
            log.exception(f"写入 JSON 文件失败: {json_path} - {write_err}")
            message_queue.put(("error", f"写入 JSON 文件失败: {write_err}"))
            message_queue.put(("status", "创建 JSON 失败"))
            message_queue.put(("done", None))

    except OSError as os_err:
        log.exception(f"文件系统操作失败，无法继续创建JSON: {os_err}")
        message_queue.put(("error", f"文件系统错误: {os_err}"))
        message_queue.put(("status", "创建 JSON 失败 (文件系统问题)"))
        message_queue.put(("done", None))
    except Exception as e:
        log.exception("创建 JSON 文件任务执行期间发生意外错误。")
        message_queue.put(("error", f"创建 JSON 文件过程中发生严重错误: {e}"))
        message_queue.put(("status", "创建 JSON 失败"))
        message_queue.put(("done", None))
