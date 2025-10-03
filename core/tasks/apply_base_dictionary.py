import os
import csv
import json
import logging
from core.utils import file_system, text_processing, dictionary_manager
from core.config import DEFAULT_WORLD_DICT_CONFIG

log = logging.getLogger(__name__)

def _count_term_in_json_originals(json_path, term):
    """
    计算指定术语在 JSON 文件所有原文 Key 中出现的次数。

    Args:
        json_path (str): translation.json 文件路径。
        term (str): 要搜索的术语。

    Returns:
        int: 术语出现的总次数。
    """
    count = 0
    if not os.path.exists(json_path):
        log.warning(f"用于计数的 JSON 文件未找到: {json_path}")
        # 此函数内部不直接发消息给UI，由调用者决定如何处理
        return 0
    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        if isinstance(data, dict):
            for item in data.values():
                if isinstance(item, dict):
                    for original_text in item.keys():
                        if isinstance(original_text, str):
                            count += original_text.count(term)
                elif isinstance(item, list):
                    for sub_item in item:
                        if isinstance(sub_item, str):
                            count += sub_item.count(term)
                        elif isinstance(sub_item, dict):
                            for original_text in sub_item.keys():
                                if isinstance(original_text, str):
                                    count += original_text.count(term)
                elif isinstance(item, str):
                    count += item.count(term)
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, str):
                    count += item.count(term)
                elif isinstance(item, dict):
                    for original_text in item.keys():
                        if isinstance(original_text, str):
                            count += original_text.count(term)

    except Exception as e:
        log.exception(f"读取或解析 JSON 文件进行术语计数时出错 ({json_path}): {e}")
        return 0 # 出错则返回0
    return count

