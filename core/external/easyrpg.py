# core/external/easyrpg.py
import os
import logging
import subprocess
from core.utils.file_system import get_modules_path
from core.utils import file_system

log = logging.getLogger(__name__)

# EasyRPG 模块源路径
EASYRPG_SRC_DIR = os.path.join(get_modules_path(), "EasyRPG")
EASYRPG_PLAYER_PATH = os.path.join(EASYRPG_SRC_DIR, "Player.exe")
EASYRPG_ENCODING_MAP = {
    "ibm-943_p15a-2003": "932",
    "windows-936-2000": "936",
    "windows-949-2000": "949",
    "big5": "950",
    "windows-950": "950",
    "windows-874": "874",
    "ibm-5348_p100-1997": "1252",
    "windows-1252": "1252",
    "ibm-5346_p100-1998": "1250",
    "windows-1250": "1250",
    "ibm-5347_p100-1998": "1251",
    "windows-1251": "1251",
}


class EncodingDetectionError(RuntimeError):
    pass


def detect_game_encoding(game_path):
    """Detect an RPG Maker 2000/2003 project's encoding without trusting RPG_RT.ini."""
    if not os.path.isfile(EASYRPG_PLAYER_PATH):
        raise EncodingDetectionError(f"未找到 EasyRPG Player: {EASYRPG_PLAYER_PATH}")

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    completed = subprocess.run(
        [EASYRPG_PLAYER_PATH, "--detect-encoding"],
        cwd=game_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        creationflags=creationflags,
        check=False,
    )
    if completed.returncode != 0:
        details = (completed.stderr or completed.stdout).strip()
        raise EncodingDetectionError(f"EasyRPG 编码检测失败: {details[:500]}")

    raw_encoding = completed.stdout.strip()
    encoding = EASYRPG_ENCODING_MAP.get(raw_encoding.casefold())
    if encoding is None:
        raise EncodingDetectionError(
            f"EasyRPG 返回了无法识别的编码 {raw_encoding!r}，请上报开发者或手动指定编码。"
        )
    return encoding


def resolve_game_encoding(game_path, selection):
    return detect_game_encoding(game_path) if str(selection).casefold() == "auto" else str(selection)

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
