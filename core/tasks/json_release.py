# core/tasks/json_release.py
import os
import re
import json
import logging
import shutil 
from core.utils import file_system, text_processing

log = logging.getLogger(__name__)

def _apply_translations_to_file(file_path, translations_for_this_file):
    """
    将加载的单个文件的翻译数据应用到对应的 StringScripts txt 文件。
    只使用翻译结果对象中的 "text" 字段进行替换。

    Args:
        file_path (str): 目标 StringScripts txt 文件路径。
        translations_for_this_file (dict): 针对此文件的翻译字典，key是原文，
                                           value是包含 "text", "original_marker", "speaker_id" 的对象。
    Returns:
        tuple: (applied_count, skipped_count)
    """
    applied_count = 0
    skipped_count = 0
    new_lines = []
    file_basename = os.path.basename(file_path) # 用于日志

    try:
        with open(file_path, 'r', encoding='utf-8-sig', errors='replace') as file: # 使用 utf-8-sig
            lines = file.readlines()
    except FileNotFoundError:
        log.error(f"读取文件失败 (文件: {file_basename}): {file_path} 未找到。")
        return 0, 0
    except Exception as e:
        log.error(f"读取文件 {file_basename} 时出错: {e}")
        return 0, 0

    i = 0
    while i < len(lines):
        line = lines[i]
        marker_match = re.match(r'#(.+)#', line.strip())
        if marker_match:
            original_marker_type = marker_match.group(1)
            new_lines.append(line) 
            i += 1 

            if original_marker_type in ['Message']: # 只处理 Message 作为对话型文本
                message_block_original_text = ""
                temp_block_lines = []
                while i < len(lines) and not lines[i].strip() == '##':
                    temp_block_lines.append(lines[i])
                    message_block_original_text += lines[i]
                    i += 1
                
                message_key_for_lookup = message_block_original_text.rstrip('\n')

                if message_key_for_lookup in translations_for_this_file:
                    translation_metadata_obj = translations_for_this_file[message_key_for_lookup]
                    if isinstance(translation_metadata_obj, dict) and "text" in translation_metadata_obj:
                        translated_message_text = translation_metadata_obj["text"]
                        if translated_message_text is not None and translated_message_text.strip() != "":
                            if message_block_original_text.endswith('\n') and not translated_message_text.endswith('\n'):
                                new_lines.append(translated_message_text + '\n')
                            elif not message_block_original_text.endswith('\n') and translated_message_text.endswith('\n'):
                                new_lines.append(translated_message_text.rstrip('\n'))
                            else:
                                new_lines.append(translated_message_text)
                            applied_count += 1
                            log.debug(f"应用翻译到 {file_basename} (块原文: '{message_key_for_lookup[:30].replace(chr(10),'/LF/') + ('...' if len(message_key_for_lookup)>30 else '')}'): '{translated_message_text[:30].replace(chr(10),'/LF/') + ('...' if len(translated_message_text)>30 else '')}'")
                        else:
                            new_lines.extend(temp_block_lines) 
                            skipped_count += 1
                            log.warning(f"在文件 {file_basename} 找到 key '{message_key_for_lookup[:30]}...' 的翻译，但译文为空，保留原文。")
                    else:
                        new_lines.extend(temp_block_lines)
                        skipped_count += 1
                        log.warning(f"在文件 {file_basename} 找到 key '{message_key_for_lookup[:30]}...'，但翻译元数据格式不正确 ({type(translation_metadata_obj)})，保留原文。")
                else:
                    new_lines.extend(temp_block_lines)
                
                if i < len(lines) and lines[i].strip() == '##':
                    new_lines.append(lines[i])
                    i += 1
            
            elif original_marker_type == 'EventName':
                if i < len(lines):
                      new_lines.append(lines[i]) 
                      i+=1

            elif original_marker_type == 'Choice': # 选项采用特殊的处理方式：逐行进行比对
                choice_block_lines = []
                while i < len(lines) and not lines[i].strip() == '##':
                    choice_line = lines[i].strip()
                    if choice_line in translations_for_this_file:
                        translation_metadata_obj = translations_for_this_file[choice_line]
                        if isinstance(translation_metadata_obj, dict) and "text" in translation_metadata_obj:
                            translated_choice_text = translation_metadata_obj["text"]
                            if translated_choice_text is not None and translated_choice_text.strip() != "":
                                # 保持原有的缩进
                                leading_spaces = len(lines[i]) - len(lines[i].lstrip())
                                new_lines.append(' ' * leading_spaces + translated_choice_text + '\n')
                                applied_count += 1
                                log.debug(f"应用翻译到 {file_basename} (选项原文: '{choice_line}'): '{translated_choice_text}'")
                            else:
                                new_lines.append(lines[i])
                                skipped_count += 1
                                log.warning(f"在文件 {file_basename} 找到选项 '{choice_line}' 的翻译，但译文为空，保留原文。")
                        else:
                            new_lines.append(lines[i])
                            skipped_count += 1
                            log.warning(f"在文件 {file_basename} 找到选项 '{choice_line}'，但翻译元数据格式不正确 ({type(translation_metadata_obj)})，保留原文。")
                    else:
                        new_lines.append(lines[i])
                    i += 1

                if i < len(lines) and lines[i].strip() == '##':
                    new_lines.append(lines[i])
                    i += 1

            else: # 其他单行内容的标记
                if i < len(lines):
                    single_line_content_key = lines[i].strip() 
                    original_line_with_newline = lines[i] 

                    if single_line_content_key in translations_for_this_file:
                        translation_metadata_obj = translations_for_this_file[single_line_content_key]
                        if isinstance(translation_metadata_obj, dict) and "text" in translation_metadata_obj:
                            translated_single_line_text = translation_metadata_obj["text"]
                            if translated_single_line_text is not None and translated_single_line_text.strip() != "":
                                new_lines.append(translated_single_line_text.rstrip('\n') + '\n')
                                applied_count += 1
                                log.debug(f"应用翻译到 {file_basename} (行原文: '{single_line_content_key[:30]}...'): '{translated_single_line_text[:30]}...'")
                            else:
                                 new_lines.append(original_line_with_newline) 
                                 skipped_count += 1
                                 log.warning(f"在文件 {file_basename} 找到 key '{single_line_content_key[:30]}...' 的翻译，但译文为空，保留原文。")
                        else:
                            new_lines.append(original_line_with_newline)
                            skipped_count += 1
                            log.warning(f"在文件 {file_basename} 找到 key '{single_line_content_key[:30]}...'，但翻译元数据格式不正确 ({type(translation_metadata_obj)})，保留原文。")
                    else:
                        new_lines.append(original_line_with_newline)
                    i += 1 
                else:
                     log.warning(f"在文件 {file_basename} 中，标记 #{original_marker_type}# 后面没有内容行。")
        else:
            new_lines.append(line)
            i += 1

    try:
        with open(file_path, 'w', encoding='utf-8') as file_out: # RPG Maker 2000/2003 脚本通常是特定编码，但我们内部处理用UTF-8，写回时RPGRewriter会处理编码
            file_out.writelines(new_lines)
        return applied_count, skipped_count
    except Exception as e_write:
        log.error(f"写入文件失败 (文件: {file_basename}): {file_path} - {e_write}")
        return 0, skipped_count


