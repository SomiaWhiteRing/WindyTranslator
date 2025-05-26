# core/tasks/translate.py
import os
import json
import csv
import re
import time
import datetime
import logging
import queue
import threading
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor
from core.api_clients import deepseek # 导入 DeepSeek (OpenAI 兼容) 客户端
from core.utils import file_system, text_processing
# 导入默认配置以获取默认文件名和列定义
from core.config import DEFAULT_WORLD_DICT_CONFIG, DEFAULT_TRANSLATE_CONFIG

log = logging.getLogger(__name__)

# --- 批量翻译工作单元 (函数本身逻辑与上一版基本一致) ---
# 它的输入 batch_metadata_items 和 context_metadata_items 将由新的 run_translate 按文件范围传入。
# 它的输出是 {原文: 翻译结果对象}，这个结构也适合按文件汇总。
def _translate_batch_with_retry(
    batch_metadata_items, 
    context_metadata_items, 
    character_dictionary,
    entity_dictionary,
    api_client,
    config,
    error_log_path, # 全局错误日志路径
    error_log_lock,
    current_processing_file_name=None # 新增：当前处理的文件名，用于更详细的日志
):
    # (函数内部逻辑与你提供的上一版本保持一致，只是在调用 _log_batch_error 时可以考虑加入 current_processing_file_name)
    prompt_template = config.get("prompt_template", DEFAULT_TRANSLATE_CONFIG["prompt_template"])
    model_name = config.get("model", "")
    source_language = config.get("source_language", "日语")
    target_language = config.get("target_language", "简体中文")
    max_retries = config.get("max_retries", 3)
    context_lines = config.get("context_lines", 10) 
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
        actual_context_items_to_use = context_metadata_items[-context_lines:]
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
                          log.warning(f"人物词典不一致(文件: {current_processing_file_name or 'N/A'}): 昵称 '{char_original}' 的对应原名 '{main_name_ref}' 未找到。") # 添加文件名
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

        log.debug(f"调用 API 翻译批次 (文件: {current_processing_file_name or 'N/A'}, 大小: {current_batch_size}, 尝试 {attempt+1}/{max_retries+1})") # 添加文件名
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
                             file_name_for_log=current_processing_file_name) # 传递文件名
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
            for line_from_api in raw_lines_from_api:
                stripped_line_for_num_match = line_from_api.lstrip()
                num_line_match = re.match(r'^(\d+)\.\s*(.*)', stripped_line_for_num_match)
                if num_line_match:
                    num_val = int(num_line_match.group(1)); text_after_num = num_line_match.group(2)
                    if current_collecting_number != -1: numbered_translations_from_api[current_collecting_number] = "\n".join(current_collecting_text_parts).rstrip()
                    current_collecting_number = num_val; current_collecting_text_parts = [text_after_num]
                    max_number_found_in_response = max(max_number_found_in_response, current_collecting_number)
                elif current_collecting_number != -1: current_collecting_text_parts.append(line_from_api)
            if current_collecting_number != -1: numbered_translations_from_api[current_collecting_number] = "\n".join(current_collecting_text_parts).rstrip()
        else:
            log.warning(f"API 响应未找到 <textarea> (文件: {current_processing_file_name or 'N/A'}). 响应: '{api_response_content[:100]}...'")
            last_failed_raw_translation_block = api_response_content.strip()
            last_validation_reason = "响应格式错误：未找到 <textarea>"
            failure_context_for_batch_item = "响应格式错误：未找到 <textarea>"
            _log_batch_error(error_log_path, error_log_lock, "响应格式错误", batch_original_texts_for_logging,
                             last_validation_reason, model_name, last_failed_api_kwargs,
                             last_failed_api_messages, last_failed_response_content, attempt, max_retries,
                             file_name_for_log=current_processing_file_name) # 传递文件名
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
                original_text_for_validation = original_item_data["text_to_translate"]
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
                                     file_name_for_log=current_processing_file_name) # 传递文件名
                    break
                temp_results_for_this_attempt[original_text_for_validation] = {
                    "text": post_processed_text_for_validation, "status": "success", "failure_context": None,
                    "original_marker": original_item_data["original_marker"], "speaker_id": original_item_data["speaker_id"]
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
                             file_name_for_log=current_processing_file_name) # 传递文件名
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
                         file_name_for_log=current_processing_file_name) # 传递文件名
        fallback_results = {}
        for item_data in batch_metadata_items:
            original_text_key = item_data["text_to_translate"]
            fallback_results[original_text_key] = {
                "text": original_text_key, "status": "fallback", "failure_context": final_fallback_reason,
                "original_marker": item_data["original_marker"], "speaker_id": item_data["speaker_id"]
            }
        return fallback_results

