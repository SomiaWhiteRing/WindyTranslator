# core/tasks/dict_generation.py
import os
import json
import csv
import io # 用于将字符串模拟成文件给 csv reader
import logging
import time
import re
from core.api_clients import gemini, deepseek # 导入 Gemini 或 OpenAI 兼容客户端模块
from core.utils import file_system, text_processing, default_database
# 导入默认配置以获取默认文件名
from core.config import DEFAULT_WORLD_DICT_CONFIG
from . import apply_base_dictionary

log = logging.getLogger(__name__)

PROVIDER_GEMINI = 'gemini'
PROVIDER_OPENAI = 'openai'

# --- CSV 解析辅助函数 ---
def _parse_csv_response(csv_response_text, expected_columns, message_queue):
    """
    解析字典模型返回的 CSV 文本。

    Args:
        csv_response_text (str): 字典模型返回的原始文本。
        expected_columns (int): 期望的列数。
        message_queue (queue.Queue): 用于发送日志消息的队列。

    Returns:
        list[list[str]]: 解析后的数据列表，每个子列表是一行。
                         如果解析失败或无有效数据，返回空列表。
    """
    parsed_data = []
    if not csv_response_text:
        log.warning("字典模型 API 未返回任何内容。")
        message_queue.put(("log", ("warning", "字典模型 API 未返回任何内容。")))
        return parsed_data

    try:
        # 移除响应前后可能存在的空白或```csv标记
        clean_csv_response = csv_response_text.strip().strip('```csv').strip('```').strip()
        if not clean_csv_response:
            log.warning("清理后的字典模型 CSV 响应为空。")
            message_queue.put(("log", ("warning", "字典模型 API 返回的 CSV 内容为空或仅包含标记。")))
            return parsed_data

        csv_file = io.StringIO(clean_csv_response)
        # 严格按照 Prompt 要求解析：双引号包围，逗号分隔
        reader = csv.reader(csv_file, quotechar='"', delimiter=',',
                            quoting=csv.QUOTE_ALL, skipinitialspace=True)

        for i, row in enumerate(reader):
            # 跳过空行
            if not row:
                continue
            if len(row) == expected_columns:
                parsed_data.append(row)
            else:
                log.warning(f"跳过格式错误的 CSV 行 #{i+1} (期望{expected_columns}列，得到{len(row)}列): {row}")
                message_queue.put(("log", ("warning", f"跳过格式错误的 CSV 行 #{i+1} (期望{expected_columns}列): {row}")))

    except csv.Error as csv_err: # 捕捉更具体的 CSV 解析错误
        log.error(f"解析字典模型返回的 CSV 数据时发生 CSV 错误: {csv_err}")
        log.error(f"原始 CSV 响应内容 (前500字符):\n{csv_response_text[:500]}") # 记录部分原始响应供调试
        message_queue.put(("error", f"解析字典模型返回的 CSV 数据失败 (CSV格式问题): {csv_err}"))
        # 解析出错时，返回已成功解析的部分（如果有）
    except Exception as parse_err:
        log.error(f"解析字典模型返回的 CSV 数据时发生意外错误: {parse_err}")
        log.error(f"原始 CSV 响应内容 (前500字符):\n{csv_response_text[:500]}") # 记录部分原始响应供调试
        message_queue.put(("error", f"解析字典模型返回的 CSV 数据失败: {parse_err}"))
        # 解析出错时，返回已成功解析的部分（如果有）

    return parsed_data


RE_RETRY_DELAY = re.compile(r"retry(?: in)?\s*(?P<seconds>\d+(?:\.\d+)?)s", re.IGNORECASE)
RE_RETRYINFO_DELAY = re.compile(r"'retryDelay':\s*'(?P<seconds>\d+)s'", re.IGNORECASE)
DEFAULT_RETRY_WAIT = 20
MAX_RETRY_WAIT = 60
MAX_WORLD_DICT_RETRIES = 3
RATE_LIMIT_KEYWORDS = ("resource_exhausted", "429", "quota", "rate limit", "频率超限", "超限")