def run_apply_base_dictionary(game_path, works_dir, world_dict_config, message_queue, **kwargs):
    """
    应用基础字典到游戏特定的生成字典。

    Args:
        game_path (str): 游戏根目录路径。
        works_dir (str): Works 工作目录的根路径。
        world_dict_config (dict): 包含世界观字典文件名和启用状态的配置。
        message_queue (queue.Queue): 用于向主线程发送消息的队列。
        **kwargs 可能包含 'task_id_for_callback'，用于回调给特定的UI组件。
    """
    message_queue.put(("status", "正在应用基础字典..."))
    log.info("开始应用基础字典流程...")
    
    task_id_for_callback = kwargs.get('task_id_for_callback')
    if task_id_for_callback:
        log.info(f"此 'apply_base_dictionary' 任务关联的回调ID: {task_id_for_callback}")
        
    # --- 用于回调的最终结果变量 ---
    callback_task_successful = False
    callback_message_content = "应用基础字典操作已开始，但未正常完成。" # 默认失败消息

    try:
        # --- 1. 确定文件路径 ---
        game_folder_name = text_processing.sanitize_filename(os.path.basename(game_path))
        if not game_folder_name: game_folder_name = "UntitledGame"
        work_game_dir = os.path.join(works_dir, game_folder_name)

        char_gen_filename = world_dict_config.get("character_dict_filename", DEFAULT_WORLD_DICT_CONFIG["character_dict_filename"])
        entity_gen_filename = world_dict_config.get("entity_dict_filename", DEFAULT_WORLD_DICT_CONFIG["entity_dict_filename"])
        game_char_dict_path = os.path.join(work_game_dir, char_gen_filename)
        game_entity_dict_path = os.path.join(work_game_dir, entity_gen_filename)
        untranslated_json_path = os.path.join(work_game_dir, "untranslated", "translation.json")

        # 初始文件检查，主要是为了原文 JSON
        if not os.path.exists(untranslated_json_path):
            message_queue.put(("error", f"未找到原文 JSON 文件: {untranslated_json_path}，无法执行添加步骤。替换步骤仍可进行。"))
            # 如果 JSON 不存在，替换步骤仍可进行，但添加步骤无法进行，所以不立即返回

        # --- 2. 加载字典 ---
        base_char_dict, base_entity_dict = dictionary_manager.load_base_dictionaries()
        if not base_char_dict and not base_entity_dict:
            message_queue.put(("log", ("normal", "基础字典为空或加载失败，跳过应用基础字典流程。")))
            message_queue.put(("status", "应用基础字典已跳过 (无基础数据)"))
            callback_task_successful = True # 视为一种“成功”的跳过
            callback_message_content = "基础字典为空或加载失败，已跳过应用。"
            return

        # 加载游戏特定的人物生成字典
        game_char_data_list = []
        game_char_headers = dictionary_manager.BASE_CHARACTER_HEADERS # 默认表头
        if os.path.exists(game_char_dict_path):
            try:
                with open(game_char_dict_path, 'r', encoding='utf-8-sig', newline='') as f:
                    dict_reader_obj = csv.DictReader(f)
                    if dict_reader_obj.fieldnames:
                        game_char_headers = dict_reader_obj.fieldnames
                    else:
                        dict_reader_obj = [] # 空文件或无表头，迭代器将为空
                    game_char_data_list = [dict(row) for row in dict_reader_obj if row.get('原文','').strip()]
                message_queue.put(("log", ("normal", f"已加载游戏人物生成字典 '{char_gen_filename}': {len(game_char_data_list)} 条。使用表头: {game_char_headers}")))
            except Exception as e:
                log.exception(f"加载游戏人物生成字典 {game_char_dict_path} 失败: {e}")
                message_queue.put(("warning", f"加载游戏人物生成字典 '{char_gen_filename}' 失败: {e}。相关操作可能受影响。将使用默认表头。"))
                game_char_headers = dictionary_manager.BASE_CHARACTER_HEADERS # 确保有默认表头
        else:
            message_queue.put(("log", ("normal", f"游戏人物生成字典 '{char_gen_filename}' 未找到，将使用默认表头并尝试处理。")))
            game_char_headers = dictionary_manager.BASE_CHARACTER_HEADERS

        # 加载游戏特定的事物生成字典
        game_entity_data_list = []
        game_entity_headers = dictionary_manager.BASE_ENTITY_HEADERS # 默认表头
        if os.path.exists(game_entity_dict_path):
            try:
                with open(game_entity_dict_path, 'r', encoding='utf-8-sig', newline='') as f:
                    dict_reader_obj = csv.DictReader(f)
                    if dict_reader_obj.fieldnames:
                        game_entity_headers = dict_reader_obj.fieldnames
                    else:
                        dict_reader_obj = []
                    game_entity_data_list = [dict(row) for row in dict_reader_obj if row.get('原文','').strip()]
                message_queue.put(("log", ("normal", f"已加载游戏事物生成字典 '{entity_gen_filename}': {len(game_entity_data_list)} 条。使用表头: {game_entity_headers}")))
            except Exception as e:
                log.exception(f"加载游戏事物生成字典 {game_entity_dict_path} 失败: {e}")
                message_queue.put(("warning", f"加载游戏事物生成字典 '{entity_gen_filename}' 失败: {e}。相关操作可能受影响。将使用默认表头。"))
                game_entity_headers = dictionary_manager.BASE_ENTITY_HEADERS
        else:
            message_queue.put(("log", ("normal", f"游戏事物生成字典 '{entity_gen_filename}' 未找到，将使用默认表头并尝试处理。")))
            game_entity_headers = dictionary_manager.BASE_ENTITY_HEADERS

        # --- 3. 步骤 1 (替换) ---
        message_queue.put(("log", ("normal", "开始执行替换步骤...")))
        replacements_made_count = 0
        fields_updated_count = 0

        base_translation_map = {}
        for item in base_char_dict + base_entity_dict:
            original = item.get('原文')
            translation = item.get('译文')
            if original and translation:
                base_translation_map[original] = translation
        
        old_to_new_translation_global_map = {}

        temp_game_char_data_list = []
        for gen_item in game_char_data_list:
            gen_original = gen_item.get('原文')
            gen_old_translation = gen_item.get('译文')
            if gen_original in base_translation_map:
                base_new_translation = base_translation_map[gen_original]
                if gen_old_translation != base_new_translation:
                    log.debug(f"人物字典替换: 原文 '{gen_original}', 旧译文 '{gen_old_translation}' -> 新译文 '{base_new_translation}'")
                    if gen_old_translation and gen_old_translation not in old_to_new_translation_global_map:
                         old_to_new_translation_global_map[gen_old_translation] = base_new_translation
                    gen_item['译文'] = base_new_translation
                    replacements_made_count += 1
            temp_game_char_data_list.append(gen_item)
        game_char_data_list = temp_game_char_data_list

        temp_game_entity_data_list = []
        for gen_item in game_entity_data_list:
            gen_original = gen_item.get('原文')
            gen_old_translation = gen_item.get('译文')
            if gen_original in base_translation_map:
                base_new_translation = base_translation_map[gen_original]
                if gen_old_translation != base_new_translation:
                    log.debug(f"事物字典替换: 原文 '{gen_original}', 旧译文 '{gen_old_translation}' -> 新译文 '{base_new_translation}'")
                    if gen_old_translation and gen_old_translation not in old_to_new_translation_global_map:
                         old_to_new_translation_global_map[gen_old_translation] = base_new_translation
                    gen_item['译文'] = base_new_translation
                    replacements_made_count += 1
            temp_game_entity_data_list.append(gen_item)
        game_entity_data_list = temp_game_entity_data_list
        
        message_queue.put(("log", ("normal", f"主译名替换完成，共替换 {replacements_made_count} 个条目的译名。")))

        if old_to_new_translation_global_map:
            message_queue.put(("log", ("normal", f"开始在所有字段中查找并替换旧译名 (共 {len(old_to_new_translation_global_map)} 组替换)...")))
            def replace_in_dict_fields(dict_list):
                nonlocal fields_updated_count
                updated_list = []
                for item_dict in dict_list:
                    new_item_dict = {}
                    for field, value in item_dict.items():
                        if isinstance(value, str):
                            original_field_value = value
                            for old_trans, new_trans in old_to_new_translation_global_map.items():
                                if old_trans in value:
                                    value = value.replace(old_trans, new_trans)
                            if value != original_field_value:
                                fields_updated_count +=1
                                log.debug(f"字段内容更新: 字段 '{field}', 原值 '{original_field_value[:30]}...', 新值 '{value[:30]}...'")
                        new_item_dict[field] = value
                    updated_list.append(new_item_dict)
                return updated_list
            game_char_data_list = replace_in_dict_fields(game_char_data_list)
            game_entity_data_list = replace_in_dict_fields(game_entity_data_list)
            message_queue.put(("log", ("normal", f"字段内容替换完成，共更新 {fields_updated_count} 个字段。")))
        else:
            message_queue.put(("log", ("normal", "没有需要进行全局字段内容替换的旧译名。")))

        # --- 4. 步骤 2 (添加) ---
        message_queue.put(("log", ("normal", "开始执行添加步骤...")))
        added_char_count = 0
        added_entity_count = 0

        if not os.path.exists(untranslated_json_path):
            message_queue.put(("warning", f"原文 JSON 文件 ({os.path.basename(untranslated_json_path)}) 不存在，无法执行添加新术语的步骤。"))
        else:
            existing_gen_originals = set()
            for item in game_char_data_list: existing_gen_originals.add(item.get('原文'))
            for item in game_entity_data_list: existing_gen_originals.add(item.get('原文'))
            
            for base_item in base_char_dict:
                base_original = base_item.get('原文')
                if base_original and base_original not in existing_gen_originals:
                    term_count = _count_term_in_json_originals(untranslated_json_path, base_original)
                    if term_count >= 3:
                        log.info(f"添加基础人物词条: '{base_original}' (出现 {term_count} 次) 到游戏人物字典。")
                        new_entry = {header: base_item.get(header, '') for header in game_char_headers}
                        game_char_data_list.append(new_entry)
                        added_char_count += 1
                        existing_gen_originals.add(base_original)
            
            for base_item in base_entity_dict:
                base_original = base_item.get('原文')
                if base_original and base_original not in existing_gen_originals:
                    term_count = _count_term_in_json_originals(untranslated_json_path, base_original)
                    if term_count >= 3:
                        log.info(f"添加基础事物词条: '{base_original}' (出现 {term_count} 次) 到游戏事物字典。")
                        new_entry = {header: base_item.get(header, '') for header in game_entity_headers}
                        game_entity_data_list.append(new_entry)
                        added_entity_count += 1
                        existing_gen_originals.add(base_original)
            
            message_queue.put(("log", ("normal", f"添加步骤完成，新增人物条目: {added_char_count}，新增事物条目: {added_entity_count}。")))

        # --- 5. 保存修改后的游戏特定字典 ---
        message_queue.put(("log", ("normal", "正在保存更新后的游戏特定字典...")))
        char_save_success = False
        entity_save_success = False

        if os.path.exists(game_char_dict_path) or game_char_data_list:
            try:
                file_system.ensure_dir_exists(os.path.dirname(game_char_dict_path))
                with open(game_char_dict_path, 'w', encoding='utf-8-sig', newline='') as f:
                    writer = csv.DictWriter(f, fieldnames=game_char_headers, quoting=csv.QUOTE_ALL, extrasaction='ignore')
                    writer.writeheader()
                    writer.writerows(game_char_data_list)
                char_save_success = True
                message_queue.put(("log", ("success", f"游戏人物字典 '{char_gen_filename}' 已更新并保存。")))
            except Exception as e:
                log.exception(f"保存更新后的游戏人物字典 '{char_gen_filename}' 失败: {e}")
                message_queue.put(("error", f"保存游戏人物字典 '{char_gen_filename}' 失败: {e}"))
        else:
            char_save_success = True
            log.info(f"游戏人物字典文件 '{char_gen_filename}' 不存在且无数据可写，跳过保存。")
            message_queue.put(("log", ("normal", f"跳过保存游戏人物字典 '{char_gen_filename}' (文件不存在且无数据)。")))


        if os.path.exists(game_entity_dict_path) or game_entity_data_list:
            try:
                file_system.ensure_dir_exists(os.path.dirname(game_entity_dict_path))
                with open(game_entity_dict_path, 'w', encoding='utf-8-sig', newline='') as f:
                    writer = csv.DictWriter(f, fieldnames=game_entity_headers, quoting=csv.QUOTE_ALL, extrasaction='ignore')
                    writer.writeheader()
                    writer.writerows(game_entity_data_list)
                entity_save_success = True
                message_queue.put(("log", ("success", f"游戏事物字典 '{entity_gen_filename}' 已更新并保存。")))
            except Exception as e:
                log.exception(f"保存更新后的游戏事物字典 '{entity_gen_filename}' 失败: {e}")
                message_queue.put(("error", f"保存游戏事物字典 '{entity_gen_filename}' 失败: {e}"))
        else:
            entity_save_success = True
            log.info(f"游戏事物字典文件 '{entity_gen_filename}' 不存在且无数据可写，跳过保存。")
            message_queue.put(("log", ("normal", f"跳过保存游戏事物字典 '{entity_gen_filename}' (文件不存在且无数据)。")))


        if char_save_success and entity_save_success:
            final_msg = (f"基础字典应用完成。替换译名: {replacements_made_count} 条，"
                         f"更新字段: {fields_updated_count} 处，"
                         f"新增人物: {added_char_count} 条，新增事物: {added_entity_count} 条。")
            message_queue.put(("success", final_msg))
            message_queue.put(("status", "应用基础字典完成"))
        else:
            message_queue.put(("error", "应用基础字典过程中保存文件失败，请检查日志。"))
            message_queue.put(("status", "应用基础字典失败 (保存错误)"))
        
        # --- 准备回调信息 ---
        if char_save_success and entity_save_success:
            callback_task_successful = True
            callback_message_content = (f"基础字典应用完成。替换译名: {replacements_made_count} 条，"
                                        f"更新字段: {fields_updated_count} 处，"
                                        f"新增人物: {added_char_count} 条，新增事物: {added_entity_count} 条。")
            message_queue.put(("success", callback_message_content)) # 主日志的成功消息
            message_queue.put(("status", "应用基础字典完成"))
        else:
            callback_task_successful = False
            callback_message_content = "应用基础字典过程中保存文件失败，请检查日志。"
            message_queue.put(("error", callback_message_content)) # 主日志的错误消息
            message_queue.put(("status", "应用基础字典失败 (保存错误)"))

    except Exception as e:
        log.exception("应用基础字典任务执行期间发生意外错误。")
        message_queue.put(("error", f"应用基础字典过程中发生严重错误: {e}"))
        message_queue.put(("status", "应用基础字典失败"))
        callback_task_successful = False # 标记任务因异常失败
        callback_message_content = f"应用基础字典过程中发生严重错误: {e}"
        
    finally:
        # --- 修改点 3: 任务结束时，发送带 task_id 的 "done" 信号 ---
        if task_id_for_callback:
            # content 是一个元组 (task_id, (success_bool, message_str))
            # success_bool 和 message_str 用于回调给编辑器
            log.debug(f"任务完成，发送带回调ID '{task_id_for_callback}' 的 done 信号。成功: {callback_task_successful}")
            message_queue.put(("done", (task_id_for_callback, (callback_task_successful, callback_message_content))))
        else:
            # 如果没有 task_id (例如从主流程或其他地方调用)，则发送普通的 done 信号
            log.debug("任务完成，发送普通 done 信号。")
            message_queue.put(("done", None))