# --- 辅助函数：记录批次错误日志 (添加文件名参数) ---
def _log_batch_error(
    error_log_path, error_log_lock, error_type, batch_keys, reason,
    model_name, api_kwargs, api_messages, response_content,
    attempt, max_retries, failed_item_index=None, raw_item_translation=None,
    file_name_for_log=None # 新增：可选的文件名参数
):
    """记录批次处理中的错误到日志文件。"""
    try:
        with error_log_lock:
            with open(error_log_path, 'a', encoding='utf-8') as elog:
                elog.write(f"[{datetime.datetime.now().isoformat()}] {error_type} (尝试 {attempt+1}/{max_retries+1})\n")
                if file_name_for_log: # 如果提供了文件名，记录下来
                    elog.write(f"  所属文件: {file_name_for_log}\n")
                elog.write(f"  批次大小: {len(batch_keys)}\n")
                elog.write(f"  失败原因: {reason}\n")
                if failed_item_index is not None:
                    elog.write(f"  失败原文 (索引 {failed_item_index}): {batch_keys[failed_item_index]}\n")
                    if raw_item_translation:
                        elog.write(f"  失败原文的原始译文: {raw_item_translation}\n")
                elog.write(f"  涉及原文 Keys (最多显示5条):\n")
                for i, key in enumerate(batch_keys[:5]):
                    elog.write(f"    - {key[:80]}...\n") # 原文key可能很长，截断显示
                if len(batch_keys) > 5:
                    elog.write(f"    - ... (等 {len(batch_keys) - 5} 个)\n")
                elog.write(f"  模型: {model_name}\n")
                if api_kwargs:
                    elog.write(f"  API Kwargs: {json.dumps(api_kwargs, ensure_ascii=False)}\n")
                if response_content:
                    elog.write(f"  原始 API 响应体 (截断):\n{response_content[:500]}...\n")
                if api_messages: # 记录完整的 Prompt 内容
                    elog.write(f"  API Messages (Prompt):\n{json.dumps(api_messages, indent=2, ensure_ascii=False)}\n")
                elog.write("-" * 20 + "\n")
    except Exception as log_err:
        # 避免因日志记录本身失败导致程序中断
        log.error(f"写入批次错误日志失败: {log_err}")

# --- 线程工作函数 (添加文件名参数) ---
def _translation_worker(
    batch_metadata_items,
    context_metadata_items_for_batch,
    character_dictionary,
    entity_dictionary,
    api_client,
    config,
    translated_data_shared_dict, 
    results_lock,
    progress_queue,
    error_log_path,
    error_log_lock,
    current_processing_file_name_for_worker # 新增：当前文件名
):
    """处理一个批次的翻译任务，调用 _translate_batch_with_retry。"""
    if not batch_metadata_items:
        log.warning(f"工作线程收到来自文件 '{current_processing_file_name_for_worker or 'N/A'}' 的空批次，跳过。")
        return

    batch_size_for_progress = len(batch_metadata_items)
    original_texts_in_batch_for_logging = [item["text_to_translate"] for item in batch_metadata_items]

    try:
        batch_results_from_retry_func = _translate_batch_with_retry(
            batch_metadata_items,
            context_metadata_items_for_batch,
            character_dictionary,
            entity_dictionary,
            api_client,
            config,
            error_log_path,
            error_log_lock,
            current_processing_file_name_for_worker # 传递文件名
        )
        with results_lock:
            translated_data_shared_dict.update(batch_results_from_retry_func)
        progress_queue.put(batch_size_for_progress) # 报告完成的条目数
        log.debug(f"工作线程完成文件 '{current_processing_file_name_for_worker or 'N/A'}' 的批次处理，大小: {batch_size_for_progress}。")
    except Exception as worker_exception:
        log.exception(f"工作线程处理文件 '{current_processing_file_name_for_worker or 'N/A'}' 的批次时发生意外顶层错误: {worker_exception} - 批内所有项目将回退")
        final_fallback_reason_worker_ex = f"[工作线程顶层异常({current_processing_file_name_for_worker or 'N/A'}): {worker_exception}]"
        _log_batch_error(error_log_path, error_log_lock, "工作线程意外错误", original_texts_in_batch_for_logging,
                         str(worker_exception), config.get("model"), {}, [], "无响应体", 0, 0,
                         file_name_for_log=current_processing_file_name_for_worker) # 传递文件名
        fallback_results_for_worker_ex = {}
        for item_data in batch_metadata_items:
            original_text_key = item_data["text_to_translate"]
            fallback_results_for_worker_ex[original_text_key] = {
                "text": original_text_key, "status": "fallback", "failure_context": final_fallback_reason_worker_ex,
                "original_marker": item_data["original_marker"], "speaker_id": item_data["speaker_id"]
            }
        with results_lock:
            translated_data_shared_dict.update(fallback_results_for_worker_ex)
        progress_queue.put(batch_size_for_progress) # 仍然汇报进度

