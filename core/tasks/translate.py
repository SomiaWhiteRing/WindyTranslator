# core/tasks/translate.py
import os
import json
import csv
import re
import time
import datetime
import logging
import queue # 虽然主进度通信可能不再直接依赖它，但保留以防未来需要
import threading
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor, as_completed # 使用 as_completed
from core.api_clients import deepseek
from core.utils import file_system, text_processing, default_database
from core.config import DEFAULT_WORLD_DICT_CONFIG, DEFAULT_TRANSLATE_CONFIG
from collections import OrderedDict

log = logging.getLogger(__name__)

TRANSLATION_METADATA_PREFIX_RE = re.compile(r'^(?:\s*\[(?:MARKER|FACE):[^\]]+\]\s*)+')

# --- 批量翻译工作单元 (与上一版几乎一致，增加了 current_processing_file_name 的使用) ---
def _translate_batch_with_retry(
    batch_metadata_items, 
    context_metadata_items, 
    character_dictionary,
    entity_dictionary,
    api_client,
    config,
    error_log_path, 
    error_log_lock,
    current_processing_file_name=None 
):
    prompt_template = config.get("prompt_template", DEFAULT_TRANSLATE_CONFIG["prompt_template"])
    model_name = config.get("model", "")
    source_language = config.get("source_language", "日语")
    target_language = config.get("target_language", "简体中文")
    max_retries = config.get("max_retries", 3)
    context_lines_config = config.get("context_lines", 10) 
    min_batch_size = 1
    
    batch_original_texts_for_logging = [item["text_to_translate"] for item in batch_metadata_items]
    current_batch_size = len(batch_metadata_items)

    last_failed_raw_translation_block = None
    last_failed_prompt = None
    last_failed_api_messages = None
    last_failed_api_kwargs = None
    last_failed_response_content = None
    last_validation_reason = "未知错误"
    failure_context_for_batch_item = None

    processed_original_texts_for_glossary_matching = [
        text_processing.pre_process_text_for_llm(item["text_to_translate"]) for item in batch_metadata_items
    ]
    combined_processed_lower_for_glossary = "\n".join(processed_original_texts_for_glossary_matching).lower()

    for attempt in range(max_retries + 1):
        actual_context_items_to_use = context_metadata_items[-context_lines_config:]
        context_text_lines_for_prompt = [item_data["text_to_translate"] for item_data in actual_context_items_to_use]
        context_section = ""
        if context_text_lines_for_prompt:
            context_section = f"### 上文内容 ({source_language})\n<context>\n" + "\n".join(context_text_lines_for_prompt) + "\n</context>\n"

        relevant_char_entries = []
        originals_to_include_in_glossary = set()
        char_lookup = {}
        if character_dictionary:
             char_lookup = {entry.get('原文'): entry for entry in character_dictionary if entry.get('原文')}
             for entry in character_dictionary:
                 char_original = entry.get('原文')
                 if not char_original: continue
                 if char_original.lower() in combined_processed_lower_for_glossary:
                     originals_to_include_in_glossary.add(char_original)
                     main_name_ref = entry.get('对应原名')
                     if main_name_ref and main_name_ref in char_lookup:
                         originals_to_include_in_glossary.add(main_name_ref)
                     elif main_name_ref and main_name_ref not in char_lookup:
                          log.warning(f"人物词典不一致(文件: {current_processing_file_name or 'N/A'}): 昵称 '{char_original}' 的对应原名 '{main_name_ref}' 未找到。")
             char_cols_for_prompt = ['原文', '译文', '对应原名', '性别', '年龄', '性格', '口吻', '描述']
             for char_original in sorted(list(originals_to_include_in_glossary)):
                 entry = char_lookup.get(char_original)
                 if entry:
                     values = [str(entry.get(col, '')) for col in char_cols_for_prompt]
                     entry_line = "|".join(values)
                     relevant_char_entries.append(entry_line)
        character_glossary_section = ""
        if relevant_char_entries:
            character_glossary_section = f"### 人物术语参考 (格式: {'|'.join(char_cols_for_prompt)})\n" + "\n".join(relevant_char_entries) + "\n"

        relevant_entity_entries = []
        if entity_dictionary:
            for entry in entity_dictionary:
                entity_original = entry.get('原文')
                if entity_original and entity_original.lower() in combined_processed_lower_for_glossary:
                    desc = entry.get('描述', '')
                    category = entry.get('类别', '')
                    category_desc = f"{category} - {desc}" if category and desc else category or desc
                    entry_line = f"{entry['原文']}|{entry.get('译文', '')}|{category_desc}"
                    relevant_entity_entries.append(entry_line)
        entity_glossary_section = ""
        if relevant_entity_entries:
            entity_glossary_section = "### 事物术语参考 (格式: 原文|译文|类别 - 描述)\n" + "\n".join(relevant_entity_entries) + "\n"

        numbered_batch_text_lines_for_prompt = []
        for i, item_data in enumerate(batch_metadata_items):
            original_text_content = item_data["text_to_translate"]
            marker_type = item_data["original_marker"]
            speaker_id = item_data["speaker_id"] 
            pua_processed_text = text_processing.pre_process_text_for_llm(original_text_content)
            marker_tag_for_prompt = f"[MARKER: {marker_type}]"
            face_tag_for_prompt = ""
            if speaker_id: 
                face_tag_for_prompt = f"[FACE: {speaker_id}]"
            line_for_prompt = f"{marker_tag_for_prompt} {face_tag_for_prompt}".strip() + f" {i+1}.{pua_processed_text}"
            numbered_batch_text_lines_for_prompt.append(line_for_prompt)
        
        batch_text_for_prompt_payload = "\n".join(numbered_batch_text_lines_for_prompt)
        timestamp_suffix = f"\n[timestamp: {datetime.datetime.now().timestamp()}]" if attempt > 0 else ""
        current_final_prompt_payload = prompt_template.format(
            source_language=source_language, target_language=target_language,
            character_glossary_section=character_glossary_section, entity_glossary_section=entity_glossary_section,
            context_section=context_section, batch_text=batch_text_for_prompt_payload
        ) + timestamp_suffix

        log.debug(f"调用 API 翻译批次 (文件: {current_processing_file_name or 'N/A'}, 大小: {current_batch_size}, 尝试 {attempt+1}/{max_retries+1})")
        current_api_messages_payload = [{"role": "user", "content": current_final_prompt_payload}]
        current_api_kwargs_payload = {}
        if "temperature" in config: current_api_kwargs_payload["temperature"] = config["temperature"]
        if "max_tokens" in config: current_api_kwargs_payload["max_tokens"] = config["max_tokens"]
        
        api_success, api_response_content, api_error_message = api_client.chat_completion(
            model_name, current_api_messages_payload, **current_api_kwargs_payload
        )
        
        last_failed_prompt = current_final_prompt_payload
        last_failed_api_messages = current_api_messages_payload
        last_failed_api_kwargs = current_api_kwargs_payload
        last_failed_response_content = api_response_content if api_success else f"[API错误: {api_error_message}]"

        if not api_success:
            log.warning(f"API 调用失败 (文件: {current_processing_file_name or 'N/A'}, 批次大小 {current_batch_size}, 尝试 {attempt+1}): {api_error_message}")
            last_failed_raw_translation_block = f"[API错误: {api_error_message}]"
            last_validation_reason = f"API调用失败: {api_error_message}"
            failure_context_for_batch_item = f"API调用失败: {api_error_message}"
            _log_batch_error(error_log_path, error_log_lock, "API 调用失败", batch_original_texts_for_logging,
                             last_validation_reason, model_name, last_failed_api_kwargs,
                             last_failed_api_messages, last_failed_response_content, attempt, max_retries,
                             file_name_for_log=current_processing_file_name)
            if attempt < max_retries: time.sleep(1); continue
            else: break

        textarea_match = re.search(r'<textarea>(.*?)</textarea>', api_response_content, re.DOTALL | re.IGNORECASE)
        raw_translated_text_block_from_api = ""
        numbered_translations_from_api = {}
        max_number_found_in_response = 0
        if textarea_match:
            raw_translated_text_block_from_api = textarea_match.group(1).strip()
            raw_lines_from_api = raw_translated_text_block_from_api.split('\n')
            current_collecting_number = -1; current_collecting_text_parts = []
            expected_number = 1
            for line_from_api in raw_lines_from_api:
                line_without_meta = line_from_api
                leading_meta_match = TRANSLATION_METADATA_PREFIX_RE.match(line_without_meta)
                removed_only_meta = False
                if leading_meta_match:
                    line_without_meta = line_without_meta[leading_meta_match.end():]
                    removed_only_meta = line_without_meta == ""
                stripped_line_for_num_match = line_without_meta.lstrip()
                num_line_match = re.match(r'^(\d+)\.\s*(.*)', stripped_line_for_num_match)
                if num_line_match:
                    num_val = int(num_line_match.group(1)); text_after_num = num_line_match.group(2)
                    if num_val == expected_number:
                        if current_collecting_number != -1:
                            numbered_translations_from_api[current_collecting_number] = "\n".join(current_collecting_text_parts).rstrip()
                        current_collecting_number = num_val; current_collecting_text_parts = [text_after_num]
                        max_number_found_in_response = max(max_number_found_in_response, current_collecting_number)
                        expected_number += 1
                        continue
                if current_collecting_number != -1:
                    if removed_only_meta and line_without_meta == "":
                        continue
                    current_collecting_text_parts.append(line_without_meta)
            if current_collecting_number != -1:
                numbered_translations_from_api[current_collecting_number] = "\n".join(current_collecting_text_parts).rstrip()
        else:
            log.warning(f"API 响应未找到 <textarea> (文件: {current_processing_file_name or 'N/A'}). 响应: '{api_response_content[:100]}...'")
            last_failed_raw_translation_block = api_response_content.strip()
            last_validation_reason = "响应格式错误：未找到 <textarea>"
            failure_context_for_batch_item = "响应格式错误：未找到 <textarea>"
            _log_batch_error(error_log_path, error_log_lock, "响应格式错误", batch_original_texts_for_logging,
                             last_validation_reason, model_name, last_failed_api_kwargs,
                             last_failed_api_messages, last_failed_response_content, attempt, max_retries,
                             file_name_for_log=current_processing_file_name)
            if attempt < max_retries: continue
            else: break
        last_failed_raw_translation_block = raw_translated_text_block_from_api

        missing_numbers_in_response = []
        all_expected_numbers_found = True
        final_translated_lines_from_api = [] 
        for i in range(1, current_batch_size + 1):
            if i not in numbered_translations_from_api:
                missing_numbers_in_response.append(i); all_expected_numbers_found = False
                final_translated_lines_from_api.append(None)
            else: final_translated_lines_from_api.append(numbered_translations_from_api[i])

        if all_expected_numbers_found:
            log.info(f"批次翻译响应包含所有 {current_batch_size} 个预期编号 (文件: {current_processing_file_name or 'N/A'}, 尝试 {attempt+1})")
            batch_is_fully_valid = True; temp_results_for_this_attempt = {}
            for i, original_item_data in enumerate(batch_metadata_items):
                result_key = original_item_data["original_json_key"] 
                original_text_for_validation = original_item_data["text_to_translate"] # 这个仍然是用于翻译和验证的文本
                raw_translation_for_this_item = final_translated_lines_from_api[i] 
                restored_text_for_validation = text_processing.restore_pua_placeholders(raw_translation_for_this_item)
                post_processed_text_for_validation = text_processing.post_process_translation(
                    restored_text_for_validation, original_text_for_validation
                )
                is_line_valid, line_validation_reason = text_processing.validate_translation(
                    original_text_for_validation, restored_text_for_validation, post_processed_text_for_validation
                )
                if not is_line_valid:
                    log.warning(f"批次内单行验证失败 (文件: {current_processing_file_name or 'N/A'}, 尝试 {attempt+1}): '{original_text_for_validation[:30]}...' 原因: {line_validation_reason}")
                    last_validation_reason = f"单行验证失败: {line_validation_reason} (原文: {original_text_for_validation[:30]}...)"
                    failure_context_for_batch_item = f"单行验证失败 ({line_validation_reason}): \"{restored_text_for_validation[:50]}...\""
                    batch_is_fully_valid = False
                    _log_batch_error(error_log_path, error_log_lock, "单行验证失败", batch_original_texts_for_logging,
                                     last_validation_reason, model_name, last_failed_api_kwargs,
                                     last_failed_api_messages, last_failed_response_content, attempt, max_retries,
                                     failed_item_index=i, raw_item_translation=raw_translation_for_this_item,
                                     file_name_for_log=current_processing_file_name)
                    break
                temp_results_for_this_attempt[result_key] = {
                    "text": post_processed_text_for_validation, 
                    "status": "success", 
                    "failure_context": None,
                    "original_marker": original_item_data["original_marker"], 
                    "speaker_id": original_item_data["speaker_id"]
                }
            if batch_is_fully_valid: return temp_results_for_this_attempt
            if attempt < max_retries: log.info(f"由于批次内单行验证失败，准备重试整个批次 (文件: {current_processing_file_name or 'N/A'}, 尝试 {attempt+1} 失败)..."); continue
            else: log.error(f"由于批次内单行验证失败，且已达到最大重试次数 (文件: {current_processing_file_name or 'N/A'}, {max_retries+1})。"); break
        else:
            log.warning(f"验证失败 (文件: {current_processing_file_name or 'N/A'}, 尝试 {attempt+1}): API响应未能包含所有预期编号。")
            log.warning(f"  期望: 1-{current_batch_size}, 找到最大: {max_number_found_in_response}, 缺失: {missing_numbers_in_response}")
            last_validation_reason = f"响应缺少编号 (期望 1-{current_batch_size}, 缺失: {missing_numbers_in_response})"
            failure_context_for_batch_item = f"响应缺少编号: {missing_numbers_in_response}"
            _log_batch_error(error_log_path, error_log_lock, "响应缺少编号", batch_original_texts_for_logging,
                             last_validation_reason, model_name, last_failed_api_kwargs,
                             last_failed_api_messages, last_failed_response_content, attempt, max_retries,
                             file_name_for_log=current_processing_file_name)
            if attempt < max_retries: log.info(f"准备重试批次 (文件: {current_processing_file_name or 'N/A'}, 因响应缺少编号)..."); continue
            else: log.error(f"因API响应缺少编号，且已达到最大重试次数 (文件: {current_processing_file_name or 'N/A'}, {max_retries+1})。"); break
            
    if current_batch_size > min_batch_size:
        log.warning(f"批次翻译和重试均失败 (文件: {current_processing_file_name or 'N/A'}, 大小: {current_batch_size})，原因: '{last_validation_reason}'。尝试拆分批次...")
        mid_point = (current_batch_size + 1) // 2
        first_half_metadata_items = batch_metadata_items[:mid_point]
        second_half_metadata_items = batch_metadata_items[mid_point:]
        log.info(f"拆分批次 (文件: {current_processing_file_name or 'N/A'}) 为: {len(first_half_metadata_items)} 和 {len(second_half_metadata_items)}")
        first_half_results = _translate_batch_with_retry(
            first_half_metadata_items, context_metadata_items, character_dictionary, entity_dictionary, 
            api_client, config, error_log_path, error_log_lock, current_processing_file_name
        )
        second_half_results = _translate_batch_with_retry(
            second_half_metadata_items, context_metadata_items, character_dictionary, entity_dictionary, 
            api_client, config, error_log_path, error_log_lock, current_processing_file_name
        )
        combined_results = {**first_half_results, **second_half_results}
        log.info(f"完成拆分批次处理 (文件: {current_processing_file_name or 'N/A'}, 原大小: {current_batch_size})")
        return combined_results
    else:
        log.error(f"批次翻译失败，且无法进一步拆分 (文件: {current_processing_file_name or 'N/A'}, 大小: {current_batch_size})。批内所有项目将回退。最终原因: '{last_validation_reason}'")
        final_fallback_reason = failure_context_for_batch_item or last_validation_reason or "[最终回退，未知具体原因]"
        _log_batch_error(error_log_path, error_log_lock, "最终回退(无法拆分或单项失败)", batch_original_texts_for_logging,
                         last_validation_reason, model_name, last_failed_api_kwargs,
                         last_failed_api_messages, last_failed_response_content, max_retries, max_retries,
                         file_name_for_log=current_processing_file_name)
        fallback_results = {}
        for item_data in batch_metadata_items:
            original_text_key = item_data["text_to_translate"]
            fallback_results[original_text_key] = {
                "text": original_text_key, 
                "status": "fallback", 
                "failure_context": final_fallback_reason,
                "original_marker": item_data["original_marker"], 
                "speaker_id": item_data["speaker_id"]
            }
        return fallback_results

