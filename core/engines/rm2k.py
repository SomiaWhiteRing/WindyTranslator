"""
RPG Maker 2000/2003 (RM2k/RM2k3) map name support.

Exports and re-imports map names from the LCF binary file RPG_RT.lmt.
Map names are shown in teleport skills, so translating them lets players
know which locations they can travel to.

Export writes  StringScripts/RM2K_MapNames.txt  (UTF-8).
Import reads that file and patches RPG_RT.lmt in-place (with .bak backup).
"""

import logging
import os
import shutil
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

STRING_SCRIPTS_DIRNAME = "StringScripts"
RM2K_MAPNAMES_FILENAME = "RM2K_MapNames.txt"

FIELD_NAME = 0x01

_ENTRY_SEP = "*" * 5
_ID_SEP    = "-" * 5
SEC_MAP_NAMES = "RM2K_MapNames"

_CODE_TO_CODEC: Dict[str, str] = {
    "932":   "cp932",
    "936":   "gbk",
    "950":   "big5",
    "1252":  "cp1252",
    "1251":  "cp1251",
    "1250":  "cp1250",
    "65001": "utf-8",
    "0":     "cp932",
}

_DETECT_CANDIDATES = ["cp932", "gbk", "big5", "cp1252", "utf-8"]


def _codec(code: str) -> str:
    return _CODE_TO_CODEC.get(str(code), "cp932")


def auto_detect_encoding(game_path: str) -> str:
    """Sniff the source encoding of RPG_RT.lmt by scoring map name bytes.

    Tries candidate codecs and picks the one with the fewest replacement
    characters.  Falls back to cp932 if nothing conclusive is found.
    """
    lmt_path = os.path.join(game_path, "RPG_RT.lmt")
    if not os.path.isfile(lmt_path):
        return "cp932"

    try:
        _, entries, _ = _read_lmt(open(lmt_path, "rb").read())
        sample: List[bytes] = []
        for eid, fields in entries:
            if eid == 0:
                continue
            nb = fields.get(FIELD_NAME, b"")
            if nb:
                sample.append(nb)

        if not sample:
            return "cp932"

        combined = b" ".join(sample)
        best_enc, best_score = "cp932", -1
        for enc in _DETECT_CANDIDATES:
            try:
                decoded = combined.decode(enc, errors="replace")
                score = decoded.count("�")
                if score < best_score or best_score == -1:
                    best_score = score
                    best_enc = enc
            except (LookupError, UnicodeDecodeError):
                continue

        log.info(f"[RM2K] 自動偵測編碼: {best_enc} (替換字元數: {best_score})")
        return best_enc

    except Exception as e:
        log.warning(f"[RM2K] 編碼偵測失敗，使用預設 cp932: {e}")
        return "cp932"


# ── BER codec ────────────────────────────────────────────────────────────────

def _read_ber(data: bytes, pos: int) -> Tuple[int, int]:
    value = 0
    while pos < len(data):
        b = data[pos]; pos += 1
        value = (value << 7) | (b & 0x7F)
        if not (b & 0x80):
            break
    return value, pos


def _write_ber(n: int) -> bytes:
    if n == 0:
        return b"\x00"
    parts: List[int] = []
    while n:
        parts.append(n & 0x7F)
        n >>= 7
    parts.reverse()
    for i in range(len(parts) - 1):
        parts[i] |= 0x80
    return bytes(parts)


# ── Generic flat-array parser/serialiser ─────────────────────────────────────

def _parse_array_end(data: bytes, pos: int = 0) -> Tuple[List[Tuple[int, Dict[int, bytes]]], int]:
    count, pos = _read_ber(data, pos)
    entries: List[Tuple[int, Dict[int, bytes]]] = []
    for _ in range(count):
        if pos >= len(data):
            break
        eid, pos = _read_ber(data, pos)
        fields: Dict[int, bytes] = {}
        while pos < len(data):
            fk, pos = _read_ber(data, pos)
            if fk == 0:
                break
            fs, pos = _read_ber(data, pos)
            fields[fk] = data[pos: pos + fs]
            pos += fs
        entries.append((eid, fields))
    return entries, pos


def _serialise_array(entries: List[Tuple[int, Dict[int, bytes]]]) -> bytes:
    out = bytearray(_write_ber(len(entries)))
    for eid, fields in entries:
        out += _write_ber(eid)
        for fk in sorted(fields.keys()):
            fdata = fields[fk]
            out += _write_ber(fk)
            out += _write_ber(len(fdata))
            out += fdata
        out += b"\x00"
    return bytes(out)


# ── LMT helpers ──────────────────────────────────────────────────────────────

def _read_lmt(data: bytes) -> Tuple[bytes, List[Tuple[int, Dict[int, bytes]]], bytes]:
    """Return (magic_bytes, node_entries, tail_bytes).

    tail_bytes holds everything after the node array (starting location,
    active_node, vehicle positions, etc.) and must be written back verbatim.
    """
    ml, pos = _read_ber(data, 0)
    magic = data[pos: pos + ml]
    pos += ml
    entries, end_pos = _parse_array_end(data, pos)
    tail = data[end_pos:]
    return magic, entries, tail


def _write_lmt(magic: bytes, entries: List[Tuple[int, Dict[int, bytes]]],
               tail: bytes = b"") -> bytes:
    out = bytearray(_write_ber(len(magic)))
    out += magic
    out += _serialise_array(entries)
    out += tail
    return bytes(out)


# ── StringScripts format ─────────────────────────────────────────────────────

