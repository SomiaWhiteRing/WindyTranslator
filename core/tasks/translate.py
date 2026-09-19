# core/tasks/translate.py
import os
import json
import csv
import re
import time
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed # 使用 as_completed
from core.api_clients import deepseek
from core.utils import file_system, text_processing, default_database, control_tokens
from core.utils.engine_detection import detect_game_engine
from core.config import DEFAULT_WORLD_DICT_CONFIG
from collections import OrderedDict
from core.tasks.translation_runtime import TranslationSession, TranslationPaused, translate_batch
from core.tasks.translation_protocol import pack_batches

log = logging.getLogger(__name__)

WOLF_API_TAG_RE = re.compile(r"\{\{WINDY_WOLF_(\d+)_([0-9a-f]{8})\}\}", re.IGNORECASE)
WOLF_API_MASK_RE = re.compile(r"\[\[W\d+:[0-9a-f]{4}\]\]", re.IGNORECASE)


def _mask_wolf_transport(text, optional_tags=()):
    if WOLF_API_MASK_RE.search(text):
        return text, ()
    optional = set(optional_tags)
    mappings = []

    def replace(match):
        if match.group(0) in optional:
            return ""
        mask = f"[[W{match.group(1)}:{match.group(2)[:4]}]]"
        mappings.append((mask, match.group(0)))
        return mask

    return WOLF_API_TAG_RE.sub(replace, text), tuple(mappings)


def _restore_wolf_transport_masks(text, mappings):
    if not mappings:
        return True, text, ""
    expected = [mask for mask, _tag in mappings]
    actual = WOLF_API_MASK_RE.findall(text)
    if actual != expected:
        return False, text, f"WOLF API 短标签序列不一致: {expected!r} != {actual!r}"
    restored = text
    for mask, tag in mappings:
        restored = restored.replace(mask, tag, 1)
    return True, restored, ""


def _protected_literals_for_text(text, protected_literals):
    quote_pairs = (("「", "」"), ("『", "』"), ('"', '"'), ("'", "'"))
    selected = []
    for literal in protected_literals:
        if any(f"{left}{literal}{right}" in text for left, right in quote_pairs):
            selected.append(literal)
    return selected

# --- 批量翻译工作单元 (与上一版几乎一致，增加了 current_processing_file_name 的使用) ---
def _translate_batch_with_retry(
    batch_metadata_items, context_metadata_items, character_dictionary,
    entity_dictionary, api_client, config, error_log_path, error_log_lock,
    current_processing_file_name=None, previous_failures=None,
):
    return translate_batch(
        batch_metadata_items, context_metadata_items, character_dictionary,
        entity_dictionary, api_client, config, current_processing_file_name,
    )


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
    except TranslationPaused:
        raise
    except Exception as error:
        session = config.get("_translation_session")
        if session:
            session.pause(f"翻译处理异常: {error}")
        log.exception("翻译工作线程异常，保留已经保存的结果")
        raise TranslationPaused(str(error)) from error

    # 返回源文件名和这个批次的结果
    return source_file_name_for_worker, batch_processing_result


def _load_existing_translated_data(translated_json_path):
    """加载已有翻译结果，用于中断后续跑。"""
    if not os.path.exists(translated_json_path):
        return {}
    try:
        with open(translated_json_path, 'r', encoding='utf-8') as f_existing:
            existing_data = json.load(f_existing)
        if isinstance(existing_data, dict):
            return existing_data
        log.warning(f"已有翻译文件不是预期的字典结构，将忽略: {translated_json_path}")
    except json.JSONDecodeError as decode_err:
        log.warning(f"已有翻译文件无法解析，将忽略并重新生成: {translated_json_path} - {decode_err}")
    except OSError as os_err:
        log.warning(f"读取已有翻译文件失败，将忽略并重新生成: {translated_json_path} - {os_err}")
    return {}