# --- 主任务函数 ---
def run_release_json(game_path, works_dir, selected_json_path, message_queue):
    """
    将翻译后的、按文件组织的 JSON 文件内容写回到 StringScripts 目录。
    在应用翻译前，会先从 StringScripts_Origin 恢复 StringScripts。
    """
    try:
        message_queue.put(("status", "准备应用翻译 (按文件)..."))
        message_queue.put(("log", ("normal", "步骤 6: 开始释放 JSON 文件到 StringScripts (按文件)...")))

        string_scripts_path = os.path.join(game_path, "StringScripts")
        backup_path = os.path.join(game_path, "StringScripts_Origin")

        # --- 恢复 StringScripts_Origin (逻辑不变) ---
        message_queue.put(("log", ("normal", "检查原始备份 StringScripts_Origin...")))
        if not os.path.isdir(backup_path):
            message_queue.put(("error", f"错误：未找到原始脚本备份目录 StringScripts_Origin: {backup_path}"))
            message_queue.put(("status", "释放 JSON 失败 (无备份)"))
            message_queue.put(("done", None)); return
        else:
             message_queue.put(("log", ("normal", "找到备份目录 StringScripts_Origin，准备恢复...")))
        try:
            if os.path.exists(string_scripts_path):
                 if not file_system.safe_remove(string_scripts_path):
                      message_queue.put(("error", f"错误：无法删除现有的 StringScripts 目录: {string_scripts_path}"))
                      message_queue.put(("status", "释放 JSON 失败 (删除旧目录失败)")); message_queue.put(("done", None)); return
                 else: message_queue.put(("log", ("normal", "现有的 StringScripts 目录已删除。")))
            shutil.copytree(backup_path, string_scripts_path)
            message_queue.put(("log", ("success", "成功从 StringScripts_Origin 恢复 StringScripts 目录。")))
        except Exception as restore_err:
            log.exception(f"从 StringScripts_Origin 恢复 StringScripts 失败。")
            message_queue.put(("error", f"错误：从 StringScripts_Origin 恢复时出错: {restore_err}"))
            message_queue.put(("status", "释放 JSON 失败 (恢复备份失败)")); message_queue.put(("done", None)); return
        
        if not os.path.isdir(string_scripts_path):
            message_queue.put(("error", f"严重错误：恢复 StringScripts 后目录仍不存在: {string_scripts_path}"))
            message_queue.put(("status", "释放 JSON 失败 (恢复后目录丢失)")); message_queue.put(("done", None)); return

        # --- 加载按文件组织的翻译 JSON ---
        if not selected_json_path or not os.path.exists(selected_json_path):
            message_queue.put(("error", f"指定的翻译 JSON 文件无效或不存在: {selected_json_path}"))
            message_queue.put(("status", "释放 JSON 失败 (JSON文件无效)")); message_queue.put(("done", None)); return

        message_queue.put(("status", "正在加载翻译并按文件应用..."))
        message_queue.put(("log", ("normal", f"使用翻译文件: {selected_json_path}")))
        
        # all_translations_per_file 的结构是: { "文件名1.txt": {原文1: 元数据对象1,...}, "文件名2.txt": {...} }
        all_translations_per_file = {} 
        try:
            with open(selected_json_path, 'r', encoding='utf-8') as f_json_in:
                all_translations_per_file = json.load(f_json_in)
            message_queue.put(("log", ("normal", f"已加载按文件组织的翻译数据，共涉及 {len(all_translations_per_file)} 个源文件。")))
        except Exception as load_json_err:
            log.exception(f"加载翻译 JSON 文件失败: {selected_json_path} - {load_json_err}")
            message_queue.put(("error", f"加载翻译 JSON 文件失败: {load_json_err}"))
            message_queue.put(("status", "释放 JSON 失败 (加载JSON出错)")); message_queue.put(("done", None)); return

        # --- *** 按文件遍历并应用翻译 *** ---
        overall_applied_count = 0
        overall_skipped_count = 0
        processed_source_files_count = 0
        
        log.info(f"开始按文件遍历 StringScripts 目录并应用翻译: {string_scripts_path}")
        message_queue.put(("log", ("normal", "开始将翻译按文件写回 StringScripts...")))

        # 遍历翻译JSON中的文件名，而不是os.walk，以确保只处理JSON中有的文件
        for source_file_name, translations_for_this_file in all_translations_per_file.items():
            target_string_script_path = os.path.join(string_scripts_path, source_file_name) # 假设JSON中的文件名直接对应StringScripts下的文件名

            if not os.path.exists(target_string_script_path):
                log.warning(f"翻译JSON中包含文件 '{source_file_name}' 的数据，但在恢复的 StringScripts 目录中未找到该文件 ({target_string_script_path})。跳过此文件。")
                continue # 跳过此文件

            log.debug(f"正在将翻译释放到文件: {target_string_script_path}")
            
            # translations_for_this_file 是 {原文: 元数据对象} 结构
            applied_in_file, skipped_in_file = _apply_translations_to_file(
                target_string_script_path, 
                translations_for_this_file 
            )
            overall_applied_count += applied_in_file
            overall_skipped_count += skipped_in_file
            processed_source_files_count += 1
            if applied_in_file > 0 or skipped_in_file > 0: # 只记录有变化的
                log.info(f"文件 '{source_file_name}' 处理完成: 应用 {applied_in_file} 条, 跳过 {skipped_in_file} 条。")

        message_queue.put(("log", ("success", f"所有文件处理完毕。共处理 {processed_source_files_count} 个源文件，总计应用了 {overall_applied_count} 个翻译条目，跳过了 {overall_skipped_count} 个。")))
        message_queue.put(("success", f"JSON 文件释放完成。总应用 {overall_applied_count} 翻译，总跳过 {overall_skipped_count}。"))
        message_queue.put(("status", "释放 JSON 完成"))
        message_queue.put(("done", None))

    except Exception as main_release_err:
        log.exception("释放 JSON 文件任务执行期间发生意外错误。")
        message_queue.put(("error", f"释放 JSON 文件过程中发生严重错误: {main_release_err}"))
        message_queue.put(("status", "释放 JSON 失败"))
        message_queue.put(("done", None))