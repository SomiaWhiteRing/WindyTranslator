import json
import logging
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor


log = logging.getLogger(__name__)

MULTILINE_BLOCK_MARKERS = {"Message", "StringPicture", "ScrollText"}
DEFAULT_RELEASE_WORKERS = min(4, max(1, os.cpu_count() or 1))
MAX_SCHEMA_ERRORS_TO_REPORT = 10


def _json_type_name(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "布尔值"
    if isinstance(value, dict):
        return "对象"
    if isinstance(value, list):
        return "数组"
    if isinstance(value, str):
        return "字符串"
    if isinstance(value, (int, float)):
        return "数字"
    return type(value).__name__


def _short_repr(value, max_len=80):
    text = str(value).replace("\r", "\\r").replace("\n", "\\n")
    if len(text) > max_len:
        return text[:max_len] + "..."
    return text


def _find_json_key_line(json_text, key, start_pos=0):
    if not isinstance(key, str):
        return None, -1

    candidates = [
        json.dumps(key, ensure_ascii=False),
        json.dumps(key, ensure_ascii=True),
    ]
    for candidate in candidates:
        pos = json_text.find(candidate, start_pos)
        if pos != -1:
            return json_text.count("\n", 0, pos) + 1, pos
    return None, -1


def _validate_translation_json_schema(data, json_text):
    errors = []

    def add_error(path, line, message):
        if len(errors) < MAX_SCHEMA_ERRORS_TO_REPORT:
            errors.append({"path": path, "line": line, "message": message})

    if not isinstance(data, dict):
        add_error(
            "$",
            None,
            f"JSON 顶层必须是按文件名组织的对象，实际为{_json_type_name(data)}。",
        )
        return errors

    for source_file_name, translations_for_this_file in data.items():
        file_path_label = f"$[{source_file_name!r}]"
        file_line, file_pos = _find_json_key_line(json_text, source_file_name)

        if not isinstance(source_file_name, str) or not source_file_name:
            add_error(
                "$",
                None,
                f"文件名键必须是非空字符串，实际为{_json_type_name(source_file_name)}：{_short_repr(source_file_name)}",
            )
            continue

        if not isinstance(translations_for_this_file, dict):
            add_error(
                file_path_label,
                file_line,
                f"文件 '{source_file_name}' 下必须是原文到翻译对象的映射，实际为{_json_type_name(translations_for_this_file)}。"
            )
            continue

        for original_text, translation_metadata_obj in translations_for_this_file.items():
            entry_line, _ = _find_json_key_line(json_text, original_text, max(file_pos, 0))
            entry_label = f"{file_path_label}[{_short_repr(original_text)!r}]"

            if not isinstance(original_text, str):
                add_error(
                    file_path_label,
                    file_line,
                    f"文件 '{source_file_name}' 下的原文键必须是字符串，实际为{_json_type_name(original_text)}。"
                )
                continue

            if not isinstance(translation_metadata_obj, dict):
                add_error(
                    entry_label,
                    entry_line,
                    "每个原文条目的值必须是对象，至少包含 text 字段；"
                    f"实际为{_json_type_name(translation_metadata_obj)}：{_short_repr(translation_metadata_obj)}"
                )
                continue

            if "text" not in translation_metadata_obj:
                add_error(
                    entry_label,
                    entry_line,
                    "翻译对象缺少 text 字段。"
                )
                continue

            translated_text = translation_metadata_obj["text"]
            if translated_text is not None and not isinstance(translated_text, str):
                add_error(
                    entry_label + "['text']",
                    entry_line,
                    f"text 字段必须是字符串或 null，实际为{_json_type_name(translated_text)}。"
                )

    return errors


def _format_schema_errors(selected_json_path, errors):
    lines = [
        f"翻译 JSON 结构不符合释放器要求：{selected_json_path}",
        "期望格式示例：",
        '{',
        '  "Map001.txt": {',
        '    "原文": { "text": "译文", "status": "success" }',
        '  }',
        '}',
        "发现的问题：",
    ]

    for error in errors:
        line_text = f"行 {error['line']}，" if error["line"] else ""
        lines.append(f"- {line_text}位置 {error['path']}：{error['message']}")

    if len(errors) >= MAX_SCHEMA_ERRORS_TO_REPORT:
        lines.append(f"- 仅显示前 {MAX_SCHEMA_ERRORS_TO_REPORT} 个结构问题，请先修正这些问题后重试。")

    return "\n".join(lines)


def _extract_marker_type(line):
    stripped = line.strip()
    if len(stripped) >= 3 and stripped.startswith("#"):
        end_index = stripped.find("#", 1)
        if end_index > 1:
            return stripped[1:end_index]
    return None


def _is_multiline_block_marker(marker):
    return marker in MULTILINE_BLOCK_MARKERS or marker.startswith("PluginCommand_")


def _format_translated_line(original_line_no_nl, translated_text):
    translated_core = translated_text.strip()
    leading_len = len(original_line_no_nl) - len(original_line_no_nl.lstrip())
    trailing_len = len(original_line_no_nl) - len(original_line_no_nl.rstrip())
    leading = original_line_no_nl[:leading_len]
    trailing = original_line_no_nl[len(original_line_no_nl) - trailing_len:] if trailing_len else ""
    return f"{leading}{translated_core}{trailing}\n"


def _copy_file_pair(file_pair):
    src_path, dst_path = file_pair
    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
    shutil.copy2(src_path, dst_path)


def _restore_string_scripts_from_backup(backup_path, string_scripts_path):
    file_pairs = []

    for current_root, _, filenames in os.walk(backup_path):
        relative_root = os.path.relpath(current_root, backup_path)
        target_root = string_scripts_path if relative_root == "." else os.path.join(string_scripts_path, relative_root)
        os.makedirs(target_root, exist_ok=True)

        for filename in filenames:
            src_path = os.path.join(current_root, filename)
            dst_path = os.path.join(target_root, filename)
            file_pairs.append((src_path, dst_path))

    if not file_pairs:
        return 0, 0

    worker_count = min(DEFAULT_RELEASE_WORKERS, len(file_pairs))
    if worker_count == 1:
        for file_pair in file_pairs:
            _copy_file_pair(file_pair)
    else:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            list(executor.map(_copy_file_pair, file_pairs))

    return len(file_pairs), worker_count


def _apply_translations_worker(task):
    source_file_name, target_path, translations_for_this_file = task
    applied_count, skipped_count = _apply_translations_to_file(target_path, translations_for_this_file)
    return source_file_name, applied_count, skipped_count


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
    file_basename = os.path.basename(file_path)

    try:
        with open(file_path, "r", encoding="utf-8-sig", errors="replace") as file:
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
        original_marker_type = _extract_marker_type(line)

        if original_marker_type is None:
            new_lines.append(line)
            i += 1
            continue

        new_lines.append(line)
        i += 1

        if _is_multiline_block_marker(original_marker_type):
            temp_block_lines = []
            while i < len(lines) and lines[i].strip() != "##":
                temp_block_lines.append(lines[i])
                i += 1

            message_block_original_text = "".join(temp_block_lines)
            message_key_for_lookup = message_block_original_text.rstrip("\n")

            if message_key_for_lookup in translations_for_this_file:
                translation_metadata_obj = translations_for_this_file[message_key_for_lookup]
                if isinstance(translation_metadata_obj, dict) and "text" in translation_metadata_obj:
                    translated_message_text = translation_metadata_obj["text"]
                    if translated_message_text is not None and translated_message_text.strip() != "":
                        if message_block_original_text.endswith("\n") and not translated_message_text.endswith("\n"):
                            new_lines.append(translated_message_text + "\n")
                        elif not message_block_original_text.endswith("\n") and translated_message_text.endswith("\n"):
                            new_lines.append(translated_message_text.rstrip("\n"))
                        else:
                            new_lines.append(translated_message_text)
                        applied_count += 1
                        log.debug(
                            f"应用翻译到 {file_basename} (块原文: '{message_key_for_lookup[:30].replace(chr(10), '/LF/') + ('...' if len(message_key_for_lookup) > 30 else '')}'): "
                            f"'{translated_message_text[:30].replace(chr(10), '/LF/') + ('...' if len(translated_message_text) > 30 else '')}'"
                        )
                    else:
                        new_lines.extend(temp_block_lines)
                        skipped_count += 1
                        log.warning(f"在文件 {file_basename} 找到 key '{message_key_for_lookup[:30]}...' 的翻译，但译文为空，保留原文。")
                else:
                    new_lines.extend(temp_block_lines)
                    skipped_count += 1
                    log.warning(
                        f"在文件 {file_basename} 找到 key '{message_key_for_lookup[:30]}...'，但翻译元数据格式不正确 ({type(translation_metadata_obj)})，保留原文。"
                    )
            else:
                new_lines.extend(temp_block_lines)

            if i < len(lines) and lines[i].strip() == "##":
                new_lines.append(lines[i])
                i += 1
            continue

        if original_marker_type == "EventName":
            if i < len(lines):
                new_lines.append(lines[i])
                i += 1
            continue

        if original_marker_type == "Choice":
            while i < len(lines) and lines[i].strip() != "##":
                original_line_with_newline = lines[i]
                original_line_no_nl = original_line_with_newline.rstrip("\r\n")
                choice_line_key = original_line_no_nl.strip()

                if choice_line_key in translations_for_this_file:
                    translation_metadata_obj = translations_for_this_file[choice_line_key]
                    if isinstance(translation_metadata_obj, dict) and "text" in translation_metadata_obj:
                        translated_choice_text = translation_metadata_obj["text"]
                        if translated_choice_text is not None and translated_choice_text.strip() != "":
                            new_lines.append(_format_translated_line(original_line_no_nl, translated_choice_text))
                            applied_count += 1
                            log.debug(f"应用翻译到 {file_basename} (选项原文: '{choice_line_key}'): '{translated_choice_text}'")
                        else:
                            new_lines.append(original_line_with_newline)
                            skipped_count += 1
                            log.warning(f"在文件 {file_basename} 找到选项 '{choice_line_key}' 的翻译，但译文为空，保留原文。")
                    else:
                        new_lines.append(original_line_with_newline)
                        skipped_count += 1
                        log.warning(
                            f"在文件 {file_basename} 找到选项 '{choice_line_key}'，但翻译元数据格式不正确 ({type(translation_metadata_obj)})，保留原文。"
                        )
                else:
                    new_lines.append(original_line_with_newline)
                i += 1

            if i < len(lines) and lines[i].strip() == "##":
                new_lines.append(lines[i])
                i += 1
            continue

        if i < len(lines):
            original_line_with_newline = lines[i]
            original_line_no_nl = original_line_with_newline.rstrip("\r\n")
            single_line_content_key = original_line_no_nl.strip()

            if single_line_content_key in translations_for_this_file:
                translation_metadata_obj = translations_for_this_file[single_line_content_key]
                if isinstance(translation_metadata_obj, dict) and "text" in translation_metadata_obj:
                    translated_single_line_text = translation_metadata_obj["text"]
                    if translated_single_line_text is not None and translated_single_line_text.strip() != "":
                        new_lines.append(_format_translated_line(original_line_no_nl, translated_single_line_text))
                        applied_count += 1
                        log.debug(
                            f"应用翻译到 {file_basename} (行原文: '{single_line_content_key[:30]}...'): '{translated_single_line_text[:30]}...'"
                        )
                    else:
                        new_lines.append(original_line_with_newline)
                        skipped_count += 1
                        log.warning(f"在文件 {file_basename} 找到 key '{single_line_content_key[:30]}...' 的翻译，但译文为空，保留原文。")
                else:
                    new_lines.append(original_line_with_newline)
                    skipped_count += 1
                    log.warning(
                        f"在文件 {file_basename} 找到 key '{single_line_content_key[:30]}...'，但翻译元数据格式不正确 ({type(translation_metadata_obj)})，保留原文。"
                    )
            else:
                new_lines.append(original_line_with_newline)
            i += 1
        else:
            log.warning(f"在文件 {file_basename} 中，标记 #{original_marker_type}# 后面没有内容行。")

    try:
        with open(file_path, "w", encoding="utf-8") as file_out:
            file_out.writelines(new_lines)
        return applied_count, skipped_count
    except Exception as e_write:
        log.error(f"写入文件失败 (文件: {file_basename}): {file_path} - {e_write}")
        return 0, skipped_count


def run_release_json(game_path, works_dir, selected_json_path, message_queue):
    """
    将翻译后的、按文件组织的 JSON 文件内容写回到 StringScripts 目录。
    在应用翻译前，会先从 StringScripts_Origin 恢复 StringScripts。
    """
    release_start_time = time.perf_counter()

    try:
        message_queue.put(("status", "准备应用翻译 (按文件)..."))
        message_queue.put(("log", ("normal", "步骤 6: 开始释放 JSON 文件到 StringScripts (按文件)...")))

        string_scripts_path = os.path.join(game_path, "StringScripts")
        backup_path = os.path.join(game_path, "StringScripts_Origin")

        message_queue.put(("log", ("normal", "检查原始备份 StringScripts_Origin...")))
        if not os.path.isdir(backup_path):
            message_queue.put(("error", f"错误：未找到原始脚本备份目录 StringScripts_Origin: {backup_path}"))
            message_queue.put(("status", "释放 JSON 失败 (无备份)"))
            message_queue.put(("done", None))
            return False

        if not selected_json_path or not os.path.exists(selected_json_path):
            message_queue.put(("error", f"指定的翻译 JSON 文件无效或不存在: {selected_json_path}"))
            message_queue.put(("status", "释放 JSON 失败 (JSON文件无效)"))
            message_queue.put(("done", None))
            return False

        message_queue.put(("status", "正在加载并校验翻译 JSON..."))
        message_queue.put(("log", ("normal", f"使用翻译文件: {selected_json_path}")))

        load_json_start_time = time.perf_counter()
        try:
            with open(selected_json_path, "r", encoding="utf-8") as f_json_in:
                translation_json_text = f_json_in.read()
            all_translations_per_file = json.loads(translation_json_text)
        except json.JSONDecodeError as load_json_err:
            log.exception(f"翻译 JSON 语法错误: {selected_json_path} - {load_json_err}")
            message_queue.put((
                "error",
                f"翻译 JSON 不是合法 JSON：{selected_json_path}\n"
                f"第 {load_json_err.lineno} 行，第 {load_json_err.colno} 列：{load_json_err.msg}"
            ))
            message_queue.put(("status", "释放 JSON 失败 (JSON语法错误)"))
            message_queue.put(("done", None))
            return False
        except Exception as load_json_err:
            log.exception(f"加载翻译 JSON 文件失败: {selected_json_path} - {load_json_err}")
            message_queue.put(("error", f"加载翻译 JSON 文件失败: {load_json_err}"))
            message_queue.put(("status", "释放 JSON 失败 (加载JSON出错)"))
            message_queue.put(("done", None))
            return False

        schema_errors = _validate_translation_json_schema(all_translations_per_file, translation_json_text)
        if schema_errors:
            message_queue.put(("error", _format_schema_errors(selected_json_path, schema_errors)))
            message_queue.put(("status", "释放 JSON 失败 (JSON结构错误)"))
            message_queue.put(("done", None))
            return False

        load_json_elapsed = time.perf_counter() - load_json_start_time
        log.debug(f"加载按文件组织的翻译数据完成。共涉及 {len(all_translations_per_file)} 个源文件，耗时 {load_json_elapsed:.2f} 秒。")
        message_queue.put(("log", ("normal", f"已加载按文件组织的翻译数据，共涉及 {len(all_translations_per_file)} 个源文件。")))
        message_queue.put(("status", "正在恢复 StringScripts 并应用翻译..."))

        message_queue.put(("log", ("normal", "找到备份目录 StringScripts_Origin，准备恢复...")))
        restore_start_time = time.perf_counter()
        try:
            os.makedirs(string_scripts_path, exist_ok=True)
            restored_files_count, restore_workers = _restore_string_scripts_from_backup(backup_path, string_scripts_path)
        except Exception as restore_err:
            log.exception("从 StringScripts_Origin 恢复 StringScripts 失败。")
            message_queue.put(("error", f"错误：从 StringScripts_Origin 恢复时出错: {restore_err}"))
            message_queue.put(("status", "释放 JSON 失败 (恢复备份失败)"))
            message_queue.put(("done", None))
            return False

        restore_elapsed = time.perf_counter() - restore_start_time
        log.debug(
            f"从 StringScripts_Origin 恢复 StringScripts 完成。覆盖 {restored_files_count} 个文件，线程数 {restore_workers}，耗时 {restore_elapsed:.2f} 秒。"
        )
        message_queue.put(("log", ("success", "成功从 StringScripts_Origin 恢复 StringScripts 目录。")))

        if not os.path.isdir(string_scripts_path):
            message_queue.put(("error", f"严重错误：恢复 StringScripts 后目录仍不存在: {string_scripts_path}"))
            message_queue.put(("status", "释放 JSON 失败 (恢复后目录丢失)"))
            message_queue.put(("done", None))
            return False

        overall_applied_count = 0
        overall_skipped_count = 0
        processed_source_files_count = 0
        tasks_to_process = []

        for source_file_name, translations_for_this_file in all_translations_per_file.items():
            backup_source_path = os.path.join(backup_path, source_file_name)
            if not os.path.exists(backup_source_path):
                log.warning(
                    f"翻译 JSON 中包含文件 '{source_file_name}' 的数据，但在 StringScripts_Origin 中未找到该文件 ({backup_source_path})。跳过此文件。"
                )
                continue

            target_string_script_path = os.path.join(string_scripts_path, source_file_name)
            if not os.path.exists(target_string_script_path):
                log.warning(
                    f"翻译 JSON 中包含文件 '{source_file_name}' 的数据，但在恢复后的 StringScripts 目录中未找到该文件 ({target_string_script_path})。跳过此文件。"
                )
                continue

            tasks_to_process.append((source_file_name, target_string_script_path, translations_for_this_file))

        log.info(f"开始按文件遍历 StringScripts 目录并应用翻译: {string_scripts_path}")
        message_queue.put(("log", ("normal", "开始将翻译按文件写回 StringScripts...")))

        apply_start_time = time.perf_counter()
        if tasks_to_process:
            apply_workers = min(DEFAULT_RELEASE_WORKERS, len(tasks_to_process))
            if apply_workers == 1:
                results = map(_apply_translations_worker, tasks_to_process)
            else:
                executor = ThreadPoolExecutor(max_workers=apply_workers)
                results = executor.map(_apply_translations_worker, tasks_to_process)

            try:
                for source_file_name, applied_in_file, skipped_in_file in results:
                    overall_applied_count += applied_in_file
                    overall_skipped_count += skipped_in_file
                    processed_source_files_count += 1

                    if applied_in_file > 0 or skipped_in_file > 0:
                        log.info(f"文件 '{source_file_name}' 处理完成: 应用 {applied_in_file} 条, 跳过 {skipped_in_file} 条。")
            finally:
                if apply_workers > 1:
                    executor.shutdown(wait=True)
        else:
            apply_workers = 0

        apply_elapsed = time.perf_counter() - apply_start_time
        total_elapsed = time.perf_counter() - release_start_time
        log.debug(f"释放 JSON 应用阶段完成。线程数 {apply_workers}，耗时 {apply_elapsed:.2f} 秒，总耗时 {total_elapsed:.2f} 秒。")

        message_queue.put(("log", ("success", f"所有文件处理完毕。共处理 {processed_source_files_count} 个源文件，总计应用了 {overall_applied_count} 个翻译条目，跳过了 {overall_skipped_count} 个。")))
        message_queue.put(("success", f"JSON 文件释放完成。总应用 {overall_applied_count} 翻译，总跳过 {overall_skipped_count}。"))
        message_queue.put(("status", "释放 JSON 完成"))
        message_queue.put(("done", None))
        return True

    except Exception as main_release_err:
        log.exception("释放 JSON 文件任务执行期间发生意外错误。")
        message_queue.put(("error", f"释放 JSON 文件过程中发生严重错误: {main_release_err}"))
        message_queue.put(("status", "释放 JSON 失败"))
        message_queue.put(("done", None))
        return False