def _is_reusable_translation_result(result_obj, metadata_obj=None):
    """仅复用已成功的译文；fallback 会在本轮重新尝试。"""
    return (
        isinstance(result_obj, dict)
        and result_obj.get("status") == "success"
        and isinstance(result_obj.get("text"), str)
        and result_obj.get("text").strip() != ""
        and not (
            isinstance(metadata_obj, dict)
            and result_obj.get("original_marker") == "WOLFLogic"
            and metadata_obj.get("original_marker") != "WOLFLogic"
        )
        and not (
            isinstance(metadata_obj, dict)
            and metadata_obj.get("wolf_codes")
            and (
                metadata_obj.get("wolf_export_schema") is None
                or result_obj.get("wolf_export_schema")
                != metadata_obj.get("wolf_export_schema")
                or result_obj.get("wolf_codes") != metadata_obj.get("wolf_codes")
            )
        )
    )


def _reuse_translation_result(result_obj, metadata_obj):
    reused = dict(result_obj)
    reused["status"] = "success"
    reused["failure_context"] = None
    reused["original_marker"] = metadata_obj.get("original_marker", reused.get("original_marker", "UnknownMarker"))
    reused["speaker_id"] = metadata_obj.get("speaker_id", reused.get("speaker_id"))
    if metadata_obj.get("wolf_codes"):
        reused["wolf_codes"] = list(metadata_obj["wolf_codes"])
        reused["wolf_export_schema"] = metadata_obj["wolf_export_schema"]
    return reused


def _save_translation_results_atomic(translated_json_path, untranslated_data, translated_data):
    """按原始顺序原子写入翻译结果，避免中途退出留下半截 JSON。"""
    for file_name, source_entries in untranslated_data.items():
        results = translated_data.get(file_name)
        if not isinstance(source_entries, dict) or not isinstance(results, dict):
            continue
        for source_text, source_metadata in source_entries.items():
            result = results.get(source_text)
            if (
                isinstance(source_metadata, dict)
                and source_metadata.get("wolf_codes")
                and isinstance(result, dict)
            ):
                result["wolf_codes"] = list(source_metadata["wolf_codes"])
                result["wolf_export_schema"] = source_metadata["wolf_export_schema"]
    file_system.ensure_dir_exists(os.path.dirname(translated_json_path))
    reordered_results = _reorder_translation_results(untranslated_data, translated_data)
    tmp_path = f"{translated_json_path}.tmp"
    with open(tmp_path, 'w', encoding='utf-8') as f_json_out:
        json.dump(reordered_results, f_json_out, ensure_ascii=False, indent=4)
        f_json_out.flush()
        os.fsync(f_json_out.fileno())
    os.replace(tmp_path, translated_json_path)
    return reordered_results


def _discard_stale_translation_outputs(paths):
    for path in paths:
        if os.path.exists(path) and not file_system.safe_remove(path):
            raise OSError(f"无法废弃旧版翻译中间态: {path}")