def _extract_retry_delay_seconds(error_message):
    if not error_message:
        return None
    match = RE_RETRY_DELAY.search(error_message)
    if match:
        try:
            return min(float(match.group('seconds')), MAX_RETRY_WAIT)
        except ValueError:
            pass
    match = RE_RETRYINFO_DELAY.search(error_message)
    if match:
        try:
            return min(float(match.group('seconds')), MAX_RETRY_WAIT)
        except ValueError:
            pass
    return None

def _call_world_dict_model_with_retry(request_callable, stage_desc, message_queue):
    last_error = None
    wait_seconds = DEFAULT_RETRY_WAIT
    for attempt in range(1, MAX_WORLD_DICT_RETRIES + 1):
        success, response_text, error_message = request_callable()
        if success:
            if attempt > 1:
                log.info(f"{stage_desc} - 在第 {attempt} 次尝试时成功。")
            return True, response_text, None
        last_error = error_message or '模型调用未返回错误信息'
        lower_err = last_error.lower() if isinstance(last_error, str) else ''
        if any(keyword in lower_err for keyword in RATE_LIMIT_KEYWORDS):
            suggested = _extract_retry_delay_seconds(last_error)
            if suggested is not None:
                wait_seconds = max(1, suggested)
            message_queue.put(('log', ('warning', f"{stage_desc} 因配额限制暂停 {wait_seconds:.1f} 秒后重试 (尝试 {attempt}/{MAX_WORLD_DICT_RETRIES})...")))
            log.warning(f"{stage_desc} 命中配额限制，等待 {wait_seconds:.1f} 秒后重试 (尝试 {attempt}/{MAX_WORLD_DICT_RETRIES})。错误: {last_error}")
            time.sleep(wait_seconds)
            wait_seconds = min(wait_seconds * 2, MAX_RETRY_WAIT)
            continue
        else:
            break
    return False, None, last_error

