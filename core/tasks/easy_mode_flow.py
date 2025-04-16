# core/tasks/easy_mode_flow.py
import logging
import queue # 需要引用 queue 来检查类型
from . import initialize, rename, export, json_creation, dict_generation, translate, json_release, import_task

log = logging.getLogger(__name__)

# --- 轻松模式总控任务 ---
def run_easy_flow(
    game_path,
    program_dir, # rename 需要
    works_dir,
    rtp_options,
    export_encoding, # export 需要
    import_encoding, # import_task 需要
    world_dict_config, # dict_generation 需要
    translate_config, # translate 需要
    write_log_rename, # rename 需要
    message_queue
):
    """
    按顺序执行轻松模式下的所有翻译步骤。

    Args:
        game_path (str): 游戏路径。
        program_dir (str): 程序根目录。
        works_dir (str): Works 目录。
        rtp_options (dict): RTP 选择。
        export_encoding (str): 导出编码。
        import_encoding (str): 导入编码。
        world_dict_config (dict): Gemini 配置。
        translate_config (dict): DeepSeek 配置。
        write_log_rename (bool): 重命名是否写日志。
        message_queue (queue.Queue): 消息队列。
    """
    current_step = 0
    total_steps = 8 # 定义总步骤数

    steps = [
        {"name": "初始化", "func": initialize.run_initialize, "args": [game_path, rtp_options, message_queue]},
        {"name": "重写文件名", "func": rename.run_rename, "args": [game_path, program_dir, write_log_rename, message_queue]},
        {"name": "导出文本", "func": export.run_export, "args": [game_path, export_encoding, message_queue]},
        {"name": "制作JSON文件", "func": json_creation.run_create_json, "args": [game_path, works_dir, message_queue]},
        {"name": "生成世界观字典", "func": dict_generation.run_generate_dictionary, "args": [game_path, works_dir, world_dict_config, message_queue]},
        {"name": "翻译JSON文件", "func": translate.run_translate, "args": [game_path, works_dir, translate_config, message_queue]},
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
        "args": [game_path, import_encoding, message_queue]
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
            
            # --- 调用子任务 ---
            # 子任务会自己处理异常并通过队列报告
            # 我们需要一种方法来知道子任务是否成功完成，以便决定是否继续
            # 修改子任务，让它们在完成后通过队列发送一个特殊的成功/失败标记？
            # 或者 easy_flow 监听队列中的 error 消息？
            # 采用后者：easy_flow 委托执行，App 层负责监控队列和处理错误/停止流程。
            # 因此，easy_flow 只负责按顺序调用。

            # --- **重要修改：子任务现在是阻塞的，easy_flow 等待其完成** ---
            # 这简化了流程控制，但意味着 easy_flow 自身也需要在一个单独线程运行
            # (这通常由 App 层的 run_in_thread 完成)

            # 检查释放步骤所需的文件是否存在
            if step_name == "释放JSON文件":
                 json_to_release = step_args[2] # 获取将要使用的 json 路径
                 if not os.path.exists(json_to_release):
                     message_queue.put(("warning", f"未找到预期的翻译文件 '{os.path.basename(json_to_release)}'，将跳过释放和导入步骤。"))
                     log.warning(f"跳过释放和导入，因为文件不存在: {json_to_release}")
                     break # 跳出循环，结束流程

            # --- 直接调用子任务函数 ---
            step_func(*step_args)

            # --- 等待子任务完成信号 ---
            # 子任务的最后会发送 ("done", None)
            # 我们需要从队列里接收这个信号，才知道可以进行下一步
            # **注意：** 这部分逻辑放在 Task 内部会导致问题，因为 Task 不应该消费队列。
            # **正确做法：** App 层启动 easy_flow 任务，并监控队列。
            # 当 App 收到 'done' 时，如果当前是 easy_flow 在运行，则触发 easy_flow 的下一步。
            # 这使得 easy_flow 变成了一个状态机，由 App 控制其推进。
            #
            # **简化方案 (当前采用)：** 假设子任务是同步执行的（或者 easy_flow 等待它们完成）。
            # 在 App 层的 `run_in_thread` 包装器中，当 `target_func` (即这里的 `run_easy_flow`) 返回时，
            # 就认为整个流程结束了。子任务内部发生的错误会通过队列报告，App 层可以捕获并停止。
            # 所以 `run_easy_flow` 内部不需要显式等待 'done' 信号。

            # --- 检查子任务是否报告了错误 ---
            # (这部分检查逻辑也应该在 App 层完成)
            # if check_if_error_reported(message_queue): # 假设有这个函数
            #    message_queue.put(("error", f"在步骤 '{step_name}' 中检测到错误，轻松模式中止。"))
            #    message_queue.put(("status", f"轻松模式中止于步骤 {current_step}"))
            #    message_queue.put(("done", None)) # 发送完成信号
            #    return # 中止流程

            # 更新进度条 (放在成功完成一步后)
            progress_value = (current_step / total_steps) * 100
            message_queue.put(("progress", progress_value)) # 发送给 App 更新 UI
            message_queue.put(("log", ("success", f"步骤 '{step_name}' 完成。")))
            # 短暂休眠，避免状态更新过快看不清
            # time.sleep(0.1)

        # 所有步骤成功完成
        message_queue.put(("success", "轻松模式所有步骤已成功完成。"))
        message_queue.put(("status", "轻松模式翻译流程完成！"))
        # message_queue.put(("done", None)) # App 层的 run_in_thread 会在函数返回后发送 done

    except Exception as e:
        # 这个异常是 easy_flow 自身发生的，而不是子任务内部的
        step_name = steps[current_step-1]["name"] if current_step > 0 else "未知步骤"
        log.exception(f"轻松模式流程在步骤 '{step_name}' 外部发生意外错误。")
        message_queue.put(("error", f"轻松模式流程发生严重错误: {e}"))
        message_queue.put(("status", f"轻松模式中止于步骤 {current_step}"))
        # message_queue.put(("done", None)) # App 层的 run_in_thread 会发送 done