# core/external/rpgrewriter.py
import os
import subprocess
import logging
import sys
import platform # 新增导入

log = logging.getLogger(__name__)

# 定义 RPGRewriter.exe 的预期相对路径或绝对路径
# 更好的做法是在配置中管理这个路径，但暂时硬编码，后续可移到 config 或 app
# 假设程序结构如前所述，RPGRewriter 在 modules 目录下
PROGRAM_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) # 获取项目根目录 rpg_translator/
RPGREWRITER_EXE_PATH = os.path.join(PROGRAM_DIR, "modules", "RPGRewriter", "RPGRewriter.exe")

# --- RPGRewriter 核心交互函数 ---

def run_rpgrewriter_command(lmt_path, command_args, interact_input=None):
    """
    执行 RPGRewriter 命令。

    Args:
        lmt_path (str): 游戏 RPG_RT.lmt 文件的完整路径。
        command_args (list): 传递给 RPGRewriter.exe 的参数列表 (不包括 exe 本身和 lmt_path)。
        interact_input (str, optional): 需要通过 stdin 输入的文本 (例如 "Y\n")。默认为 None。

    Returns:
        tuple: (return_code, stdout, stderr)
               return_code (int): 进程退出码。
               stdout (str): 标准输出内容。
               stderr (str): 标准错误内容。
    """
    if not os.path.exists(RPGREWRITER_EXE_PATH):
        log.error(f"RPGRewriter.exe 未找到: {RPGREWRITER_EXE_PATH}")
        raise FileNotFoundError(f"RPGRewriter.exe 未找到: {RPGREWRITER_EXE_PATH}")
    if not os.path.exists(lmt_path):
        log.error(f"游戏 LMT 文件未找到: {lmt_path}")
        raise FileNotFoundError(f"游戏 LMT 文件未找到: {lmt_path}")

    # 确定运行目录（通常是包含 .lmt 的目录）
    run_dir = os.path.dirname(lmt_path)
    # 如果需要，在工作目录中运行 RPGRewriter，因为它可能依赖工作目录生成文件
    # program_dir = os.path.dirname(RPGREWRITER_EXE_PATH)

    # 完整命令列表
    full_command = [RPGREWRITER_EXE_PATH, lmt_path] + command_args

    creation_flags = 0
    if platform.system() == "Windows":
        creation_flags = subprocess.CREATE_NO_WINDOW # 0x08000000

    log.info(f"执行 RPGRewriter 命令: {' '.join(full_command)} (工作目录: {run_dir})")

    try:
        # 使用 Popen 处理可能的交互
        process = subprocess.Popen(
            full_command,
            stdin=subprocess.PIPE if interact_input else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,          # 使用文本模式读写 stdout/stderr
            encoding=sys.getdefaultencoding(), # 尝试使用系统默认编码，减少乱码问题
            errors='replace',   # 替换无法解码的字符
            cwd=run_dir,         # 在 lmt 文件所在目录执行
            # cwd=program_dir     # 或者在程序所在目录执行？取决于 RPGRewriter 行为
            creationflags=creation_flags # 新增此参数
        )

        stdout, stderr = process.communicate(input=interact_input if interact_input else None)
        return_code = process.returncode

        # 日志记录输出（截断长输出）
        stdout_log = stdout.strip() if stdout else ""
        stderr_log = stderr.strip() if stderr else ""
        log.debug(f"RPGRewriter 命令完成。退出码: {return_code}")
        if stdout_log:
            log.debug(f"RPGRewriter stdout (前500字符): {stdout_log[:500]}{'...' if len(stdout_log) > 500 else ''}")
        if stderr_log:
            log.warning(f"RPGRewriter stderr (前500字符): {stderr_log[:500]}{'...' if len(stderr_log) > 500 else ''}")

        return return_code, stdout_log, stderr_log

    except FileNotFoundError:
        log.exception(f"执行失败: RPGRewriter.exe 或其依赖项未找到 ({RPGREWRITER_EXE_PATH})")
        raise # 重新抛出异常，让上层处理
    except Exception as e:
        log.exception(f"执行 RPGRewriter 命令时发生意外错误: {e}")
        # 返回一个明确的错误状态，而不是抛出通用异常
        return -1, "", f"执行 RPGRewriter 时发生 Python 内部错误: {e}"


# --- 特定命令的封装 (可选，但能简化 tasks 模块) ---

