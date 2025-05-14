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

# --- 批量翻译工作单元 (替换 _translate_single_item_with_retry) ---
def _translate_batch_with_retry(
    batch_original_keys, # 接收一个批次的原文 key 列表
    context_items, # 用于构建上下文 prompt 的 [(key, value), ...]
    character_dictionary, # 人物词典列表
    entity_dictionary,   # 事物词典列表
    api_client,
    config,
    error_log_path,
    error_log_lock
):
    """
    翻译一个批次的文本项，包含上下文、分离的人物/事物术语表、验证、重试和批次拆分逻辑。

    Args:
        batch_original_keys (list[str]): 需要翻译的原文 Key 列表。
        context_items (list): 上下文列表 [(key, value), ...] (主要用 key 构建 prompt)。
        character_dictionary (list): 人物词典 (已解析的 dict 列表)。
        entity_dictionary (list): 事物词典 (已解析的 dict 列表)。
        api_client (DeepSeekClient): API 客户端实例。
        config (dict): 翻译配置 (包含 prompt 模板、模型、语言、重试、拆分设置等)。
        error_log_path (str): 错误日志文件路径。
        error_log_lock (threading.Lock): 错误日志文件写入锁。

    Returns:
        dict[str, tuple[str, str, str | None]]: 返回一个字典，key 是原文 key，
            value 是一个元组 (final_text, status, failure_context)。
            final_text 是最终翻译结果或原文。
            status 为 'success' 或 'fallback'。
            failure_context: 如果 status='fallback'，则包含导致回退的错误信息或最后一次尝试的（未后处理的）译文；否则为 None。
    """
    prompt_template = config.get("prompt_template", DEFAULT_TRANSLATE_CONFIG["prompt_template"])
    model_name = config.get("model", "")
    source_language = config.get("source_language", "日语")
    target_language = config.get("target_language", "简体中文")
    max_retries = config.get("max_retries", 3)
    context_lines = config.get("context_lines", 10)
    min_batch_size = 1 # 最小拆分批次大小
    batch_size = len(batch_original_keys) # 当前处理的实际批次大小

    results = {} # 存储这个批次的结果

    # --- 用于详细错误日志记录 ---
    last_failed_raw_translation_block = None
    last_failed_prompt = None
    last_failed_api_messages = None
    last_failed_api_kwargs = None
    last_failed_response_content = None
    last_validation_reason = "未知错误"
    failure_context_for_batch = None # <--- 新增: 存储导致当前批次回退的原因

    # *** 步骤 1: 预处理批次内所有原文 (PUA 替换) ***
    processed_batch_texts = [text_processing.pre_process_text_for_llm(key) for key in batch_original_keys]
    # 将所有预处理后的文本合并，用于术语匹配检查 (小写)
    combined_processed_lower = "\n".join(processed_batch_texts).lower()

    for attempt in range(max_retries + 1):
        # a. 构建上下文 (基于批次开始前的条目)
        context_original_keys = [item[0] for item in context_items[-context_lines:]]
        context_section = ""
        if context_original_keys:
            context_section = f"### 上文内容 ({source_language})\n<context>\n" + "\n".join(context_original_keys) + "\n</context>\n"

        # b. 构建适用于整个批次的术语表
        # --- 人物术语表 ---
        relevant_char_entries = []
        originals_to_include_in_glossary = set()
        char_lookup = {}
        if character_dictionary:
             char_lookup = {entry.get('原文'): entry for entry in character_dictionary if entry.get('原文')}
             for entry in character_dictionary:
                 char_original = entry.get('原文')
                 if not char_original: continue
                 # 在合并后的批次文本中查找术语
                 if char_original.lower() in combined_processed_lower:
                     originals_to_include_in_glossary.add(char_original)
                     main_name_ref = entry.get('对应原名')
                     if main_name_ref and main_name_ref in char_lookup:
                         originals_to_include_in_glossary.add(main_name_ref)
                     elif main_name_ref and main_name_ref not in char_lookup:
                          log.warning(f"人物词典不一致: 昵称 '{char_original}' 的对应原名 '{main_name_ref}' 未找到。")

             char_cols = ['原文', '译文', '对应原名', '性别', '年龄', '性格', '口吻', '描述']
             for char_original in sorted(list(originals_to_include_in_glossary)):
                 entry = char_lookup.get(char_original)
                 if entry:
                     values = [str(entry.get(col, '')) for col in char_cols]
                     entry_line = "|".join(values)
                     relevant_char_entries.append(entry_line)

        character_glossary_section = ""
        if relevant_char_entries:
            character_glossary_section = f"### 人物术语参考 (格式: {'|'.join(char_cols)})\n" + "\n".join(relevant_char_entries) + "\n"

        # --- 事物术语表 ---
        relevant_entity_entries = []
        if entity_dictionary:
            for entry in entity_dictionary:
                entity_original = entry.get('原文')
                # 在合并后的批次文本中查找术语
                if entity_original and entity_original.lower() in combined_processed_lower:
                    desc = entry.get('描述', '')
                    category = entry.get('类别', '')
                    category_desc = f"{category} - {desc}" if category and desc else category or desc
                    entry_line = f"{entry['原文']}|{entry.get('译文', '')}|{category_desc}"
                    relevant_entity_entries.append(entry_line)

        entity_glossary_section = ""
        if relevant_entity_entries:
            entity_glossary_section = "### 事物术语参考 (格式: 原文|译文|类别 - 描述)\n" + "\n".join(relevant_entity_entries) + "\n"

        # c. 构建包含批次内所有文本的最终 Prompt
        # 将批次内所有 PUA 处理后的文本格式化为带编号的列表
        numbered_batch_text_lines = [f"{i+1}.{text}" for i, text in enumerate(processed_batch_texts)]
        batch_text_for_prompt = "\n".join(numbered_batch_text_lines)
        timestamp_suffix = f"\n[timestamp: {datetime.datetime.now().timestamp()}]" if attempt > 0 else ""

        current_final_prompt = prompt_template.format(
            source_language=source_language,
            target_language=target_language,
            character_glossary_section=character_glossary_section,
            entity_glossary_section=entity_glossary_section,
            context_section=context_section,
            batch_text=batch_text_for_prompt, # 使用格式化后的多行文本
            # target_language_placeholder=target_language # prompt模板中可能不再需要这个
        ) + timestamp_suffix

        # d. 调用 API
        log.debug(f"调用 API 翻译批次 (大小: {batch_size}, 尝试 {attempt+1}/{max_retries+1})")
        current_api_messages = [{"role": "user", "content": current_final_prompt}]
        current_api_kwargs = {}
        if "temperature" in config: current_api_kwargs["temperature"] = config["temperature"]
        if "max_tokens" in config: current_api_kwargs["max_tokens"] = config["max_tokens"]

        # --- 新增日志: 记录 API 请求详情 ---
        log.debug(f"[Translate Task] Attempt {attempt+1}/{max_retries+1}: Calling API model '{model_name}' for batch size {batch_size}.")
        # 记录 API URL，有助于区分不同的 API 源
        if hasattr(api_client, 'base_url'): # DeepSeekClient 有这个属性
             log.debug(f"[Translate Task]   API URL: {api_client.base_url}")
        # 记录完整的 messages 和 kwargs，使用 json.dumps 美化输出且处理非 ASCII 字符
        try:
             log.debug(f"[Translate Task]   API Messages (full):\n{json.dumps(current_api_messages, indent=2, ensure_ascii=False)}")
             log.debug(f"[Translate Task]   API Kwargs:\n{json.dumps(current_api_kwargs, indent=2, ensure_ascii=False)}")
        except Exception as e:
             log.warning(f"[Translate Task] Failed to log API messages/kwargs: {e}")
        # ----------------------------------------

        success, current_response_content, error_message = api_client.chat_completion(
            model_name,
            current_api_messages,
            **current_api_kwargs
        )

        # --- 新增日志: 记录 API 返回详情 ---
        if success:
            log.debug(f"[Translate Task] Attempt {attempt+1}: API call successful.")
            # 记录原始的响应内容，这对于调试响应格式问题非常重要
            log.debug(f"[Translate Task]   Raw response content (full):\n{current_response_content}")
        else:
            log.warning(f"[Translate Task] Attempt {attempt+1}: API call failed. Error: {error_message}")
            # API错误信息已在后续逻辑 (_log_batch_error) 中记录到专门的错误日志文件，这里在主日志流中简要记录。
        # ---------------------------------------

        # --- 记录本次尝试的请求和响应信息 ---
        last_failed_prompt = current_final_prompt
        last_failed_api_messages = current_api_messages
        last_failed_api_kwargs = current_api_kwargs
        last_failed_response_content = current_response_content if success else f"[API错误: {error_message}]"

        if not success:
            log.warning(f"API 调用失败 (批次大小 {batch_size}, 尝试 {attempt+1}): {error_message}")
            last_failed_raw_translation_block = f"[API错误: {error_message}]"
            last_validation_reason = f"API调用失败: {error_message}"
            failure_context_for_batch = f"[API调用失败: {error_message}]" # <--- 更新: 记录失败原因

            # *** 记录详细错误到文件 ***
            _log_batch_error(error_log_path, error_log_lock, "API 调用失败", batch_original_keys,
                             last_validation_reason, model_name, last_failed_api_kwargs,
                             last_failed_api_messages, last_failed_response_content, attempt, max_retries)

            if attempt < max_retries:
                time.sleep(1) # 简单等待后重试
                continue
            else:
                break # API 连续失败，跳出重试

        # e. 提取并根据编号重组翻译结果
        match = re.search(r'<textarea>(.*?)</textarea>', current_response_content, re.DOTALL | re.IGNORECASE) # 忽略大小写
        raw_translated_block = ""
        # 使用字典来存储按编号提取的翻译，键是行号(int)，值是该编号对应的完整翻译文本(str)
        numbered_translations = {}
        max_number_found = 0 # 追踪找到的最大编号

        if match:
            raw_translated_block = match.group(1).strip()
            raw_lines = raw_translated_block.split('\n') # 仍然先按行分割

            current_number = -1 # 当前正在收集文本的编号
            current_text_parts = [] # 临时存储当前编号的文本片段

            for line in raw_lines:
                # 注意：这里不strip()行首空格，保留模型可能输出的缩进，但在匹配编号时strip
                stripped_line_for_match = line.lstrip() # 只移除左侧空格用于匹配编号
                line_match = re.match(r'^(\d+)\.\s*(.*)', stripped_line_for_match) # 匹配 "数字." 开头

                if line_match:
                    # 找到了一个新的编号行
                    num = int(line_match.group(1))
                    text_after_number = line_match.group(2)

                    # 1. 保存上一个编号收集到的文本 (如果存在)
                    if current_number != -1:
                        numbered_translations[current_number] = "\n".join(current_text_parts).rstrip() # 合并片段并移除末尾可能的多余换行

                    # 2. 开始收集新编号的文本
                    current_number = num
                    current_text_parts = [text_after_number] # 新的片段列表，包含当前行的文本 (编号之后的部分)
                    max_number_found = max(max_number_found, current_number)

                elif current_number != -1:
                    # 没有找到新编号，并且我们正在收集某个编号的文本
                    # 将当前行追加到当前编号的文本片段中 (保留原始行的格式)
                    current_text_parts.append(line) # 直接添加原始行，保留其内部换行和格式

                # else: 如果 current_number == -1 并且没匹配到编号，说明是 <textarea> 内但在第一个编号之前的内容，忽略。

            # 保存最后一个编号收集到的文本
            if current_number != -1:
                numbered_translations[current_number] = "\n".join(current_text_parts).rstrip()

        else: # API 响应未找到 <textarea>
            log.warning(f"API 响应未找到 <textarea>，批次翻译可能失败。响应: '{current_response_content[:100]}...'")
            raw_translated_block = current_response_content.strip()
            last_validation_reason = "响应格式错误：未找到 <textarea>"
            failure_context_for_batch = "[响应格式错误：未找到 <textarea>]" # <--- 更新: 记录失败原因
            # 记录错误 (保持不变)
            _log_batch_error(error_log_path, error_log_lock, "响应格式错误", batch_original_keys,
                             last_validation_reason, model_name, last_failed_api_kwargs,
                             last_failed_api_messages, last_failed_response_content, attempt, max_retries)
            if attempt < max_retries:
                continue # 重试
            else:
                break # 格式错误且达到重试次数

        last_failed_raw_translation_block = raw_translated_block # 记录本次原始译文块

        # f. 验证翻译结果 (基于编号重组后的结果)
        # 检查是否成功提取了从 1 到 batch_size 的所有编号的翻译
        missing_numbers = []
        final_translated_lines = [] # 存储最终按顺序排列的、重组好的翻译行
        all_numbers_found = True
        for i in range(1, batch_size + 1):
            if i not in numbered_translations:
                missing_numbers.append(i)
                all_numbers_found = False
            else:
                # 如果找到了编号，将其对应的文本添加到最终列表
                final_translated_lines.append(numbered_translations[i])

        if all_numbers_found:
            # 找到了所有预期的编号
            log.info(f"批次翻译验证通过 (所有 {batch_size} 个编号均找到，尝试 {attempt+1})")

            # 现在 final_translated_lines 包含了 batch_size 条按顺序排列的翻译文本
            # 可以进行后续处理（PUA恢复、后处理等）
            temp_results = {}
            valid_batch = True # 假设批次有效，除非后续单行验证失败
            # --- 存储单行验证失败时的译文 ---
            failed_line_original_key = None
            failed_line_raw_translation = None # <--- 新增: 存储失败行的原始（还原后）译文

            for i, original_key in enumerate(batch_original_keys):
                # 从 final_translated_lines 获取对应的重组后的翻译文本
                reconstructed_translation = final_translated_lines[i]

                # 应用之前的处理流程
                restored_text = text_processing.restore_pua_placeholders(reconstructed_translation)
                post_processed_text = text_processing.post_process_translation(restored_text, original_key)

                # --- 可选的单行精细验证 ---
                is_line_valid, line_reason = text_processing.validate_translation(
                    original_key, restored_text, post_processed_text
                )
                if not is_line_valid:
                    log.warning(f"批次内单行验证失败 (尝试 {attempt+1}): '{original_key[:30]}...' 原因: {line_reason}")
                    last_validation_reason = f"单行验证失败: {line_reason} (原文: {original_key[:30]}...)"
                    failure_context_for_batch = restored_text # <--- 更新: 记录导致失败的还原后译文
                    failed_line_original_key = original_key  # 记录失败的原文
                    failed_line_raw_translation = reconstructed_translation # 记录失败的原始译文（API返回）
                    valid_batch = False
                    # 记录错误
                    _log_batch_error(error_log_path, error_log_lock, "单行验证失败", batch_original_keys,
                                     last_validation_reason, model_name, last_failed_api_kwargs,
                                     last_failed_api_messages, last_failed_response_content, attempt, max_retries,
                                     failed_item_index=i, raw_item_translation=reconstructed_translation)
                    break # 失败则跳出

                temp_results[original_key] = (post_processed_text, 'success', None)
                # --- 可选验证结束 ---

                # 如果没有启用单行验证，或者验证通过了
                # temp_results[original_key] = (post_processed_text, 'success') # 这行移到可选验证内部或之后

            if valid_batch: # 只有在所有行都验证通过时才返回成功 (如果启用了单行验证)
                return temp_results # 成功，返回整个批次的结果

            # 如果是因为单行验证失败跳出循环 (valid_batch is False)
            if attempt < max_retries:
                 log.info(f"批次内单行验证失败，准备重试批次...")
                 continue # 重试整个批次
            else:
                 log.error(f"批次内单行验证失败，达到最大重试次数 ({max_retries+1})")
                 # 确保 failure_context_for_batch 存储的是导致失败的 restored_text
                 failure_context_for_batch = failure_context_for_batch or "[单行验证失败，达到最大重试]" # 兜底
                 break # 跳出重试，进入拆分或回退

        else: # 没有找到所有编号 1 到 batch_size
            log.warning(f"验证失败 (尝试 {attempt+1}): 未能从响应中提取所有预期的编号。")
            log.warning(f"  期望编号: 1-{batch_size}")
            log.warning(f"  实际找到的最大编号: {max_number_found}")
            log.warning(f"  缺失的编号: {missing_numbers}")
            log.warning(f"  原始响应块 (截断): {raw_translated_block[:200]}...") # 记录部分原始块帮助调试
            last_validation_reason = f"响应缺少编号 (期望 1-{batch_size}, 缺失: {missing_numbers})"
            failure_context_for_batch = f"[响应缺少编号: {missing_numbers}]" # <--- 更新: 记录失败原因
            # 记录错误
            _log_batch_error(error_log_path, error_log_lock, "响应缺少编号", batch_original_keys,
                             last_validation_reason, model_name, last_failed_api_kwargs,
                             last_failed_api_messages, last_failed_response_content, attempt, max_retries)
            if attempt < max_retries:
                log.info(f"准备重试批次...")
                continue
            else:
                log.error(f"验证失败，达到最大重试次数 ({max_retries+1})")
                failure_context_for_batch = failure_context_for_batch or "[响应缺少编号，达到最大重试]" # 兜底
                break # 跳出重试，进入拆分或回退

    # --- 重试循环结束 ---

    # g. 尝试拆分批次 (如果重试都失败了)
    current_batch_size = len(batch_original_keys)
    # 只有在批次大小大于最小允许值时才尝试拆分
    if current_batch_size > min_batch_size:
        log.warning(f"批次翻译和重试均失败 (大小: {current_batch_size})，尝试拆分批次...")
        mid_point = (current_batch_size + 1) // 2
        first_half_keys = batch_original_keys[:mid_point]
        second_half_keys = batch_original_keys[mid_point:]

        log.info(f"拆分批次为: {len(first_half_keys)} 和 {len(second_half_keys)}")

        # 递归调用处理第一个子批次
        first_half_results = _translate_batch_with_retry(
            first_half_keys, context_items, character_dictionary, entity_dictionary, api_client,
            config, error_log_path, error_log_lock
        )

        # 递归调用处理第二个子批次 (上下文保持不变，仍然是原始批次之前的上下文)
        second_half_results = _translate_batch_with_retry(
            second_half_keys, context_items, character_dictionary, entity_dictionary, api_client,
            config, error_log_path, error_log_lock
        )

        # 合并两个子批次的结果
        # 注意：子批次的结果字典可能包含 'fallback' 状态
        combined_results = {**first_half_results, **second_half_results}
        log.info(f"完成拆分批次处理 (原大小: {current_batch_size})")
        return combined_results # 返回合并后的结果，无论子批次是否成功

    else:
        # h. 无法拆分 (批次大小已达下限) 或 拆分后的单项仍然失败，执行最终回退
        log.error(f"批次翻译失败，且无法进一步拆分 (当前大小: {current_batch_size}, 最小允许: {min_batch_size})。批内所有项目将回退到原文。")
        final_failure_context = failure_context_for_batch or "[最终回退，未知具体原因]" # <--- 获取最终的失败原因
        # 记录最终回退到错误日志
        _log_batch_error(error_log_path, error_log_lock, "最终回退(无法拆分或拆分后失败)", batch_original_keys,
                         last_validation_reason, model_name, last_failed_api_kwargs,
                         last_failed_api_messages, last_failed_response_content, max_retries, max_retries) # 用 max_retries 表示最终失败

        # 为批次内所有 key 设置 fallback 状态
        fallback_results = {key: (key, 'fallback', final_failure_context) for key in batch_original_keys} # <--- 更新: 返回包含 failure_context 的元组
        return fallback_results