def _write_mapnames_file(names: Dict[int, str]) -> str:
    lines: List[str] = []
    lines.append(f"{_ENTRY_SEP}{SEC_MAP_NAMES}{_ENTRY_SEP}")
    for eid in sorted(names.keys()):
        lines.append(f"{_ID_SEP}{eid}{_ID_SEP}")
        lines.append(names[eid])
    lines.append("")
    return "\n".join(lines)


def _parse_mapnames_file(content: str) -> Dict[int, str]:
    result: Dict[int, str] = {}
    in_section = False
    current_id: Optional[int] = None
    pending_lines: List[str] = []

    def _flush():
        if current_id is not None:
            result[current_id] = "\n".join(pending_lines).rstrip("\n")

    for raw in content.splitlines():
        if raw.startswith(_ENTRY_SEP) and raw.endswith(_ENTRY_SEP) and len(raw) > 10:
            _flush()
            pending_lines.clear()
            current_id = None
            in_section = (raw.strip("*").strip() == SEC_MAP_NAMES)
            continue
        if not in_section:
            continue
        if raw.startswith(_ID_SEP) and raw.endswith(_ID_SEP) and len(raw) > 10:
            _flush()
            pending_lines.clear()
            try:
                current_id = int(raw.strip("-").strip())
            except ValueError:
                current_id = None
            continue
        if current_id is not None:
            pending_lines.append(raw)

    _flush()
    return result


# ── Public API ────────────────────────────────────────────────────────────────

def export_names(game_path: str, encoding_code: str, message_queue=None) -> bool:
    """Export map names from RPG_RT.lmt to StringScripts/RM2K_MapNames.txt."""
    def _log(level: str, msg: str):
        log.info(msg)
        if message_queue:
            message_queue.put(("log", (level, msg)))

    enc = _codec(encoding_code)
    lmt_path = os.path.join(game_path, "RPG_RT.lmt")
    ss_dir   = os.path.join(game_path, STRING_SCRIPTS_DIRNAME)
    os.makedirs(ss_dir, exist_ok=True)

    if not os.path.isfile(lmt_path):
        return True

    try:
        _, lmt_entries, _ = _read_lmt(open(lmt_path, "rb").read())
        names: Dict[int, str] = {}
        for eid, fields in lmt_entries:
            if eid == 0:
                continue
            raw = fields.get(FIELD_NAME, b"")
            if raw:
                names[eid] = raw.decode(enc, errors="replace")
        _log("normal", f"  [RM2K] 地圖名稱: {len(names)} 個")
    except Exception as e:
        _log("warning", f"  [RM2K] 解析 RPG_RT.lmt 失敗: {e}")
        return False

    out_path = os.path.join(ss_dir, RM2K_MAPNAMES_FILENAME)
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(_write_mapnames_file(names))
        _log("success", f"  [RM2K] 已寫入 {RM2K_MAPNAMES_FILENAME}，共 {len(names)} 個地圖名稱")
        return True
    except Exception as e:
        _log("error", f"  [RM2K] 寫入地圖名稱失敗: {e}")
        return False


def import_names(game_path: str, encoding_code: str, message_queue=None) -> bool:
    """Import translated map names from StringScripts/RM2K_MapNames.txt into RPG_RT.lmt."""
    def _log(level: str, msg: str):
        log.info(msg)
        if message_queue:
            message_queue.put(("log", (level, msg)))

    enc        = _codec(encoding_code)
    lmt_path   = os.path.join(game_path, "RPG_RT.lmt")
    names_path = os.path.join(game_path, STRING_SCRIPTS_DIRNAME, RM2K_MAPNAMES_FILENAME)

    if not os.path.isfile(names_path):
        _log("normal", f"  [RM2K] 未找到 {RM2K_MAPNAMES_FILENAME}，跳過地圖名稱導入")
        return True

    try:
        translations = _parse_mapnames_file(
            open(names_path, "r", encoding="utf-8-sig").read()
        )
    except Exception as e:
        _log("error", f"  [RM2K] 讀取地圖名稱文件失敗: {e}")
        return False

    if not translations:
        _log("normal", "  [RM2K] 地圖名稱文件無可導入內容")
        return True

    if not os.path.isfile(lmt_path):
        _log("error", f"  [RM2K] 未找到 RPG_RT.lmt: {lmt_path}")
        return False

    try:
        magic, entries, lmt_tail = _read_lmt(open(lmt_path, "rb").read())
        new_entries = []
        count = 0
        for eid, fields in entries:
            if eid in translations:
                try:
                    nb = translations[eid].encode(enc, errors="strict")
                    fields = dict(fields)
                    fields[FIELD_NAME] = nb
                    count += 1
                except (UnicodeEncodeError, LookupError):
                    _log("warning", f"  [RM2K] 無法將 map {eid} 名稱編碼為 {enc}，保留原文")
            new_entries.append((eid, fields))

        if count == 0:
            _log("normal", "  [RM2K] LMT: 無符合的地圖名稱需要更新")
            return True

        bak = lmt_path + ".bak"
        if not os.path.exists(bak):
            shutil.copy2(lmt_path, bak)
            _log("normal", "  [RM2K] 已備份 RPG_RT.lmt → .bak")

        with open(lmt_path, "wb") as f:
            f.write(_write_lmt(magic, new_entries, lmt_tail))
        _log("success", f"  [RM2K] RPG_RT.lmt: 已更新 {count} 個地圖名稱")
        return True

    except Exception as e:
        _log("error", f"  [RM2K] 更新 RPG_RT.lmt 失敗: {e}")
        return False
