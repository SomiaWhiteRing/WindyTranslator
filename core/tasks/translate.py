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

# --- 翻译工作单元 ---
def _translate_single_item_with_retry(
    original_key,
    context_items, # 用于构建上下文 prompt 的 [(key, value), ...]
    character_dictionary, # 人物词典列表
    entity_dictionary,   # 事物词典列表
    api_client,
    config,
    error_log_path,
    error_log_lock
):
    """
    翻译单个文本项，包含上下文、分离的人物/事物术语表、验证、重试和拆分逻辑。

    Args:
        original_key (str): 需要翻译的原文（未经 PUA 处理）。
        context_items (list): 上下文列表 [(key, value), ...] (主要用 key 构建 prompt)。
        character_dictionary (list): 人物词典 (已解析的 dict 列表)。
        entity_dictionary (list): 事物词典 (已解析的 dict 列表)。
        api_client (DeepSeekClient): API 客户端实例。
        config (dict): 翻译配置 (包含 prompt 模板、模型、语言等)。
        error_log_path (str): 错误日志文件路径。
        error_log_lock (threading.Lock): 错误日志文件写入锁。

    Returns:
        tuple[str, str]: 返回一个元组 (final_text, status)。
            final_text 是最终翻译结果或原文。
            status 为 'success' 表示翻译成功（即使结果等于原文）。
            status 为 'fallback' 表示所有尝试失败，显式回退到原文。
    """
    prompt_template = config.get("prompt_template", DEFAULT_TRANSLATE_CONFIG["prompt_template"])
    model_name = config.get("model", "")
    source_language = config.get("source_language", "日语")
    target_language = config.get("target_language", "简体中文")
    max_retries = config.get("max_retries", 3)
    context_lines = config.get("context_lines", 10)

    # *** 步骤 1: 预处理原文 (PUA 替换) ***
    processed_original_text = text_processing.pre_process_text_for_llm(original_key)
    processed_original_text_lower = processed_original_text.lower() # 预先计算小写版本用于术语匹配

    # --- 用于详细错误日志记录 ---
    last_failed_raw_translation = None
    last_failed_prompt = None
    last_failed_api_messages = None
    last_failed_api_kwargs = None
    last_failed_response_content = None
    last_validation_reason = "未知错误"

    for attempt in range(max_retries + 1):
        # a. 构建上下文
        # 注意：这里 context_items 仍然是 [(key, value), ...]，我们只用 key
        context_original_keys = [item[0] for item in context_items[-context_lines:]]
        # context_processed_keys = [text_processing.pre_process_text_for_llm(ctx_key) for ctx_key in context_original_keys]

        context_section = ""
        if context_original_keys:
            # 在 Prompt 中使用原始 key，让模型看到未处理的上下文
            context_section = f"### 上文内容 ({source_language})\n<context>\n" + "\n".join(context_original_keys) + "\n</context>\n"

        # b. 构建独立的术语表
        # --- 人物术语表 ---
        relevant_char_entries = []
        originals_to_include_in_glossary = set()
        char_lookup = {} # 快速查找表

        if character_dictionary:
             # 填充查找表
             char_lookup = {entry.get('原文'): entry for entry in character_dictionary if entry.get('原文')}

             # 第一次遍历：查找直接匹配和关联的主名称
             for entry in character_dictionary:
                 char_original = entry.get('原文')
                 if not char_original: continue

                 # 使用预处理后的小写原文进行匹配检查
                 if char_original.lower() in processed_original_text_lower:
                     originals_to_include_in_glossary.add(char_original)
                     main_name_ref = entry.get('对应原名')
                     if main_name_ref and main_name_ref in char_lookup:
                         originals_to_include_in_glossary.add(main_name_ref)
                     elif main_name_ref and main_name_ref not in char_lookup:
                          log.warning(f"人物词典不一致: 昵称 '{char_original}' 的对应原名 '{main_name_ref}' 未找到。")

             # 第二次遍历：格式化选定的条目
             # 定义列顺序，与 Prompt 描述一致
             char_cols = ['原文', '译文', '对应原名', '性别', '年龄', '性格', '口吻', '描述']
             for char_original in sorted(list(originals_to_include_in_glossary)): # 排序使顺序稳定
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
                # 使用预处理后的小写原文进行匹配检查
                if entity_original and entity_original.lower() in processed_original_text_lower:
                    # 格式化：原文|译文|类别 - 描述
                    desc = entry.get('描述', '')
                    category = entry.get('类别', '')
                    category_desc = f"{category} - {desc}" if category and desc else category or desc
                    entry_line = f"{entry['原文']}|{entry.get('译文', '')}|{category_desc}"
                    relevant_entity_entries.append(entry_line)

        entity_glossary_section = ""
        if relevant_entity_entries:
            entity_glossary_section = "### 事物术语参考 (格式: 原文|译文|类别 - 描述)\n" + "\n".join(relevant_entity_entries) + "\n"


        # c. 构建最终 Prompt
        numbered_text = f"1.{processed_original_text}" # 使用处理后的文本发送给模型
        timestamp_suffix = f"\n[timestamp: {datetime.datetime.now().timestamp()}]" if attempt > 0 else ""

        current_final_prompt = prompt_template.format(
            source_language=source_language,
            target_language=target_language,
            character_glossary_section=character_glossary_section, # 独立的人物术语
            entity_glossary_section=entity_glossary_section,       # 独立的事物术语
            context_section=context_section,
            batch_text=numbered_text,
            target_language_placeholder=target_language
        ) + timestamp_suffix

        # d. 调用 API
        log.debug(f"调用 API 翻译 (尝试 {attempt+1}/{max_retries+1}): '{original_key[:30]}...'")
        current_api_messages = [{"role": "user", "content": current_final_prompt}]
        current_api_kwargs = {}
        if "temperature" in config: current_api_kwargs["temperature"] = config["temperature"]
        if "max_tokens" in config: current_api_kwargs["max_tokens"] = config["max_tokens"]
        # 可以在 config 中添加更多 API 参数，如 top_p, frequency_penalty 等

        success, current_response_content, error_message = api_client.chat_completion(
            model_name,
            current_api_messages,
            **current_api_kwargs
        )

        # --- 记录本次尝试的请求和响应信息 ---
        last_failed_prompt = current_final_prompt
        last_failed_api_messages = current_api_messages
        last_failed_api_kwargs = current_api_kwargs
        last_failed_response_content = current_response_content if success else f"[API错误: {error_message}]"

        if not success:
            log.warning(f"API 调用失败 (尝试 {attempt+1}): {error_message} for '{original_key[:30]}...'")
            last_failed_raw_translation = f"[API错误: {error_message}]"
            last_validation_reason = f"API调用失败: {error_message}"

            # *** 记录详细错误到文件 ***
            try:
                with error_log_lock:
                    with open(error_log_path, 'a', encoding='utf-8') as elog:
                        elog.write(f"[{datetime.datetime.now().isoformat()}] API 调用失败 (尝试 {attempt+1}/{max_retries+1})\n")
                        elog.write(f"  原文: {original_key}\n")
                        elog.write(f"  失败原因: {error_message}\n")
                        elog.write(f"  模型: {model_name}\n")
                        elog.write(f"  API Kwargs: {json.dumps(current_api_kwargs, ensure_ascii=False)}\n")
                        elog.write(f"  API Messages:\n{json.dumps(current_api_messages, indent=2, ensure_ascii=False)}\n")
                        elog.write("-" * 20 + "\n")
            except Exception as log_err:
                log.error(f"写入错误日志失败 (API 错误): {log_err}")

            if attempt < max_retries:
                time.sleep(1) # 简单等待后重试
                continue
            else:
                break # API 连续失败，跳出重试

        # e. 提取翻译结果
        # 逻辑保持不变：尝试提取 <textarea> 内容，去除 '1.' 前缀
        match = re.search(r'<textarea>(.*?)</textarea>', current_response_content, re.DOTALL)
        raw_translated_text = ""
        if match:
            translated_block = match.group(1).strip()
            if translated_block.startswith("1."):
                raw_translated_text = translated_block[2:]
            else:
                log.warning(f"API 响应 <textarea> 内未找到 '1.' 前缀: '{translated_block[:50]}...'")
                raw_translated_text = translated_block
        else:
            log.warning(f"API 响应未找到 <textarea>，尝试直接使用响应 (去除 '1.'): '{current_response_content[:50]}...'")
            cleaned_response = current_response_content.strip()
            if cleaned_response.startswith("1."):
                 raw_translated_text = cleaned_response[2:]
            else:
                 raw_translated_text = cleaned_response

        last_failed_raw_translation = raw_translated_text # 记录本次原始译文

        # f. 验证翻译
        restored_text = text_processing.restore_pua_placeholders(raw_translated_text)
        post_processed_text = text_processing.post_process_translation(restored_text, original_key)

        is_valid, current_validation_reason = text_processing.validate_translation(
            original_key,
            restored_text, # 验证时使用 PUA 恢复后的文本
            post_processed_text # 也考虑后处理后的文本
        )

        if is_valid:
            log.info(f"验证通过 (尝试 {attempt+1}): '{original_key[:30]}...' -> '{post_processed_text[:30]}...'")
            return post_processed_text, 'success' # 成功，返回最终处理后的结果
        else:
            log.warning(f"验证失败 (尝试 {attempt+1}) for '{original_key[:30]}...'. 原因: {current_validation_reason}")
            last_validation_reason = current_validation_reason

            # *** 记录详细验证错误到文件 ***
            try:
                with error_log_lock:
                    with open(error_log_path, 'a', encoding='utf-8') as elog:
                        elog.write(f"[{datetime.datetime.now().isoformat()}] 翻译验证失败 (尝试 {attempt+1}/{max_retries+1})\n")
                        elog.write(f"  原文: {original_key}\n")
                        elog.write(f"  失败原因: {current_validation_reason}\n")
                        elog.write(f"  模型: {model_name}\n")
                        elog.write(f"  API Kwargs: {json.dumps(current_api_kwargs, ensure_ascii=False)}\n")
                        elog.write(f"  原始 API 响应体:\n{current_response_content}\n")
                        elog.write(f"  提取的原始译文: {raw_translated_text}\n")
                        elog.write(f"  PUA恢复后译文: {restored_text}\n")
                        elog.write(f"  最终处理后译文 (用于验证): {post_processed_text}\n")
                        elog.write(f"  API Messages:\n{json.dumps(current_api_messages, indent=2, ensure_ascii=False)}\n")
                        elog.write("-" * 20 + "\n")
            except Exception as log_err:
                log.error(f"写入错误日志失败 (验证错误): {log_err}")

            if attempt < max_retries:
                log.info(f"准备重试...")
                continue
            else:
                log.error(f"验证失败，达到最大重试次数 ({max_retries+1}) for '{original_key[:30]}...'")
                break # 跳出重试，进入拆分或回退

    # --- 重试循环结束 ---

    # g. 尝试拆分翻译 (逻辑保持不变)
    lines = original_key.split('\n')
    if len(lines) > 1:
        log.warning(f"翻译和重试均失败，尝试拆分: '{original_key[:30]}...'")
        mid_point = (len(lines) + 1) // 2
        first_half_key = '\n'.join(lines[:mid_point])
        second_half_key = '\n'.join(lines[mid_point:])

        # 递归调用，传递双词典
        translated_first_half, first_status = _translate_single_item_with_retry(
            first_half_key, context_items, character_dictionary, entity_dictionary, api_client,
            config, error_log_path, error_log_lock
        )
        # 为第二部分构建上下文：包含原始上下文和第一部分的 key（及其翻译结果，但简单起见只用 key）
        context_for_second_half = context_items + [(first_half_key, translated_first_half if first_status == 'success' else "")] # 使用翻译结果或空串
        translated_second_half, second_status = _translate_single_item_with_retry(
            second_half_key, context_for_second_half, character_dictionary, entity_dictionary, api_client,
            config, error_log_path, error_log_lock
        )

        combined_result = translated_first_half + '\n' + translated_second_half

        # 验证合并结果是否有效（例如，检查是否产生了原文）
        # 如果合并结果不等于原始 key，即使部分失败，也视为拆分有效果
        if combined_result != original_key:
             log.info(f"拆分翻译完成，合并结果 for '{original_key[:30]}...'")
             # 重新验证合并后的结果？（可选，增加复杂性）
             # 这里简单地认为只要不回退到原文就算成功
             return combined_result, 'success'
        else:
             log.error(f"拆分翻译后所有部分仍回退到原文，最终回退: '{original_key[:30]}...'")
             # 继续下面的回退逻辑

    # h. 无法拆分或拆分失败，执行最终回退
    log.error(f"翻译、重试、拆分均失败或无法拆分，回退到原文: '{original_key[:50]}...'")
    # 记录最终回退到错误日志
    try:
        with error_log_lock:
            with open(error_log_path, 'a', encoding='utf-8') as elog:
                elog.write(f"[{datetime.datetime.now().isoformat()}] 翻译失败，使用原文回退 (所有尝试失败)\n")
                elog.write(f"  原文: {original_key}\n")
                elog.write(f"  最后失败原因: {last_validation_reason}\n")
                if last_failed_raw_translation:
                    elog.write(f"  最后尝试的原始译文: {last_failed_raw_translation}\n")
                if last_failed_response_content:
                     elog.write(f"  最后尝试的原始 API 响应体:\n{last_failed_response_content}\n")
                if model_name:
                     elog.write(f"  最后尝试的模型: {model_name}\n")
                if last_failed_api_kwargs:
                     elog.write(f"  最后尝试的 API Kwargs: {json.dumps(last_failed_api_kwargs, ensure_ascii=False)}\n")
                if last_failed_api_messages:
                     elog.write(f"  最后尝试的 API Messages:\n{json.dumps(last_failed_api_messages, indent=2, ensure_ascii=False)}\n")
                elog.write("-" * 20 + "\n")
    except Exception as log_err:
        log.error(f"写入最终回退错误日志失败: {log_err}")

    return original_key, 'fallback' # 返回原文作为最终结果