# --- 辅助函数：记录批次错误日志 ---
def _log_batch_error(
    error_log_path, error_log_lock, error_type, batch_keys, reason,
    model_name, api_kwargs, api_messages, response_content,
    attempt, max_retries, failed_item_index=None, raw_item_translation=None
):
    """记录批次处理中的错误到日志文件。"""
    try:
        with error_log_lock:
            with open(error_log_path, 'a', encoding='utf-8') as elog:
                elog.write(f"[{datetime.datetime.now().isoformat()}] {error_type} (尝试 {attempt+1}/{max_retries+1})\n")
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
                if api_kwargs:
                    elog.write(f"  API Kwargs: {json.dumps(api_kwargs, ensure_ascii=False)}\n")
                if response_content:
                    elog.write(f"  原始 API 响应体 (截断):\n{response_content[:500]}...\n")
                # 可选：记录完整的 Prompt (可能很长)
                if api_messages:
                    elog.write(f"  API Messages:\n{json.dumps(api_messages, indent=2, ensure_ascii=False)}\n")
                elog.write("-" * 20 + "\n")
    except Exception as log_err:
        log.error(f"写入批次错误日志失败: {log_err}")

# --- 线程工作函数 ---
def _translation_worker(
    batch_items, # 仍然接收 [(key, value), ...]
    context_items,
    character_dictionary, # 接收人物词典
    entity_dictionary,   # 接收事物词典
    api_client,
    config,
    translated_data, # 共享字典，存储最终结果
    results_lock,    # 锁 translated_data
    progress_queue,  # 用于发送进度更新的队列
    error_log_path,
    error_log_lock
):
    """处理一个批次的翻译任务，调用 _translate_batch_with_retry。"""
    batch_original_keys = [item[0] for item in batch_items] # 提取批次内的所有 keys
    batch_size = len(batch_original_keys)

    if not batch_original_keys:
        log.warning("工作线程收到空批次，跳过。")
        return

    try:
        # 直接调用批量翻译函数处理整个批次
        batch_results = _translate_batch_with_retry(
            batch_original_keys,
            context_items, # 传递批次开始前的上下文
            character_dictionary,
            entity_dictionary,
            api_client,
            config,
            error_log_path,
            error_log_lock
        )

        # 批次处理完毕后，更新共享的 translated_data 字典
        with results_lock:
            translated_data.update(batch_results)

        # 发送进度更新消息，一次性发送整个批次的大小
        progress_queue.put(batch_size)
        log.debug(f"Worker 完成批次处理，包含 {batch_size} 个条目。")

    except Exception as batch_err:
        # 捕获批量处理函数本身抛出的未预料错误 (理论上应该在内部处理了)
        log.exception(f"处理批次时发生意外顶层错误: {batch_err} - 批内所有项目将回退")
        fallback_failure_context = f"[工作线程顶层异常: {batch_err}]" # <--- 记录异常信息
        # 记录错误
        _log_batch_error(error_log_path, error_log_lock, "工作线程意外错误", batch_original_keys,
                         f"顶层异常: {batch_err}", config.get("model"), {}, [], "无响应体", 0, 0)

        # 紧急回退：将批内所有 key 标记为 fallback
        fallback_results = {key: (key, 'fallback', fallback_failure_context) for key in batch_original_keys} # <--- 更新: 包含 failure_context
        with results_lock:
            translated_data.update(fallback_results)
        # 仍然汇报进度，以便主线程不会卡住
        progress_queue.put(batch_size)

