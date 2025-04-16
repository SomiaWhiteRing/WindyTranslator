# core/tasks/rename.py
import os
import logging
from core.external import rpgrewriter # 导入 RPGRewriter 交互模块
from core.utils import file_system

log = logging.getLogger(__name__)

def _create_input_txt(lmt_path, program_dir, write_log_var):
    """
    生成 filelist.txt 并转换为 input.txt。

    Args:
        lmt_path (str): 游戏 LMT 文件路径。
        program_dir (str): 程序根目录 (用于查找生成的 filelist.txt，根据rpgrewriter行为调整)。
        write_log_var (bool): 是否输出重命名日志文件。

    Returns:
        bool: 是否成功创建 input.txt。
        str: 创建的 input.txt 的路径。
        int: 转换的非 ASCII 文件名数量。
        str: 日志文件名 (如果 write_log_var 为 True)，否则为 "null"。
    """
    log.info("步骤 1.1: 生成原始文件列表 (filelist.txt)...")
    # RPGRewriter 可能在 lmt_path 目录生成 filelist.txt
    game_dir = os.path.dirname(lmt_path)
    success, filelist_path = rpgrewriter.generate_filelist(lmt_path)

    if not success or not filelist_path:
        log.error("无法生成 filelist.txt。")
        return False, None, 0, "null"

    log.info(f"成功生成 filelist.txt: {filelist_path}")
    log.info("步骤 1.2: 处理 filelist.txt 并生成 input.txt...")

    try:
        with open(filelist_path, 'r', encoding='utf-8', errors='replace') as file:
            lines = file.readlines()
        lines = [line.rstrip('\r\n') for line in lines] # 移除换行符

        converted_count = 0
        output_lines = []
        # 假设 filelist.txt 的结构是 Original\n___\nOriginal\n___...
        # 或者可能是 Original\nUnicode\nOriginal\nUnicode... RPGRewriter 文档不清晰
        # 这里采用原脚本逻辑：查找 "___"，替换为 Unicode 或原名
        is_line_after_original = False
        last_original_name = ""
        for i, line in enumerate(lines):
            if line.strip() == "___": # 如果是占位符行
                 # 检查上一行是否有效
                 if last_original_name:
                     # 检查上一行（原始名）是否包含非ASCII字符
                     if any(ord(c) > 127 for c in last_original_name):
                         unicode_name = "".join([f"u{ord(c):04x}" if ord(c) > 127 else c for c in last_original_name])
                         output_lines.append(unicode_name) # 添加转换后的 Unicode 名
                         log.debug(f"转换文件名: {last_original_name} -> {unicode_name}")
                         converted_count += 1
                     else:
                         output_lines.append(last_original_name) # 非ASCII，保留原名
                 else:
                     log.warning(f"在 filelist.txt 第 {i+1} 行找到 '___'，但缺少前一个有效原始文件名。")
                     output_lines.append("___") # 保留占位符以防万一
                 last_original_name = "" # 重置
            else: # 如果不是占位符行，认为是原始文件名
                 output_lines.append(line) # 先原样添加原始名
                 last_original_name = line # 记录下来，供下一个'___'行使用

        # 注意：上面的逻辑假设 filelist.txt 是 原名\n___\n原名\n___ 的格式。
        # 如果 RPGRewriter -F 输出的格式是 原名\n转换名\n原名\n转换名...
        # 则需要修改逻辑为：读取一行（原名），再读一行（目标名），
        # 如果目标名是"___"，则根据原名生成 Unicode 目标名。

        # 将处理后的内容写入 input.txt (放在 lmt 同目录下，供 RPGRewriter 读取)
        input_path = os.path.join(game_dir, "input.txt")
        with open(input_path, 'w', encoding='utf-8') as file:
            file.write('\n'.join(output_lines))

        log.info(f"已生成 input.txt: {input_path}，共转换 {converted_count} 个非 ASCII 文件名。")
        # 删除临时的 filelist.txt
        file_system.safe_remove(filelist_path)

        log_filename = "renames_log.txt" if write_log_var else "null" # 使用 .txt 后缀
        return True, input_path, converted_count, log_filename

    except Exception as e:
        log.exception(f"处理 filelist.txt 或创建 input.txt 时出错: {e}")
        # 清理可能的中间文件
        file_system.safe_remove(filelist_path)
        return False, None, 0, "null"

