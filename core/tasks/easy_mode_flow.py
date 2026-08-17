# core/tasks/easy_mode_flow.py
import os
import logging
import queue # 需要引用 queue 来检查类型
from . import initialize, rename, export, json_creation, dict_generation, translate, json_release, import_task
from core.utils import (text_processing, dictionary_manager)

log = logging.getLogger(__name__)

# --- 轻松模式总控任务 ---
def run_easy_flow(
    game_path,
    works_dir,
    rtp_options,
    export_encoding, # export 需要
    import_encoding, # import_task 需要
    export_scope,
    world_dict_config, # dict_generation 需要
    translate_config, # translate 需要
    message_queue
):
    """
    按顺序执行轻松模式下的所有翻译步骤。

    Args:
        game_path (str): 游戏路径。
        works_dir (str): Works 目录。
        rtp_options (dict): RTP 选择。
        export_encoding (str): 导出编码。
        import_encoding (str): 导入编码。
        world_dict_config (dict): Gemini 配置。
        translate_config (dict): DeepSeek 配置。
        message_queue (queue.Queue): 消息队列。
    """
    current_step = 0
    total_steps = 8 # 定义总步骤数

    steps = [
        {"name": "初始化", "func": initialize.run_initialize, "args": [game_path, rtp_options, message_queue]},
        {"name": "导出文本", "func": export.run_export, "args": [game_path, export_encoding, export_scope, message_queue]},
        {"name": "重写文件名", "func": rename.run_rename, "args": [game_path, message_queue]},
        {"name": "制作JSON文件", "func": json_creation.run_create_json, "args": [game_path, works_dir, message_queue]},
        {"name": "生成世界观字典", "func": dict_generation.run_generate_dictionary, "args": [game_path, works_dir, world_dict_config, message_queue]},
        {"name": "翻译JSON文件", "func": translate.run_translate, "args": [game_path, works_dir, translate_config, world_dict_config, message_queue]},
        # {"name": "释放JSON文件", "func": json_release.run_release_json, "args": [game_path, works_dir, selected_json_path, message_queue]}, # 需要 App 决定路径
        # {"name": "导入文本", "func": import_task.run_import, "args": [game_path, import_encoding, message_queue]},
    ]
    
    # --- 特殊处理：释放 JSON 和 导入文本 ---
    # 释放 JSON 需要先确定使用哪个 translated JSON 文件，导入文本则依赖于释放成功。
    # 简单起见，轻松模式默认使用 `translation_translated.json`
    # App 层在启动轻松模式时，需要检查这个文件是否存在，如果不存在，可以跳过释放和导入，或报错。
    # 这里假设 App 层会处理，或者 Task 内部检查。我们让 Task 内部检查。

    release_step = {
        "name": "释放JSON文件",
        "func": json_release.run_release_json,
        # 参数需要 selected_json_path，这里硬编码默认值
        "args_func": lambda: [
            game_path,
            works_dir,
            os.path.join(works_dir, text_processing.sanitize_filename(os.path.basename(game_path)) or "UntitledGame", "translated", "translation_translated.json"),
            message_queue
        ]
    }
    import_step = {
        "name": "导入文本",
        "func": import_task.run_import,
        "args": [game_path, export_encoding, import_encoding, message_queue]
    }

    # 将最后两步加入
    steps.append(release_step)
    steps.append(import_step)
    total_steps = len(steps) # 更新总步骤数

    log.info("开始执行轻松模式翻译流程...")
    message_queue.put(("status", "轻松模式启动..."))

    try:
        for i, step_info in enumerate(steps):
            current_step = i + 1
            step_name = step_info["name"]
            step_func = step_info["func"]
            # 处理动态参数
            if "args_func" in step_info:
                step_args = step_info["args_func"]()
            else:
                step_args = step_info["args"]

            message_queue.put(("status", f"({current_step}/{total_steps}) 正在执行: {step_name}..."))
            message_queue.put(("log", ("normal", f"--- 轻松模式步骤 {current_step}/{total_steps}: {step_name} ---")))
            
            # 检查释放步骤所需的文件是否存在
            if step_name == "释放JSON文件":
                 json_to_release = step_args[2] # 获取将要使用的 json 路径
                 if not os.path.exists(json_to_release):
                     message_queue.put(("warning", f"未找到预期的翻译文件 '{os.path.basename(json_to_release)}'，将跳过释放和导入步骤。"))
                     log.warning(f"跳过释放和导入，因为文件不存在: {json_to_release}")
                     message_queue.put(("status", "轻松模式未生成可释放的翻译文件"))
                     return False

            step_func(*step_args)

            if getattr(message_queue, "has_problem", False):
                message_queue.put(("error", f"步骤“{step_name}”失败，轻松模式已停止。"))
                message_queue.put(("status", f"轻松模式中止于步骤 {current_step}: {step_name}"))
                return False

            # 更新进度条 (放在成功完成一步后)
            progress_value = (current_step / total_steps) * 100
            message_queue.put(("progress", progress_value)) # 发送给 App 更新 UI
            message_queue.put(("log", ("success", f"步骤 '{step_name}' 完成。")))
            # 短暂休眠，避免状态更新过快看不清
            # time.sleep(0.1)

        # 所有步骤成功完成
        message_queue.put(("success", "轻松模式所有步骤已成功完成。"))
        message_queue.put(("status", "轻松模式翻译流程完成！"))
        return True

    except Exception as e:
        # 这个异常是 easy_flow 自身发生的，而不是子任务内部的
        step_name = steps[current_step-1]["name"] if current_step > 0 else "未知步骤"
        log.exception(f"轻松模式流程在步骤 '{step_name}' 外部发生意外错误。")
        message_queue.put(("error", f"轻松模式流程发生严重错误: {e}"))
        message_queue.put(("status", f"轻松模式中止于步骤 {current_step}"))
        return False
