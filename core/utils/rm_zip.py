"""检测 RM2000/2003 资源文件名编码，并解压到新的独立目录。"""

import re
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

import chardet


RESOURCE_DIRECTORIES = {
    b"charset", b"faceset", b"chipset", b"backdrop", b"battle", b"monster",
    b"panorama", b"picture", b"system", b"title", b"gameover", b"music",
    b"sound", b"movie", b"battle2", b"battlecharset", b"battleweapon",
    b"frame", b"system2",
}


@dataclass(frozen=True)
class ZipExtractionResult:
    """保存本次解压输出路径及文件名编码的检测结果。"""

    directory: Path
    encoding: str
    confidence: float
    sample_count: int


def _check_cancelled(stop_event):
    """响应调用方的取消请求。"""
    if stop_event is not None and stop_event.is_set():
        raise ValueError("已取消解压。")


def _read_raw_name(info):
    """从 ZIP 条目还原文件名的原始字节。"""
    return info.orig_filename.encode("utf-8" if info.flag_bits & 0x800 else "cp437")


def _detect_resource_encoding(archive, stop_event):
    """拼接资源文件名字节，采用检测器置信度最高的编码。"""
    raw_names = []
    for info in archive.infolist():
        _check_cancelled(stop_event)
        raw_name = _read_raw_name(info)
        if raw_name.endswith((b"/", b"\\")):
            continue
        parts = raw_name.replace(b"\\", b"/").split(b"/")
        if any(part.lower() in RESOURCE_DIRECTORIES for part in parts[:-1]):
            raw_names.append(parts[-1])
    if not raw_names:
        raise ValueError("没有找到 RM 资源目录中的文件名。")
    sample = b"\n".join(raw_names)
    candidates = chardet.detect_all(sample, ignore_threshold=True)
    winner = max(candidates, key=lambda candidate: candidate.get("confidence") or 0)
    if not winner.get("encoding"):
        raise ValueError("检测器未返回编码。")
    encoding = winner["encoding"]
    encoding = {
        "shift_jis": "cp932", "gb2312": "gbk", "euc-kr": "cp949",
    }.get(encoding.lower(), encoding)
    return encoding, winner["confidence"], len(raw_names)


def _plan_entries(archive, encoding, stop_event):
    """解码并检查条目路径，避免越界、重名或覆盖。"""
    plan = []
    path_kinds = {}
    total_bytes = 0
    for info in archive.infolist():
        _check_cancelled(stop_event)
        name = (info.orig_filename if info.flag_bits & 0x800
                else _read_raw_name(info).decode(encoding))
        name = name.replace("\\", "/")
        is_directory = name.endswith("/")
        parts = name.rstrip("/").split("/")
        for part in parts:
            if (not part or part in (".", "..") or part.endswith((" ", "."))
                    or any(ord(character) < 32 or character in '<>:"|?*'
                           for character in part)
                    or re.fullmatch(
                        r"(?i:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])",
                        part.split(".")[0].rstrip(" "))):
                raise ValueError(f"无法安全写入路径：{name!r}")
        if (info.flag_bits & 1
                or stat.S_IFMT(info.external_attr >> 16) not in (0, stat.S_IFREG, stat.S_IFDIR)):
            raise ValueError("不支持加密 ZIP、符号链接或特殊文件。")
        if is_directory and info.file_size:
            raise ValueError("目录条目包含数据。")
        key = "/".join(parts).casefold()
        if key in path_kinds:
            raise ValueError(f"解码后的文件名重复：{name}")
        path_kinds[key] = is_directory
        total_bytes += info.file_size
        if total_bytes > 8 * 1024 ** 3 or len(path_kinds) > 100_000:
            raise ValueError("超过解压大小或文件数量上限。")
        plan.append((info, parts, is_directory))
    for key in path_kinds:
        for parent in Path(key).parents:
            if (str(parent).replace("\\", "/") in path_kinds
                    and not path_kinds[str(parent).replace("\\", "/")]):
                raise ValueError("ZIP 中存在文件与目录路径冲突。")
    return plan


def extract_zip(source_path, stop_event=None):
    """在原 ZIP 旁解压到新目录，失败或取消时清理本次临时文件。"""
    source = Path(source_path).resolve(strict=True)
    staging = None
    try:
        with zipfile.ZipFile(source) as archive:
            encoding, confidence, sample_count = _detect_resource_encoding(archive, stop_event)
            plan = _plan_entries(archive, encoding, stop_event)
            staging = Path(tempfile.mkdtemp(prefix=".windy-rmzip-", dir=source.parent)).resolve()
            for info, parts, is_directory in plan:
                _check_cancelled(stop_event)
                destination = staging.joinpath(*parts)
                if is_directory:
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                written_bytes = 0
                with archive.open(info) as input_stream, destination.open("xb") as output_stream:
                    while True:
                        _check_cancelled(stop_event)
                        chunk = input_stream.read(1024 * 1024)
                        if not chunk:
                            break
                        written_bytes += len(chunk)
                        if written_bytes > info.file_size:
                            raise ValueError("实际文件大小超过声明值。")
                        output_stream.write(chunk)
                if written_bytes != info.file_size:
                    raise ValueError("文件大小校验失败。")
        _check_cancelled(stop_event)
        output_index = 1
        while True:
            output_name = source.stem + (f"_{output_index}" if output_index > 1 else "")
            output_path = source.parent / output_name
            if not output_path.exists() and not output_path.is_symlink():
                break
            output_index += 1
        staging.rename(output_path)
        staging = None
        return ZipExtractionResult(output_path, encoding, confidence, sample_count)
    finally:
        if staging is not None:
            if staging.parent != source.parent or not staging.name.startswith(".windy-rmzip-"):
                raise ValueError("临时目录清理边界不匹配。")
            shutil.rmtree(staging)
