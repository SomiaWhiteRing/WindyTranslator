"""隐藏兼容路径使用的字体覆盖检查，不是公开功能入口。"""

import ctypes
import functools
import os
import struct
from ctypes import wintypes


class FontCoverageError(ValueError):
    pass


def _u16(data, offset):
    if offset < 0 or offset + 2 > len(data):
        raise FontCoverageError("TTF 数据被截断")
    return struct.unpack_from(">H", data, offset)[0]


def _i16(data, offset):
    if offset < 0 or offset + 2 > len(data):
        raise FontCoverageError("TTF 数据被截断")
    return struct.unpack_from(">h", data, offset)[0]


def _u32(data, offset):
    if offset < 0 or offset + 4 > len(data):
        raise FontCoverageError("TTF 数据被截断")
    return struct.unpack_from(">I", data, offset)[0]


def _font_offsets(data):
    if data[:4] != b"ttcf":
        return (0,)
    if len(data) < 12:
        raise FontCoverageError("TTC 文件头被截断")
    count = _u32(data, 8)
    if count < 1 or 12 + count * 4 > len(data):
        raise FontCoverageError("TTC 字体目录无效")
    return tuple(_u32(data, 12 + index * 4) for index in range(count))


def _table(data, tag, font_offset=0):
    if font_offset < 0 or font_offset + 12 > len(data):
        raise FontCoverageError("无效的 TTF 文件头")
    table_count = _u16(data, font_offset + 4)
    for index in range(table_count):
        record = font_offset + 12 + index * 16
        if record + 16 > len(data):
            raise FontCoverageError("TTF 表目录被截断")
        if data[record:record + 4] == tag:
            offset = _u32(data, record + 8)
            length = _u32(data, record + 12)
            if offset + length > len(data):
                raise FontCoverageError("TTF 表范围无效")
            return offset, length
    raise FontCoverageError(f"TTF 缺少 {tag.decode('ascii')} 表")


def _format_12_codepoints(data, offset, limit):
    length = _u32(data, offset + 4)
    if offset + length > limit:
        raise FontCoverageError("TTF cmap format 12 范围无效")
    groups = _u32(data, offset + 12)
    result = set()
    for index in range(groups):
        group = offset + 16 + index * 12
        start = _u32(data, group)
        end = _u32(data, group + 4)
        glyph = _u32(data, group + 8)
        if end < start or end > 0x10FFFF:
            raise FontCoverageError("TTF cmap format 12 分组无效")
        if glyph:
            result.update(range(start, end + 1))
        elif end > start:
            result.update(range(start + 1, end + 1))
    return result


def _format_4_codepoints(data, offset, limit):
    length = _u16(data, offset + 2)
    if offset + length > limit:
        raise FontCoverageError("TTF cmap format 4 范围无效")
    segment_count = _u16(data, offset + 6) // 2
    end_codes = offset + 14
    start_codes = end_codes + segment_count * 2 + 2
    deltas = start_codes + segment_count * 2
    range_offsets = deltas + segment_count * 2
    result = set()
    for index in range(segment_count):
        start = _u16(data, start_codes + index * 2)
        end = _u16(data, end_codes + index * 2)
        if end < start:
            raise FontCoverageError("TTF cmap format 4 分段无效")
        delta = _i16(data, deltas + index * 2)
        range_offset = _u16(data, range_offsets + index * 2)
        for codepoint in range(start, min(end, 0xFFFE) + 1):
            if range_offset == 0:
                glyph = (codepoint + delta) & 0xFFFF
            else:
                glyph_offset = range_offsets + index * 2 + range_offset + (codepoint - start) * 2
                if glyph_offset + 2 > offset + length:
                    raise FontCoverageError("TTF cmap format 4 字形索引越界")
                glyph = _u16(data, glyph_offset)
                if glyph:
                    glyph = (glyph + delta) & 0xFFFF
            if glyph:
                result.add(codepoint)
    return result


def _font_codepoints(data, font_offset):
    cmap_offset, cmap_length = _table(data, b"cmap", font_offset)
    limit = cmap_offset + cmap_length
    record_count = _u16(data, cmap_offset + 2)
    subtables = []
    for index in range(record_count):
        record = cmap_offset + 4 + index * 8
        subtable = cmap_offset + _u32(data, record + 4)
        if subtable + 2 > limit:
            raise FontCoverageError("TTF cmap 子表范围无效")
        subtables.append((_u16(data, subtable), subtable))

    result = set()
    for format_id, subtable in sorted(subtables, key=lambda item: item[0] != 12):
        if format_id == 12:
            result.update(_format_12_codepoints(data, subtable, limit))
        elif format_id == 4:
            result.update(_format_4_codepoints(data, subtable, limit))
    return result