def generate_filelist(lmt_path):
    """
    调用 RPGRewriter 生成 filelist.txt。
    RPGRewriter 会在程序执行目录下生成 filelist.txt。

    Args:
        lmt_path (str): 游戏 LMT 文件路径。

    Returns:
        bool: True 如果成功生成 filelist.txt，False 否则。
        str: 生成的 filelist.txt 的完整路径 (如果成功)，或 None。
    """
    args = ["-F", "Y"] # 命令参数生成 filelist.txt
    return_code, _, stderr = run_rpgrewriter_command(lmt_path, args)

    # RPGRewriter 会在 *RPGRewriter.exe 所在目录* 或 *工作目录* 生成 filelist.txt
    # 根据 run_rpgrewriter_command 的 cwd 设置，我们假设在 lmt_path 目录
    expected_filelist_path = os.path.join(os.path.dirname(lmt_path), "filelist.txt")
    # 或者在程序执行目录？这里需要确认 RPGRewriter 的实际行为
    # expected_filelist_path = os.path.join(PROGRAM_DIR, "filelist.txt") # 备选

    if return_code == 0 and os.path.exists(expected_filelist_path):
        log.info(f"成功生成 filelist.txt: {expected_filelist_path}")
        return True, expected_filelist_path
    else:
        log.error(f"生成 filelist.txt 失败。退出码: {return_code}, Stderr: {stderr}")
        if not os.path.exists(expected_filelist_path):
            log.error(f"未能找到生成的 filelist.txt 于: {expected_filelist_path}")
        return False, None

def validate_rename_input(lmt_path):
    """
    调用 RPGRewriter 使用 input.txt 验证文件名 (相当于原脚本的 -V)。
    RPGRewriter 需要 input.txt 文件存在于工作目录中。

    Args:
        lmt_path (str): 游戏 LMT 文件路径。

    Returns:
        tuple: (return_code, stdout, stderr)
    """
    args = ["-V"]
    # 确保 input.txt 在 lmt_path 所在目录
    input_txt_path = os.path.join(os.path.dirname(lmt_path), "input.txt")
    if not os.path.exists(input_txt_path):
         log.error(f"验证重命名前未找到 input.txt: {input_txt_path}")
         # 返回错误码，避免执行命令
         return -1, "", f"未找到 input.txt: {input_txt_path}"
         
    return run_rpgrewriter_command(lmt_path, args)

def rewrite_game_data(lmt_path, rewrite_all=True, log_filename=None):
    """
    调用 RPGRewriter 重写游戏数据 (-rewrite)。
    需要 input.txt 存在于工作目录。
    需要用户交互输入 "Y"。

    Args:
        lmt_path (str): 游戏 LMT 文件路径。
        rewrite_all (bool): 是否使用 -all 参数。默认为 True。
        log_filename (str, optional): 指定日志文件名 (不含路径)。如果为 None 或空，则不记录日志。

    Returns:
        tuple: (return_code, stdout, stderr)
    """
    args = ["-rewrite"]
    if rewrite_all:
        args.append("-all")
    if log_filename:
        args.extend(["-log", log_filename])
    else:
        # RPGRewriter 可能不允许没有 log 参数？或者需要指定 null？
        # 根据原脚本逻辑，指定 "null" 来避免生成文件
        args.extend(["-log", "null"])

    # 确保 input.txt 在 lmt_path 所在目录
    input_txt_path = os.path.join(os.path.dirname(lmt_path), "input.txt")
    if not os.path.exists(input_txt_path):
         log.error(f"重写数据前未找到 input.txt: {input_txt_path}")
         return -1, "", f"未找到 input.txt: {input_txt_path}"

    return run_rpgrewriter_command(lmt_path, args, interact_input="Y\n")

def export_text_command(lmt_path, encoding_code):
    """
    执行导出文本的命令。

    Args:
        lmt_path (str): 游戏 LMT 文件路径。
        encoding_code (str): 读取时使用的编码代号 (如 "932")。

    Returns:
        tuple: (return_code, stdout, stderr)
    """
    args = ["-export", "-readcode", str(encoding_code)]
    # 注意：这个命令不需要交互输入
    return run_rpgrewriter_command(lmt_path, args)

def import_text_command(lmt_path, encoding_code):
    """
    执行导入文本的命令。

    Args:
        lmt_path (str): 游戏 LMT 文件路径。
        encoding_code (str): 写入时使用的编码代号 (如 "936")。

    Returns:
        tuple: (return_code, stdout, stderr)
    """
    # 原脚本使用了 -nolimit 1 参数
    args = ["-import", "-writecode", str(encoding_code), "-nolimit", "1"]
    # 注意：这个命令不需要交互输入
    return run_rpgrewriter_command(lmt_path, args)