# --- 辅助函数：记录批次错误日志 (添加文件名参数) ---
def _log_batch_error(
    error_log_path, error_log_lock, error_type, batch_keys, reason,
    model_name, api_kwargs, api_messages, response_content,
    attempt, max_retries, failed_item_index=None, raw_item_translation=None,
    file_name_for_log=None 
):
    try:
        with error_log_lock:
            with open(error_log_path, 'a', encoding='utf-8') as elog:
                elog.write(f"[{datetime.datetime.now().isoformat()}] {error_type} (尝试 {attempt+1}/{max_retries+1})\n")
                if file_name_for_log: 
                    elog.write(f"  所属文件: {file_name_for_log}\n")
                elog.write(f"  批次大小: {len(batch_keys)}\n")
                elog.write(f"  失败原因: {reason}\n")
                if failed_item_index is not None:
                    elog.write(f"  失败原文 (索引 {failed_item_index}): {batch_keys[failed_item_index]}\n")
                    if raw_item_translation:
                        elog.write(f"  失败原文的原始译文: {raw_item_translation}\n")
                elog.write(f"  涉及原文 Keys (最多显示5条):\n")
                for i, key in enumerate(batch_keys[:5]):
                    elog.write(f"    - {key[:80]}...\n")
                if len(batch_keys) > 5:
                    elog.write(f"    - ... (等 {len(batch_keys) - 5} 个)\n")
                elog.write(f"  模型: {model_name}\n")
                if api_kwargs: elog.write(f"  API Kwargs: {json.dumps(api_kwargs, ensure_ascii=False)}\n")
                if response_content: elog.write(f"  原始 API 响应体 (截断):\n{response_content[:500]}...\n")
                if api_messages: elog.write(f"  API Messages (Prompt):\n{json.dumps(api_messages, indent=2, ensure_ascii=False)}\n")
                elog.write("-" * 20 + "\n")
    except Exception as log_err:
        log.error(f"写入批次错误日志失败: {log_err}")