def _decode_name(platform_id, encoding_id, raw):
    try:
        if platform_id in (0, 3):
            return raw.decode("utf-16-be").strip("\0 ")
        if platform_id == 1:
            return raw.decode("mac_roman").strip("\0 ")
    except UnicodeDecodeError:
        return ""
    return ""


def _font_families(data, font_offset):
    table_offset, table_length = _table(data, b"name", font_offset)
    if table_length < 6:
        raise FontCoverageError("字体 name 表被截断")
    count = _u16(data, table_offset + 2)
    strings = table_offset + _u16(data, table_offset + 4)
    if table_offset + 6 + count * 12 > table_offset + table_length:
        raise FontCoverageError("字体 name 记录范围无效")
    names = {1: set(), 16: set()}
    for index in range(count):
        record = table_offset + 6 + index * 12
        platform_id = _u16(data, record)
        encoding_id = _u16(data, record + 2)
        name_id = _u16(data, record + 6)
        if name_id not in names:
            continue
        length = _u16(data, record + 8)
        offset = strings + _u16(data, record + 10)
        if offset < strings or offset + length > table_offset + table_length:
            continue
        name = _decode_name(platform_id, encoding_id, data[offset:offset + length])
        if name:
            names[name_id].add(name)
    return names[16] | names[1]


@functools.lru_cache(maxsize=1024)
def _font_catalog_info_cached(path, size, modified_ns):
    del size, modified_ns
    with open(path, "rb") as source:
        data = source.read()
    families = set()
    family_groups = []
    embedding_flags = set()
    for font_offset in _font_offsets(data):
        face_families = _font_families(data, font_offset)
        families.update(face_families)
        family_groups.append(tuple(sorted(face_families, key=str.casefold)))
        try:
            os2_offset, os2_length = _table(data, b"OS/2", font_offset)
            if os2_length >= 10:
                embedding_flags.add(_u16(data, os2_offset + 8))
        except FontCoverageError:
            pass
    if not families:
        raise FontCoverageError("字体没有可用的字体族名称")
    return {
        "families": tuple(sorted(families, key=str.casefold)),
        "family_groups": tuple(family_groups),
        "embedding_flags": tuple(sorted(embedding_flags)),
    }


def font_catalog_info(path):
    path = os.path.abspath(path)
    stat = os.stat(path)
    return _font_catalog_info_cached(path, stat.st_size, stat.st_mtime_ns)


@functools.lru_cache(maxsize=128)
def _font_codepoints_cached(path, size, modified_ns):
    del size, modified_ns
    with open(path, "rb") as source:
        data = source.read()
    codepoints = set()
    for font_offset in _font_offsets(data):
        codepoints.update(_font_codepoints(data, font_offset))
    if not codepoints:
        raise FontCoverageError("字体没有受支持的 Unicode cmap")
    return frozenset(codepoints)


def font_file_info(path):
    path = os.path.abspath(path)
    stat = os.stat(path)
    result = dict(_font_catalog_info_cached(path, stat.st_size, stat.st_mtime_ns))
    result["codepoints"] = _font_codepoints_cached(path, stat.st_size, stat.st_mtime_ns)
    return result


def supported_codepoints(path):
    return set(font_file_info(path)["codepoints"])


def font_families(path):
    return font_catalog_info(path)["families"]


def missing_characters(path, characters):
    coverage = supported_codepoints(path)
    return {character for character in characters if ord(character) not in coverage}


def missing_characters_in_files(paths, characters):
    coverage = set()
    for path in paths:
        coverage.update(font_file_info(path)["codepoints"])
    return {character for character in characters if ord(character) not in coverage}


def _font_paths(root, recursive=True):
    if not root or not os.path.isdir(root):
        return []
    result = []
    walker = os.walk(root) if recursive else [(root, [], os.listdir(root))]
    for current, _, files in walker:
        for filename in files:
            if os.path.splitext(filename)[1].lower() in (".ttf", ".otf", ".ttc"):
                result.append(os.path.join(current, filename))
    return result


