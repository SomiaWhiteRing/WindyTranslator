import logging
import os
import re
import shutil
import struct
import zipfile
from pathlib import PurePosixPath

from core.utils import file_system
from core.utils.file_system import get_application_path

log = logging.getLogger(__name__)

# RTP 集合源路径
RTP_COLLECTION_DIR = os.path.join(get_application_path(), "modules", "RTPCollection")

_EOCD_SIGNATURE = b"PK\x05\x06"
_CENTRAL_DIR_SIGNATURE = b"PK\x01\x02"
_EOCD_MIN_SIZE = 22
_MAX_ZIP_COMMENT = 65535
_CANDIDATE_ENCODINGS = ("utf-8", "cp932", "gbk", "cp437")
_ZIP_NAME_ENCODING_OVERRIDES = {
    "2000fix.zip": "gbk",
}
_KANA_RE = re.compile(r"[\u3040-\u30ff]")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_SUSPICIOUS_CHAR_RE = re.compile(r"[\u00c0-\u024f\u0370-\u03ff\u2500-\u259f]")


def _read_raw_zip_filenames(zip_path):
    """读取 ZIP 中央目录里的原始文件名字节。"""
    with open(zip_path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        file_size = fh.tell()
        tail_size = min(file_size, _EOCD_MIN_SIZE + _MAX_ZIP_COMMENT)

        fh.seek(file_size - tail_size)
        tail = fh.read(tail_size)
        eocd_index = tail.rfind(_EOCD_SIGNATURE)
        if eocd_index == -1:
            raise zipfile.BadZipFile(f"无法在 {zip_path} 中找到 EOCD。")

        _, disk_no, central_dir_disk_no, _, total_entries, central_dir_size, central_dir_offset, _ = struct.unpack_from(
            "<4s4H2LH",
            tail,
            eocd_index,
        )
        if disk_no or central_dir_disk_no:
            raise zipfile.BadZipFile(f"不支持分卷 ZIP: {zip_path}")

        fh.seek(central_dir_offset)
        remaining = central_dir_size
        raw_names = []

        while remaining > 0:
            header = fh.read(46)
            if len(header) != 46 or not header.startswith(_CENTRAL_DIR_SIGNATURE):
                raise zipfile.BadZipFile(f"{zip_path} 的中央目录损坏。")

            fields = struct.unpack("<4s6H3L5H2L", header)
            filename_len = fields[10]
            extra_len = fields[11]
            comment_len = fields[12]

            raw_names.append(fh.read(filename_len))
            fh.seek(extra_len + comment_len, os.SEEK_CUR)
            remaining -= 46 + filename_len + extra_len + comment_len

        if len(raw_names) != total_entries:
            raise zipfile.BadZipFile(
                f"{zip_path} 的中央目录条目数不一致: 期望 {total_entries}，实际 {len(raw_names)}。"
            )

        return raw_names


def _repair_embedded_separator_bytes(raw_name):
    """修复被错误写成 '/' 的多字节编码尾字节。"""
    if raw_name.count(b"/") <= 1:
        return raw_name

    is_dir = raw_name.endswith(b"/")
    body = raw_name[:-1] if is_dir else raw_name
    head, tail = body.split(b"/", 1)
    repaired = head + b"/" + tail.replace(b"/", b"\\")
    return repaired + (b"/" if is_dir else b"")


def _score_decoded_zip_name(decoded_name, encoding, repaired):
    kana_count = len(_KANA_RE.findall(decoded_name))
    cjk_count = len(_CJK_RE.findall(decoded_name))
    suspicious_count = len(_SUSPICIOUS_CHAR_RE.findall(decoded_name))

    score = 0
    score += kana_count * 20
    score += cjk_count * 2
    score -= suspicious_count * 25
    score -= decoded_name.count("\\") * 100
    score -= max(decoded_name.count("/") - 1, 0) * 100
    score -= decoded_name.count("\ufffd") * 100
    score -= sum(1 for ch in decoded_name if ord(ch) < 32) * 100

    if decoded_name.isascii():
        score += 10
    if encoding == "utf-8":
        score += 8
    elif encoding in {"cp932", "gbk"}:
        score += 4
    if repaired:
        score -= 3

    return score


def _decode_raw_zip_name(raw_name, zip_name=None):
    """从原始文件名字节中猜测正确编码并返回解码后的路径。"""
    repaired_name = _repair_embedded_separator_bytes(raw_name)
    forced_encoding = None
    if zip_name:
        forced_encoding = _ZIP_NAME_ENCODING_OVERRIDES.get(os.path.basename(zip_name).lower())

    if forced_encoding:
        candidate_bytes = repaired_name if repaired_name != raw_name else raw_name
        try:
            decoded_name = candidate_bytes.decode(forced_encoding)
            return decoded_name, forced_encoding, candidate_bytes != raw_name
        except UnicodeDecodeError:
            log.warning(
                f"{zip_name} 的强制编码 {forced_encoding} 失败，回退到自动检测: {raw_name!r}"
            )

    candidates = []

    for encoding_index, encoding in enumerate(_CANDIDATE_ENCODINGS):
        for repaired, candidate_bytes in ((False, raw_name), (True, repaired_name)):
            if repaired and candidate_bytes == raw_name:
                continue

            try:
                decoded_name = candidate_bytes.decode(encoding)
            except UnicodeDecodeError:
                continue

            score = _score_decoded_zip_name(decoded_name, encoding, repaired)
            # 分数优先，其次偏向前面的编码和未修复路径
            candidates.append((score, -encoding_index, int(not repaired), decoded_name, encoding, repaired))

    if not candidates:
        fallback = raw_name.decode("cp437", errors="replace")
        log.warning(f"无法可靠解码 ZIP 条目文件名，回退为 cp437: {fallback!r}")
        return fallback, "cp437", False

    _, _, _, decoded_name, encoding, repaired = max(candidates)
    return decoded_name, encoding, repaired


def _normalize_member_path(decoded_name):
    is_dir = decoded_name.endswith("/")
    normalized = decoded_name[:-1] if is_dir else decoded_name

    if not normalized:
        raise ValueError("空 ZIP 路径。")

    path = PurePosixPath(normalized)
    if path.is_absolute():
        raise ValueError(f"拒绝绝对路径: {decoded_name}")

    parts = path.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"拒绝非法路径: {decoded_name}")
    if any("\\" in part for part in parts):
        raise ValueError(f"拒绝包含反斜杠的路径: {decoded_name}")

    return os.path.join(*parts), is_dir