# --- 线程工作函数 (返回文件名和结果) ---
def _translation_worker(
    batch_metadata_items,
    context_metadata_items_for_batch,
    source_file_name_for_worker, # 新增：当前批次所属的文件名
    character_dictionary,
    entity_dictionary,
    api_client,
    config,
    # translated_data_shared_dict, # 不再直接修改共享字典
    # results_lock, # 锁也不再由此函数管理
    # progress_queue, # 进度由主线程根据future结果更新
    error_log_path,
    error_log_lock
):
    """
    处理一个批次的翻译任务，并返回结果及其源文件名。
    """
    if not batch_metadata_items:
        log.warning(f"工作线程收到来自文件 '{source_file_name_for_worker or 'N/A'}' 的空批次，跳过。")
        return source_file_name_for_worker, {} # 返回空结果

    original_texts_in_batch_for_logging = [item["text_to_translate"] for item in batch_metadata_items]
    batch_processing_result = {} # 用于存储此worker处理的结果

    try:
        batch_processing_result = _translate_batch_with_retry(
            batch_metadata_items,
            context_metadata_items_for_batch,
            character_dictionary,
            entity_dictionary,
            api_client,
            config,
            error_log_path,
            error_log_lock,
            source_file_name_for_worker 
        )
        log.debug(f"工作线程完成文件 '{source_file_name_for_worker or 'N/A'}' 的批次处理，大小: {len(batch_metadata_items)}。")
    except Exception as worker_exception:
        log.exception(f"工作线程处理文件 '{source_file_name_for_worker or 'N/A'}' 的批次时发生意外顶层错误: {worker_exception} - 批内所有项目将回退")
        final_fallback_reason_worker_ex = f"[工作线程顶层异常({source_file_name_for_worker or 'N/A'}): {worker_exception}]"
        _log_batch_error(error_log_path, error_log_lock, "工作线程意外错误", original_texts_in_batch_for_logging,
                         str(worker_exception), config.get("model"), {}, [], "无响应体", 0, 0,
                         file_name_for_log=source_file_name_for_worker)
        
        batch_processing_result = {} # 确保出错时返回的是字典
        for item_data in batch_metadata_items:
            original_text_key = item_data["text_to_translate"]
            batch_processing_result[original_text_key] = {
                "text": original_text_key, 
                "status": "fallback", 
                "failure_context": final_fallback_reason_worker_ex,
                "original_marker": item_data["original_marker"], 
                "speaker_id": item_data["speaker_id"]
            }
    
    # 返回源文件名和这个批次的结果
    return source_file_name_for_worker, batch_processing_result


