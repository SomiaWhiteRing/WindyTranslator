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
import traceback # 导入 traceback 以便在 worker 异常中使用
from concurrent.futures import ThreadPoolExecutor
from typing import List, Tuple, Dict, Optional, Any # 增加类型提示

# Pydantic 相关导入
from pydantic import BaseModel, Field, ValidationError
from typing import List

from core.api_clients import deepseek # 导入客户端
from core.utils import file_system, text_processing
# 导入默认配置以获取默认文件名和列定义
from core.config import DEFAULT_WORLD_DICT_CONFIG, DEFAULT_TRANSLATE_CONFIG

log = logging.getLogger(__name__)

# --- 定义结构化输出的 Pydantic 模型 ---
class TranslationResponse(BaseModel):
    """定义期望的 API 翻译响应结构"""
    translations: List[str] = Field(
        description="包含批次中每行原文对应的译文列表。列表长度必须与输入行数完全一致。"
    )

# --- 翻译工作单元 (处理整个批次) ---
def _translate_batch_with_retry(
    original_batch: List[Tuple[str, Any]], # 输入是 [(key, original_value), ...]
    context_items: List[Tuple[str, str]], # 上下文是 [(key, translated_value_or_original), ...]
    character_dictionary: List[Dict[str, str]], # 人物词典
    entity_dictionary: List[Dict[str, str]],   # 事物词典
    api_client: deepseek.DeepSeekClient, # 使用类型提示
    config: Dict[str, Any],
    error_log_path: str,
    error_log_lock: threading.Lock
) -> Tuple[Dict[str, Tuple[str, str]], str]: # 返回 {key: (final_text, status)} 和 'success'/'fallback'/'partial_fallback'
    """
    使用结构化输出翻译一个批次的文本项，包含上下文、术语表、验证、重试和拆分逻辑。

    Args:
        original_batch (List[Tuple[str, Any]]): 需要翻译的批次 [(原文, 原始JSON值), ...]
        context_items (list): 上下文列表 [(key, value), ...] (value 是之前的翻译结果或原文)
        character_dictionary (list): 人物词典 (已解析的 dict 列表)。
        entity_dictionary (list): 事物词典 (已解析的 dict 列表)。
        api_client (DeepSeekClient): API 客户端实例。
        config (dict): 翻译配置 (包含 prompt 模板、模型、语言等)。
        error_log_path (str): 错误日志文件路径。
        error_log_lock (threading.Lock): 错误日志文件写入锁。

    Returns:
        tuple[Dict[str, Tuple[str, str]], str]: 返回一个元组 (batch_results_dict, batch_status)。
            batch_results_dict 是一个字典 {original_key: (final_text, status)}
            batch_status 为 'success' 表示批次中所有项都成功翻译（即使结果等于原文）。
            batch_status 为 'fallback' 表示整个批次所有尝试失败，全部回退到原文。
            batch_status 为 'partial_fallback' 表示拆分后部分成功部分失败。
    """
    prompt_template = config.get("prompt_template", DEFAULT_TRANSLATE_CONFIG["prompt_template"])
    model_name = config.get("model", "")
    source_language = config.get("source_language", "日语")
    target_language = config.get("target_language", "简体中文")
    max_retries = config.get("max_retries", 3)
    context_lines = config.get("context_lines", 10)
    batch_size = len(original_batch) # 当前实际批次大小

    # 提取所有原文 keys
    original_keys = [item[0] for item in original_batch]
    original_values = {item[0]: item[1] for item in original_batch} # 用于回退

    # *** 步骤 1: 预处理所有原文 (PUA 替换) ***
    processed_original_texts = [text_processing.pre_process_text_for_llm(key) for key in original_keys]
    processed_texts_lower = [text.lower() for text in processed_original_texts] # 小写版本用于术语匹配

    # --- 用于详细错误日志记录 ---
    last_failed_raw_translations = None # 将是列表
    last_failed_prompt = None
    last_failed_api_messages = None
    last_failed_api_kwargs = None
    last_failed_response_content = None # 将是解析后的对象或原始错误
    last_validation_reason = "未知错误"

    for attempt in range(max_retries + 1):
        # a. 构建上下文
        # 使用传入的 context_items (这些是 key 和它们对应的已处理结果)
        context_original_keys = [item[0] for item in context_items[-context_lines:]]
        context_section = ""
        if context_original_keys:
            # 在 Prompt 中使用原始 key，让模型看到未处理的上下文
            context_section = f"### 上文内容 ({source_language})\n<context>\n" + "\n".join(context_original_keys) + "\n</context>\n"

        # b. 构建独立的术语表 (基于整个批次的内容)
        # --- 人物术语表 ---
        relevant_char_entries = []
        originals_to_include_in_glossary = set()
        char_lookup = {} # 快速查找表

        if character_dictionary:
             char_lookup = {entry.get('原文'): entry for entry in character_dictionary if entry.get('原文')}
             # 检查批次中所有文本
             for text_lower in processed_texts_lower:
                 for entry in character_dictionary:
                     char_original = entry.get('原文')
                     if not char_original: continue
                     if char_original.lower() in text_lower:
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
            for text_lower in processed_texts_lower:
                for entry in entity_dictionary:
                    entity_original = entry.get('原文')
                    if entity_original and entity_original.lower() in text_lower:
                        desc = entry.get('描述', '')
                        category = entry.get('类别', '')
                        category_desc = f"{category} - {desc}" if category and desc else category or desc
                        entry_line = f"{entry['原文']}|{entry.get('译文', '')}|{category_desc}"
                        if entry_line not in relevant_entity_entries: # 避免重复
                             relevant_entity_entries.append(entry_line)

        entity_glossary_section = ""
        if relevant_entity_entries:
            entity_glossary_section = "### 事物术语参考 (格式: 原文|译文|类别 - 描述)\n" + "\n".join(sorted(relevant_entity_entries)) + "\n"


        # c. 构建最终 Prompt
        # 将批处理的文本编号
        numbered_batch_text = "\n".join([f"{i+1}.{text}" for i, text in enumerate(processed_original_texts)])
        timestamp_suffix = f"\n[timestamp: {datetime.datetime.now().timestamp()}]" if attempt > 0 else ""

        # *** 更新 Prompt 模板格式要求 ***
        # 移除 <textarea>，明确指示 JSON 输出和批处理
        current_final_prompt = prompt_template.format(
            source_language=source_language,
            target_language=target_language,
            character_glossary_section=character_glossary_section,
            entity_glossary_section=entity_glossary_section,
            context_section=context_section,
            batch_text=numbered_batch_text, # 使用编号的批处理文本
            target_language_placeholder=target_language # Placeholder 可能不再需要，取决于新 prompt
        ) + timestamp_suffix

        # d. 调用结构化 API
        log.debug(f"调用【结构化】API 翻译批次 (尝试 {attempt+1}/{max_retries+1}), 大小: {batch_size}, 首项: '{original_keys[0][:30]}...'")
        current_api_messages = [{"role": "user", "content": current_final_prompt}] # 简化 messages，prompt 本身包含所有信息
        current_api_kwargs = {}
        if "temperature" in config: current_api_kwargs["temperature"] = config["temperature"]
        if "max_tokens" in config: current_api_kwargs["max_tokens"] = config["max_tokens"]
        # 可以添加其他 grok 支持的参数

        success, parsed_response_object, error_message = api_client.chat_completion_structured(
            model_name,
            current_api_messages,
            response_format=TranslationResponse, # 指定 Pydantic 模型
            **current_api_kwargs
        )

        # --- 记录本次尝试的请求和响应信息 ---
        last_failed_prompt = current_final_prompt
        last_failed_api_messages = current_api_messages
        last_failed_api_kwargs = current_api_kwargs
        # 记录解析后的对象（如果成功）或错误消息
        last_failed_response_content = parsed_response_object if success else f"[API/解析错误: {error_message}]"

        if not success:
            log.warning(f"【结构化】API 调用或解析失败 (尝试 {attempt+1}): {error_message} for batch starting with '{original_keys[0][:30]}...'")
            last_failed_raw_translations = [f"[API错误: {error_message}]"] * batch_size # 标记所有行为错误
            last_validation_reason = f"API或解析失败: {error_message}"

            # *** 记录详细错误到文件 ***
            try:
                with error_log_lock:
                    with open(error_log_path, 'a', encoding='utf-8') as elog:
                        elog.write(f"[{datetime.datetime.now().isoformat()}] 【结构化】API 调用或解析失败 (尝试 {attempt+1}/{max_retries+1})\n")
                        elog.write(f"  批次大小: {batch_size}\n")
                        elog.write(f"  首项原文: {original_keys[0]}\n")
                        elog.write(f"  失败原因: {error_message}\n")
                        elog.write(f"  模型: {model_name}\n")
                        elog.write(f"  API Kwargs: {json.dumps(current_api_kwargs, ensure_ascii=False)}\n")
                        # 记录最后失败的响应内容（可能是错误信息或 None）
                        elog.write(f"  原始 API 响应/错误内容: {repr(last_failed_response_content)}\n")
                        elog.write(f"  API Messages:\n{json.dumps(current_api_messages, indent=2, ensure_ascii=False)}\n")
                        elog.write("-" * 20 + "\n")
            except Exception as log_err:
                log.error(f"写入错误日志失败 (API/解析错误): {log_err}")

            if attempt < max_retries:
                time.sleep(1) # 简单等待后重试
                continue
            else:
                break # API 连续失败，跳出重试

        # e. 提取并验证翻译结果 (来自结构化响应)
        raw_translations = parsed_response_object.translations
        last_failed_raw_translations = raw_translations # 记录本次原始译文列表

        # f. 验证翻译批次
        # f.1 检查返回数量是否匹配
        if len(raw_translations) != batch_size:
            current_validation_reason = f"翻译结果数量 ({len(raw_translations)}) 与原文数量 ({batch_size}) 不匹配。"
            log.warning(f"验证失败 (尝试 {attempt+1}): {current_validation_reason} for batch starting with '{original_keys[0][:30]}...'")
            last_validation_reason = current_validation_reason
            success = False # 标记为失败
            # *** 记录错误 *** (类似 API 错误)
            try:
                with error_log_lock:
                     with open(error_log_path, 'a', encoding='utf-8') as elog:
                        elog.write(f"[{datetime.datetime.now().isoformat()}] 翻译验证失败 - 数量不匹配 (尝试 {attempt+1}/{max_retries+1})\n")
                        elog.write(f"  批次大小: {batch_size}\n")
                        elog.write(f"  首项原文: {original_keys[0]}\n")
                        elog.write(f"  失败原因: {current_validation_reason}\n")
                        elog.write(f"  模型: {model_name}\n")
                        elog.write(f"  API Kwargs: {json.dumps(current_api_kwargs, ensure_ascii=False)}\n")
                        elog.write(f"  收到的翻译列表 (前5项): {raw_translations[:5]}\n")
                        elog.write(f"  API Messages:\n{json.dumps(current_api_messages, indent=2, ensure_ascii=False)}\n")
                        elog.write("-" * 20 + "\n")
            except Exception as log_err:
                 log.error(f"写入错误日志失败 (数量不匹配): {log_err}")
        else:
            # f.2 逐项进行后处理和基础验证（可选，可简化）
            all_valid = True
            current_batch_results = {}
            validation_reasons = []
            for i in range(batch_size):
                original_key = original_keys[i]
                raw_translated_text = raw_translations[i]
                restored_text = text_processing.restore_pua_placeholders(raw_translated_text)
                post_processed_text = text_processing.post_process_translation(restored_text, original_key)

                # 可以简化验证逻辑，例如只检查是否为空或完全等于原文
                # is_item_valid, item_reason = text_processing.validate_translation(original_key, restored_text, post_processed_text)
                # 简单的检查：非空且不完全等于原文（允许翻译结果等于原文，但如果需要更严格，取消注释下一行）
                is_item_valid = bool(post_processed_text) # and post_processed_text != original_key
                item_reason = "有效" if is_item_valid else "翻译结果为空" # or "翻译结果等于原文"

                if is_item_valid:
                    current_batch_results[original_key] = (post_processed_text, 'success')
                else:
                    all_valid = False
                    validation_reasons.append(f"索引 {i} ('{original_key[:20]}...'): {item_reason}")
                    # 记录单项失败，但继续检查其他项，批次作为一个整体重试
                    current_batch_results[original_key] = (original_values[original_key], 'validation_failed') # 暂时标记，重试时会覆盖

            if all_valid:
                log.info(f"批次验证通过 (尝试 {attempt+1}): batch starting with '{original_keys[0][:30]}...'")
                return current_batch_results, 'success' # 批次成功
            else:
                current_validation_reason = "批次中部分条目验证失败: " + "; ".join(validation_reasons)
                log.warning(f"批次验证失败 (尝试 {attempt+1}) for batch starting with '{original_keys[0][:30]}...'. 原因: {current_validation_reason}")
                last_validation_reason = current_validation_reason
                success = False # 标记批次失败以触发重试
                # *** 记录详细验证错误到文件 *** (记录整个批次的失败)
                try:
                    with error_log_lock:
                        with open(error_log_path, 'a', encoding='utf-8') as elog:
                            elog.write(f"[{datetime.datetime.now().isoformat()}] 批次翻译验证失败 (尝试 {attempt+1}/{max_retries+1})\n")
                            elog.write(f"  批次大小: {batch_size}\n")
                            elog.write(f"  首项原文: {original_keys[0]}\n")
                            elog.write(f"  失败原因: {current_validation_reason}\n")
                            elog.write(f"  模型: {model_name}\n")
                            elog.write(f"  API Kwargs: {json.dumps(current_api_kwargs, ensure_ascii=False)}\n")
                            elog.write(f"  原始 API 响应列表 (前5项): {raw_translations[:5]}\n")
                            # 可以选择性记录 post_processed 文本
                            elog.write(f"  API Messages:\n{json.dumps(current_api_messages, indent=2, ensure_ascii=False)}\n")
                            elog.write("-" * 20 + "\n")
                except Exception as log_err:
                    log.error(f"写入错误日志失败 (批次验证错误): {log_err}")


        # g. 如果失败，判断是否重试
        if not success and attempt < max_retries:
            log.info(f"准备重试批次...")
            continue
        elif not success:
            log.error(f"批次翻译达到最大重试次数 ({max_retries+1}) for batch starting with '{original_keys[0][:30]}...'")
            break # 跳出重试，进入拆分或回退

    # --- 重试循环结束 ---

    # h. 尝试拆分批次 (如果重试都失败了)
    if not success and batch_size > 1:
        log.warning(f"批次翻译和重试均失败，尝试拆分批次: size {batch_size}, starting with '{original_keys[0][:30]}...'")
        mid_point = (batch_size + 1) // 2
        first_half_batch = original_batch[:mid_point]
        second_half_batch = original_batch[mid_point:]

        # 递归调用，传递同样的上下文和词典
        # 注意：这里的上下文传递策略可以优化，但为简单起见，先用原始上下文
        first_half_results, first_status = _translate_batch_with_retry(
            first_half_batch, context_items, character_dictionary, entity_dictionary, api_client,
            config, error_log_path, error_log_lock
        )

        # 为第二部分构建上下文：包含原始上下文和第一部分的翻译结果 (如果成功)
        context_for_second_half = context_items # 基础上下文
        if first_status != 'fallback': # 如果第一部分有任何成功的结果
            # 将第一部分的结果（无论成功与否）加入上下文参考
             context_for_second_half = context_items + [(k, v[0]) for k, v in first_half_results.items()]

        second_half_results, second_status = _translate_batch_with_retry(
            second_half_batch, context_for_second_half, character_dictionary, entity_dictionary, api_client,
            config, error_log_path, error_log_lock
        )

        # 合并结果
        combined_results = {**first_half_results, **second_half_results}

        # 判断拆分是否有效：只要合并结果中至少有一个不是 fallback 状态，就认为拆分有进展
        has_success_or_partial = any(status != 'fallback' for _, status in combined_results.values())

        if has_success_or_partial:
             log.info(f"拆分批次翻译完成，部分或全部成功 for original batch starting with '{original_keys[0][:30]}...'")
             # 返回合并结果，状态标记为 'partial_fallback' 以区分完全成功
             return combined_results, 'partial_fallback'
        else:
             log.error(f"拆分批次翻译后所有部分仍回退到原文，最终回退: batch starting with '{original_keys[0][:30]}...'")
             # 继续下面的最终回退逻辑，使用合并后的全回退结果

    # i. 无法拆分或拆分完全失败，执行最终回退
    log.error(f"批次翻译、重试、拆分均失败或无法拆分，整个批次回退到原文: size {batch_size}, starting with '{original_keys[0][:50]}...'")
    final_fallback_results = {
        key: (original_values[key], 'fallback') for key in original_keys
    }
    # 记录最终回退到错误日志
    try:
        with error_log_lock:
            with open(error_log_path, 'a', encoding='utf-8') as elog:
                elog.write(f"[{datetime.datetime.now().isoformat()}] 批次翻译失败，使用原文回退 (所有尝试失败)\n")
                elog.write(f"  批次大小: {batch_size}\n")
                elog.write(f"  首项原文: {original_keys[0]}\n")
                elog.write(f"  最后失败原因: {last_validation_reason}\n")
                if last_failed_raw_translations:
                    # 只记录部分原始译文避免日志过大
                    elog.write(f"  最后尝试的原始译文列表 (前5项): {last_failed_raw_translations[:5]}\n")
                if last_failed_response_content:
                     elog.write(f"  最后尝试的原始 API 响应/错误内容: {repr(last_failed_response_content)}\n")
                if model_name:
                     elog.write(f"  最后尝试的模型: {model_name}\n")
                if last_failed_api_kwargs:
                     elog.write(f"  最后尝试的 API Kwargs: {json.dumps(last_failed_api_kwargs, ensure_ascii=False)}\n")
                if last_failed_api_messages:
                     elog.write(f"  最后尝试的 API Messages:\n{json.dumps(last_failed_api_messages, indent=2, ensure_ascii=False)}\n")
                elog.write("-" * 20 + "\n")
    except Exception as log_err:
        log.error(f"写入最终批次回退错误日志失败: {log_err}")

    return final_fallback_results, 'fallback' # 返回全部回退的结果