# --- 主任务函数 ---
def run_generate_dictionary(game_path, works_dir, world_dict_config, message_queue):
    """
    使用字典模型 API 从提取的文本生成人物词典和事物词典 (CSV 文件)。

    Args:
        game_path (str): 游戏根目录路径。
        works_dir (str): Works 工作目录的根路径。
        world_dict_config (dict): 包含字典模型所需的 API 配置（Key、模型、Prompt模板、词典文件名等）。
        message_queue (queue.Queue): 用于向主线程发送消息的队列。
    """
    # 定义两个阶段的状态变量
    character_dict_success = False
    entity_dict_success = False
    character_entries_count = 0
    entity_entries_count = 0
    temp_text_path = None # 初始化，确保 finally 中可用

    try:
        message_queue.put(("status", "正在准备生成字典..."))
        # --- 获取配置 ---
        api_key = world_dict_config.get("api_key", "").strip()
        model_name = world_dict_config.get("model", "").strip()
        char_prompt_template = world_dict_config.get("character_prompt_template", "")
        entity_prompt_template = world_dict_config.get("entity_prompt_template", "")
        char_dict_filename = world_dict_config.get("character_dict_filename", DEFAULT_WORLD_DICT_CONFIG["character_dict_filename"])
        entity_dict_filename = world_dict_config.get("entity_dict_filename", DEFAULT_WORLD_DICT_CONFIG["entity_dict_filename"])
        enable_base_dict = world_dict_config.get("enable_base_dictionary", True)
        provider_raw = world_dict_config.get("provider", PROVIDER_GEMINI)
        provider = (provider_raw or PROVIDER_GEMINI).strip().lower()
        if provider not in (PROVIDER_GEMINI, PROVIDER_OPENAI):
            raise ValueError(f"不支持的字典模型提供方: {provider_raw}")
        provider_display = "Gemini API" if provider == PROVIDER_GEMINI else "OpenAI 兼容 API"
        api_url = world_dict_config.get("api_url", "").strip()
        openai_temperature = world_dict_config.get("openai_temperature", 0.2)
        openai_max_tokens = world_dict_config.get("openai_max_tokens")
        openai_extra_params = world_dict_config.get("openai_extra_params", {})
        if provider == PROVIDER_OPENAI:
            try:
                openai_temperature = float(openai_temperature)
            except (TypeError, ValueError):
                log.warning("OpenAI 温度配置无效，使用默认 0.2。")
                openai_temperature = 0.2
            if isinstance(openai_max_tokens, str) and openai_max_tokens.strip():
                try:
                    openai_max_tokens = int(openai_max_tokens.strip())
                except ValueError:
                    log.warning("OpenAI max_tokens 配置无效，已忽略。")
                    openai_max_tokens = None
        else:
            openai_max_tokens = None
        if not isinstance(openai_extra_params, dict):
            log.warning("openai_extra_params 配置不是字典类型，已忽略。")
            openai_extra_params = {}

        # --- 检查配置 ---
        if not api_key:
            raise ValueError(f"{provider_display} Key 未配置。")
        if provider == PROVIDER_OPENAI and not api_url:
            raise ValueError("OpenAI 兼容 API 基础地址 (api_url) 未配置。")
        if not model_name:
            raise ValueError("字典模型名称未配置。")
        if not char_prompt_template:
            raise ValueError("人物提取 Prompt 模板未配置。")
        if not entity_prompt_template:
            raise ValueError("事物提取 Prompt 模板未配置。")
        if not char_dict_filename:
            raise ValueError("人物词典文件名未配置。")
        if not entity_dict_filename:
            raise ValueError("事物词典文件名未配置。")

        message_queue.put(("log", ("normal", f"步骤 4: 开始生成世界观字典 (使用 {provider_display})...")))

        # --- 确定文件路径 ---
        game_folder_name = text_processing.sanitize_filename(os.path.basename(game_path))
        if not game_folder_name: game_folder_name = "UntitledGame"
        work_game_dir = os.path.join(works_dir, game_folder_name)
        untranslated_dir = os.path.join(work_game_dir, "untranslated")
        json_path = os.path.join(untranslated_dir, "translation.json")
        # 词典文件路径
        character_dict_path = os.path.join(work_game_dir, char_dict_filename)
        entity_dict_path = os.path.join(work_game_dir, entity_dict_filename)
        temp_text_path = os.path.join(work_game_dir, "_temp_world_text.txt") # 临时聚合文本文件

        if not os.path.exists(json_path):
            raise FileNotFoundError(f"未找到未翻译的 JSON 文件: {json_path}，请先执行步骤 3。")

        # --- 加载 JSON 并准备输入文本 ---
        message_queue.put(("log", ("normal", "加载按文件组织的 JSON 文件并提取所有原文...")))
        # all_texts_with_metadata_prefix 用于存储所有文件中带有前缀的 text_to_translate
        all_texts_with_metadata_prefix = [] 
        default_db_filtered_count = 0

        # 默认数据库过滤/映射
        # 固定从 modules/dict 默认数据库映射加载
        default_db_mapping, default_db_originals = default_database.load_default_db_mapping()

        try:
            with open(json_path, 'r', encoding='utf-8') as f_json_in:
                untranslated_data_per_file = json.load(f_json_in)

            if not untranslated_data_per_file:
                message_queue.put(("warning", f"JSON 文件 '{json_path}' 为空或无效，无法提取原文。"))
                message_queue.put(("status", "生成字典跳过(JSON无效)"))
                message_queue.put(("done", None))
                return

            # 遍历每个文件的数据

            for file_name, file_data_dict in untranslated_data_per_file.items():
                if not isinstance(file_data_dict, dict):
                    log.warning(f"文件 '{file_name}' 在JSON中的数据不是预期的字典格式，已跳过。")
                    continue
                
                log.debug(f"为字典生成准备文件 '{file_name}' 的文本...")
                # 遍历该文件内的所有条目 (其值是元数据对象)
                for original_key, metadata_object in file_data_dict.items(): # 依赖字典保持顺序
                    if isinstance(metadata_object, dict) and "text_to_translate" in metadata_object:
                        text_content = metadata_object["text_to_translate"]
                        original_marker = metadata_object.get("original_marker", "UnknownMarker")
                        speaker_id = metadata_object.get("speaker_id") # 可能为 None

                        if text_content and isinstance(text_content, str):
                            # 先用原始JSON键(原文)进行精确匹配排除，避免半角->全角转换导致的不一致
                            if default_database.should_exclude_text(original_key, default_db_originals):
                                default_db_filtered_count += 1
                                continue
                            # 过滤默认数据库原文（精确匹配）
                            # 冗余保护：如上一步未命中，再比对 text_to_translate（通常两者一致）
                            if default_database.should_exclude_text(text_content, default_db_originals):
                                default_db_filtered_count += 1
                                continue
                            # 将文本中的换行符替换为特殊标记
                            text_content = text_content.replace('\n', '[LINEBREAK]')
                            
                            # 构建元数据前缀
                            marker_tag = f"[MARKER: {original_marker}]"
                            face_tag = ""
                            if speaker_id: # 只有当 speaker_id 有效时才添加
                                face_tag = f"[FACE: {speaker_id}]"
                            
                            # 组合前缀和文本内容
                            line_with_prefix = f"{marker_tag} {face_tag}".strip() + f" {text_content}"
                            all_texts_with_metadata_prefix.append(line_with_prefix)
                        else:
                            log.debug(f"文件 '{file_name}', 原文key '{original_key}' 的 text_to_translate 为空或非字符串，已跳过。")
                    else:
                        log.warning(f"文件 '{file_name}', 原文key '{original_key}' 对应的元数据对象格式不正确或缺少 'text_to_translate'，已跳过。 对象: {metadata_object}")
            
            total_extracted_lines_with_prefix = len(all_texts_with_metadata_prefix)
            message_queue.put(("log", ("normal", f"共从所有文件中提取并格式化 {total_extracted_lines_with_prefix} 行带元数据前缀的文本用于世界观字典分析。")))
            if default_db_filtered_count > 0:
                message_queue.put(("log", ("normal", f"已按默认数据库排除 {default_db_filtered_count} 条重复/模板条目 (精确匹配)。")))
            
            if not all_texts_with_metadata_prefix: 
                 message_queue.put(("warning", "未从任何文件中提取到有效的原文内容用于分析。"))
                 message_queue.put(("status", "生成字典跳过(无有效文本)"))
                 message_queue.put(("done", None))
                 return

            # 将提取的所有带前缀的原文行写入临时文件
            message_queue.put(("log", ("normal", "将所有带前缀的原文行写入临时文件...")))
            with open(temp_text_path, 'w', encoding='utf-8') as f_temp_out:
                # 每行是一个带前缀的原文（可能包含多行）
                f_temp_out.write("\n".join(all_texts_with_metadata_prefix).replace('[LINEBREAK]', '\\n')) 
            log.info(f"带元数据前缀的临时原文聚合文件已创建: {temp_text_path}")

            # 读取游戏文本内容用于 Prompt 格式化
            with open(temp_text_path, 'r', encoding='utf-8') as f_temp_read:
                game_text_content = f_temp_read.read() # 这个现在是带前缀的文本块

            # 检查文本大小 (与之前相同)
            MAX_TEXT_SIZE_MB = 10 # 这个限制可能需要根据实际带前缀的文本长度调整
            if len(game_text_content.encode('utf-8')) / (1024*1024) > MAX_TEXT_SIZE_MB:
                 log.warning(f"聚合后的带前缀游戏文本大小 ({len(game_text_content.encode('utf-8')) / (1024*1024):.2f} MB) 较大，API 调用可能耗时较长或失败。")
                 message_queue.put(("log",("warning", "游戏文本内容（含元数据前缀）较多，API处理可能需要较长时间。")))

        except json.JSONDecodeError as json_err: 
            log.exception(f"加载或解析JSON文件 '{json_path}' 失败: {json_err}")
            raise RuntimeError(f"加载或解析JSON文件失败: {json_err}") from json_err
        except Exception as e_load_prepare: 
            log.exception(f"加载 JSON 或准备临时输入文件时出错: {e_load_prepare}")
            raise RuntimeError(f"加载 JSON 或准备输入文本失败: {e_load_prepare}") from e_load_prepare

        # --- 初始化字典模型客户端 ---
        try:
            if provider == PROVIDER_GEMINI:
                client = gemini.GeminiClient(api_key)
                log.info("Gemini 客户端初始化成功。")
                def request_model(prompt):
                    return client.generate_content(model_name, prompt)
            else:
                client = deepseek.DeepSeekClient(api_url, api_key)
                log.info(f"OpenAI 兼容客户端初始化成功 (URL: {api_url}).")
                def request_model(prompt):
                    extra_kwargs = dict(openai_extra_params)
                    temperature = extra_kwargs.pop('temperature', openai_temperature)
                    try:
                        temperature = float(temperature)
                    except (TypeError, ValueError):
                        temperature = openai_temperature
                    max_tokens_value = extra_kwargs.pop('max_tokens', openai_max_tokens)
                    if isinstance(max_tokens_value, str) and max_tokens_value.strip():
                        try:
                            max_tokens_value = int(max_tokens_value.strip())
                        except ValueError:
                            log.warning("openai_extra_params 中的 max_tokens 无效，已忽略。")
                            max_tokens_value = openai_max_tokens
                    if isinstance(max_tokens_value, float):
                        max_tokens_value = int(max_tokens_value)
                    return client.chat_completion(
                        model_name,
                        [{"role": "user", "content": prompt}],
                        temperature=temperature,
                        max_tokens=max_tokens_value,
                        **extra_kwargs
                    )
        except Exception as client_err:
            raise ConnectionError(f"初始化字典模型 API 客户端失败: {client_err}") from client_err

        # ==================================================
        # === 阶段一：生成人物词典 (Character Dictionary) ===
        # ==================================================
        message_queue.put(("status", "正在生成人物词典..."))
        message_queue.put(("log", ("normal", "阶段 1: 开始生成人物词典...")))
        character_data = []
        try:
            char_final_prompt = char_prompt_template.format(game_text=game_text_content)
            message_queue.put(("log", ("normal", f"调用 {provider_display} (人物提取)...")))
            success, char_csv_response, error_message = _call_world_dict_model_with_retry(
                lambda: request_model(char_final_prompt), '人物词典生成', message_queue
            )

            if not success:
                message_queue.put(("log", ("error", f"人物词典生成失败: {provider_display} 调用失败: {error_message}")))
                # 不抛出异常，允许继续尝试生成事物词典，但记录失败状态
            else:
                message_queue.put(("log", ("success", "人物提取 API 调用成功，正在解析...")))
                # 解析 CSV (期望 8 列)
                character_data = _parse_csv_response(char_csv_response, 8, message_queue)
                character_entries_count = len(character_data)
                message_queue.put(("log", ("normal", f"解析完成，获得 {character_entries_count} 条有效人物条目。")))

                # 保存人物词典 CSV 文件
                message_queue.put(("log", ("normal", f"正在保存人物词典到: {character_dict_path}")))
                try:
                    # 确保目录存在
                    file_system.ensure_dir_exists(os.path.dirname(character_dict_path))
                    with open(character_dict_path, 'w', newline='', encoding='utf-8-sig') as f_csv:
                        writer = csv.writer(f_csv, quoting=csv.QUOTE_ALL)
                        # 写入表头
                        writer.writerow(['原文', '译文', '对应原名', '性别', '年龄', '性格', '口吻', '描述'])
                        if character_data:
                            writer.writerows(character_data)
                    character_dict_success = True
                    message_queue.put(("log", ("success", f"人物词典保存成功: {character_dict_path}")))
                except Exception as write_err:
                    log.exception(f"保存人物词典 CSV 文件失败: {character_dict_path} - {write_err}")
                    message_queue.put(("log", ("error", f"保存人物词典失败: {write_err}")))
                    # 保存失败也标记为失败

        except Exception as char_err:
            log.exception(f"生成人物词典阶段发生错误: {char_err}")
            message_queue.put(("log", ("error", f"生成人物词典时出错: {char_err}")))
            # 记录错误，但不中断流程

        # --- 准备人物词典参考内容 ---
        character_reference_csv_content = ""
        if character_dict_success and character_entries_count > 0:
            message_queue.put(("log", ("normal", "准备人物词典参考内容...")))
            try:
                with open(character_dict_path, 'r', encoding='utf-8-sig') as f_ref:
                    # 跳过表头读取剩余内容
                    reader = csv.reader(f_ref)
                    next(reader) # 跳过表头
                    # 将剩余行重新格式化为 CSV 字符串 (或者直接读取文件内容)
                    # 使用 io.StringIO 来写入内存中的 CSV 字符串，确保格式正确
                    output = io.StringIO()
                    writer = csv.writer(output, quoting=csv.QUOTE_ALL)
                    for row in reader:
                         writer.writerow(row) # 写入数据行
                    character_reference_csv_content = output.getvalue().strip() # 获取字符串内容并去除末尾换行
                    output.close()
                log.info(f"成功准备了 {len(character_reference_csv_content.splitlines())} 行人物词典参考。")
            except Exception as ref_err:
                log.exception(f"读取人物词典参考内容时出错: {ref_err}")
                message_queue.put(("log", ("warning", f"无法读取人物词典参考: {ref_err}，事物词典将不包含参考。")))
                character_reference_csv_content = "" # 出错则清空
        elif not character_dict_success:
             message_queue.put(("log", ("warning", "人物词典生成失败，事物词典将不包含人物参考。")))
        else: # 成功但无条目
             message_queue.put(("log", ("normal", "人物词典为空，事物词典将不包含人物参考。")))


        # ==================================================
        # === 阶段二：生成事物词典 (Entity Dictionary) =====
        # ==================================================
        message_queue.put(("status", "正在生成事物词典..."))
        message_queue.put(("log", ("normal", "阶段 2: 开始生成事物词典...")))
        entity_data = []
        try:
            entity_final_prompt = entity_prompt_template.format(
                game_text=game_text_content,
                character_reference_csv_content=character_reference_csv_content
            )
            message_queue.put(("log", ("normal", f"调用 {provider_display} (事物提取)...")))
            success, entity_csv_response, error_message = _call_world_dict_model_with_retry(
                lambda: request_model(entity_final_prompt), '事物词典生成', message_queue
            )

            if not success:
                message_queue.put(("log", ("error", f"事物词典生成失败: {provider_display} 调用失败: {error_message}")))
                # 记录失败状态
            else:
                message_queue.put(("log", ("success", "事物提取 API 调用成功，正在解析...")))
                # 解析 CSV (期望 4 列)
                entity_data = _parse_csv_response(entity_csv_response, 4, message_queue)
                entity_entries_count = len(entity_data)
                message_queue.put(("log", ("normal", f"解析完成，获得 {entity_entries_count} 条有效事物条目。")))

                # 保存事物词典 CSV 文件
                message_queue.put(("log", ("normal", f"正在保存事物词典到: {entity_dict_path}")))
                try:
                    # 确保目录存在
                    file_system.ensure_dir_exists(os.path.dirname(entity_dict_path))
                    with open(entity_dict_path, 'w', newline='', encoding='utf-8-sig') as f_csv:
                        writer = csv.writer(f_csv, quoting=csv.QUOTE_ALL)
                        # 写入表头
                        writer.writerow(['原文', '译文', '类别', '描述'])
                        if entity_data:
                            writer.writerows(entity_data)
                    entity_dict_success = True
                    message_queue.put(("log", ("success", f"事物词典保存成功: {entity_dict_path}")))
                except Exception as write_err:
                    log.exception(f"保存事物词典 CSV 文件失败: {entity_dict_path} - {write_err}")
                    message_queue.put(("log", ("error", f"保存事物词典失败: {write_err}")))
                    # 保存失败也标记为失败

        except Exception as entity_err:
            log.exception(f"生成事物词典阶段发生错误: {entity_err}")
            message_queue.put(("log", ("error", f"生成事物词典时出错: {entity_err}")))
            # 记录错误

        # ==================================================
        # === 阶段三：应用基础字典 (Base Dictionary) ===
        # ==================================================
        if enable_base_dict:
            message_queue.put(("log", ("normal", "阶段 4.3: 检查并应用基础字典...")))
            try:
                apply_base_dictionary.run_apply_base_dictionary(
                    game_path,
                    works_dir,
                    world_dict_config, # 传递完整的 world_dict_config
                    message_queue
                )

            except Exception as apply_err:
                log.exception(f"调用应用基础字典流程时发生错误: {apply_err}")
                message_queue.put(("error", f"应用基础字典时出错: {apply_err}"))
                message_queue.put(("status", "生成字典完成 (应用基础字典时出错)"))
                message_queue.put(("done", None)) # 标记整个任务完成（即使子步骤失败）
                return # 应用出错，直接返回，不再执行后续的原始 "done"

        # --- 最终总结 ---
        if character_dict_success and entity_dict_success:
            message_queue.put(("success", f"字典生成成功: 人物({character_entries_count}条), 事物({entity_entries_count}条)。"))
            message_queue.put(("status", "生成字典完成"))
        elif character_dict_success:
            message_queue.put(("warning", f"字典生成部分成功: 人物({character_entries_count}条)成功, 事物失败。"))
            message_queue.put(("status", "生成字典部分完成 (事物失败)"))
        elif entity_dict_success:
            message_queue.put(("warning", f"字典生成部分成功: 人物失败, 事物({entity_entries_count}条)成功。"))
            message_queue.put(("status", "生成字典部分完成 (人物失败)"))
        else:
            message_queue.put(("error", "字典生成失败: 人物和事物词典均未成功生成。"))
            message_queue.put(("status", "生成字典失败"))

        message_queue.put(("done", None))

    except (ValueError, FileNotFoundError, RuntimeError, ConnectionError) as config_or_setup_err:
        # 捕捉配置、文件或初始化错误
        log.error(f"字典生成前置检查或初始化失败: {config_or_setup_err}")
        message_queue.put(("error", f"字典生成准备失败: {config_or_setup_err}"))
        message_queue.put(("status", "生成字典失败"))
        message_queue.put(("done", None))
    except Exception as e:
        # 捕捉其他意外顶级错误
        log.exception("生成字典任务执行期间发生意外错误。")
        message_queue.put(("error", f"生成字典过程中发生严重错误: {e}"))
        message_queue.put(("status", "生成字典失败"))
        message_queue.put(("done", None))
    finally:
        # 确保临时文件被删除
        if temp_text_path and os.path.exists(temp_text_path):
            if file_system.safe_remove(temp_text_path):
                 log.info(f"已清理临时原文聚合文件: {temp_text_path}")
            else:
                 log.warning(f"清理临时文件失败: {temp_text_path}")