# --- 主任务函数 ---
def run_rename(game_path, program_dir, write_log, message_queue):
    """
    执行重写文件名流程。

    Args:
        game_path (str): 游戏根目录路径。
        program_dir (str): 程序根目录路径。
        write_log (bool): 是否输出重命名日志。
        message_queue (queue.Queue): 用于向主线程发送消息的队列。
    """
    try:
        message_queue.put(("status", "正在重写文件名..."))
        message_queue.put(("log", ("normal", "步骤 1: 开始重写文件名...")))

        lmt_path = os.path.join(game_path, "RPG_RT.lmt")
        if not os.path.exists(lmt_path):
            message_queue.put(("error", f"未找到 RPG_RT.lmt 文件: {lmt_path}"))
            message_queue.put(("status", "重写文件名失败"))
            message_queue.put(("done", None))
            return

        # 1. 生成 input.txt
        success_input, input_txt_path, converted_count, log_filename = _create_input_txt(lmt_path, program_dir, write_log)
        if not success_input:
            message_queue.put(("error", "生成 input.txt 文件失败。"))
            message_queue.put(("status", "重写文件名失败"))
            message_queue.put(("done", None))
            return

        # 2. 验证文件名 (RPGRewriter -V)
        message_queue.put(("log", ("normal", "步骤 1.3: 验证文件名 (RPGRewriter -V)...")))
        return_code_v, stdout_v, stderr_v = rpgrewriter.validate_rename_input(lmt_path)
        if return_code_v != 0:
            message_queue.put(("error", f"文件名验证失败。退出码: {return_code_v}"))
            if stderr_v: message_queue.put(("log", ("error", f"RPGRewriter 错误信息: {stderr_v}")))
            message_queue.put(("status", "重写文件名失败"))
            message_queue.put(("done", None))
            # 清理 input.txt? 可选
            # file_system.safe_remove(input_txt_path)
            return
        message_queue.put(("log", ("normal", "文件名验证通过。")))

        # 3. 重写游戏数据 (RPGRewriter -rewrite)
        message_queue.put(("log", ("normal", "步骤 1.4: 重写游戏数据 (RPGRewriter -rewrite)...")))
        return_code_rw, stdout_rw, stderr_rw = rpgrewriter.rewrite_game_data(lmt_path, rewrite_all=True, log_filename=log_filename)
        if return_code_rw != 0:
            message_queue.put(("error", f"重写游戏数据失败。退出码: {return_code_rw}"))
            if stderr_rw: message_queue.put(("log", ("error", f"RPGRewriter 错误信息: {stderr_rw}")))
            message_queue.put(("status", "重写文件名失败"))
            message_queue.put(("done", None))
            # 清理 input.txt? 可选
            # file_system.safe_remove(input_txt_path)
            return

        message_queue.put(("success", "文件名重写完成"))
        message_queue.put(("log", ("success", f"成功转换 {converted_count} 个非 ASCII 文件名并重写游戏数据。")))

        # 检查日志文件 (如果生成了)
        if write_log and log_filename != "null":
            # RPGRewriter 在哪里生成日志？假设在 lmt 同目录
            actual_log_path = os.path.join(os.path.dirname(lmt_path), log_filename)
            if os.path.exists(actual_log_path):
                try:
                    with open(actual_log_path, 'r', encoding='utf-8', errors='replace') as log_f:
                        log_content = log_f.read().strip()
                    if log_content:
                         missing_count = log_content.count('\n') + 1
                         message_queue.put(("log", ("warning", f"重命名日志 '{log_filename}' 显示有 {missing_count} 个文件名未找到对应翻译或引用。")))
                    else:
                         message_queue.put(("log", ("normal", f"重命名日志 '{log_filename}' 为空，所有引用均已更新。")))
                except Exception as log_e:
                    message_queue.put(("log", ("error", f"读取重命名日志 '{actual_log_path}' 时出错: {log_e}")))
            else:
                 message_queue.put(("log", ("warning", f"请求生成重命名日志，但未找到文件: {actual_log_path}")))


        message_queue.put(("status", "文件名重写完成"))
        message_queue.put(("done", None)) # 标记任务完成

        # 清理 input.txt
        file_system.safe_remove(input_txt_path)

    except Exception as e:
        log.exception("重写文件名任务执行期间发生意外错误。")
        message_queue.put(("error", f"重写文件名过程中发生严重错误: {e}"))
        message_queue.put(("status", "重写文件名失败"))
        message_queue.put(("done", None))