# --- 主任务函数 ---
def run_translate(game_path, works_dir, translate_config, world_dict_config, message_queue): # 添加 world_dict_config
    """
    执行 JSON 文件的翻译流程。

    Args:
        game_path (str): 游戏根目录路径。
        works_dir (str): Works 工作目录的根路径。
        translate_config (dict): 包含翻译 API 配置的字典。
        world_dict_config (dict): 包含世界观字典文件名的配置。
        message_queue (queue.Queue): 用于向主线程发送消息的队列。
    """
    processed_count = 0
    start_time = time.time()
    character_dictionary = [] # 初始化人物词典列表
    entity_dictionary = []   # 初始化事物词典列表
    # 定义回退修正 CSV 文件名
    fallback_csv_filename = "fallback_corrections.csv" # <--- 新增

    try:
        message_queue.put(("status", "正在准备翻译任务..."))
        message_queue.put(("log", ("normal", "步骤 5: 开始翻译 JSON 文件...")))

        # --- 确定路径 ---
        game_folder_name = text_processing.sanitize_filename(os.path.basename(game_path))
        if not game_folder_name: game_folder_name = "UntitledGame"
        work_game_dir = os.path.join(works_dir, game_folder_name)
        untranslated_dir = os.path.join(work_game_dir, "untranslated")
        translated_dir = os.path.join(work_game_dir, "translated")
        untranslated_json_path = os.path.join(untranslated_dir, "translation.json")
        translated_json_path = os.path.join(translated_dir, "translation_translated.json")
        error_log_path = os.path.join(translated_dir, "translation_errors.log")
        # 构建回退 CSV 的完整路径
        fallback_csv_path = os.path.join(translated_dir, fallback_csv_filename) # <--- 新增

        # 获取词典文件名
        char_dict_filename = world_dict_config.get("character_dict_filename", DEFAULT_WORLD_DICT_CONFIG["character_dict_filename"])
        entity_dict_filename = world_dict_config.get("entity_dict_filename", DEFAULT_WORLD_DICT_CONFIG["entity_dict_filename"])
        character_dict_path = os.path.join(work_game_dir, char_dict_filename)
        entity_dict_path = os.path.join(work_game_dir, entity_dict_filename)

        # 确保输出目录存在和清理旧日志 (保持不变)
        if not file_system.ensure_dir_exists(translated_dir):
            raise OSError(f"无法创建翻译输出目录: {translated_dir}")
        if os.path.exists(error_log_path):
            log.info(f"删除旧的翻译错误日志: {error_log_path}")
            file_system.safe_remove(error_log_path)

        # --- 加载未翻译 JSON (保持不变) ---
        if not os.path.exists(untranslated_json_path):
            raise FileNotFoundError(f"未找到未翻译的 JSON 文件: {untranslated_json_path}")
        message_queue.put(("log", ("normal", "加载未翻译的 JSON 文件...")))
        with open(untranslated_json_path, 'r', encoding='utf-8') as f:
            untranslated_data = json.load(f)
        original_items = list(untranslated_data.items())
        total_items = len(original_items)
        if total_items == 0:
            message_queue.put(("warning", "未翻译的 JSON 文件为空，无需翻译。"))
            message_queue.put(("status", "翻译跳过(无内容)"))
            message_queue.put(("done", None))
            return
        message_queue.put(("log", ("normal", f"成功加载 JSON，共有 {total_items} 个待翻译条目。")))
        # 存储结果的共享字典 (存储元组 (translation, status))
        translated_data = {key: None for key, _ in original_items}

        # --- 加载双词典 ---
        # 加载人物词典
        char_cols_expected = len(DEFAULT_WORLD_DICT_CONFIG['character_prompt_template'].split('\n')[1].split(',')) # 从Prompt默认值推断列数
        if os.path.exists(character_dict_path):
            message_queue.put(("log", ("normal", f"加载人物词典: {char_dict_filename}...")))
            try:
                with open(character_dict_path, 'r', newline='', encoding='utf-8-sig') as f:
                    reader = csv.DictReader(f)
                    # 简单的列名检查 (可选但推荐)
                    if not reader.fieldnames or not all(h in reader.fieldnames for h in ['原文', '译文']):
                         log.warning(f"人物词典 {char_dict_filename} 缺少必要的表头列 (至少需要'原文', '译文')。")
                    else:
                         # 过滤掉没有'原文'的行
                         character_dictionary = [row for row in reader if row.get('原文')]
                         message_queue.put(("log", ("success", f"成功加载人物词典，共 {len(character_dictionary)} 条有效条目。")))
            except Exception as e:
                log.exception(f"加载人物词典失败: {character_dict_path} - {e}")
                message_queue.put(("log", ("error", f"加载人物词典失败: {e}，将不使用人物术语。")))
        else:
            message_queue.put(("log", ("normal", f"未找到人物词典文件 ({char_dict_filename})，不使用人物术语。")))

        # 加载事物词典
        entity_cols_expected = 4 # 事物词典固定4列
        if os.path.exists(entity_dict_path):
            message_queue.put(("log", ("normal", f"加载事物词典: {entity_dict_filename}...")))
            try:
                with open(entity_dict_path, 'r', newline='', encoding='utf-8-sig') as f:
                    reader = csv.DictReader(f)
                    if not reader.fieldnames or not all(h in reader.fieldnames for h in ['原文', '译文', '类别', '描述']):
                         log.warning(f"事物词典 {entity_dict_filename} 缺少必要的表头列。")
                    else:
                         entity_dictionary = [row for row in reader if row.get('原文')]
                         message_queue.put(("log", ("success", f"成功加载事物词典，共 {len(entity_dictionary)} 条有效条目。")))
            except Exception as e:
                log.exception(f"加载事物词典失败: {entity_dict_path} - {e}")
                message_queue.put(("log", ("error", f"加载事物词典失败: {e}，将不使用事物术语。")))
        else:
            message_queue.put(("log", ("normal", f"未找到事物词典文件 ({entity_dict_filename})，不使用事物术语。")))


        # --- 获取翻译配置 (保持不变) ---
        config = translate_config.copy()
        api_url = config.get("api_url", "").strip()
        api_key = config.get("api_key", "").strip()
        model_name = config.get("model", "").strip()
        batch_size = config.get("batch_size", 10)
        concurrency = config.get("concurrency", 16)

        if not api_url or not api_key or not model_name:
             raise ValueError("DeepSeek/OpenAI 兼容 API 配置不完整 (URL, Key, Model)。")

        message_queue.put(("log", ("normal", f"翻译配置: 模型={model_name}, 并发数={concurrency}, 批次大小={batch_size}")))

        # --- 初始化 API 客户端 (保持不变) ---
        try:
            api_client = deepseek.DeepSeekClient(api_url, api_key)
            message_queue.put(("log", ("normal", "DeepSeek/OpenAI 兼容 API 客户端初始化成功。")))
        except Exception as client_err:
            raise ConnectionError(f"初始化 API 客户端失败: {client_err}") from client_err

        # --- 并发处理 ---
        results_lock = threading.Lock()
        error_log_lock = threading.Lock()
        progress_queue = queue.Queue() # 进度队列

        message_queue.put(("status", f"开始翻译，总条目: {total_items}，并发数: {concurrency}..."))
        message_queue.put(("log", ("normal", f"开始使用 {concurrency} 个工作线程进行翻译...")))

        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = []
            for i in range(0, total_items, batch_size): # 使用配置的 batch_size 分割任务
                batch = original_items[i : i + batch_size]
                if not batch: continue # 如果最后为空批次则跳过

                # 上下文：取当前批次之前的所有原始条目（受 context_lines 限制）
                context_start_index = max(0, i - config.get("context_lines", 10))
                # 上下文传递原始的 (key, value) 对，因为 worker 内部的 _translate_batch_with_retry
                # 会自己处理如何使用这些上下文（当前只用了 key）。
                # 如果需要更精细的上下文（例如包含之前的翻译结果），这里的 context 构建需要调整。
                context = original_items[context_start_index : i]

                futures.append(executor.submit(
                    _translation_worker, # worker 现在处理整个批次
                    batch,
                    context,
                    character_dictionary,
                    entity_dictionary,
                    api_client,
                    config,
                    translated_data,
                    results_lock,
                    progress_queue,
                    error_log_path,
                    error_log_lock
                ))

            # --- 监控进度 (修改累加逻辑) ---
            completed_count = 0
            last_update_time = time.time()
            update_interval = 0.5 # 每 0.5 秒更新一次状态栏，避免过于频繁

            while completed_count < total_items:
                try:
                    # 等待 worker 完成一个批次，并返回该批次的大小
                    items_in_batch = progress_queue.get(timeout=1.0)
                    completed_count += items_in_batch # 累加完成的条目数

                    current_time = time.time()
                    # 更新状态栏和进度条 (可以降低更新频率)
                    if current_time - last_update_time >= update_interval or completed_count == total_items:
                        progress_percent = (completed_count / total_items) * 100 if total_items > 0 else 0
                        elapsed_time = current_time - start_time
                        est_total_time = (elapsed_time / completed_count) * total_items if completed_count > 0 else 0
                        remaining_time = max(0, est_total_time - elapsed_time)

                        status_msg = (f"正在翻译: {completed_count}/{total_items} ({progress_percent:.1f}%) "
                                    f"- 预计剩余: {remaining_time:.0f}s")
                        message_queue.put(("status", status_msg))
                        message_queue.put(("progress", progress_percent))
                        last_update_time = current_time

                except queue.Empty:
                    # 超时，检查是否有任务异常结束 (逻辑不变)
                    all_futures_done = all(f.done() for f in futures)
                    if all_futures_done:
                        exceptions = [f.exception() for f in futures if f.exception()]
                        if exceptions:
                            log.error(f"翻译过程中出现 {len(exceptions)} 个线程错误。第一个错误: {exceptions[0]}")
                        if completed_count < total_items:
                            log.warning(f"所有线程已结束，但完成计数 ({completed_count}) 少于总数 ({total_items})。可能存在问题。强制完成。")
                            completed_count = total_items # 强制完成
                            message_queue.put(("progress", 100.0))
                            # 确保最后状态更新
                            final_status_msg = f"翻译处理完成: {completed_count}/{total_items}"
                            message_queue.put(("status", final_status_msg))
                        break
                except Exception as monitor_err:
                    log.error(f"进度监控出错: {monitor_err}")
                    break

            # 确保最终状态是 100% (如果循环正常结束)
            if completed_count >= total_items:
                final_status_msg = f"翻译处理完成: {completed_count}/{total_items}" # completed_count 可能略大于 total_items (如果最后一个批次超额?)，用 min(cc, ti) 更安全
                message_queue.put(("status", final_status_msg))
                message_queue.put(("progress", 100.0))
            message_queue.put(("log", ("normal", "所有翻译工作线程已完成。")))

        # --- 检查错误日志 (逻辑保持不变) ---
        error_count_in_log = 0
        if os.path.exists(error_log_path):
            try:
                with open(error_log_path, 'r', encoding='utf-8') as elog_read:
                    log_content = elog_read.read()
                    error_count_in_log = log_content.count("-" * 20) # 估算错误条目数
                if error_count_in_log > 0:
                    message_queue.put(("log", ("warning", f"检测到翻译过程中可能发生了 {error_count_in_log} 次错误。")))
                    message_queue.put(("log", ("warning", f"详情请查看错误日志: {error_log_path}")))
            except Exception as read_log_err:
                log.error(f"读取错误日志时出错: {read_log_err}")

        # --- 整理最终结果 (逻辑保持不变) ---
        final_translated_data = {}
        fallback_items = [] # <--- 新增: 存储回退项信息 (原文 key, 失败上下文)
        explicit_fallback_count = 0
        missing_count = 0

        # 确保原始值字典存在，用于回退
        original_values_map = dict(original_items)

        # 遍历原始 keys 保证顺序
        for key in original_values_map.keys():
            result_tuple = translated_data.get(key) # 从共享字典获取结果

            if result_tuple is None:
                # 这种情况理论上不应该发生，因为 worker 会确保所有 key 都有结果 (即使是 fallback)
                log.error(f"严重问题：条目 '{key[:50]}...' 的翻译结果在最终收集中丢失！将使用原文回退。")
                final_translated_data[key] = original_values_map[key] # 使用原始 JSON 的 value 回退
                missing_count += 1
                # 丢失也算一种回退，但没有明确的上下文
                fallback_items.append((key, "[结果丢失，强制回退]")) # <--- 新增: 记录丢失的回退
                explicit_fallback_count += 1
            else:
                # 解包三元组
                final_text, status, failure_context = result_tuple # <--- 修改: 解包三元组
                if status == 'fallback':
                    explicit_fallback_count += 1
                    final_translated_data[key] = original_values_map[key] # 显式回退也用原始 value
                    fallback_items.append((key, failure_context or "[未知回退原因]")) # <--- 新增: 记录回退项和失败上下文
                elif status == 'success':
                    final_translated_data[key] = final_text # 使用翻译结果
                else:
                    # 不期望的其他状态，也视为回退
                    log.warning(f"条目 '{key[:50]}...' 收到意外状态 '{status}'，将使用原文回退。")
                    final_translated_data[key] = original_values_map[key]
                    explicit_fallback_count += 1
                    fallback_items.append((key, f"[未知状态: {status}]")) # <--- 新增: 记录未知状态的回退


        if missing_count > 0:
            log.error(f"严重问题：有 {missing_count} 个条目的翻译结果丢失！")
            message_queue.put(("error", f"严重警告: {missing_count} 个翻译结果丢失，已强制回退。"))

        if explicit_fallback_count > 0:
            message_queue.put(("log", ("warning", f"翻译完成，有 {explicit_fallback_count} 个条目最终使用了原文回退。")))

        # --- 生成或删除回退修正 CSV 文件 ---
        message_queue.put(("log", ("normal", "检查并处理回退修正文件...")))
        try:
            if fallback_items:
                # 如果有回退项，生成 CSV 文件
                log.info(f"检测到 {len(fallback_items)} 个回退项，正在生成修正文件: {fallback_csv_path}")
                # 确保目录存在
                file_system.ensure_dir_exists(os.path.dirname(fallback_csv_path))
                # 定义 CSV 表头
                csv_header = ["原文", "最终尝试结果", "修正译文"]
                # 准备数据行
                csv_data = [csv_header] + [[key, context, ""] for key, context in fallback_items]

                # 写入 CSV 文件
                with open(fallback_csv_path, 'w', newline='', encoding='utf-8-sig') as f_csv:
                    writer = csv.writer(f_csv, quoting=csv.QUOTE_ALL)
                    writer.writerows(csv_data)
                message_queue.put(("log", ("success", f"回退修正文件已生成: {fallback_csv_filename}")))
            else:
                # 如果没有回退项，删除旧的 CSV 文件（如果存在）
                if os.path.exists(fallback_csv_path):
                    log.info(f"没有回退项，正在删除旧的修正文件: {fallback_csv_path}")
                    if file_system.safe_remove(fallback_csv_path):
                        message_queue.put(("log", ("normal", f"旧的回退修正文件已删除。")))
                    else:
                        message_queue.put(("log", ("warning", f"删除旧的回退修正文件失败: {fallback_csv_path}")))
                else:
                    log.info("没有回退项，无需生成或删除修正文件。")
        except Exception as csv_err:
            log.exception(f"处理回退修正 CSV 文件时出错: {csv_err}")
            message_queue.put(("log", ("error", f"处理回退修正文件 ({fallback_csv_filename}) 时出错: {csv_err}")))

        # --- 保存翻译后的 JSON (逻辑保持不变) ---
        message_queue.put(("log", ("normal", f"正在保存翻译结果到: {translated_json_path}")))
        try:
            # 确保目录存在
            file_system.ensure_dir_exists(os.path.dirname(translated_json_path))
            with open(translated_json_path, 'w', encoding='utf-8') as f_out:
                json.dump(final_translated_data, f_out, ensure_ascii=False, indent=4)

            elapsed = time.time() - start_time
            message_queue.put(("log", ("success", f"翻译后的 JSON 文件保存成功。耗时: {elapsed:.2f} 秒。")))

            result_message = "JSON 文件翻译完成"
            status_message = "翻译完成"
            log_level = "success"
            if explicit_fallback_count > 0:
                 result_message += f" (有 {explicit_fallback_count} 个回退，请查看 '{fallback_csv_filename}')" # <--- 修改: 提示查看 CSV
                 status_message += f" (有 {explicit_fallback_count} 个回退)"
                 log_level = "warning" # 如果有回退，使用警告级别

            message_queue.put((log_level, f"{result_message}，结果已保存。"))
            message_queue.put(("status", status_message))
            message_queue.put(("done", None))

        except Exception as save_err:
            log.exception(f"保存翻译后的 JSON 文件失败: {save_err}")
            message_queue.put(("error", f"保存翻译结果失败: {save_err}"))
            message_queue.put(("status", "翻译失败(保存错误)"))
            message_queue.put(("done", None))

    except (ValueError, FileNotFoundError, OSError, ConnectionError) as setup_err:
        log.error(f"翻译任务准备或初始化失败: {setup_err}")
        message_queue.put(("error", f"翻译任务失败: {setup_err}"))
        message_queue.put(("status", "翻译失败"))
        message_queue.put(("done", None))
    except Exception as e:
        log.exception("翻译任务执行期间发生意外错误。")
        message_queue.put(("error", f"翻译过程中发生严重错误: {e}"))
        message_queue.put(("status", "翻译失败"))
        message_queue.put(("done", None))