def _system_font_paths():
    if os.name != "nt":
        return []
    import winreg

    paths = set()
    font_dirs = {
        os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "Fonts"),
    }
    keys = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
    )
    for hive, key_name in keys:
        try:
            with winreg.OpenKey(hive, key_name) as key:
                for index in range(winreg.QueryInfoKey(key)[1]):
                    _name, value, _kind = winreg.EnumValue(key, index)
                    if not isinstance(value, str):
                        continue
                    if os.path.isabs(value) and os.path.isfile(value):
                        paths.add(os.path.abspath(value))
                        continue
                    for directory in font_dirs:
                        candidate = os.path.join(directory, value)
                        if os.path.isfile(candidate):
                            paths.add(os.path.abspath(candidate))
                            break
        except OSError:
            continue
    return sorted(paths, key=str.casefold)


def discover_font_candidates(module_root=None, game_path=None, include_system=True):
    sources = []
    if module_root:
        sources.append(("module", module_root, _font_paths(module_root)))
    if game_path:
        game_paths = _font_paths(game_path, recursive=False)
        game_paths += _font_paths(os.path.join(game_path, "Data"), recursive=False)
        sources.append(("game", game_path, game_paths))
    if include_system:
        sources.append(("system", None, _system_font_paths()))

    grouped = {}
    for source, root, paths in sources:
        for path in paths:
            try:
                info = font_catalog_info(path)
            except (OSError, FontCoverageError):
                continue
            aliases_by_family = {}
            for group in info["family_groups"]:
                for family in group:
                    aliases_by_family.setdefault(family, set()).update(group)
            for family in info["families"]:
                key = (source, family.casefold())
                item = grouped.setdefault(key, {
                    "source": source,
                    "family": family,
                    "aliases": set(),
                    "files": [],
                })
                item["aliases"].update(aliases_by_family.get(family, (family,)))
                relative = os.path.relpath(path, root).replace(os.sep, "/") if root else os.path.basename(path)
                item["files"].append({
                    "path": path,
                    "relative": relative,
                    "size": os.path.getsize(path),
                    "modified_ns": os.stat(path).st_mtime_ns,
                })
    result = []
    order = {"module": 0, "game": 1, "system": 2}
    for item in grouped.values():
        item["aliases"] = tuple(sorted(item["aliases"], key=str.casefold))
        item["files"].sort(key=lambda file: file["path"].casefold())
        item["id"] = f"{item['source']}:{item['family']}"
        result.append(item)
    result.sort(key=lambda item: (order[item["source"]], item["family"].casefold()))
    return result


def visible_font_candidates(candidates, required_characters, system_missing_limit=10):
    result = []
    seen_families = set()
    missing_by_files = {}
    for candidate in candidates:
        family_key = candidate["family"].casefold()
        if family_key in seen_families:
            continue
        seen_families.add(family_key)
        if candidate["source"] == "system":
            paths = tuple(item["path"] for item in candidate["files"])
            try:
                if paths not in missing_by_files:
                    missing_by_files[paths] = missing_characters_in_files(paths, required_characters)
                missing = missing_by_files[paths]
            except (OSError, FontCoverageError):
                continue
            if len(missing) >= system_missing_limit:
                continue
        result.append(candidate)
    return result


def resolve_available_font_family(candidate, available_families):
    available = {family.casefold(): family for family in available_families}
    for family in (candidate["family"], *candidate.get("aliases", ())):
        if family.casefold() in available:
            return available[family.casefold()]
    return candidate["family"]


def font_catalog_fingerprint(module_root=None, game_path=None, include_system=True):
    paths = _font_paths(module_root) + _font_paths(game_path, recursive=False)
    paths += _font_paths(os.path.join(game_path, "Data"), recursive=False) if game_path else []
    if include_system and os.name == "nt":
        paths += _system_font_paths()
    return tuple(sorted(
        (os.path.normcase(path), os.path.getsize(path), os.stat(path).st_mtime_ns)
        for path in set(paths)
        if os.path.isfile(path)
    ))


def register_private_font(path):
    if os.name != "nt":
        return False
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    gdi32.AddFontResourceExW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID]
    gdi32.AddFontResourceExW.restype = ctypes.c_int
    return bool(gdi32.AddFontResourceExW(os.path.abspath(path), 0x10, None))


def unregister_private_font(path):
    if os.name != "nt":
        return False
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    gdi32.RemoveFontResourceExW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID]
    gdi32.RemoveFontResourceExW.restype = wintypes.BOOL
    return bool(gdi32.RemoveFontResourceExW(os.path.abspath(path), 0x10, None))