# --- 线程工作函数 ---
def _translation_worker(
    batch_items: List[Tuple[str, Any]], # 输入是 [(key, original_value), ...]
    context_items: List[Tuple[str, str]], # 上下文是 [(key, translated_value_or_original), ...]
    character_dictionary: List[Dict[str, str]],
    entity_dictionary: List[Dict[str, str]],
    api_client: deepseek.DeepSeekClient,
    config: Dict[str, Any],
    translated_data: Dict[str, Optional[Tuple[str, str]]], # 共享字典
    results_lock: threading.Lock,    # 锁 translated_data
    progress_queue: queue.Queue,  # 用于发送进度更新的队列
    error_log_path: str,
    error_log_lock: threading.Lock
):
    """处理一个批次的翻译任务，调用 _translate_batch_with_retry。"""
    batch_size = len(batch_items)
    if batch_size == 0:
        return # 空批次直接返回

    batch_start_key = batch_items[0][0][:50] if batch_items else "N/A"

    try:
        # 调用【批处理】的翻译逻辑
        batch_results_dict, batch_status = _translate_batch_with_retry(
            batch_items,
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
            translated_data.update(batch_results_dict)

        log.debug(f"Worker 完成批次 (状态: {batch_status}), 大小: {batch_size}, 起始: '{batch_start_key}...'.")

    except Exception as batch_err:
        # 捕获 _translate_batch_with_retry 或其内部未处理的意外错误
        log.exception(f"处理批次时发生意外严重错误: {batch_err} for batch starting with '{batch_start_key}...'. 整个批次将回退。")
        # 标记整个批次为回退
        fallback_results = {
            item[0]: (item[1], 'fallback_exception') # 使用原始 JSON 值回退，标记特殊状态
            for item in batch_items
        }
        with results_lock:
            translated_data.update(fallback_results)
        # 记录到错误日志
        try:
            with error_log_lock:
                with open(error_log_path, 'a', encoding='utf-8') as elog:
                    elog.write(f"[{datetime.datetime.now().isoformat()}] 处理批次时发生意外错误，整个批次回退:\n")
                    elog.write(f"  批次大小: {batch_size}\n")
                    elog.write(f"  首项原文: {batch_start_key}...\n")
                    elog.write(f"  错误: {batch_err}\n")
                    elog.write(f"  Traceback:\n{traceback.format_exc()}\n") # 包含 traceback
                    elog.write("-" * 20 + "\n")
        except Exception as log_err:
            log.error(f"写入批次意外错误日志失败: {log_err}")

    finally:
        # 不论成功失败，都报告整个批次已处理完毕
        progress_queue.put(batch_size)


# --- 主任务函数 (run_translate) ---
# 主要修改：调用 worker 时传递上下文的方式，以及进度更新的理解
# 注意：run_translate 函数本身结构变化不大，主要是调用 worker 的方式改变了
def run_translate(game_path, works_dir, translate_config, world_dict_config, message_queue): # 添加 world_dict_config
    """
    执行 JSON 文件的翻译流程 (使用批处理和结构化输出)。

    Args:
        game_path (str): 游戏根目录路径。
        works_dir (str): Works 工作目录的根路径。
        translate_config (dict): 包含翻译 API 配置的字典。
        world_dict_config (dict): 包含世界观字典文件名的配置。
        message_queue (queue.Queue): 用于向主线程发送消息的队列。
    """
    # --- 初始化和路径设置 (保持不变) ---
    processed_count = 0
    start_time = time.time()
    character_dictionary = []
    entity_dictionary = []

    try:
        message_queue.put(("status", "正在准备翻译任务..."))
        message_queue.put(("log", ("normal", "步骤 5: 开始翻译 JSON 文件 (使用批处理模式)...")))

        game_folder_name = text_processing.sanitize_filename(os.path.basename(game_path))
        if not game_folder_name: game_folder_name = "UntitledGame"
        work_game_dir = os.path.join(works_dir, game_folder_name)
        untranslated_dir = os.path.join(work_game_dir, "untranslated")
        translated_dir = os.path.join(work_game_dir, "translated")
        untranslated_json_path = os.path.join(untranslated_dir, "translation.json")
        translated_json_path = os.path.join(translated_dir, "translation_translated.json")
        error_log_path = os.path.join(translated_dir, "translation_errors.log")

        char_dict_filename = world_dict_config.get("character_dict_filename", DEFAULT_WORLD_DICT_CONFIG["character_dict_filename"])
        entity_dict_filename = world_dict_config.get("entity_dict_filename", DEFAULT_WORLD_DICT_CONFIG["entity_dict_filename"])
        character_dict_path = os.path.join(work_game_dir, char_dict_filename)
        entity_dict_path = os.path.join(work_game_dir, entity_dict_filename)

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
        # original_items 现在是 [(key, original_value), ...]
        original_items = list(untranslated_data.items())
        total_items = len(original_items)
        if total_items == 0:
            # ... (处理空文件，代码不变) ...
            message_queue.put(("warning", "未翻译的 JSON 文件为空，无需翻译。"))
            message_queue.put(("status", "翻译跳过(无内容)"))
            message_queue.put(("done", None))
            return
        message_queue.put(("log", ("normal", f"成功加载 JSON，共有 {total_items} 个待翻译条目。")))
        # translated_data 初始化为 {key: None}
        translated_data = {key: None for key, _ in original_items}

        # --- 加载双词典 (保持不变) ---
        # ... (加载 character_dictionary 的代码) ...
        # ... (加载 entity_dictionary 的代码) ...
        # (加载逻辑与之前相同)
        char_cols_expected = len(DEFAULT_WORLD_DICT_CONFIG['character_prompt_template'].split('\n')[1].split(',')) # 从Prompt默认值推断列数
        if os.path.exists(character_dict_path):
            message_queue.put(("log", ("normal", f"加载人物词典: {char_dict_filename}...")))
            try:
                with open(character_dict_path, 'r', newline='', encoding='utf-8-sig') as f:
                    reader = csv.DictReader(f)
                    if not reader.fieldnames or not all(h in reader.fieldnames for h in ['原文', '译文']):
                         log.warning(f"人物词典 {char_dict_filename} 缺少必要的表头列 (至少需要'原文', '译文')。")
                    else:
                         character_dictionary = [row for row in reader if row.get('原文')]
                         message_queue.put(("log", ("success", f"成功加载人物词典，共 {len(character_dictionary)} 条有效条目。")))
            except Exception as e:
                log.exception(f"加载人物词典失败: {character_dict_path} - {e}")
                message_queue.put(("log", ("error", f"加载人物词典失败: {e}，将不使用人物术语。")))
        else:
            message_queue.put(("log", ("normal", f"未找到人物词典文件 ({char_dict_filename})，不使用人物术语。")))

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
        batch_size = config.get("batch_size", 10) # batch_size 现在定义了 API 调用中的行数
        concurrency = config.get("concurrency", 16) # 并发线程数

        if not api_url or not api_key or not model_name:
             raise ValueError("API 配置不完整 (URL, Key, Model)。请检查 translate_config。")

        message_queue.put(("log", ("normal", f"翻译配置: 模型={model_name}, 并发数={concurrency}, API批次大小={batch_size}")))

        # --- 初始化 API 客户端 (保持不变) ---
        try:
            api_client = deepseek.DeepSeekClient(api_url, api_key)
            message_queue.put(("log", ("normal", "API 客户端初始化成功。")))
            # 可选：执行连接测试
            # test_success, test_msg = api_client.test_connection(model_name)
            # message_queue.put(("log", ("normal" if test_success else "error", f"API 连接测试: {test_msg}")))
            # if not test_success:
            #      raise ConnectionError("API 连接测试失败。")
            # # 可选：执行结构化输出测试
            # class SimpleSchema(BaseModel): name: str; age: int
            # struct_test_success, struct_test_msg = api_client.test_structured_connection(model_name, SimpleSchema)
            # message_queue.put(("log", ("normal" if struct_test_success else "error", f"API 结构化输出测试: {struct_test_msg}")))
            # if not struct_test_success:
            #      log.warning("结构化输出测试失败，翻译可能无法正常工作。") # 非致命错误

        except Exception as client_err:
            raise ConnectionError(f"初始化 API 客户端失败: {client_err}") from client_err


        # --- 并发处理 ---
        results_lock = threading.Lock()
        error_log_lock = threading.Lock()
        progress_queue = queue.Queue() # 进度队列

        message_queue.put(("status", f"开始翻译，总条目: {total_items}，并发数: {concurrency}，批次大小: {batch_size}..."))
        message_queue.put(("log", ("normal", f"开始使用 {concurrency} 个工作线程进行批处理翻译...")))

        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = []
            # 按照 batch_size 切分任务
            for i in range(0, total_items, batch_size):
                # 当前批次的原始数据 [(key, original_value), ...]
                current_batch = original_items[i : i + batch_size]
                # 上下文：取当前批次之前的 *原始* 条目 (key, original_value)
                # worker 内部的 _translate_batch_with_retry 会处理这些原始上下文
                context_start_index = max(0, i - config.get("context_lines", 10))
                # 提供给 worker 的上下文应该是 [(key, 已翻译或原文), ...]
                # 但在启动时，我们还没有任何已翻译结果，所以先传递原始 key 供 worker 参考
                # 注意：严格来说，并发执行时，一个批次的上下文不包含*同时进行中*的其他批次的结果
                # 这里的上下文是基于*提交任务时*能获取到的之前的数据
                # 更精确的上下文需要更复杂的依赖管理或串行处理，目前简单处理
                # 这里传递原始的 (key, original_value) 对，worker 的 prompt 构建会用到 key
                context_batch = original_items[context_start_index : i]
                # 转换上下文格式为 [(key, value_placeholder)]，worker内部构建prompt时只用key
                # 或者，我们可以只传递原始 key 列表？但保留 (key, value) 对可能未来有用
                context_for_worker = [(ctx_key, ctx_val) for ctx_key, ctx_val in context_batch]


                futures.append(executor.submit(
                    _translation_worker, # 调用 worker
                    current_batch,       # 传递当前批次的 [(key, original_value), ...]
                    context_for_worker,  # 传递计算出的上下文 [(key, original_value), ...]
                    character_dictionary,
                    entity_dictionary,
                    api_client,
                    config,
                    translated_data,     # 共享结果字典
                    results_lock,
                    progress_queue,
                    error_log_path,
                    error_log_lock
                ))

            # --- 监控进度 ---
            # worker 完成后会 put(batch_size)，所以 completed_count 仍然代表已处理的条目总数
            completed_count = 0
            while completed_count < total_items:
                try:
                    # 等待 worker 完成一个批次，获取该批次的大小
                    items_in_batch = progress_queue.get(timeout=5.0) # 增加超时时间
                    completed_count += items_in_batch

                    # 更新状态栏和进度条 (频率可以调整)
                    if completed_count % (max(1, total_items // 50)) == 0 or completed_count >= total_items: # 更新更频繁些
                        progress_percent = min(100.0, (completed_count / total_items) * 100) # 确保不超过100%
                        elapsed_time = time.time() - start_time
                        est_total_time = (elapsed_time / completed_count) * total_items if completed_count > 0 else 0
                        remaining_time = max(0, est_total_time - elapsed_time)

                        status_msg = (f"正在翻译: {completed_count}/{total_items} ({progress_percent:.1f}%) "
                                      f"- 预计剩余: {remaining_time:.0f}s")
                        message_queue.put(("status", status_msg))
                        message_queue.put(("progress", progress_percent))

                except queue.Empty:
                    # 超时，检查 future 状态
                    all_futures_done = all(f.done() for f in futures)
                    if all_futures_done:
                        # 检查是否有线程异常退出
                        exceptions = [f.exception() for f in futures if f.exception()]
                        if exceptions:
                             log.error(f"翻译过程中出现 {len(exceptions)} 个线程错误。第一个错误: {exceptions[0]}")
                             message_queue.put(("error", f"翻译线程出错: {exceptions[0]}"))
                             # 可能需要提前终止或标记为失败

                        # 检查完成计数是否最终匹配 (可能因为异常导致不匹配)
                        final_processed = sum(f.result() if not f.exception() else 0 for f in futures if hasattr(f, 'result') and f.result() is not None) # 这不准确，因为worker不返回计数
                        # 重新计算已处理数量 (通过检查 translated_data)
                        with results_lock:
                             actually_processed = sum(1 for v in translated_data.values() if v is not None)

                        if actually_processed < total_items:
                            log.warning(f"所有线程已结束，但完成计数 ({actually_processed}/{total_items}) 可能不完整。强制完成进度。")
                        else:
                             log.info(f"所有线程已结束，完成计数 ({actually_processed}/{total_items})。")

                        completed_count = total_items # 强制完成
                        message_queue.put(("progress", 100.0))
                        break # 所有 future 完成，跳出循环
                    else:
                        # 仍在运行，继续等待
                        log.debug("进度队列超时，但有任务仍在运行...")
                        pass
                except Exception as monitor_err:
                     log.exception(f"进度监控出错: {monitor_err}")
                     message_queue.put(("error", f"进度监控错误: {monitor_err}"))
                     break # 避免监控错误导致卡死

            # --- 最终状态和日志检查 (保持不变) ---
            final_status_msg = f"翻译处理完成: {min(completed_count, total_items)}/{total_items}" # 确保不超过 total
            message_queue.put(("status", final_status_msg))
            message_queue.put(("progress", 100.0))
            message_queue.put(("log", ("normal", "所有翻译工作线程已完成。")))

            error_count_in_log = 0
            if os.path.exists(error_log_path):
                 try:
                     with open(error_log_path, 'r', encoding='utf-8') as elog_read:
                         log_content = elog_read.read()
                         # 使用更可靠的分隔符计数
                         error_count_in_log = log_content.count("-" * 20)
                     if error_count_in_log > 0:
                         message_queue.put(("log", ("warning", f"检测到翻译过程中可能发生了 {error_count_in_log} 个错误事件。")))
                         message_queue.put(("log", ("warning", f"详情请查看错误日志: {error_log_path}")))
                 except Exception as read_log_err:
                     log.error(f"读取错误日志时出错: {read_log_err}")

            # --- 整理最终结果 (逻辑微调) ---
            final_translated_data = {}
            explicit_fallback_count = 0
            missing_or_failed_count = 0 # 合并计数

            for key, original_value in original_items: # 遍历原始项目以保持顺序
                 result_tuple = translated_data.get(key) # 从共享字典获取结果 (text, status)

                 if result_tuple is None:
                     log.error(f"条目 '{key[:50]}...' 的翻译结果丢失，将使用原文回退。")
                     final_translated_data[key] = original_value # 使用原始 JSON 的 value 回退
                     missing_or_failed_count += 1
                 else:
                     final_text, status = result_tuple
                     if status.startswith('fallback'): # 包括 'fallback', 'fallback_exception'
                         explicit_fallback_count += 1
                         final_translated_data[key] = original_value # 显式回退也用原始 value
                         if status != 'fallback': # 如果是其他失败状态
                              missing_or_failed_count +=1
                     elif status == 'validation_failed': # 批次中单项验证失败，也算回退
                          explicit_fallback_count += 1
                          final_translated_data[key] = original_value
                          missing_or_failed_count += 1
                     elif status == 'partial_fallback': # 这种情况在单个条目上不应该出现，但在批次状态中可能
                          log.warning(f"条目 '{key[:50]}...' 状态为 'partial_fallback'，这不符合预期，视为成功。")
                          final_translated_data[key] = final_text
                     elif status == 'success':
                          final_translated_data[key] = final_text # 使用翻译结果
                     else: # 其他未知状态
                          log.error(f"条目 '{key[:50]}...' 存在未知状态 '{status}'，将使用原文回退。")
                          final_translated_data[key] = original_value
                          missing_or_failed_count += 1


            if missing_or_failed_count > 0:
                 log.error(f"严重问题：有 {missing_or_failed_count} 个条目的翻译结果丢失或处理失败！")
                 message_queue.put(("error", f"严重警告: {missing_or_failed_count} 个翻译结果丢失或失败，已强制回退。"))

            if explicit_fallback_count > 0:
                 message_queue.put(("log", ("warning", f"翻译完成，共 {explicit_fallback_count} 个条目最终使用了原文回退。")))


            # --- 保存翻译后的 JSON (保持不变) ---
            message_queue.put(("log", ("normal", f"正在保存翻译结果到: {translated_json_path}")))
            try:
                file_system.ensure_dir_exists(os.path.dirname(translated_json_path))
                with open(translated_json_path, 'w', encoding='utf-8') as f_out:
                    json.dump(final_translated_data, f_out, ensure_ascii=False, indent=4)

                elapsed = time.time() - start_time
                message_queue.put(("log", ("success", f"翻译后的 JSON 文件保存成功。耗时: {elapsed:.2f} 秒。")))

                result_message = "JSON 文件翻译完成"
                status_message = "翻译完成"
                log_level = "success"
                if explicit_fallback_count > 0 or missing_or_failed_count > 0 :
                     fallback_msg = f" (有 {explicit_fallback_count} 个回退"
                     if missing_or_failed_count > explicit_fallback_count:
                          fallback_msg += f", {missing_or_failed_count - explicit_fallback_count} 个其他失败/丢失"
                     fallback_msg += ")"
                     result_message += fallback_msg
                     status_message += fallback_msg
                     log_level = "warning" # 如果有回退或失败，使用警告级别

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