import logging
import os
import platform
import subprocess

from core.utils.file_system import get_application_path

log = logging.getLogger(__name__)

UBERWOLF_DIR = os.path.join(get_application_path(), "modules", "UberWolf")
UBERWOLF_CLI = os.path.join(UBERWOLF_DIR, "UberWolfCli.exe")
WOLF_TEXT_HELPER = os.path.join(UBERWOLF_DIR, "WolfRPGText.exe")


class UberWolfError(RuntimeError):
    pass


def _run(executable, args, cwd=None):
    if not os.path.isfile(executable):
        raise FileNotFoundError(f"WOLF 工具不存在: {executable}")

    command = [executable, *[str(arg) for arg in args]]
    creation_flags = subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
    completed = subprocess.run(
        command,
        cwd=cwd or os.path.dirname(executable),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creation_flags,
        check=False,
    )
    stdout = completed.stdout.strip()
    stderr = completed.stderr.strip()
    log.debug("WOLF 命令完成: %s (exit=%s)", command, completed.returncode)
    if completed.returncode != 0:
        details = "\n".join(part for part in (stdout, stderr) if part)
        raise UberWolfError(f"WOLF 工具执行失败 (exit={completed.returncode}): {details}")
    return stdout, stderr


def unpack_game(game_path):
    game_exe = os.path.join(game_path, "Game.exe")
    _run(UBERWOLF_CLI, [game_exe])
    data_path = os.path.join(game_path, "Data")
    if not os.path.isdir(data_path):
        raise UberWolfError(f"UberWolf 未生成 Data 目录: {data_path}")
    return data_path


def pack_game(game_path, pack_mode):
    game_exe = os.path.join(game_path, "Game.exe")
    archive_path = os.path.join(game_path, "Data.wolf")
    data_path = os.path.join(game_path, "Data")
    if os.path.isdir(data_path) and not os.path.exists(archive_path):
        # UberWolf chooses its packing layout from the presence of Data.wolf.
        with open(archive_path, "wb"):
            pass
    stdout, stderr = _run(UBERWOLF_CLI, ["--override", "--pack", str(pack_mode), game_exe])
    if not os.path.isfile(archive_path):
        details = "\n".join(part for part in (stdout, stderr) if part)
        raise UberWolfError(f"UberWolf 未生成 Data.wolf: {archive_path}\n{details}")
    if os.path.getsize(archive_path) == 0:
        raise UberWolfError(f"UberWolf 生成了空的 Data.wolf: {archive_path}")
    return archive_path


def dump_text(data_path, json_path):
    return _run(WOLF_TEXT_HELPER, ["dump", data_path, json_path])


def apply_text(data_path, json_path, output_data_path):
    return _run(WOLF_TEXT_HELPER, ["apply", data_path, json_path, output_data_path])


def detect_pack_mode(game_exe):
    try:
        import win32api

        version = win32api.GetFileVersionInfo(game_exe, "\\")
        major = version["FileVersionMS"] >> 16
        minor = version["FileVersionMS"] & 0xFFFF
    except Exception as error:
        raise UberWolfError(f"无法读取 Game.exe 的 WOLF 版本: {error}") from error

    if major >= 3:
        if minor >= 500:
            return 7
        if minor >= 310:
            return 6 if minor < 500 else 7
        if minor >= 140:
            return 5
        return 4
    if major == 2:
        if minor >= 225:
            return 3
        if minor >= 200:
            return 2
        if minor >= 100:
            return 1
        return 0
    raise UberWolfError(f"暂不支持 WOLF RPG Editor {major}.{minor} 的重新封包")