# --- 线程工作函数 ---
def _translation_worker(
    batch_items,
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
    """处理一个批次的翻译任务，调用 _translate_single_item_with_retry。"""
    processed_count = 0
    batch_results = {} # 存储当前批次的结果

    for i, (original_key, _) in enumerate(batch_items):
        try:
            # 构建当前条目的上下文（包含批内已处理的）
            # context 使用 (key, value) 对，其中 value 是之前的翻译结果或原文
            current_context = context_items + list(batch_results.items())[:i] # 使用当前批次已完成的结果作为上下文

            # 调用单个条目的翻译逻辑，传递双词典
            final_translation, status = _translate_single_item_with_retry(
                original_key,
                current_context,
                character_dictionary, # 传递
                entity_dictionary,   # 传递
                api_client,
                config,
                error_log_path,
                error_log_lock
            )
            batch_results[original_key] = (final_translation, status) # 存储元组
        except Exception as item_err:
            # 捕获单条处理中的意外错误
            log.exception(f"处理条目时发生意外错误: {item_err} for '{original_key[:50]}...' - 将使用原文回退")
            batch_results[original_key] = (original_key, 'fallback') # 回退并标记状态
            # 记录到错误日志
            try:
                with error_log_lock:
                    with open(error_log_path, 'a', encoding='utf-8') as elog:
                        elog.write(f"[{datetime.datetime.now().isoformat()}] 处理条目时发生意外错误，使用原文回退:\n")
                        elog.write(f"  原文: {original_key}\n")
                        elog.write(f"  错误: {item_err}\n")
                        elog.write("-" * 20 + "\n")
            except Exception as log_err:
                log.error(f"写入意外错误日志失败: {log_err}")
        finally:
            processed_count += 1
            # 发送进度更新消息
            progress_queue.put(1)

    # 批次处理完毕后，更新共享的 translated_data 字典
    with results_lock:
        translated_data.update(batch_results)

    log.debug(f"Worker 完成批次，处理 {processed_count} 个条目。")


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
            for i in range(0, total_items, batch_size):
                batch = original_items[i : i + batch_size]
                # 上下文：取当前批次之前的所有原始条目（受 context_lines 限制）
                context_start_index = max(0, i - config.get("context_lines", 10))
                context = original_items[context_start_index : i]
                # 注意：上下文现在只包含原始 (key, value) 对，worker 内部会根据已翻译结果构建更精确的上下文

                futures.append(executor.submit(
                    _translation_worker,
                    batch,
                    context, # 传递原始上下文
                    character_dictionary, # 传递人物词典
                    entity_dictionary,   # 传递事物词典
                    api_client,
                    config,
                    translated_data,
                    results_lock,
                    progress_queue,
                    error_log_path,
                    error_log_lock
                ))

            # --- 监控进度 (逻辑保持不变) ---
            completed_count = 0
            while completed_count < total_items:
                try:
                    progress_queue.get(timeout=1.0) # 等待 worker 完成一个条目
                    completed_count += 1

                    # 更新状态栏和进度条 (可以降低更新频率)
                    if completed_count % (max(1, total_items // 100)) == 0 or completed_count == total_items: # 大约每 1% 更新一次
                        progress_percent = (completed_count / total_items) * 100
                        elapsed_time = time.time() - start_time
                        est_total_time = (elapsed_time / completed_count) * total_items if completed_count > 0 else 0
                        remaining_time = max(0, est_total_time - elapsed_time)

                        status_msg = (f"正在翻译: {completed_count}/{total_items} ({progress_percent:.1f}%) "
                                      f"- 预计剩余: {remaining_time:.0f}s")
                        message_queue.put(("status", status_msg))
                        message_queue.put(("progress", progress_percent))

                except queue.Empty:
                    # 超时，检查是否有任务异常结束
                    all_futures_done = all(f.done() for f in futures)
                    if all_futures_done:
                        # 检查是否有异常
                        exceptions = [f.exception() for f in futures if f.exception()]
                        if exceptions:
                             log.error(f"翻译过程中出现 {len(exceptions)} 个线程错误。第一个错误: {exceptions[0]}")
                             # 可能需要更复杂的错误处理
                        # 检查完成计数是否匹配
                        if completed_count < total_items:
                            log.warning(f"所有线程已结束，但完成计数 ({completed_count}) 少于总数 ({total_items})。可能存在问题。强制完成。")
                            completed_count = total_items # 强制完成
                            message_queue.put(("progress", 100.0))
                        break # 所有 future 完成，跳出循环
                except Exception as monitor_err:
                     log.error(f"进度监控出错: {monitor_err}")
                     break # 避免监控错误导致卡死

            # 确保最终状态是 100%
            final_status_msg = f"翻译处理完成: {completed_count}/{total_items}"
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
        explicit_fallback_count = 0
        missing_count = 0

        for key, original_value in original_items: # 遍历原始项目以保持顺序
             result_tuple = translated_data.get(key) # 从共享字典获取结果

             if result_tuple is None:
                 log.error(f"条目 '{key[:50]}...' 的翻译结果丢失，将使用原文回退。")
                 final_translated_data[key] = original_value # 使用原始 JSON 的 value 回退
                 missing_count += 1
             else:
                 final_text, status = result_tuple
                 if status == 'fallback':
                     explicit_fallback_count += 1
                     final_translated_data[key] = original_value # 显式回退也用原始 value
                 else:
                     final_translated_data[key] = final_text # 使用翻译结果

        if missing_count > 0:
             log.error(f"严重问题：有 {missing_count} 个条目的翻译结果丢失！")
             message_queue.put(("error", f"严重警告: {missing_count} 个翻译结果丢失，已强制回退。"))

        if explicit_fallback_count > 0:
             message_queue.put(("log", ("warning", f"翻译完成，有 {explicit_fallback_count} 个条目最终使用了原文回退。")))
             # 可以在状态栏提示，但避免覆盖最终的“完成”状态
             # message_queue.put(("status", f"翻译完成 (有 {explicit_fallback_count} 个回退)"))


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
                 result_message += f" (有 {explicit_fallback_count} 个回退)"
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