# --- 主任务函数 ---
def run_translate(game_path, works_dir, translate_config, world_dict_config, message_queue):
    start_time = time.time()
    character_dictionary = [] 
    entity_dictionary = []   
    fallback_csv_filename = "fallback_corrections.csv"
    all_files_translated_data = {} # *** 用于存储所有文件最终翻译结果的顶层字典 ***

    try:
        message_queue.put(("status", "正在准备翻译任务 (全局预切分)..."))
        message_queue.put(("log", ("normal", "步骤 5: 开始翻译 JSON 文件 (全局预切分, 按文件隔离上下文)...")))

        # --- 路径和配置加载 ---
        game_folder_name = text_processing.sanitize_filename(os.path.basename(game_path))
        if not game_folder_name: game_folder_name = "UntitledGame"
        work_game_dir = os.path.join(works_dir, game_folder_name)
        untranslated_dir = os.path.join(work_game_dir, "untranslated")
        translated_dir = os.path.join(work_game_dir, "translated")
        untranslated_json_path = os.path.join(untranslated_dir, "translation.json")
        translated_json_path = os.path.join(translated_dir, "translation_translated.json")
        error_log_path = os.path.join(translated_dir, "translation_errors.log")
        fallback_csv_path = os.path.join(translated_dir, fallback_csv_filename)
        
        if not file_system.ensure_dir_exists(translated_dir): raise OSError(f"无法创建目录: {translated_dir}")
        if os.path.exists(error_log_path):
            log.info(f"删除旧翻译错误日志: {error_log_path}")
            file_system.safe_remove(error_log_path)
        
        if not os.path.exists(untranslated_json_path):
            raise FileNotFoundError(f"未找到未翻译的 JSON 文件: {untranslated_json_path}")
        message_queue.put(("log", ("normal", "加载按文件组织的未翻译 JSON 文件...")))
        with open(untranslated_json_path, 'r', encoding='utf-8') as f_in:
            untranslated_data_per_file = json.load(f_in)
        
        if not untranslated_data_per_file:
            message_queue.put(("warning", "未翻译的 JSON 文件为空或无效，无需翻译。")); message_queue.put(("status", "翻译跳过(无内容)")); message_queue.put(("done", None)); return
        
        # --- 加载词典 (全局共享) ---
        char_dict_filename = world_dict_config.get("character_dict_filename", DEFAULT_WORLD_DICT_CONFIG["character_dict_filename"])
        entity_dict_filename = world_dict_config.get("entity_dict_filename", DEFAULT_WORLD_DICT_CONFIG["entity_dict_filename"])
        character_dict_path = os.path.join(work_game_dir, char_dict_filename)
        entity_dict_path = os.path.join(work_game_dir, entity_dict_filename)
        if os.path.exists(character_dict_path):
            try:
                with open(character_dict_path, 'r', newline='', encoding='utf-8-sig') as f_char:
                    character_dictionary = [row for row in csv.DictReader(f_char) if row.get('原文')]
                message_queue.put(("log", ("success", f"加载人物词典: {len(character_dictionary)} 条。")))
            except Exception as e_char: message_queue.put(("log", ("error", f"加载人物词典失败: {e_char}")))
        if os.path.exists(entity_dict_path):
            try:
                with open(entity_dict_path, 'r', newline='', encoding='utf-8-sig') as f_ent:
                    entity_dictionary = [row for row in csv.DictReader(f_ent) if row.get('原文')]
                message_queue.put(("log", ("success", f"加载事物词典: {len(entity_dictionary)} 条。")))
            except Exception as e_ent: message_queue.put(("log", ("error", f"加载事物词典失败: {e_ent}")))

        # --- 获取翻译配置 ---
        current_translate_config = translate_config.copy()
        api_url = current_translate_config.get("api_url", "").strip()
        api_key = current_translate_config.get("api_key", "").strip()
        model_name = current_translate_config.get("model", "").strip()
        batch_size_config = current_translate_config.get("batch_size", 10)
        concurrency_config = current_translate_config.get("concurrency", 16)
        context_lines_count = current_translate_config.get("context_lines", 10) # 获取上下文行数配置
        source_language_cfg = current_translate_config.get("source_language", "日语")
        # 判断是否为日语源语言（粗略检查：包含 “日”，或以 ja 开头，或包含 'japanese'）
        src_lang_lc = str(source_language_cfg).lower()
        is_source_language_japanese = ("日" in str(source_language_cfg)) or src_lang_lc.startswith("ja") or ("japanese" in src_lang_lc)
        if not api_url or not api_key or not model_name:
             raise ValueError("翻译API 配置不完整 (URL, Key, Model)。")

        try: api_client_instance = deepseek.DeepSeekClient(api_url, api_key)
        except Exception as client_err: raise ConnectionError(f"初始化 API 客户端失败: {client_err}")
        message_queue.put(("log", ("normal", f"API客户端初始化成功。翻译配置: 模型={model_name}, 并发={concurrency_config}, 批大小={batch_size_config}, 上下文行数={context_lines_count}")))

        # --- 默认数据库过滤与自动填充准备（固定启用，读取 modules/dict） ---
        default_db_mapping, default_db_originals = default_database.load_default_db_mapping()

        # --- *** 任务预切分 *** ---
        global_translation_tasks = [] # 存储所有 (batch_meta, context_meta, file_name) 的任务单元
        overall_total_items_in_all_files = 0
        overall_default_db_prefilled_count = 0
        overall_no_content_prefilled_count = 0

        message_queue.put(("log", ("normal", "开始预切分所有翻译任务...")))
        for file_name, data_for_this_file in untranslated_data_per_file.items():
            if not data_for_this_file:
                log.info(f"文件 '{file_name}' 为空，跳过预切分。")
                all_files_translated_data[file_name] = {} # 预先设置空结果
                continue

            items_with_original_key_for_this_file = []
            prefilled_count_for_this_file = 0
            no_content_prefilled_for_this_file = 0
            for original_json_key, metadata_obj in data_for_this_file.items():
                # 确保元数据对象中有一个字段存储这个原始的JSON键
                metadata_obj['original_json_key'] = original_json_key 
                # 过滤默认数据库条目（精确匹配），并就地自动填充译文
                # 注意：以原始JSON键(原文)做精确匹配，避免半角片假名转换造成的不一致
                if default_database.should_exclude_text(original_json_key, default_db_originals):
                    prefilled = default_database.get_prefill_for_text(
                        original_json_key,
                        default_db_mapping,
                        metadata_obj.get('original_marker'),
                        metadata_obj.get('speaker_id')
                    )
                    # 确保文件条目存在
                    all_files_translated_data.setdefault(file_name, {})
                    if prefilled is not None:
                        all_files_translated_data[file_name][original_json_key] = prefilled
                    else:
                        # 如果参考库中没翻译，仅标记为 success 但使用原文，避免进入API
                        all_files_translated_data[file_name][original_json_key] = {
                            'text': metadata_obj.get('text_to_translate'),
                            'status': 'success',
                            'failure_context': None,
                            'original_marker': metadata_obj.get('original_marker', 'UnknownMarker'),
                            'speaker_id': metadata_obj.get('speaker_id')
                        }
                    prefilled_count_for_this_file += 1
                    continue
                # 若源语言为日语且文本中无假名或汉字，则视为“无需翻译”，直接保留原状
                if is_source_language_japanese:
                    orig_has_jp = text_processing.has_japanese_letters(original_json_key)
                    text_has_jp = text_processing.has_japanese_letters(metadata_obj.get('text_to_translate'))
                    if not orig_has_jp and not text_has_jp:
                        all_files_translated_data.setdefault(file_name, {})
                        all_files_translated_data[file_name][original_json_key] = {
                            'text': metadata_obj.get('text_to_translate'),
                            'status': 'success',
                            'failure_context': None,
                            'original_marker': metadata_obj.get('original_marker', 'UnknownMarker'),
                            'speaker_id': metadata_obj.get('speaker_id')
                        }
                        no_content_prefilled_for_this_file += 1
                        continue

                items_with_original_key_for_this_file.append(metadata_obj)

            all_metadata_items_for_this_file = items_with_original_key_for_this_file
            num_items_in_file = len(all_metadata_items_for_this_file)
            overall_total_items_in_all_files += num_items_in_file
            overall_default_db_prefilled_count += prefilled_count_for_this_file
            # 同步累计“无需翻译”预填数量，排除在需译计数之外
            overall_no_content_prefilled_count += no_content_prefilled_for_this_file
            
            # 预先为这个文件在最终结果字典中创建条目
            all_files_translated_data.setdefault(file_name, {})


            for i in range(0, num_items_in_file, batch_size_config):
                batch_metadata_for_task = all_metadata_items_for_this_file[i : i + batch_size_config]
                if not batch_metadata_for_task: continue

                context_start_idx = max(0, i - context_lines_count)
                # 上下文严格从当前文件内选取
                context_metadata_for_task = all_metadata_items_for_this_file[context_start_idx : i]
                
                global_translation_tasks.append({
                    "batch_items": batch_metadata_for_task,
                    "context_items": context_metadata_for_task,
                    "source_file": file_name,
                    # 其他参数可以作为字典传递给worker，或者worker直接从config取
                })
        
        if not global_translation_tasks:
            message_queue.put(("warning", "所有文件均为空，或未提取到任何可翻译条目。无需翻译。"))
            message_queue.put(("status", "翻译跳过(无内容)")); message_queue.put(("done", None)); return

        total_batches_to_process = len(global_translation_tasks)
        # 计算仅需翻译的总条目（排除默认库预填+无内容预填）
        overall_only_need_translate = overall_total_items_in_all_files - overall_no_content_prefilled_count
        total_need_translate = overall_only_need_translate
        message_queue.put(("log", ("normal", f"任务预切分完成。共 {total_batches_to_process} 个批次（来自 {len(untranslated_data_per_file)} 个文件），总计 {overall_only_need_translate} 个需翻译原文条目。")))
        if overall_default_db_prefilled_count > 0:
            message_queue.put(("log", ("normal", f"按默认数据库规则自动填充 {overall_default_db_prefilled_count} 条模板词条译文，避免重复请求 API。")))
        if overall_no_content_prefilled_count > 0:
            message_queue.put(("log", ("normal", f"按源语言(日语)规则保留原文 {overall_no_content_prefilled_count} 条，无需翻译。")))
        message_queue.put(("status", f"开始翻译，总批次数: {total_batches_to_process}，并发数: {concurrency_config}..."))

        # --- 并发处理全局任务列表 ---
        error_log_lock_obj = threading.Lock() # 全局错误日志锁
        
        # 使用 futures 字典来映射 future 到其对应的任务信息，方便调试或重试特定失败任务 (可选)
        # futures_map = {} 

        completed_batches_count = 0 # 按批次计数
        processed_items_count = 0   # 仅统计需要翻译的条目数（不含预填）

        with ThreadPoolExecutor(max_workers=concurrency_config) as executor:
            # 提交所有任务
            future_to_task_info = {
                executor.submit(
                    _translation_worker,
                    task_unit["batch_items"],
                    task_unit["context_items"],
                    task_unit["source_file"], # 传递源文件名
                    character_dictionary,
                    entity_dictionary,
                    api_client_instance,
                    current_translate_config,
                    error_log_path,
                    error_log_lock_obj
                ): task_unit 
                for task_unit in global_translation_tasks
            }

            last_status_update_time = time.time()
            status_update_interval_sec = 0.5

            for future in as_completed(future_to_task_info):
                task_info_for_this_future = future_to_task_info[future]
                source_file_of_this_batch = task_info_for_this_future["source_file"]
                num_items_in_this_batch = len(task_info_for_this_future["batch_items"])

                try:
                    # _translation_worker 现在返回 (source_file_name, batch_result_dict)
                    processed_file_name, batch_result_dict_from_worker = future.result()
                    
                    # 将批次结果合并到对应文件的结果中
                    # 注意：这里需要确保 all_files_translated_data[processed_file_name] 已经存在
                    # 在预切分阶段，我们已经用 setdefault 初始化了
                    if processed_file_name in all_files_translated_data:
                        all_files_translated_data[processed_file_name].update(batch_result_dict_from_worker)
                    else:
                        # 理论上不应该发生，因为预切分时已初始化
                        log.error(f"严重错误：尝试将批次结果存入未初始化的文件条目 '{processed_file_name}'")
                        all_files_translated_data[processed_file_name] = batch_result_dict_from_worker # 尝试补救

                except Exception as exc:
                    log.exception(f"处理文件 '{source_file_of_this_batch}' 的一个批次时发生异常: {exc}")
                    # 即使worker内部有回退，如果worker本身抛出异常，也需要在这里处理
                    # 构建回退结果并合并
                    fallback_reason_exc = f"[Future执行异常({source_file_of_this_batch}): {exc}]"
                    for item_data_in_failed_batch in task_info_for_this_future["batch_items"]:
                        original_text_key = item_data_in_failed_batch["text_to_translate"]
                        if source_file_of_this_batch not in all_files_translated_data:
                            all_files_translated_data[source_file_of_this_batch] = {}
                        all_files_translated_data[source_file_of_this_batch][original_text_key] = {
                            "text": original_text_key, 
                            "status": "fallback", 
                            "failure_context": fallback_reason_exc,
                            "original_marker": item_data_in_failed_batch["original_marker"], 
                            "speaker_id": item_data_in_failed_batch["speaker_id"]
                        }
                
                completed_batches_count += 1
                processed_items_count += num_items_in_this_batch

                current_time = time.time()
                if current_time - last_status_update_time >= status_update_interval_sec or completed_batches_count == total_batches_to_process:
                    # 仅按需要翻译的条目统计进度（排除预填）
                    progress_percentage = (processed_items_count / total_need_translate) * 100 if total_need_translate > 0 else 100.0
                    elapsed_processing_time = current_time - start_time
                    est_total_processing_time = (elapsed_processing_time / processed_items_count) * total_need_translate if processed_items_count > 0 else 0
                    remaining_processing_time = max(0, est_total_processing_time - elapsed_processing_time)
                    
                    status_update_msg = (f"已处理批次: {completed_batches_count}/{total_batches_to_process} "
                                         f"| 需译原文: {processed_items_count}/{total_need_translate} ({progress_percentage:.1f}%) "
                                         f"| 预填: {overall_default_db_prefilled_count} "
                                          f"- 预计剩余: {remaining_processing_time:.0f}s")
                    message_queue.put(("status", status_update_msg))
                    message_queue.put(("progress", progress_percentage))
                    last_status_update_time = current_time

        message_queue.put(("log", ("normal", f"所有 {total_batches_to_process} 个翻译批次已提交处理。等待完成...")))
        # （as_completed 循环结束后，所有任务都已完成或异常）
        message_queue.put(("status", f"翻译处理完成: {completed_batches_count}/{total_batches_to_process} 批次。"))
        message_queue.put(("progress", 100.0)) # 确保最终是100%
        message_queue.put(("log", ("normal", "所有翻译工作线程已完成。")))


        # --- 后续处理：错误日志检查、回退CSV生成、最终JSON保存 ---
        # (这部分逻辑与上一版类似，但现在是基于 all_files_translated_data 和全局回退列表)
        errors_found_in_log_file = 0 # 与之前相同
        if os.path.exists(error_log_path):
            try:
                with open(error_log_path, 'r', encoding='utf-8') as elog_read:
                    errors_found_in_log_file = elog_read.read().count("-" * 20)
                if errors_found_in_log_file > 0:
                    message_queue.put(("log", ("warning", f"翻译共检测到 {errors_found_in_log_file} 次错误，详情见日志: {error_log_path}")))
            except Exception as e_read_log: log.error(f"读取错误日志失败: {e_read_log}")

        # --- 整理最终结果并生成回退CSV ---
        all_fallback_items_for_csv_global = [] 
        overall_explicit_fallback_count_global = 0
        
        # 遍历 all_files_translated_data 来收集回退项
        for file_name_key, translated_content_for_file in all_files_translated_data.items():
            if not isinstance(translated_content_for_file, dict): # 防御性编程
                log.error(f"严重错误: 文件 '{file_name_key}' 的翻译结果不是预期的字典格式，无法收集回退项。")
                continue
            for original_text, result_obj in translated_content_for_file.items():
                if isinstance(result_obj, dict) and result_obj.get("status") == "fallback":
                    overall_explicit_fallback_count_global += 1
                    all_fallback_items_for_csv_global.append((
                        file_name_key, # 源文件名
                        original_text, # 原文
                        result_obj.get("original_marker", "UnknownMarker"),
                        result_obj.get("failure_context", "[未知回退原因]")
                    ))
        
        if overall_explicit_fallback_count_global > 0:
            message_queue.put(("log", ("warning", f"翻译总计完成，有 {overall_explicit_fallback_count_global} 个条目使用了原文回退。")))

        message_queue.put(("log", ("normal", "检查并处理全局回退修正文件...")))
        try:
            if all_fallback_items_for_csv_global:
                log.info(f"检测到 {len(all_fallback_items_for_csv_global)} 个回退项，生成全局修正文件: {fallback_csv_path}")
                file_system.ensure_dir_exists(os.path.dirname(fallback_csv_path))
                csv_header_fallback_global = ["源文件名", "原文", "原始标记", "最终尝试结果/原因", "修正译文"]
                csv_data_fallback_global = [csv_header_fallback_global] + \
                                           [[fname, key, marker, context, ""] for fname, key, marker, context in all_fallback_items_for_csv_global]
                with open(fallback_csv_path, 'w', newline='', encoding='utf-8-sig') as f_csv_global:
                    writer_global = csv.writer(f_csv_global, quoting=csv.QUOTE_ALL)
                    writer_global.writerows(csv_data_fallback_global)
                message_queue.put(("log", ("success", f"全局回退修正文件已生成: {fallback_csv_filename}")))
            elif os.path.exists(fallback_csv_path):
                file_system.safe_remove(fallback_csv_path)
                message_queue.put(("log", ("normal", "无回退项，旧的全局修正文件已删除。")))
        except Exception as csv_err_global:
            log.exception(f"处理全局回退 CSV 时出错: {csv_err_global}")
            message_queue.put(("log", ("error", f"处理全局回退文件 ({fallback_csv_filename}) 时出错: {csv_err_global}")))

        # --- 保存最终的按文件组织的翻译JSON ---
        message_queue.put(("log", ("normal", f"正在保存按文件组织的翻译结果到: {translated_json_path}")))
        try:
            file_system.ensure_dir_exists(os.path.dirname(translated_json_path))
            
            # 在保存前重排序结果
            message_queue.put(("log", ("normal", "正在重排序翻译结果以匹配原始文件顺序...")))
            all_files_translated_data = _reorder_translation_results(untranslated_data_per_file, all_files_translated_data)
            
            with open(translated_json_path, 'w', encoding='utf-8') as f_json_final_out:
                json.dump(all_files_translated_data, f_json_final_out, ensure_ascii=False, indent=4)
            
            total_elapsed_time_overall = time.time() - start_time
            message_queue.put(("log", ("success", f"所有文件的翻译及保存完成。总耗时: {total_elapsed_time_overall:.2f} 秒。")))

            final_msg_overall = "所有文件翻译完成"
            final_status_overall = "翻译全部完成"
            final_log_level_overall = "success"
            if overall_explicit_fallback_count_global > 0:
                 final_msg_overall += f" (共 {overall_explicit_fallback_count_global} 个回退，详见 '{fallback_csv_filename}')"
                 final_status_overall += f" (有回退)"
                 final_log_level_overall = "warning"
            message_queue.put((final_log_level_overall, f"{final_msg_overall}"))
            message_queue.put(("status", final_status_overall))
            message_queue.put(("done", None))

        except Exception as final_save_json_err:
            log.exception(f"保存最终翻译 JSON 文件失败: {final_save_json_err}")
            message_queue.put(("error", f"保存最终翻译结果失败: {final_save_json_err}"))
            message_queue.put(("status", "翻译失败(最终保存错误)"))
            message_queue.put(("done", None))

    except (ValueError, FileNotFoundError, OSError, ConnectionError) as task_prep_err:
        log.error(f"翻译任务准备或初始化失败: {task_prep_err}")
        message_queue.put(("error", f"翻译任务失败: {task_prep_err}"))
        message_queue.put(("status", "翻译失败"))
        message_queue.put(("done", None))
    except Exception as general_err:
        log.exception("翻译任务执行期间发生最顶层意外错误。")
        message_queue.put(("error", f"翻译过程中发生严重错误: {general_err}"))
        message_queue.put(("status", "翻译失败"))
        message_queue.put(("done", None))

def _reorder_translation_results(untranslated_data, translated_data):
    """
    重排序翻译结果，确保与原始数据顺序一致。
    
    Args:
        untranslated_data (dict): 原始未翻译数据字典，按文件组织
        translated_data (dict): 翻译后的数据字典，按文件组织
        
    Returns:
        OrderedDict: 重排序后的翻译结果字典
    """
    reordered_results = OrderedDict()
    for file_name, original_file_data in untranslated_data.items():
        if file_name not in translated_data:
            continue
        reordered_results[file_name] = OrderedDict()
        # 按原始数据的键顺序重新排列
        for original_key in original_file_data.keys():
            if original_key in translated_data[file_name]:
                reordered_results[file_name][original_key] = translated_data[file_name][original_key]
    return reordered_results
