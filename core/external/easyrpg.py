# core/external/easyrpg.py
import os
import logging
from core.utils import file_system # 导入我们的文件系统工具

log = logging.getLogger(__name__)

# EasyRPG 模块源路径 (同样，最好由配置或 App 层提供)
PROGRAM_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EASYRPG_SRC_DIR = os.path.join(PROGRAM_DIR, "modules", "EasyRPG")

def copy_easyrpg_files(target_game_dir):
    """
    将 EasyRPG 模块中的文件复制到游戏目录。

    Args:
        target_game_dir (str): 目标游戏目录路径。

    Returns:
        tuple: (success, copied_count, skipped_count)
               success (bool): 操作是否整体成功（没有致命错误）。
               copied_count (int): 成功复制的文件数量。
               skipped_count (int): 因目标文件已存在而跳过的文件数量。
    """
    if not os.path.isdir(EASYRPG_SRC_DIR):
        log.error(f"EasyRPG 源目录未找到: {EASYRPG_SRC_DIR}")
        return False, 0, 0
    if not os.path.isdir(target_game_dir):
        log.error(f"目标游戏目录不存在: {target_game_dir}")
        return False, 0, 0

    log.info(f"开始将 EasyRPG 文件从 {EASYRPG_SRC_DIR} 复制到 {target_game_dir}")
    copied_count = 0
    skipped_count = 0
    overall_success = True

    try:
        for item in os.listdir(EASYRPG_SRC_DIR):
            src_path = os.path.join(EASYRPG_SRC_DIR, item)
            dst_path = os.path.join(target_game_dir, item)

            if os.path.isfile(src_path):
                if not os.path.exists(dst_path):
                    if file_system.safe_copy(src_path, dst_path):
                        copied_count += 1
                    else:
                        overall_success = False # 记录有复制失败的情况
                        # 可以选择在这里停止，或者继续复制其他文件
                else:
                    log.debug(f"文件已存在，跳过: {dst_path}")
                    skipped_count += 1
            # 可以选择性地处理子目录，但原脚本似乎只复制文件
            # elif os.path.isdir(src_path):
            #     # 实现目录复制逻辑 (e.g., using shutil.copytree with dirs_exist_ok=True)
            #     pass

        log.info(f"EasyRPG 文件复制完成: 复制 {copied_count} 个文件，跳过 {skipped_count} 个已存在文件。")
        return overall_success, copied_count, skipped_count

    except Exception as e:
        log.exception(f"复制 EasyRPG 文件时发生错误: {e}")
        return False, copied_count, skipped_count