# --- 主任务函数 ---
def run_translate(game_path, works_dir, translate_config, world_dict_config, message_queue):
    start_time = time.time()
    character_dictionary = [] 
    entity_dictionary = []   
    fallback_csv_filename = "fallback_corrections.csv"
    # *** 新增：用于存储所有文件翻译结果的顶层字典 ***
    all_files_translated_data = {}

    try:
        message_queue.put(("status", "正在准备翻译任务..."))
        message_queue.put(("log", ("normal", "步骤 5: 开始翻译 JSON 文件 (按文件隔离上下文)...")))

        # --- 路径和配置加载 (与之前类似，但 untranslated_data 会是按文件组织的) ---
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
            # untranslated_data_per_file 的结构是 { "文件名1": {原文1: 元数据对象1,...}, "文件名2": {...} }
            untranslated_data_per_file = json.load(f_in)
        
        if not untranslated_data_per_file:
            message_queue.put(("warning", "未翻译的 JSON 文件为空或无效，无需翻译。"))
            message_queue.put(("status", "翻译跳过(无内容)"))
            message_queue.put(("done", None))
            return
        
        # --- 加载词典 (全局共享，与之前相同) ---
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

        # --- 获取翻译配置 (与之前相同) ---
        current_translate_config = translate_config.copy()
        api_url = current_translate_config.get("api_url", "").strip()
        api_key = current_translate_config.get("api_key", "").strip()
        model_name = current_translate_config.get("model", "").strip()
        batch_size_config = current_translate_config.get("batch_size", 10)
        concurrency_config = current_translate_config.get("concurrency", 16) # 并发作用于文件内的批次
        if not api_url or not api_key or not model_name:
             raise ValueError("DeepSeek/OpenAI 兼容 API 配置不完整 (URL, Key, Model)。")
        
        try: # API客户端初始化
            api_client_instance = deepseek.DeepSeekClient(api_url, api_key)
            message_queue.put(("log", ("normal", "API 客户端初始化成功。")))
        except Exception as client_err: raise ConnectionError(f"初始化 API 客户端失败: {client_err}")

        # --- 按文件遍历并处理 ---
        total_files_to_process = len(untranslated_data_per_file)
        processed_files_count = 0
        overall_total_items_processed = 0 # 用于全局进度（可选）
        overall_total_items_in_all_files = sum(len(data) for data in untranslated_data_per_file.values())


        results_lock_obj = threading.Lock()
        error_log_lock_obj = threading.Lock() # 全局错误日志锁

        for current_file_name, data_for_this_file in untranslated_data_per_file.items():
            processed_files_count += 1
            message_queue.put(("status", f"正在处理文件: {current_file_name} ({processed_files_count}/{total_files_to_process})..."))
            message_queue.put(("log", ("normal", f"--- 开始翻译文件: {current_file_name} ---")))

            if not data_for_this_file:
                log.info(f"文件 '{current_file_name}' 不包含可翻译条目，跳过。")
                all_files_translated_data[current_file_name] = {} # 存储空结果
                continue

            # all_metadata_items_for_this_file 是当前文件内所有元数据对象的列表
            all_metadata_items_for_this_file = list(data_for_this_file.values())
            total_items_in_this_file = len(all_metadata_items_for_this_file)
            
            # translated_data_for_this_file 存储当前文件的翻译结果
            translated_data_for_this_file = {
                item_data["text_to_translate"]: None for item_data in all_metadata_items_for_this_file
            }
            
            progress_report_queue_for_file = queue.Queue() # 每个文件独立的进度队列，或调整全局队列的用法

            with ThreadPoolExecutor(max_workers=concurrency_config) as executor_for_file_batches:
                submitted_futures_for_file = []
                for i in range(0, total_items_in_this_file, batch_size_config):
                    batch_metadata_for_worker = all_metadata_items_for_this_file[i : i + batch_size_config]
                    if not batch_metadata_for_worker: continue

                    context_start_idx = max(0, i - current_translate_config.get("context_lines", 10))
                    # 上下文严格从当前文件内选取
                    context_metadata_for_worker = all_metadata_items_for_this_file[context_start_idx : i]
                    
                    submitted_futures_for_file.append(executor_for_file_batches.submit(
                        _translation_worker,
                        batch_metadata_for_worker,
                        context_metadata_for_worker,
                        character_dictionary, # 全局词典
                        entity_dictionary,   # 全局词典
                        api_client_instance,
                        current_translate_config,
                        translated_data_for_this_file, # 传递当前文件的结果字典
                        results_lock_obj, # 可以是全局锁，如果 translated_data_for_this_file 在worker内部直接修改
                        progress_report_queue_for_file,
                        error_log_path, # 全局错误日志
                        error_log_lock_obj,
                        current_file_name # 传递当前文件名给worker
                    ))
                
                # 监控当前文件的进度
                completed_items_in_file = 0
                last_file_progress_update_time = time.time()
                while completed_items_in_file < total_items_in_this_file:
                    try:
                        items_done_in_batch = progress_report_queue_for_file.get(timeout=1.0)
                        completed_items_in_file += items_done_in_batch
                        overall_total_items_processed += items_done_in_batch # 更新全局计数

                        current_time = time.time()
                        if current_time - last_file_progress_update_time > 0.5 or completed_items_in_file == total_items_in_this_file:
                            # 文件内部进度
                            file_progress_percent = (completed_items_in_file / total_items_in_this_file) * 100 if total_items_in_this_file > 0 else 0
                            # 全局进度
                            overall_progress_percent = (overall_total_items_processed / overall_total_items_in_all_files) * 100 if overall_total_items_in_all_files > 0 else 0
                            
                            status_msg_file = (f"文件 '{current_file_name}': {completed_items_in_file}/{total_items_in_this_file} ({file_progress_percent:.1f}%) "
                                               f"| 总进度: {overall_progress_percent:.1f}%")
                            message_queue.put(("status", status_msg_file))
                            message_queue.put(("progress", overall_progress_percent)) # 主进度条用全局进度
                            last_file_progress_update_time = current_time
                    except queue.Empty:
                        if all(f.done() for f in submitted_futures_for_file):
                            if completed_items_in_file < total_items_in_this_file:
                                log.warning(f"文件 '{current_file_name}' 所有线程结束，但完成计数({completed_items_in_file}) < 总数({total_items_in_this_file})。")
                                # 强制完成当前文件计数，但不影响全局 overall_total_items_processed，因为它由实际完成的批次累加
                            break 
                    except Exception as monitor_file_err:
                        log.error(f"文件 '{current_file_name}' 进度监控出错: {monitor_file_err}")
                        break
            
            # 当前文件处理完毕，将其结果存入顶层字典
            all_files_translated_data[current_file_name] = translated_data_for_this_file
            message_queue.put(("log", ("normal", f"--- 文件 '{current_file_name}' 翻译完成 ({total_items_in_this_file} 条) ---")))

        # --- 所有文件处理完毕后，进行最终的整理和保存 ---
        # (错误日志检查逻辑与之前相同，现在是全局的)
        errors_found_in_log_file = 0
        if os.path.exists(error_log_path):
            try:
                with open(error_log_path, 'r', encoding='utf-8') as elog_read:
                    errors_found_in_log_file = elog_read.read().count("-" * 20)
                if errors_found_in_log_file > 0:
                    message_queue.put(("log", ("warning", f"翻译共检测到 {errors_found_in_log_file} 次错误，详情见日志: {error_log_path}")))
            except Exception as e_read_log: log.error(f"读取错误日志失败: {e_read_log}")

        # --- 整理最终结果并生成回退CSV (现在需要遍历 all_files_translated_data) ---
        # final_json_to_save_output 仍然是按文件组织的，结构与输入JSON一致
        final_json_to_save_output = {} 
        all_fallback_items_for_csv = [] # 用于全局回退CSV
        overall_explicit_fallback_count = 0
        overall_missing_results_count = 0

        for file_name, translated_data_for_one_file in all_files_translated_data.items():
            output_for_this_file = {}
            # 原始数据也需要按文件名获取
            original_metadata_for_this_file = untranslated_data_per_file.get(file_name, {})

            for original_text_key, original_metadata_obj in original_metadata_for_this_file.items():
                translated_result_obj = translated_data_for_one_file.get(original_text_key)
                if translated_result_obj is None:
                    log.error(f"严重(文件 '{file_name}'): 条目 '{original_text_key[:50]}...' 翻译结果丢失！将回退。")
                    output_for_this_file[original_text_key] = {
                        "text": original_text_key, 
                        "original_marker": original_metadata_obj["original_marker"],
                        "speaker_id": original_metadata_obj["speaker_id"]
                    }
                    overall_missing_results_count += 1
                    all_fallback_items_for_csv.append((file_name, original_text_key, "[结果丢失，强制回退]", original_metadata_obj["original_marker"]))
                    overall_explicit_fallback_count +=1
                else:
                    output_for_this_file[original_text_key] = {
                        "text": translated_result_obj.get("text", original_text_key),
                        "original_marker": translated_result_obj.get("original_marker", original_metadata_obj["original_marker"]),
                        "speaker_id": translated_result_obj.get("speaker_id", original_metadata_obj["speaker_id"])
                    }
                    if translated_result_obj.get("status") == 'fallback':
                        overall_explicit_fallback_count += 1
                        output_for_this_file[original_text_key]["text"] = original_text_key # 确保回退用原文
                        all_fallback_items_for_csv.append((
                            file_name, 
                            original_text_key, 
                            translated_result_obj.get("failure_context") or "[未知回退原因]", 
                            original_metadata_obj["original_marker"]
                        ))
            final_json_to_save_output[file_name] = output_for_this_file
        
        if overall_missing_results_count > 0:
            message_queue.put(("error", f"总警告: {overall_missing_results_count} 个翻译结果丢失，已强制回退。"))
        if overall_explicit_fallback_count > 0:
            message_queue.put(("log", ("warning", f"翻译总计完成，有 {overall_explicit_fallback_count} 个条目使用了原文回退。")))

        # --- 生成全局回退CSV (添加文件名列) ---
        message_queue.put(("log", ("normal", "检查并处理全局回退修正文件...")))
        try:
            if all_fallback_items_for_csv:
                log.info(f"检测到 {len(all_fallback_items_for_csv)} 个回退项，生成全局修正文件: {fallback_csv_path}")
                file_system.ensure_dir_exists(os.path.dirname(fallback_csv_path))
                csv_header_fallback_global = ["源文件名", "原文", "原始标记", "最终尝试结果/原因", "修正译文"]
                csv_data_fallback_global = [csv_header_fallback_global] + \
                                           [[fname, key, marker, context, ""] for fname, key, context, marker in all_fallback_items_for_csv]
                with open(fallback_csv_path, 'w', newline='', encoding='utf-8-sig') as f_csv_global:
                    writer_global = csv.writer(f_csv_global, quoting=csv.QUOTE_ALL)
                    writer_global.writerows(csv_data_fallback_global)
                message_queue.put(("log", ("success", f"全局回退修正文件已生成: {fallback_csv_filename}")))
            elif os.path.exists(fallback_csv_path):
                file_system.safe_remove(fallback_csv_path)
                message_queue.put(("log", ("normal", "无回退项，旧的全局修正文件已删除。")))
            else:
                log.info("无回退项，无需生成或删除全局修正文件。")
        except Exception as csv_err_global:
            log.exception(f"处理全局回退 CSV 时出错: {csv_err_global}")
            message_queue.put(("log", ("error", f"处理全局回退文件 ({fallback_csv_filename}) 时出错: {csv_err_global}")))

        # --- 保存最终的按文件组织的翻译JSON ---
        message_queue.put(("log", ("normal", f"正在保存按文件组织的翻译结果到: {translated_json_path}")))
        try:
            file_system.ensure_dir_exists(os.path.dirname(translated_json_path))
            with open(translated_json_path, 'w', encoding='utf-8') as f_json_final_out:
                json.dump(final_json_to_save_output, f_json_final_out, ensure_ascii=False, indent=4)
            
            total_elapsed_time_overall = time.time() - start_time
            message_queue.put(("log", ("success", f"所有文件的翻译及保存完成。总耗时: {total_elapsed_time_overall:.2f} 秒。")))

            final_msg_overall = "所有文件翻译完成"
            final_status_overall = "翻译全部完成"
            final_log_level_overall = "success"
            if overall_explicit_fallback_count > 0:
                 final_msg_overall += f" (共 {overall_explicit_fallback_count} 个回退，详见 '{fallback_csv_filename}')"
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