def _discard_translation_cache_if_dictionaries_changed(translated_json_path, dictionary_paths, outputs):
    if not os.path.exists(translated_json_path):
        return False
    translated_mtime = os.path.getmtime(translated_json_path)
    if not any(os.path.exists(path) and os.path.getmtime(path) > translated_mtime for path in dictionary_paths):
        return False
    log.info("词典已更新：保留已有成功译文，新词典用于剩余条目。需要重译时请明确移除对应译文。")
    return False


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
        char_dict_filename = world_dict_config.get("character_dict_filename", DEFAULT_WORLD_DICT_CONFIG["character_dict_filename"])
        entity_dict_filename = world_dict_config.get("entity_dict_filename", DEFAULT_WORLD_DICT_CONFIG["entity_dict_filename"])
        dictionary_paths = (
            os.path.join(work_game_dir, char_dict_filename),
            os.path.join(work_game_dir, entity_dict_filename),
        )
        from core.engines import wolf as wolf_engine
        detected_game = detect_game_engine(game_path)
        is_wolf_game = bool(detected_game and detected_game.engine == "wolf")
        stale_wolf_outputs = (
            translated_json_path,
            f"{translated_json_path}.tmp",
            fallback_csv_path,
        )
        
        if not file_system.ensure_dir_exists(translated_dir): raise OSError(f"无法创建目录: {translated_dir}")
        _discard_translation_cache_if_dictionaries_changed(
            translated_json_path, dictionary_paths, stale_wolf_outputs
        )
        # Keep legacy error logs for diagnosis; new requests append to JSONL.

        if not os.path.exists(untranslated_json_path):
            if is_wolf_game:
                _discard_stale_translation_outputs(stale_wolf_outputs)
            raise FileNotFoundError(f"未找到未翻译的 JSON 文件: {untranslated_json_path}")
        message_queue.put(("log", ("normal", "加载按文件组织的未翻译 JSON 文件...")))
        try:
            with open(untranslated_json_path, 'r', encoding='utf-8') as f_in:
                untranslated_data_per_file = json.load(f_in)
        except (OSError, json.JSONDecodeError):
            if is_wolf_game:
                _discard_stale_translation_outputs(stale_wolf_outputs)
            raise
        if not isinstance(untranslated_data_per_file, dict):
            if is_wolf_game:
                _discard_stale_translation_outputs(stale_wolf_outputs)
            raise ValueError("未翻译 JSON 顶层结构无效")
        if is_wolf_game and not untranslated_data_per_file:
            _discard_stale_translation_outputs(stale_wolf_outputs)
        for file_name, source_entries in untranslated_data_per_file.items():
            if not isinstance(source_entries, dict):
                if is_wolf_game:
                    _discard_stale_translation_outputs(stale_wolf_outputs)
                    raise ValueError(
                        f"WOLF 未翻译 JSON 版本已过期，请重新创建 JSON: {file_name}"
                    )
                continue
            for source_text, metadata in source_entries.items():
                if (
                    is_wolf_game
                    and (
                        not isinstance(metadata, dict)
                        or metadata.get("wolf_export_schema") != wolf_engine.WOLF_EXPORT_SCHEMA
                        or not isinstance(metadata.get("wolf_codes"), list)
                        or not metadata["wolf_codes"]
                        or not all(
                            isinstance(code, str) and code
                            for code in metadata["wolf_codes"]
                        )
                    )
                ):
                    _discard_stale_translation_outputs(stale_wolf_outputs)
                    raise ValueError(
                        f"WOLF 未翻译 JSON 版本已过期，请重新创建 JSON: "
                        f"{file_name}: {source_text[:80]!r}"
                    )
        
        if not untranslated_data_per_file:
            message_queue.put(("warning", "未翻译的 JSON 文件为空或无效，无需翻译。")); message_queue.put(("status", "翻译跳过(无内容)")); message_queue.put(("done", None)); return

        existing_translated_data = _load_existing_translated_data(translated_json_path)
        existing_success_count = 0
        if existing_translated_data:
            for file_name, data_for_this_file in untranslated_data_per_file.items():
                existing_file_data = existing_translated_data.get(file_name, {})
                if not isinstance(existing_file_data, dict) or not isinstance(data_for_this_file, dict):
                    continue
                existing_success_count += sum(
                    1 for original_json_key in data_for_this_file.keys()
                    if _is_reusable_translation_result(
                        existing_file_data.get(original_json_key),
                        data_for_this_file.get(original_json_key),
                    )
                )
            if existing_success_count > 0:
                message_queue.put(("log", ("normal", f"检测到已有翻译结果，可续跑复用 {existing_success_count} 条成功译文。")))
            else:
                message_queue.put(("log", ("normal", "检测到已有翻译文件，但没有可复用的成功译文。")))
        
        # --- 加载词典 (全局共享) ---
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
        apply_gbk_compatibility = not detected_game or detected_game.engine == "rm200x"
        current_translate_config["_apply_gbk_compatibility_postprocess"] = apply_gbk_compatibility
        control_profile = control_tokens.profile_from_game(game_path)
        current_translate_config["_control_code_profile"] = control_profile
        if detected_game and detected_game.engine == "wolf":
            from core.engines import wolf

            wolf_logic_literals = wolf.get_protected_logic_literals(game_path)
            optional_wolf_tags = wolf.get_optional_transport_tags(game_path)
            current_translate_config["_translation_validator"] = (
                lambda source, translated, optional=optional_wolf_tags:
                wolf.validate_translation_transport(source, translated, optional)
            )
            current_translate_config["_translation_validator_instruction"] = wolf.TRANSLATION_TRANSPORT_INSTRUCTION
            current_translate_config["_mask_wolf_transport"] = True
            current_translate_config["_optional_wolf_transport_tags"] = optional_wolf_tags
            current_translate_config["_protected_literals"] = tuple(sorted(
                set(wolf_logic_literals) | {
                    text_processing.convert_half_to_full_katakana(literal)
                    for literal in wolf_logic_literals
                },
                key=len,
                reverse=True,
            ))
        if detected_game and not apply_gbk_compatibility:
            message_queue.put(("log", ("normal", f"检测到 {detected_game.engine}：跳过 RM2000/2003 的 GBK 字符兼容化后处理。")))
        else:
            message_queue.put(("log", ("normal", "启用 RM2000/2003 的 GBK 字符兼容化后处理。")))
        message_queue.put(("log", ("normal", f"控制码保护配置: {control_profile.summary}")))
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
        default_db_mapping, default_db_originals = default_database.load_default_db_mapping(game_path)

        # --- *** 任务预切分 *** ---
        global_translation_tasks = [] # 存储所有 (batch_meta, context_meta, file_name) 的任务单元
        overall_total_items_in_all_files = 0
        overall_default_db_prefilled_count = 0
        overall_no_content_prefilled_count = 0
        overall_resumed_success_count = 0

        message_queue.put(("log", ("normal", "开始预切分所有翻译任务...")))
        for file_name, data_for_this_file in untranslated_data_per_file.items():
            if not data_for_this_file:
                log.info(f"文件 '{file_name}' 为空，跳过预切分。")
                all_files_translated_data[file_name] = {} # 预先设置空结果
                continue

            items_with_original_key_for_this_file = []
            prefilled_count_for_this_file = 0
            no_content_prefilled_for_this_file = 0
            resumed_count_for_this_file = 0
            existing_file_translations = existing_translated_data.get(file_name, {})
            if not isinstance(existing_file_translations, dict):
                existing_file_translations = {}
            for original_json_key, metadata_obj in data_for_this_file.items():
                # 确保元数据对象中有一个字段存储这个原始的JSON键
                metadata_obj['original_json_key'] = original_json_key 
                if (
                    detected_game
                    and detected_game.engine == "wolf"
                    and metadata_obj.get("original_marker") == "WOLFLogic"
                ):
                    all_files_translated_data.setdefault(file_name, {})[original_json_key] = {
                        "text": original_json_key,
                        "status": "success",
                        "failure_context": None,
                        "original_marker": "WOLFLogic",
                        "speaker_id": metadata_obj.get("speaker_id"),
                    }
                    no_content_prefilled_for_this_file += 1
                    continue
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

                existing_result_obj = existing_file_translations.get(original_json_key)
                if _is_reusable_translation_result(existing_result_obj, metadata_obj):
                    all_files_translated_data.setdefault(file_name, {})
                    all_files_translated_data[file_name][original_json_key] = _reuse_translation_result(
                        existing_result_obj,
                        metadata_obj
                    )
                    resumed_count_for_this_file += 1
                    continue

                items_with_original_key_for_this_file.append(metadata_obj)

            all_metadata_items_for_this_file = items_with_original_key_for_this_file
            num_items_in_file = len(all_metadata_items_for_this_file)
            overall_total_items_in_all_files += num_items_in_file
            overall_default_db_prefilled_count += prefilled_count_for_this_file
            # 同步累计“无需翻译”预填数量，排除在需译计数之外
            overall_no_content_prefilled_count += no_content_prefilled_for_this_file
            overall_resumed_success_count += resumed_count_for_this_file
            
            # 预先为这个文件在最终结果字典中创建条目
            all_files_translated_data.setdefault(file_name, {})


            i = 0
            for batch_metadata_for_task in pack_batches(
                all_metadata_items_for_this_file, batch_size_config,
            ):
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
                i += len(batch_metadata_for_task)
        
        if not global_translation_tasks:
            try:
                all_files_translated_data = _save_translation_results_atomic(
                    translated_json_path,
                    untranslated_data_per_file,
                    all_files_translated_data
                )
            except Exception as checkpoint_err:
                log.exception(f"保存续跑结果失败: {checkpoint_err}")
                message_queue.put(("error", f"保存续跑结果失败: {checkpoint_err}"))
                message_queue.put(("status", "翻译失败(保存错误)")); message_queue.put(("done", None)); return

            if overall_resumed_success_count > 0:
                message_queue.put(("success", f"所有条目已有成功译文，已复用 {overall_resumed_success_count} 条并完成续跑。"))
                message_queue.put(("status", "翻译全部完成(续跑复用)")); message_queue.put(("progress", 100.0)); message_queue.put(("done", None)); return

            message_queue.put(("warning", "所有文件均为空，或未提取到任何可翻译条目。无需翻译。"))
            message_queue.put(("status", "翻译跳过(无内容)")); message_queue.put(("progress", 100.0)); message_queue.put(("done", None)); return

        total_batches_to_process = len(global_translation_tasks)
        # overall_total_items_in_all_files 已经是过滤后需要API翻译的条目数（不包含预填充和无需翻译的）
        total_need_translate = overall_total_items_in_all_files
        message_queue.put(("log", ("normal", f"任务预切分完成。共 {total_batches_to_process} 个批次（来自 {len(untranslated_data_per_file)} 个文件），总计 {total_need_translate} 个需翻译原文条目。")))
        if overall_resumed_success_count > 0:
            message_queue.put(("log", ("normal", f"续跑已跳过 {overall_resumed_success_count} 条已有成功译文。")))
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

        session = TranslationSession(
            current_translate_config, os.path.join(translated_dir, "translation_requests.jsonl")
        )
        current_translate_config["_translation_session"] = session
        checkpoint_lock = threading.Lock()

        def checkpoint(source_file, results):
            nonlocal all_files_translated_data
            with checkpoint_lock:
                all_files_translated_data.setdefault(source_file, {}).update(results)
                try:
                    all_files_translated_data = _save_translation_results_atomic(
                        translated_json_path, untranslated_data_per_file, all_files_translated_data
                    )
                except Exception as error:
                    session.pause(f"保存失败，已停止后续请求: {error}")
                    raise TranslationPaused(session.reason) from error

        current_translate_config["_checkpoint_translation"] = checkpoint
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
                    
                    checkpoint(processed_file_name, batch_result_dict_from_worker)
                except TranslationPaused:
                    # Successful records have already been checkpointed by the worker.
                    continue
                except Exception as exc:
                    session.pause(f"翻译任务异常: {exc}")
                    continue

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
                                         f"| 请求: {session.stats['requests']} | token: {session.stats['input_tokens'] + session.stats['output_tokens']} "
                                          f"- 预计剩余: {remaining_processing_time:.0f}s")
                    message_queue.put(("status", status_update_msg))
                    message_queue.put(("progress", progress_percentage))
                    last_status_update_time = current_time

        summary_path = os.path.join(translated_dir, "translation_run_summary.json")
        summary = {"run_id": session.run_id, "paused": session.stop.is_set(),
                   "reason": session.reason, **session.stats,
                   "saved_success": sum(
                       result.get("status") == "success"
                       for entries in all_files_translated_data.values() for result in entries.values()
                   )}
        with open(summary_path, "w", encoding="utf-8") as summary_file:
            json.dump(summary, summary_file, ensure_ascii=False, indent=2)
        message_queue.put(("log", ("normal", f"实际请求 {session.stats['requests']} 次；输入 {session.stats['input_tokens']} / 输出 {session.stats['output_tokens']} token；拆批 {session.stats['splits']} 次。")))
        if session.stop.is_set():
            message_queue.put(("error", f"翻译已暂停，成功译文已保存，可续跑。{session.reason}"))
            message_queue.put(("status", "翻译暂停(已保存进度)"))
            message_queue.put(("done", None))
            return

        message_queue.put(("log", ("normal", f"所有 {total_batches_to_process} 个翻译批次已提交处理。等待完成...")))
        # （as_completed 循环结束后，所有任务都已完成或异常）
        message_queue.put(("status", f"翻译处理完成: {completed_batches_count}/{total_batches_to_process} 批次。"))
        message_queue.put(("progress", 100.0)) # 确保最终是100%
        message_queue.put(("log", ("normal", "所有翻译工作线程已完成。")))


        # --- 后续处理：错误日志检查、回退CSV生成、最终JSON保存 ---
        # (这部分逻辑与上一版类似，但现在是基于 all_files_translated_data 和全局回退列表)
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
            # 在保存前重排序结果
            message_queue.put(("log", ("normal", "正在重排序翻译结果以匹配原始文件顺序...")))
            all_files_translated_data = _save_translation_results_atomic(
                translated_json_path,
                untranslated_data_per_file,
                all_files_translated_data
            )
            
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