def install_rtp_files(target_game_dir, selected_rtp_zips):
    """
    解压选定的 RTP zip 文件并将内容安装到游戏目录。

    Args:
        target_game_dir (str): 目标游戏目录路径。
        selected_rtp_zips (list): 包含选定 RTP 文件名 (如 "2000.zip") 的列表。

    Returns:
        bool: 操作是否整体成功完成（所有选定RTP都处理完毕，即使部分文件跳过）。
               返回 False 如果遇到解压错误或主要复制错误。
    """
    if not os.path.isdir(RTP_COLLECTION_DIR):
        log.error(f"RTP 集合目录未找到: {RTP_COLLECTION_DIR}")
        return False
    if not os.path.isdir(target_game_dir):
        log.error(f"目标游戏目录不存在: {target_game_dir}")
        return False
    if not selected_rtp_zips:
        log.warning("未选择任何 RTP 文件进行安装。")
        return True

    overall_success = True
    log.info(f"开始安装 RTP 文件到 {target_game_dir}...")

    for rtp_zip_name in selected_rtp_zips:
        rtp_zip_path = os.path.join(RTP_COLLECTION_DIR, rtp_zip_name)

        if not os.path.exists(rtp_zip_path):
            log.error(f"找不到 RTP 文件: {rtp_zip_path}")
            overall_success = False
            continue

        log.info(f"正在处理 RTP 文件: {rtp_zip_name}")
        rtp_copied = 0
        rtp_skipped = 0
        encoding_stats = {}
        repaired_entries = 0

        try:
            raw_names = _read_raw_zip_filenames(rtp_zip_path)

            with zipfile.ZipFile(rtp_zip_path, "r") as zip_ref:
                zip_infos = zip_ref.infolist()
                if len(zip_infos) != len(raw_names):
                    raise zipfile.BadZipFile(
                        f"{rtp_zip_name} 的 ZipInfo 数量与原始目录条目数量不一致。"
                    )

                for info, raw_name in zip(zip_infos, raw_names):
                    decoded_name, encoding_used, repaired = _decode_raw_zip_name(raw_name, rtp_zip_name)
                    encoding_stats[encoding_used] = encoding_stats.get(encoding_used, 0) + 1
                    if repaired:
                        repaired_entries += 1

                    try:
                        relative_path, is_dir = _normalize_member_path(decoded_name)
                    except ValueError as path_err:
                        log.warning(f"跳过非法 RTP 路径 {decoded_name!r}: {path_err}")
                        continue

                    destination_path = os.path.join(target_game_dir, relative_path)

                    if is_dir or info.is_dir():
                        file_system.ensure_dir_exists(destination_path)
                        continue

                    parent_dir = os.path.dirname(destination_path)
                    if parent_dir and not file_system.ensure_dir_exists(parent_dir):
                        log.warning(f"创建 RTP 目标目录失败（但继续）: {parent_dir}")
                        continue

                    if os.path.exists(destination_path):
                        rtp_skipped += 1
                        continue

                    try:
                        with zip_ref.open(info, "r") as src, open(destination_path, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        rtp_copied += 1
                    except Exception as copy_err:
                        log.warning(
                            f"复制 RTP 文件失败（但继续）: {decoded_name!r} -> {destination_path} - {copy_err}"
                        )

            if encoding_stats:
                stats_text = ", ".join(f"{encoding}={count}" for encoding, count in sorted(encoding_stats.items()))
                log.debug(f"{rtp_zip_name} 文件名解码统计: {stats_text}; 修复条目 {repaired_entries} 个。")

            log.info(f"{rtp_zip_name} 处理完成: 复制 {rtp_copied} 个新文件，跳过 {rtp_skipped} 个已存在文件。")

        except zipfile.BadZipFile:
            log.error(f"解压 RTP 文件失败: {rtp_zip_path} 不是有效的 ZIP 文件。")
            overall_success = False
        except Exception as e:
            log.exception(f"处理 RTP 文件 {rtp_zip_name} 时发生意外错误: {e}")
            overall_success = False

    log.info("所有选定的 RTP 文件处理完毕。")
    return overall_success
