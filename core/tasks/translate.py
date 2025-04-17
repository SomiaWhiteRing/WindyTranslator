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

log = logging.getLogger(__name__)

# --- 翻译工作单元 ---
def _translate_single_item_with_retry(
    original_key,
    context_items, # 仅用于构建上下文 prompt
    world_dictionary,
    api_client,
    config,
    error_log_path,
    error_log_lock
):
    """
    翻译单个文本项，包含上下文、术语表、验证、重试和拆分逻辑。
    与原脚本的 _translate_item_recursive 逻辑类似，但现在是独立函数。

    Args:
        original_key (str): 需要翻译的原文（未经 PUA 处理）。
        context_items (list): 上下文列表 [(key, value), ...] (此处简化，主要用 key 构建 prompt)。
        world_dictionary (list): 世界观字典 (已解析的 dict 列表)。
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
    prompt_template = config.get("prompt_template", "")
    model_name = config.get("model", "")
    source_language = config.get("source_language", "日语")
    target_language = config.get("target_language", "简体中文")
    max_retries = config.get("max_retries", 3) # 可配置重试次数
    context_lines = config.get("context_lines", 10)

    # *** 步骤 1: 预处理原文 (PUA 替换) ***
    processed_original_text = text_processing.pre_process_text_for_llm(original_key)

    # --- 用于详细错误日志记录 ---
    last_failed_raw_translation = None # 记录最后一次失败的原始译文（API返回，未处理）
    last_failed_prompt = None           # 记录最后一次失败的完整 Prompt
    last_failed_api_messages = None     # 记录最后一次失败的 API messages
    last_failed_api_kwargs = None       # 记录最后一次失败的 API kwargs
    last_failed_response_content = None # 记录最后一次失败的原始 API 响应体
    last_validation_reason = "未知错误"  # 记录最后一次验证失败的原因

    for attempt in range(max_retries + 1):
        # a. 构建上下文和术语表
        context_original_keys = [item[1] for item in context_items[-context_lines:]]
        processed_context_keys = [text_processing.pre_process_text_for_llm(ctx_key) for ctx_key in context_original_keys]

        context_section = ""
        if processed_context_keys:
            context_section = f"### 上文内容 ({source_language})\n<context>\n" + "\n".join(context_original_keys) + "\n</context>\n"

        relevant_dict_entries = []
        if world_dictionary:
            text_lower = processed_original_text.lower()
            for entry in world_dictionary:
                dict_original = entry.get('原文')
                if dict_original and dict_original.lower() in text_lower:
                    entry_line = f"{entry['原文']}|{entry.get('译文', '')}|{entry.get('类别', '')} - {entry.get('描述', '')}"
                    relevant_dict_entries.append(entry_line)

        glossary_section = ""
        if relevant_dict_entries:
            glossary_section = "### 术语表\n原文|译文|类别 - 描述\n" + "\n".join(relevant_dict_entries) + "\n"

        # b. 构建最终 Prompt
        numbered_text = f"1.{processed_original_text}"
        timestamp_suffix = f"\n[timestamp: {datetime.datetime.now().timestamp()}]" if attempt > 0 else ""

        current_final_prompt = prompt_template.format(
            source_language=source_language,
            target_language=target_language,
            glossary_section=glossary_section,
            context_section=context_section,
            batch_text=numbered_text,
            target_language_placeholder=target_language
        ) + timestamp_suffix

        # c. 调用 API
        log.debug(f"调用 API 翻译 (尝试 {attempt+1}/{max_retries+1}): '{original_key[:30]}...'")
        current_api_messages = [{"role": "user", "content": current_final_prompt}]
        current_api_kwargs = {}
        if "temperature" in config: current_api_kwargs["temperature"] = config["temperature"]
        if "max_tokens" in config: current_api_kwargs["max_tokens"] = config["max_tokens"]

        success, current_response_content, error_message = api_client.chat_completion(
            model_name,
            current_api_messages,
            **current_api_kwargs
        )

        # --- 记录本次尝试的请求和响应信息，以备失败时使用 ---
        last_failed_prompt = current_final_prompt
        last_failed_api_messages = current_api_messages
        last_failed_api_kwargs = current_api_kwargs
        last_failed_response_content = current_response_content if success else f"[API错误: {error_message}]"

        if not success:
            log.warning(f"API 调用失败 (尝试 {attempt+1}): {error_message} for '{original_key[:30]}...'")
            last_failed_raw_translation = f"[API错误: {error_message}]" # 更新最后失败的“译文”
            last_validation_reason = f"API调用失败: {error_message}" # 更新失败原因

            # *** 在 API 调用失败时记录详细错误 ***
            try:
                with error_log_lock:
                    with open(error_log_path, 'a', encoding='utf-8') as elog:
                        elog.write(f"[{datetime.datetime.now().isoformat()}] API 调用失败 (尝试 {attempt+1}/{max_retries+1})\n")
                        elog.write(f"  原文: {original_key}\n")
                        elog.write(f"  失败原因: {error_message}\n")
                        elog.write(f"  模型: {model_name}\n")
                        elog.write(f"  API Kwargs: {json.dumps(current_api_kwargs, ensure_ascii=False)}\n")
                        elog.write(f"  API Messages:\n{json.dumps(current_api_messages, indent=2, ensure_ascii=False)}\n")
                        # elog.write(f"  完整 Prompt:\n{current_final_prompt}\n") # 可选，Messages 通常更结构化
                        elog.write("-" * 20 + "\n")
            except Exception as log_err:
                log.error(f"写入错误日志失败 (API 错误): {log_err}")

            if attempt < max_retries:
                time.sleep(1)
                continue
            else:
                break # API 连续失败，跳出重试

        # d. 提取翻译结果
        match = re.search(r'<textarea>(.*?)</textarea>', current_response_content, re.DOTALL)
        if match:
            translated_block = match.group(1).strip()
            if translated_block.startswith("1."):
                raw_translated_text = translated_block[2:]
            else:
                log.warning(f"API 响应未找到预期的 '1.' 前缀，将直接使用提取内容: '{translated_block[:50]}...'")
                raw_translated_text = translated_block
        else:
            log.warning(f"API 响应未找到 <textarea>，尝试直接使用响应内容 (移除编号): '{current_response_content[:50]}...'")
            if current_response_content.strip().startswith("1."):
                 raw_translated_text = current_response_content.strip()[2:]
            else:
                 raw_translated_text = current_response_content.strip()

        last_failed_raw_translation = raw_translated_text # 记录本次尝试的原始译文

        # e. 验证翻译
        restored_text = text_processing.restore_pua_placeholders(raw_translated_text)
        post_processed_text = text_processing.post_process_translation(restored_text, original_key)

        is_valid, current_validation_reason = text_processing.validate_translation(
            original_key,
            restored_text,
            post_processed_text
        )

        if is_valid:
            log.info(f"验证通过 (尝试 {attempt+1}): '{original_key[:30]}...' -> '{post_processed_text[:30]}...'")
            return post_processed_text, 'success' # 成功，返回最终处理后的结果
        else:
            log.warning(f"验证失败 (尝试 {attempt+1}) for '{original_key[:30]}...'. 原因: {current_validation_reason}")
            last_validation_reason = current_validation_reason # 更新最后失败原因

            # *** 在验证失败时记录详细错误 ***
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
                        elog.write(f"  最终处理后译文 (用于验证): {post_processed_text}\n")
                        elog.write(f"  API Messages:\n{json.dumps(current_api_messages, indent=2, ensure_ascii=False)}\n")
                        # elog.write(f"  完整 Prompt:\n{current_final_prompt}\n") # 可选
                        elog.write("-" * 20 + "\n")
            except Exception as log_err:
                log.error(f"写入错误日志失败 (验证错误): {log_err}")

            if attempt < max_retries:
                log.info(f"准备重试...")
                continue # 继续下一次重试
            else:
                log.error(f"验证失败，达到最大重试次数 ({max_retries+1}) for '{original_key[:30]}...'")
                break # 跳出重试循环，进入拆分或回退

    # --- 重试循环结束 ---

    # g. 尝试拆分翻译 (如果包含多行且不止一行)
    lines = original_key.split('\n')
    if len(lines) > 1:
        log.warning(f"翻译和重试均失败，尝试拆分: '{original_key[:30]}...'")
        mid_point = (len(lines) + 1) // 2
        first_half_key = '\n'.join(lines[:mid_point])
        second_half_key = '\n'.join(lines[mid_point:])

        translated_first_half, first_status = _translate_single_item_with_retry(
            first_half_key, context_items, world_dictionary, api_client,
            config, error_log_path, error_log_lock
        )
        # 为第二部分构建上下文（包含第一部分的原文或翻译结果？）
        # 简单起见，仍然使用原始上下文+第一部分的 key
        context_for_second_half = context_items + [(first_half_key, "")] # 模拟上下文
        translated_second_half, second_status = _translate_single_item_with_retry(
            second_half_key, context_for_second_half, world_dictionary, api_client,
            config, error_log_path, error_log_lock
        )
             
        combined_result = translated_first_half + '\n' + translated_second_half

        if combined_result != original_key:
             # 即使拆分的部分有 fallback，只要合并结果不等于原始 key，就视为成功避免了完全 fallback
             log.info(f"拆分翻译完成，合并结果 for '{original_key[:30]}...'")
             return combined_result, 'success' # 返回合并后的结果
        else:
             log.error(f"拆分翻译后所有部分仍回退到原文，最终回退: '{original_key[:30]}...'")
             # 继续执行下面的回退逻辑，此时 last_failed_* 变量来自第二部分的失败尝试

    # h. 无法拆分或拆分后仍然失败，执行最终回退
    log.error(f"翻译、重试、拆分均失败或无法拆分，回退到原文: '{original_key[:50]}...'")
    # 记录到错误日志 (使用最后一次失败的信息)
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
                # if last_failed_prompt:
                #     elog.write(f"  最后尝试的完整 Prompt:\n{last_failed_prompt}\n") # 可选
                elog.write("-" * 20 + "\n")
    except Exception as log_err:
        log.error(f"写入最终回退错误日志失败: {log_err}")

    return original_key, 'fallback' # 返回原文作为最终结果

# --- 线程工作函数 ---
def _translation_worker(
    batch_items,
    context_items,
    world_dictionary,
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
    batch_results = {} # 存储当前批次的结果，完成后一次性更新共享字典

    for i, (original_key, _) in enumerate(batch_items):
        try:
            # 构建当前条目的上下文（包含批内已处理的）
            current_context = context_items + batch_items[:i]
            
            # 调用单个条目的翻译逻辑
            final_translation, status = _translate_single_item_with_retry(
                original_key,
                current_context,
                world_dictionary,
                api_client,
                config,
                error_log_path,
                error_log_lock
            )
            batch_results[original_key] = (final_translation, status)
        except Exception as item_err:
            # 捕获单条处理中的意外错误（理论上不应发生）
            log.exception(f"处理条目时发生意外错误: {item_err} for '{original_key[:50]}...' - 将使用原文回退")
            batch_results[original_key] = (original_key, 'fallback') # 回退
            # 记录到错误日志
            try:
                with error_log_lock:
                    with open(error_log_path, 'a', encoding='utf-8') as elog:
                        elog.write(f"[{datetime.datetime.now().isoformat()}] 处理条目时发生意外错误，使用原文回退:\n")
                        elog.write(f"  原文: {original_key}\n")
                        elog.write(f"  错误: {item_err}\n")
                        elog.write("-" * 20 + "\n")
            except Exception as log_err:
                log.error(f"写入错误日志失败: {log_err}")
        finally:
            processed_count += 1
            # 发送进度更新消息（处理完一个条目就发）
            progress_queue.put(1) # 发送数字 1 表示完成了一个

    # 批次处理完毕后，更新共享的 translated_data 字典
    with results_lock:
        translated_data.update(batch_results)

    log.debug(f"Worker 完成批次，处理 {processed_count} 个条目。")


# --- 主任务函数 ---
def run_translate(game_path, works_dir, translate_config, message_queue):
    """
    执行 JSON 文件的翻译流程。

    Args:
        game_path (str): 游戏根目录路径。
        works_dir (str): Works 工作目录的根路径。
        translate_config (dict): 包含翻译 API (DeepSeek/OpenAI 兼容) 配置的字典。
        message_queue (queue.Queue): 用于向主线程发送消息的队列。
    """
    processed_count = 0 # 在主函数中跟踪总进度
    start_time = time.time()

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
        # 输出文件名可以自定义，或保持与原脚本一致
        translated_json_path = os.path.join(translated_dir, "translation_translated.json")
        dict_csv_path = os.path.join(work_game_dir, "world_dictionary.csv")
        error_log_path = os.path.join(translated_dir, "translation_errors.log") # 错误日志

        # 确保输出目录存在
        if not file_system.ensure_dir_exists(translated_dir):
            raise OSError(f"无法创建翻译输出目录: {translated_dir}")

        # 清理旧的错误日志
        if os.path.exists(error_log_path):
            log.info(f"发现旧的翻译错误日志，将删除: {error_log_path}")
            file_system.safe_remove(error_log_path)

        # --- 加载未翻译 JSON ---
        if not os.path.exists(untranslated_json_path):
            message_queue.put(("error", f"未找到未翻译的 JSON 文件: {untranslated_json_path}"))
            message_queue.put(("status", "翻译失败"))
            message_queue.put(("done", None))
            return
        message_queue.put(("log", ("normal", "加载未翻译的 JSON 文件...")))
        with open(untranslated_json_path, 'r', encoding='utf-8') as f:
            untranslated_data = json.load(f)
        original_items = list(untranslated_data.items()) # [(key, value), ...]
        total_items = len(original_items)
        if total_items == 0:
            message_queue.put(("warning", "未翻译的 JSON 文件为空，无需翻译。"))
            message_queue.put(("status", "翻译跳过(无内容)"))
            message_queue.put(("done", None))
            return
        message_queue.put(("log", ("normal", f"成功加载 JSON，共有 {total_items} 个待翻译条目。")))
        # 创建一个共享字典来存储结果，初始值设为 None 或标记值，以区分未处理和翻译失败回退到原文的情况
        translated_data = {key: None for key, _ in original_items}


        # --- 加载世界观字典 ---
        world_dictionary = []
        if os.path.exists(dict_csv_path):
            message_queue.put(("log", ("normal", "加载世界观字典...")))
            try:
                with open(dict_csv_path, 'r', newline='', encoding='utf-8-sig') as f:
                    # 跳过表头行
                    reader = csv.DictReader(f)
                    world_dictionary = [row for row in reader if row.get('原文')] # 确保原文存在
                message_queue.put(("log", ("success", f"成功加载世界观字典，共 {len(world_dictionary)} 条有效条目。")))
            except Exception as e:
                log.exception(f"加载世界观字典失败: {dict_csv_path} - {e}")
                message_queue.put(("log", ("error", f"加载世界观字典失败: {e}，将不使用字典。")))
        else:
            message_queue.put(("log", ("normal", "未找到世界观字典文件，不使用字典。")))

        # --- 获取翻译配置 ---
        config = translate_config.copy() # 使用配置副本
        api_url = config.get("api_url", "").strip()
        api_key = config.get("api_key", "").strip()
        model_name = config.get("model", "").strip()
        batch_size = config.get("batch_size", 10)
        # context_lines = config.get("context_lines", 10) # 上下文行数在 worker 内部使用
        concurrency = config.get("concurrency", 16)

        if not api_url or not api_key or not model_name:
            message_queue.put(("error", "DeepSeek/OpenAI 兼容 API 配置不完整 (URL, Key, Model)。"))
            message_queue.put(("status", "翻译失败"))
            message_queue.put(("done", None))
            return

        message_queue.put(("log", ("normal", f"翻译配置: 模型={model_name}, 并发数={concurrency}, 批次大小={batch_size}")))

        # --- 初始化 API 客户端 ---
        try:
            api_client = deepseek.DeepSeekClient(api_url, api_key)
            message_queue.put(("log", ("normal", "DeepSeek/OpenAI 兼容 API 客户端初始化成功。")))
        except Exception as client_err:
            log.exception("初始化 API 客户端失败。")
            message_queue.put(("error", f"初始化 API 客户端失败: {client_err}"))
            message_queue.put(("status", "翻译失败"))
            message_queue.put(("done", None))
            return

        # --- 并发处理 ---
        results_lock = threading.Lock()
        error_log_lock = threading.Lock()
        # 创建一个队列用于从 worker 接收进度信号
        progress_queue = queue.Queue()

        message_queue.put(("status", f"开始翻译，总条目: {total_items}，并发数: {concurrency}..."))
        message_queue.put(("log", ("normal", f"开始使用 {concurrency} 个工作线程进行翻译...")))
        
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = []
            for i in range(0, total_items, batch_size):
                batch = original_items[i : i + batch_size]
                # 上下文应包含当前批次之前的所有项（或按 context_lines 限制）
                context_start_index = max(0, i - config.get("context_lines", 10))
                context = original_items[context_start_index : i]

                futures.append(executor.submit(
                    _translation_worker,
                    batch,
                    context,
                    world_dictionary,
                    api_client,
                    config, # 传递完整配置
                    translated_data,
                    results_lock,
                    progress_queue,
                    error_log_path,
                    error_log_lock
                ))

            # --- 监控进度 ---
            completed_count = 0
            while completed_count < total_items:
                try:
                    # 从进度队列获取信号，表示一个条目已处理完成
                    # 设置超时以避免永久阻塞，并允许检查 future 状态
                    progress_queue.get(timeout=1.0) 
                    completed_count += 1
                    
                    # 更新状态栏（可以降低更新频率）
                    if completed_count % (total_items // 20 or 1) == 0 or completed_count == total_items: # 大约每 5% 更新一次
                        progress_percent = (completed_count / total_items) * 100
                        elapsed_time = time.time() - start_time
                        est_total_time = (elapsed_time / completed_count) * total_items if completed_count > 0 else 0
                        remaining_time = est_total_time - elapsed_time
                        
                        status_msg = (f"正在翻译: {completed_count}/{total_items} ({progress_percent:.1f}%) "
                                      f"- 预计剩余: {remaining_time:.0f}s")
                        message_queue.put(("status", status_msg))
                        # 同时发送进度值给轻松模式
                        message_queue.put(("progress", progress_percent))

                except queue.Empty:
                    # 超时，检查是否有任务异常结束
                    all_done = True
                    for future in futures:
                        if not future.done():
                            all_done = False
                            break
                        elif future.exception():
                            exc = future.exception()
                            log.error(f"翻译工作线程异常: {exc}")
                            # 这里可以考虑更复杂的错误处理，比如取消其他任务
                            # message_queue.put(("error", f"翻译线程出错: {exc}"))
                    if all_done and completed_count < total_items:
                         log.warning(f"所有线程已结束，但完成计数 ({completed_count}) 少于总数 ({total_items})。可能存在未捕获的问题。")
                         # 强制完成进度
                         completed_count = total_items
                         progress_percent = 100.0
                         status_msg = f"翻译结束 (可能存在问题): {completed_count}/{total_items} (100.0%)"
                         message_queue.put(("status", status_msg))
                         message_queue.put(("progress", progress_percent))
                         break # 跳出循环
                    elif all_done: # 正常完成
                         break # 跳出循环
                except Exception as monitor_err:
                     log.error(f"进度监控出错: {monitor_err}")
                     # 避免监控错误导致卡死
                     break

            # 确保最终状态是 100%
            final_status_msg = f"翻译处理完成: {completed_count}/{total_items}"
            message_queue.put(("status", final_status_msg))
            message_queue.put(("progress", 100.0))
            message_queue.put(("log", ("normal", "所有翻译工作线程已完成。")))


        # --- 检查错误日志 ---
        error_count_in_log = 0
        if os.path.exists(error_log_path):
            try:
                with open(error_log_path, 'r', encoding='utf-8') as elog_read:
                    log_content = elog_read.read()
                    # 计算分隔符数量来估计错误条目数
                    error_count_in_log = log_content.count("-" * 20)
                if error_count_in_log > 0:
                    message_queue.put(("log", ("warning", f"检测到翻译过程中发生了 {error_count_in_log} 次错误（并不一定影响结果）。")))
                    message_queue.put(("log", ("warning", f"详情请查看错误日志: {error_log_path}")))
            except Exception as read_log_err:
                log.error(f"读取错误日志时出错: {read_log_err}")
        
        # 再次检查 translated_data，确认有多少条目最终等于其原始 key (回退)
        final_translated_data = {}
        explicit_fallback_count = 0 # 用于统计显式回退的数量
        missing_count = 0 # 统计结果丢失的数量 (理论上为 0)

        for key, result_tuple in translated_data.items():
            final_text = key # 默认回退
            status = 'fallback' # 默认状态

            if result_tuple is None: # 如果仍然是 None，表示 worker 未能成功写入结果？极端情况
                log.error(f"条目 '{key[:50]}...' 的翻译结果丢失，将使用原文回退。")
                final_translated_data[key] = key # 回退
                missing_count += 1
            else:
                #  final_translated_data[key] = translated_value # 使用翻译结果
                # 解包元组
                final_text, status = result_tuple
                if status == 'fallback':
                    explicit_fallback_count += 1
                final_translated_data[key] = final_text # 更新最终翻译结果

        if missing_count > 0:
             log.error(f"严重问题：有 {missing_count} 个条目的翻译结果丢失！")
             message_queue.put(("error", f"严重警告: {missing_count} 个翻译结果丢失，已强制回退。"))

        if explicit_fallback_count > 0:
             message_queue.put(("log", ("error", f"翻译完成，但有 {explicit_fallback_count} 个条目最终使用了原文回退。")))
             # 更新状态栏以提示用户
             message_queue.put(("error", f"警告: {explicit_fallback_count} 个翻译使用了原文回退，请检查日志。"))


        # --- 保存翻译后的 JSON ---
        message_queue.put(("log", ("normal", f"正在保存翻译结果到: {translated_json_path}")))
        try:
            with open(translated_json_path, 'w', encoding='utf-8') as f_out:
                json.dump(final_translated_data, f_out, ensure_ascii=False, indent=4)

            elapsed = time.time() - start_time
            message_queue.put(("log", ("success", f"翻译后的 JSON 文件保存成功。耗时: {elapsed:.2f} 秒。")))
            if explicit_fallback_count == 0:
                message_queue.put(("success", f"JSON 文件翻译完成，结果已保存。"))
                message_queue.put(("status", "翻译完成"))
            else:
                message_queue.put(("success", f"JSON 文件翻译完成 (有 {explicit_fallback_count} 个回退)，结果已保存。"))
                message_queue.put(("status", f"翻译完成 (有 {explicit_fallback_count} 个回退)"))
            message_queue.put(("done", None))

        except Exception as save_err:
            log.exception(f"保存翻译后的 JSON 文件失败: {save_err}")
            message_queue.put(("error", f"保存翻译结果失败: {save_err}"))
            message_queue.put(("status", "翻译失败(保存错误)"))
            message_queue.put(("done", None))

    except Exception as e:
        log.exception("翻译任务执行期间发生意外错误。")
        message_queue.put(("error", f"翻译过程中发生严重错误: {e}"))
        message_queue.put(("status", "翻译失败"))
        message_queue.put(("done", None))