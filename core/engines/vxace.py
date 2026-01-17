"""
RPG Maker VX Ace (RGSS3) support.

This module builds a StringScripts directory from `.rvdata2` files and imports
translated StringScripts back into game data, without requiring the editor.
"""

import base64
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from core.utils import file_system

log = logging.getLogger(__name__)


STRING_SCRIPTS_DIRNAME = "StringScripts"
STRING_SCRIPTS_ORIGIN_DIRNAME = "StringScripts_Origin"
ORIGINAL_DB_STORE_FILENAME = "RTA_VXACE_ORIGINAL_DB.json"

MESSAGE_MARKER_PREFIX = "<ORIGINAL_TEXT:"
MESSAGE_MARKER_SUFFIX = ">"

# Choice markers are not provided by the original Ruby script; we add our own.
CHOICE_MARKER_PREFIX = "<ORIGINAL_CHOICE:"
CHOICE_MARKER_SUFFIX = ">"

_VXACE_VOCAB_MARKERS: Dict[str, Dict[int, str]] = {
    # RPG::System::Terms.basic (RGSS3): level, level_a, hp, hp_a, mp, mp_a, tp, tp_a
    "basic": {
        0: "Level",
        1: "LevelShort",
        2: "HP",
        3: "HPShort",
        4: "MP",
        5: "MPShort",
        6: "TP",
        7: "TPShort",
    },
    # RPG::System::Terms.params (RGSS3): mhp, mmp, atk, def, mat, mdf, agi, luk
    # Use marker names that match the RM200x vocab style where possible.
    "params": {
        0: "MaxHP",
        1: "MaxMP",
        2: "Offense",
        3: "Defense",
        4: "Mind",
        5: "MagicDefense",
        6: "Agility",
        7: "Luck",
    },
    # RPG::System::Terms.etypes (RGSS3): weapon, shield, head, body, accessory
    "etypes": {
        0: "Arms",
        1: "Shield",
        2: "Helmet",
        3: "Armor",
        4: "Other",
    },
    # RPG::System::Terms.commands (RGSS3): a mixed list (battle/menu/item categories/etc).
    # Note: some games keep placeholder empty strings; we skip them on export.
    "commands": {
        0: "Fight",
        1: "Escape",
        2: "Attack",
        3: "Defend",
        4: "Item",
        5: "Skill",
        6: "Equip",
        7: "Status",
        8: "Formation",
        9: "Save",
        10: "EndGame",
        12: "WeaponCategory",
        13: "ArmorCategory",
        14: "KeyItem",
        15: "Equip2",
        16: "Optimize",
        17: "Clear",
        18: "NewGame",
        19: "Continue",
        20: "Quit",
        21: "ToTitle",
        22: "Cancel",
    },
}


class VXAceError(RuntimeError):
    pass


def _import_rubymarshal():
    try:
        from rubymarshal import reader, writer
        from rubymarshal.classes import RubyObject, RubyString
    except Exception as e:  # pragma: no cover
        raise VXAceError("缺少依赖：rubymarshal。请先安装 requirements.txt 中的依赖。") from e
    return reader, writer, RubyObject, RubyString


def _load_rvdata2(path: str) -> Any:
    reader, _, _, _ = _import_rubymarshal()
    with open(path, "rb") as f:
        return reader.load(f)


def _save_rvdata2(path: str, obj: Any) -> None:
    _, writer, _, _ = _import_rubymarshal()
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "wb") as f:
        writer.write(f, obj)
    os.replace(tmp_path, path)


def _ivar(name: str) -> str:
    return name if name.startswith("@") else f"@{name}"


def _get_attr(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    attrs = getattr(obj, "attributes", None)
    if not isinstance(attrs, dict):
        return default
    return attrs.get(_ivar(name), default)


def _set_attr(obj: Any, name: str, value: Any) -> None:
    attrs = getattr(obj, "attributes", None)
    if not isinstance(attrs, dict):
        raise VXAceError(f"对象不支持 attributes: {type(obj)}")
    attrs[_ivar(name)] = value


def _as_str(val: Any) -> str:
    _, _, _, RubyString = _import_rubymarshal()
    if isinstance(val, RubyString):
        return str(val.text)
    if val is None:
        return ""
    return str(val)


def _str_like(val: str, template: Any) -> Any:
    """
    If the original value is a RubyString, preserve its attributes when replacing.
    Otherwise return a plain Python str.
    """
    _, _, _, RubyString = _import_rubymarshal()
    if isinstance(template, RubyString):
        return RubyString(val, attributes=dict(getattr(template, "attributes", {}) or {}))
    return val


def _event_command_fields(cmd: Any) -> Tuple[int, int, List[Any]]:
    code = _get_attr(cmd, "code", 0)
    indent = _get_attr(cmd, "indent", 0)
    params = _get_attr(cmd, "parameters", [])
    try:
        code_int = int(code)
    except Exception:
        code_int = 0
    try:
        indent_int = int(indent)
    except Exception:
        indent_int = 0
    if not isinstance(params, list):
        params = []
    return code_int, indent_int, params


def _new_event_command(code: int, indent: int, parameters: List[Any]) -> Any:
    _, _, RubyObject, _ = _import_rubymarshal()
    return RubyObject(
        "RPG::EventCommand",
        attributes={
            "@code": int(code),
            "@indent": int(indent),
            "@parameters": parameters,
        },
    )


def _encode_message_marker(original_text: str) -> str:
    encoded = base64.b64encode(original_text.encode("utf-8")).decode("ascii")
    return f"{MESSAGE_MARKER_PREFIX}{encoded}{MESSAGE_MARKER_SUFFIX}"


def _decode_message_marker(comment: str) -> Optional[str]:
    if not isinstance(comment, str):
        return None
    if not (comment.startswith(MESSAGE_MARKER_PREFIX) and comment.endswith(MESSAGE_MARKER_SUFFIX)):
        return None
    payload = comment[len(MESSAGE_MARKER_PREFIX) : -len(MESSAGE_MARKER_SUFFIX)]
    try:
        return base64.b64decode(payload).decode("utf-8", errors="strict")
    except Exception:
        return None


def _encode_choice_marker(original_choices: List[str]) -> str:
    raw = json.dumps(original_choices, ensure_ascii=False, separators=(",", ":"))
    encoded = base64.b64encode(raw.encode("utf-8")).decode("ascii")
    return f"{CHOICE_MARKER_PREFIX}{encoded}{CHOICE_MARKER_SUFFIX}"


def _decode_choice_marker(comment: str) -> Optional[List[str]]:
    if not isinstance(comment, str):
        return None
    if not (comment.startswith(CHOICE_MARKER_PREFIX) and comment.endswith(CHOICE_MARKER_SUFFIX)):
        return None
    payload = comment[len(CHOICE_MARKER_PREFIX) : -len(CHOICE_MARKER_SUFFIX)]
    try:
        raw = base64.b64decode(payload).decode("utf-8", errors="strict")
        parsed = json.loads(raw)
        if isinstance(parsed, list) and all(isinstance(x, str) for x in parsed):
            return parsed
    except Exception:
        return None
    return None


def _normalize_newlines(text: str) -> str:
    return (text or "").replace("\r\n", "\n").replace("\r", "\n")


def _escape_inline_newlines(text: str) -> str:
    """
    StringScripts single-line entries cannot contain real newlines.
    We encode them as literal '\\n' so step 3/6 can stay unchanged.
    """
    return _normalize_newlines(text).replace("\n", "\\n")


def _unescape_inline_newlines(text: str) -> str:
    return (text or "").replace("\\n", "\n")


def _write_text_file(path: str, lines: List[str]) -> None:
    file_system.ensure_dir_exists(os.path.dirname(path))
    with open(path, "w", encoding="utf-8-sig", newline="\n") as f:
        f.writelines(lines)


def _string_scripts_face_line(face_name: str, face_index: int) -> str:
    face_name = _normalize_newlines(face_name).strip()
    if not face_name:
        return "{{ Select Face Graphic: Erase }}\n"
    return f"{{{{ Select Face Graphic: {face_name}, {int(face_index)} }}}}\n"


def _export_command_list_to_lines(cmd_list: Any) -> List[str]:
    if not isinstance(cmd_list, list):
        return []

    lines: List[str] = []
    text_buffer: List[str] = []
    pending_original_message: Optional[str] = None
    pending_original_choices: Optional[List[str]] = None
    skipping_translated_message = False

    def flush_message():
        if not text_buffer:
            return
        message_text = "\n".join(text_buffer)
        message_text = _normalize_newlines(message_text)
        if message_text != "":
            lines.append("#Message#\n")
            for ln in message_text.split("\n"):
                lines.append(f"{ln}\n")
            lines.append("##\n")
        text_buffer.clear()

    for cmd in cmd_list:
        code, _indent, params = _event_command_fields(cmd)

        # End of a (possibly skipped) 401 block
        if skipping_translated_message and code != 401:
            skipping_translated_message = False

        # comment marker storage (inserted by importer)
        if code == 108 and params:
            comment = _as_str(params[0])
            decoded = _decode_message_marker(comment)
            if decoded is not None:
                pending_original_message = decoded
                continue
            decoded_choices = _decode_choice_marker(comment)
            if decoded_choices is not None:
                pending_original_choices = decoded_choices
                continue

        if code == 101:  # Show Text (face/background/position)
            flush_message()
            face_name = _as_str(params[0]) if len(params) > 0 else ""
            try:
                face_index = int(params[1]) if len(params) > 1 else 0
            except Exception:
                face_index = 0
            lines.append(_string_scripts_face_line(face_name, face_index))
            continue

        if code == 401:  # text line
            if skipping_translated_message:
                continue
            if pending_original_message is not None:
                # Export original text (from marker) and skip current in-game text lines.
                message_text = _normalize_newlines(pending_original_message)
                if message_text != "":
                    lines.append("#Message#\n")
                    for ln in message_text.split("\n"):
                        lines.append(f"{ln}\n")
                    lines.append("##\n")
                pending_original_message = None
                skipping_translated_message = True
                continue

            text = _as_str(params[0]) if params else ""
            text_buffer.append(_normalize_newlines(text))
            continue

        if code == 102:  # choices
            flush_message()
            choices_raw = pending_original_choices if pending_original_choices is not None else (params[0] if params else [])
            pending_original_choices = None
            if isinstance(choices_raw, list):
                choices = [_normalize_newlines(_as_str(x)) for x in choices_raw if _as_str(x) != ""]
                if choices:
                    lines.append("#Choice#\n")
                    for ch in choices:
                        lines.append(f"{ch}\n")
                    lines.append("##\n")
            continue

        flush_message()

    flush_message()
    if pending_original_message is not None and not skipping_translated_message:
        message_text = _normalize_newlines(pending_original_message)
        if message_text != "":
            lines.append("#Message#\n")
            for ln in message_text.split("\n"):
                lines.append(f"{ln}\n")
            lines.append("##\n")
    return lines


def _load_json_if_exists(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_json(path: str, data: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _get_original_text(store: Dict[str, Any], key: str, current_value: str) -> Tuple[str, bool]:
    if key in store and isinstance(store[key], str):
        return store[key], False
    store[key] = current_value
    return current_value, True


def export_to_string_scripts(game_path: str, message_queue) -> None:
    data_dir = os.path.join(game_path, "Data")
    map_infos_path = os.path.join(data_dir, "MapInfos.rvdata2")
    if not os.path.isfile(map_infos_path):
        raise VXAceError(f"未找到 VX Ace 数据文件: {map_infos_path}")

    string_scripts_path = os.path.join(game_path, STRING_SCRIPTS_DIRNAME)
    backup_path = os.path.join(game_path, STRING_SCRIPTS_ORIGIN_DIRNAME)

    # Clean old outputs
    if os.path.exists(string_scripts_path):
        file_system.safe_remove(string_scripts_path)
    if os.path.exists(backup_path):
        file_system.safe_remove(backup_path)
    file_system.ensure_dir_exists(string_scripts_path)

    message_queue.put(("log", ("normal", "读取 VX Ace 数据并生成 StringScripts...")))

    original_db_store_path = os.path.join(game_path, ORIGINAL_DB_STORE_FILENAME)
    original_db_store: Dict[str, Any] = _load_json_if_exists(original_db_store_path)
    original_db_store_modified = False

    map_infos = _load_rvdata2(map_infos_path)
    if not isinstance(map_infos, dict):
        raise VXAceError("MapInfos.rvdata2 格式异常：预期为 Hash(MapID=>MapInfo)")

    # --- Export map dialogues ---
    exported_map_files = 0
    for map_id in sorted([k for k in map_infos.keys() if isinstance(k, int) and k > 0]):
        map_path = os.path.join(data_dir, f"Map{map_id:03d}.rvdata2")
        if not os.path.isfile(map_path):
            continue
        try:
            map_obj = _load_rvdata2(map_path)
        except Exception as e:
            log.warning(f"读取地图失败: {map_path} - {e}")
            continue

        events = _get_attr(map_obj, "events", {})
        if not isinstance(events, dict) or not events:
            continue

        out_lines: List[str] = []
        for event_id in sorted([k for k in events.keys() if isinstance(k, int) and k > 0]):
            ev = events.get(event_id)
            if ev is None:
                continue
            pages = _get_attr(ev, "pages", [])
            if not isinstance(pages, list) or not pages:
                continue

            entry_lines: List[str] = [f"*****Entry{event_id}*****\n"]
            for page_idx, page in enumerate(pages):
                entry_lines.append(f"-----Page{page_idx + 1}-----\n")
                cmd_list = _get_attr(page, "list", [])
                entry_lines.extend(_export_command_list_to_lines(cmd_list))

            if any(line.startswith("#") for line in entry_lines):
                out_lines.extend(entry_lines)

        if not any(line.startswith("#") for line in out_lines):
            continue

        out_file = os.path.join(string_scripts_path, f"Map{map_id:03d}.txt")
        _write_text_file(out_file, out_lines)
        exported_map_files += 1

    message_queue.put(("log", ("success", f"地图对话导出完成：{exported_map_files} 个文件。")))

    # --- Export common events dialogues ---
    common_path = os.path.join(data_dir, "CommonEvents.rvdata2")
    if os.path.isfile(common_path):
        try:
            common_events = _load_rvdata2(common_path)
        except Exception as e:
            log.warning(f"读取公共事件失败: {common_path} - {e}")
            common_events = None
        if isinstance(common_events, list):
            out_lines: List[str] = []
            for idx, ce in enumerate(common_events):
                if ce is None:
                    continue
                cmd_list = _get_attr(ce, "list", [])
                if not isinstance(cmd_list, list):
                    continue
                entry_lines = [f"*****Entry{idx}*****\n", "-----Page1-----\n"]
                entry_lines.extend(_export_command_list_to_lines(cmd_list))
                if any(line.startswith("#") for line in entry_lines):
                    out_lines.extend(entry_lines)
            if any(line.startswith("#") for line in out_lines):
                _write_text_file(os.path.join(string_scripts_path, "CommonEvents.txt"), out_lines)
                message_queue.put(("log", ("success", "公共事件对话导出完成：CommonEvents.txt")))

    # --- Export database ---
    original_db_store_modified, db_files = _export_database(
        data_dir=data_dir,
        map_infos=map_infos,
        out_root=os.path.join(string_scripts_path, "Database"),
        original_store=original_db_store,
    )
    message_queue.put(("log", ("success", f"数据库导出完成：{db_files} 个文件。")))

    if original_db_store_modified:
        _save_json(original_db_store_path, original_db_store)

    # Backup StringScripts -> StringScripts_Origin (required by step 6)
    shutil.copytree(string_scripts_path, backup_path)
    message_queue.put(("log", ("success", f"StringScripts 备份完成：{STRING_SCRIPTS_ORIGIN_DIRNAME}")))


def _export_database(
    *,
    data_dir: str,
    map_infos: Dict[int, Any],
    out_root: str,
    original_store: Dict[str, Any],
) -> Tuple[bool, int]:
    """
    Export database-like texts to StringScripts/Database, and persist original texts
    (by ID) into ORIGINAL_DB_STORE_FILENAME so future exports can keep original even
    after importing translations.

    Returns:
        (original_store_modified, exported_file_count)
    """
    file_system.ensure_dir_exists(out_root)

    original_store_modified = False
    exported_files = 0

    def export_array_table_compact(
        folder_name: str,
        rvdata2_name: str,
        fields: List[Tuple[str, str, bool]],
    ) -> int:
        nonlocal original_store_modified
        path = os.path.join(data_dir, rvdata2_name)
        if not os.path.isfile(path):
            return 0
        try:
            data = _load_rvdata2(path)
        except Exception as e:
            log.warning(f"读取数据库失败: {path} - {e}")
            return 0
        if not isinstance(data, list):
            return 0

        out_dir = os.path.join(out_root, folder_name)
        file_system.ensure_dir_exists(out_dir)
        out_file = os.path.join(out_dir, f"{folder_name}.txt")

        out_lines: List[str] = []
        for idx, obj in enumerate(data):
            if idx == 0 or obj is None:
                continue
            entry_lines: List[str] = [f"*****Entry{idx}*****\n"]
            for marker, attr_name, is_multiline in fields:
                current_val_obj = _get_attr(obj, attr_name, "")
                current_val = _normalize_newlines(_as_str(current_val_obj))
                store_key = f"{rvdata2_name}:{idx}:{attr_name}"
                original_val, created = _get_original_text(original_store, store_key, current_val)
                if created:
                    original_store_modified = True
                original_val = _normalize_newlines(original_val)

                if is_multiline:
                    if original_val != "":
                        entry_lines.append(f"#{marker}#\n")
                        entry_lines.append(f"{_escape_inline_newlines(original_val)}\n")
                else:
                    entry_lines.append(f"#{marker}#\n")
                    entry_lines.append(f"{original_val}\n")

            if any(l.startswith("#") for l in entry_lines):
                out_lines.extend(entry_lines)
                out_lines.append("\n")

        if any(l.startswith("#") for l in out_lines):
            _write_text_file(out_file, out_lines)
            return 1
        return 0

    exported_files += export_array_table_compact(
        "Actors",
        "Actors.rvdata2",
        [
            ("Name", "name", False),
            ("Nickname", "nickname", False),
            ("Description", "description", True),
        ],
    )
    exported_files += export_array_table_compact(
        "Classes",
        "Classes.rvdata2",
        [
            ("Name", "name", False),
            ("Description", "description", True),
        ],
    )
    exported_files += export_array_table_compact(
        "Skills",
        "Skills.rvdata2",
        [
            ("Name", "name", False),
            ("Description", "description", True),
            ("Message1", "message1", False),
            ("Message2", "message2", False),
        ],
    )
    exported_files += export_array_table_compact(
        "Items",
        "Items.rvdata2",
        [
            ("Name", "name", False),
            ("Description", "description", True),
        ],
    )
    exported_files += export_array_table_compact(
        "Weapons",
        "Weapons.rvdata2",
        [
            ("Name", "name", False),
            ("Description", "description", True),
        ],
    )
    exported_files += export_array_table_compact(
        "Armors",
        "Armors.rvdata2",
        [
            ("Name", "name", False),
            ("Description", "description", True),
        ],
    )
    exported_files += export_array_table_compact(
        "Enemies",
        "Enemies.rvdata2",
        [
            ("Name", "name", False),
            ("Description", "description", True),
        ],
    )
    exported_files += export_array_table_compact(
        "States",
        "States.rvdata2",
        [
            ("Name", "name", False),
            ("Description", "description", True),
            ("Message1", "message1", False),
            ("Message2", "message2", False),
            ("Message3", "message3", False),
            ("Message4", "message4", False),
        ],
    )

    # MapInfos names (by ID)
    mapinfo_dir = os.path.join(out_root, "MapInfos")
    file_system.ensure_dir_exists(mapinfo_dir)
    mapinfo_lines: List[str] = []
    for map_id in sorted([k for k in map_infos.keys() if isinstance(k, int) and k > 0]):
        info = map_infos.get(map_id)
        if info is None:
            continue
        name_obj = _get_attr(info, "name", "")
        name = _normalize_newlines(_as_str(name_obj))
        store_key = f"MapInfos.rvdata2:{map_id}:name"
        original_name, created = _get_original_text(original_store, store_key, name)
        if created:
            original_store_modified = True
        mapinfo_lines.append(f"*****Entry{map_id}*****\n")
        mapinfo_lines.append("#Name#\n")
        mapinfo_lines.append(f"{_normalize_newlines(original_name)}\n")
        mapinfo_lines.append("\n")
    if any(l.startswith("#") for l in mapinfo_lines):
        _write_text_file(os.path.join(mapinfo_dir, "MapInfos.txt"), mapinfo_lines)
        exported_files += 1

    # System (best-effort)
    system_path = os.path.join(data_dir, "System.rvdata2")
    if os.path.isfile(system_path):
        try:
            system_obj = _load_rvdata2(system_path)
        except Exception as e:
            log.warning(f"读取 System 失败: {system_path} - {e}")
            system_obj = None
        if system_obj is not None:
            system_dir = os.path.join(out_root, "System")
            file_system.ensure_dir_exists(system_dir)

            system_lines: List[str] = []

            def add_system_single(marker: str, attr_name: str) -> None:
                nonlocal original_store_modified
                val_obj = _get_attr(system_obj, attr_name, "")
                val = _normalize_newlines(_as_str(val_obj))
                store_key = f"System.rvdata2:0:{attr_name}"
                original_val, created = _get_original_text(original_store, store_key, val)
                if created:
                    original_store_modified = True
                system_lines.append(f"#{marker}#\n")
                system_lines.append(f"{_escape_inline_newlines(_normalize_newlines(original_val))}\n")
                system_lines.append("\n")

            add_system_single("Name", "game_title")
            if any(l.startswith("#") for l in system_lines):
                _write_text_file(os.path.join(system_dir, "System.txt"), system_lines)
                exported_files += 1

            vocab_lines: List[str] = []
            used_markers: set[str] = set()

            def add_vocab_single(marker: str, store_key: str, value: str) -> None:
                nonlocal original_store_modified
                if marker in used_markers:
                    log.warning(f"Vocab 标记重复，已跳过: {marker}")
                    return
                used_markers.add(marker)
                original_val, created = _get_original_text(original_store, store_key, value)
                if created:
                    original_store_modified = True
                original_val = _normalize_newlines(original_val)
                if original_val == "":
                    return
                vocab_lines.append(f"#{marker}#\n")
                vocab_lines.append(f"{_escape_inline_newlines(original_val)}\n")
                vocab_lines.append("\n")

            # CurrencyUnit lives on System, but it belongs to "Vocab" in the RM200x style.
            currency_obj = _get_attr(system_obj, "currency_unit", "")
            currency = _normalize_newlines(_as_str(currency_obj))
            add_vocab_single("CurrencyUnit", "System.rvdata2:0:currency_unit", currency)

            terms = _get_attr(system_obj, "terms", None)
            if terms is not None:
                for group_attr, idx_to_marker in _VXACE_VOCAB_MARKERS.items():
                    arr = _get_attr(terms, group_attr, None)
                    if not isinstance(arr, list) or not arr:
                        continue
                    for idx, marker in idx_to_marker.items():
                        if idx < 0 or idx >= len(arr):
                            continue
                        term_str = _normalize_newlines(_as_str(arr[idx]))
                        if term_str == "":
                            continue
                        store_key = f"System.rvdata2:terms.{group_attr}:{idx}"
                        add_vocab_single(marker, store_key, term_str)

            if any(l.startswith("#") for l in vocab_lines):
                _write_text_file(os.path.join(out_root, "Vocab.txt"), vocab_lines)
                exported_files += 1

    return original_store_modified, exported_files


# --- Import side ---

_MARKER_RE = re.compile(r"#(.+)#")


@dataclass
class _ParsedEntry:
    marker: str
    # For Message/StringPicture: content is a single string (may include \n).
    # For Choice: content is a list[str].
    content: Any


def _parse_string_scripts_text(text: str) -> List[_ParsedEntry]:
    lines = text.splitlines(keepends=True)
    entries: List[_ParsedEntry] = []
    i = 0
    while i < len(lines):
        m = _MARKER_RE.match(lines[i].strip())
        if not m:
            i += 1
            continue
        marker = m.group(1)
        i += 1

        if marker in ("Message", "StringPicture"):
            buf: List[str] = []
            while i < len(lines) and lines[i].strip() != "##":
                buf.append(lines[i].rstrip("\n"))
                i += 1
            if i < len(lines) and lines[i].strip() == "##":
                i += 1
            entries.append(_ParsedEntry(marker=marker, content=_normalize_newlines("\n".join(buf)).rstrip("\n")))
            continue

        if marker == "Choice":
            buf2: List[str] = []
            while i < len(lines) and lines[i].strip() != "##":
                buf2.append(lines[i].rstrip("\n"))
                i += 1
            if i < len(lines) and lines[i].strip() == "##":
                i += 1
            entries.append(_ParsedEntry(marker=marker, content=[_normalize_newlines(x).strip() for x in buf2]))
            continue

        # default: single-line entry
        if i < len(lines):
            entries.append(_ParsedEntry(marker=marker, content=_normalize_newlines(lines[i].rstrip("\n"))))
            i += 1
        else:
            entries.append(_ParsedEntry(marker=marker, content=""))
    return entries


def _build_translation_map(origin_text: str, translated_text: str) -> Dict[str, str]:
    origin_entries = _parse_string_scripts_text(origin_text)
    translated_entries = _parse_string_scripts_text(translated_text)
    mapping: Dict[str, str] = {}
    for idx in range(min(len(origin_entries), len(translated_entries))):
        o = origin_entries[idx]
        t = translated_entries[idx]
        if o.marker != t.marker:
            continue
        if o.marker in ("Message", "StringPicture"):
            if isinstance(o.content, str) and isinstance(t.content, str):
                mapping[o.content] = t.content
        elif o.marker == "Choice":
            if isinstance(o.content, list) and isinstance(t.content, list):
                for j in range(min(len(o.content), len(t.content))):
                    mapping[o.content[j]] = t.content[j]
        else:
            if isinstance(o.content, str) and isinstance(t.content, str):
                mapping[o.content] = t.content
    return mapping


def _is_comment_marker(cmd: Any, kind: str) -> Tuple[bool, Optional[Any]]:
    code, _indent, params = _event_command_fields(cmd)
    if code != 108 or not params:
        return False, None
    comment = _as_str(params[0])
    if kind == "message":
        decoded = _decode_message_marker(comment)
        return decoded is not None, decoded
    if kind == "choice":
        decoded = _decode_choice_marker(comment)
        return decoded is not None, decoded
    return False, None


def _update_event_command_list(cmd_list: Any, translation_map: Dict[str, str]) -> bool:
    if not isinstance(cmd_list, list):
        return False

    modified = False
    i = len(cmd_list) - 1
    while i >= 0:
        cmd = cmd_list[i]
        code, indent, params = _event_command_fields(cmd)

        # --- Message blocks (401 + optional 101 + optional marker 108) ---
        if code == 401:
            first_text_index = i
            while first_text_index > 0:
                prev_code, _prev_indent, _prev_params = _event_command_fields(cmd_list[first_text_index - 1])
                if prev_code != 401:
                    break
                first_text_index -= 1

            block_start_index = first_text_index
            has_face_setup = False
            if first_text_index > 0:
                prev_code, _prev_indent, _prev_params = _event_command_fields(cmd_list[first_text_index - 1])
                if prev_code == 101:
                    block_start_index = first_text_index - 1
                    has_face_setup = True

            marker_index = block_start_index - 1
            has_marker = False
            original_text: Optional[str] = None
            if marker_index >= 0:
                ok, decoded = _is_comment_marker(cmd_list[marker_index], "message")
                if ok and isinstance(decoded, str):
                    has_marker = True
                    original_text = decoded

            if not has_marker:
                buf: List[str] = []
                for k in range(first_text_index, i + 1):
                    _c, _ind, p = _event_command_fields(cmd_list[k])
                    buf.append(_normalize_newlines(_as_str(p[0]) if p else ""))
                original_text = "\n".join(buf)

            original_text = _normalize_newlines(original_text or "")
            new_text = translation_map.get(original_text)
            new_text = _normalize_newlines(new_text) if isinstance(new_text, str) else None

            # Apply translation
            if new_text is not None and new_text.strip() != "" and new_text != original_text:
                new_cmds: List[Any] = []
                if not has_marker:
                    marker_str = _encode_message_marker(original_text)
                    new_cmds.append(
                        _new_event_command(108, indent, [_str_like(marker_str, params[0] if params else "")])
                    )
                if has_face_setup:
                    new_cmds.append(cmd_list[block_start_index])
                for line in new_text.split("\n"):
                    new_cmds.append(_new_event_command(401, indent, [_str_like(line, params[0] if params else "")]))

                # Keep existing marker (if any) by replacing only the text block (101+401),
                # and insert a new marker only when it didn't exist before.
                start_idx = block_start_index
                length = i - block_start_index + 1
                cmd_list[start_idx : start_idx + length] = new_cmds
                modified = True
                i = (marker_index - 1) if has_marker else (start_idx - 1)
                continue

            # Rollback to original if marker exists and no translation (or equals original)
            if has_marker and (new_text is None or new_text.strip() == "" or new_text == original_text):
                new_cmds2: List[Any] = []
                if has_face_setup:
                    new_cmds2.append(cmd_list[block_start_index])
                for line in original_text.split("\n"):
                    new_cmds2.append(_new_event_command(401, indent, [_str_like(line, params[0] if params else "")]))

                start_idx = marker_index
                length = i - start_idx + 1
                cmd_list[start_idx : start_idx + length] = new_cmds2
                modified = True
                i = start_idx - 1
                continue

            i = (marker_index if has_marker else block_start_index) - 1
            continue

        # --- Choices (102) with optional marker 108 right before it ---
        if code == 102:
            marker_index = i - 1
            has_marker = False
            original_choices: Optional[List[str]] = None
            if marker_index >= 0:
                ok, decoded = _is_comment_marker(cmd_list[marker_index], "choice")
                if ok and isinstance(decoded, list):
                    has_marker = True
                    original_choices = decoded

            current_choices_raw = params[0] if params else []
            if not isinstance(current_choices_raw, list):
                current_choices_raw = []
            if original_choices is None:
                original_choices = [_normalize_newlines(_as_str(x)) for x in current_choices_raw]

            new_choices: List[str] = []
            changed = False
            for ch in original_choices:
                mapped = translation_map.get(ch)
                mapped = _normalize_newlines(mapped) if isinstance(mapped, str) else None
                if mapped is not None and mapped.strip() != "" and mapped != ch:
                    new_choices.append(mapped)
                    changed = True
                else:
                    new_choices.append(ch)

            if changed:
                if not has_marker:
                    marker_str = _encode_choice_marker(original_choices)
                    cmd_list.insert(i, _new_event_command(108, indent, [marker_str]))
                    i += 1  # cmd shifted right

                params[0] = [
                    _str_like(x, current_choices_raw[idx] if idx < len(current_choices_raw) else "")
                    for idx, x in enumerate(new_choices)
                ]
                _set_attr(cmd, "parameters", params)
                modified = True
                i = (marker_index if has_marker else i) - 1
                continue

            if has_marker and not changed:
                # rollback: remove marker and restore original choices
                cmd_list.pop(marker_index)
                params[0] = [
                    _str_like(x, current_choices_raw[idx] if idx < len(current_choices_raw) else "")
                    for idx, x in enumerate(original_choices)
                ]
                _set_attr(cmd, "parameters", params)
                modified = True
                i = marker_index - 1
                continue

            i -= 1
            continue

        i -= 1
    return modified


def import_from_string_scripts(game_path: str, message_queue) -> int:
    data_dir = os.path.join(game_path, "Data")
    map_infos_path = os.path.join(data_dir, "MapInfos.rvdata2")
    if not os.path.isfile(map_infos_path):
        raise VXAceError(f"未找到 VX Ace 数据文件: {map_infos_path}")

    string_scripts_path = os.path.join(game_path, STRING_SCRIPTS_DIRNAME)
    backup_path = os.path.join(game_path, STRING_SCRIPTS_ORIGIN_DIRNAME)
    if not os.path.isdir(string_scripts_path) or not os.path.isdir(backup_path):
        raise VXAceError("未找到 StringScripts 或 StringScripts_Origin，请先执行步骤 1 导出文本。")

    modified_files = 0

    # --- Maps ---
    for filename in os.listdir(backup_path):
        if not re.match(r"^Map\d{3}\.txt$", filename, re.IGNORECASE):
            continue
        origin_file = os.path.join(backup_path, filename)
        translated_file = os.path.join(string_scripts_path, filename)
        if not os.path.isfile(translated_file):
            continue

        try:
            origin_text = open(origin_file, "r", encoding="utf-8-sig", errors="replace").read()
            translated_text = open(translated_file, "r", encoding="utf-8-sig", errors="replace").read()
        except Exception:
            continue

        tmap = _build_translation_map(origin_text, translated_text)
        if not tmap:
            continue

        map_id = int(filename[3:6])
        map_path = os.path.join(data_dir, f"Map{map_id:03d}.rvdata2")
        if not os.path.isfile(map_path):
            continue
        try:
            map_obj = _load_rvdata2(map_path)
        except Exception as e:
            log.warning(f"读取地图失败: {map_path} - {e}")
            continue

        events = _get_attr(map_obj, "events", {})
        if not isinstance(events, dict):
            continue

        map_modified = False
        for ev in events.values():
            if ev is None:
                continue
            pages = _get_attr(ev, "pages", [])
            if not isinstance(pages, list):
                continue
            for page in pages:
                cmd_list = _get_attr(page, "list", [])
                if _update_event_command_list(cmd_list, tmap):
                    map_modified = True

        if map_modified:
            _save_rvdata2(map_path, map_obj)
            modified_files += 1

    # --- Common events ---
    common_origin = os.path.join(backup_path, "CommonEvents.txt")
    common_translated = os.path.join(string_scripts_path, "CommonEvents.txt")
    if os.path.isfile(common_origin) and os.path.isfile(common_translated):
        try:
            origin_text = open(common_origin, "r", encoding="utf-8-sig", errors="replace").read()
            translated_text = open(common_translated, "r", encoding="utf-8-sig", errors="replace").read()
            tmap = _build_translation_map(origin_text, translated_text)
        except Exception:
            tmap = {}
        if tmap:
            common_path = os.path.join(data_dir, "CommonEvents.rvdata2")
            if os.path.isfile(common_path):
                try:
                    common_events = _load_rvdata2(common_path)
                except Exception:
                    common_events = None
                if isinstance(common_events, list):
                    common_modified = False
                    for ce in common_events:
                        if ce is None:
                            continue
                        cmd_list = _get_attr(ce, "list", [])
                        if _update_event_command_list(cmd_list, tmap):
                            common_modified = True
                    if common_modified:
                        _save_rvdata2(common_path, common_events)
                        modified_files += 1

    # --- Database import ---
    db_dir = os.path.join(string_scripts_path, "Database")
    if os.path.isdir(db_dir):
        modified_files += _import_database(db_dir, data_dir)

    return modified_files


def _parse_db_file(path: str) -> Dict[str, Any]:
    try:
        text = open(path, "r", encoding="utf-8-sig", errors="replace").read()
    except Exception:
        return {}
    return _parse_db_text(text)


def _parse_db_text(text: str) -> Dict[str, Any]:
    entries = _parse_string_scripts_text(text)
    result: Dict[str, Any] = {}
    for e in entries:
        result[e.marker] = e.content
    return result


_DB_ENTRY_SEPARATOR_RE = re.compile(r"^\*{5,}Entry(\d+)\*{5,}$", re.IGNORECASE)


def _parse_db_compact_entries(path: str) -> Dict[int, Dict[str, Any]]:
    """
    Parse a compact Database file that groups multiple entries into one txt:
        *****Entry1*****
        #Name#
        ...

    Returns:
        { entry_id(int): { marker(str): content } }
    """
    try:
        lines = open(path, "r", encoding="utf-8-sig", errors="replace").read().splitlines(keepends=True)
    except Exception:
        return {}

    result: Dict[int, Dict[str, Any]] = {}
    current_id: Optional[int] = None
    buf: List[str] = []

    def flush():
        nonlocal buf, current_id
        if current_id is None:
            return
        entry = _parse_db_text("".join(buf))
        if entry:
            result[current_id] = entry
        buf = []

    for line in lines:
        m = _DB_ENTRY_SEPARATOR_RE.match(line.strip())
        if m:
            flush()
            try:
                current_id = int(m.group(1))
            except Exception:
                current_id = None
            continue
        if current_id is None:
            continue
        buf.append(line)

    flush()
    return result


def _import_database(db_dir: str, data_dir: str) -> int:
    modified_files = 0

    def import_array_group(folder: str, rvdata2_name: str, apply_fn):
        nonlocal modified_files
        group_dir = os.path.join(db_dir, folder)
        if not os.path.isdir(group_dir):
            return
        rv_path = os.path.join(data_dir, rvdata2_name)
        if not os.path.isfile(rv_path):
            return
        try:
            data = _load_rvdata2(rv_path)
        except Exception as e:
            log.warning(f"读取数据库失败: {rv_path} - {e}")
            return
        if not isinstance(data, list):
            return

        compact_path = os.path.join(group_dir, f"{folder}.txt")
        if not os.path.isfile(compact_path):
            return

        touched = False
        entries = _parse_db_compact_entries(compact_path)
        for idx, entry in entries.items():
            if idx <= 0 or idx >= len(data) or data[idx] is None:
                continue
            if apply_fn(data[idx], entry):
                touched = True
        if touched:
            _save_rvdata2(rv_path, data)
            modified_files += 1

    def apply_name_nickname_desc(obj: Any, entry: Dict[str, Any]) -> bool:
        changed = False
        if "Name" in entry and isinstance(entry["Name"], str):
            old = _get_attr(obj, "name", "")
            _set_attr(obj, "name", _str_like(entry["Name"], old))
            changed = True
        if "Nickname" in entry and isinstance(entry["Nickname"], str):
            old = _get_attr(obj, "nickname", "")
            _set_attr(obj, "nickname", _str_like(entry["Nickname"], old))
            changed = True
        if "Description" in entry and isinstance(entry["Description"], str):
            old = _get_attr(obj, "description", "")
            _set_attr(obj, "description", _str_like(_unescape_inline_newlines(entry["Description"]), old))
            changed = True
        return changed

    def apply_name_desc(obj: Any, entry: Dict[str, Any]) -> bool:
        changed = False
        if "Name" in entry and isinstance(entry["Name"], str):
            old = _get_attr(obj, "name", "")
            _set_attr(obj, "name", _str_like(entry["Name"], old))
            changed = True
        if "Description" in entry and isinstance(entry["Description"], str):
            old = _get_attr(obj, "description", "")
            _set_attr(obj, "description", _str_like(_unescape_inline_newlines(entry["Description"]), old))
            changed = True
        return changed

    def apply_skill(obj: Any, entry: Dict[str, Any]) -> bool:
        changed = apply_name_desc(obj, entry)
        if "Message1" in entry and isinstance(entry["Message1"], str):
            old = _get_attr(obj, "message1", "")
            _set_attr(obj, "message1", _str_like(entry["Message1"], old))
            changed = True
        if "Message2" in entry and isinstance(entry["Message2"], str):
            old = _get_attr(obj, "message2", "")
            _set_attr(obj, "message2", _str_like(entry["Message2"], old))
            changed = True
        return changed

    def apply_state(obj: Any, entry: Dict[str, Any]) -> bool:
        changed = apply_name_desc(obj, entry)
        for k in ["Message1", "Message2", "Message3", "Message4"]:
            if k in entry and isinstance(entry[k], str):
                old = _get_attr(obj, k.lower(), "")
                _set_attr(obj, k.lower(), _str_like(entry[k], old))
                changed = True
        return changed

    import_array_group("Actors", "Actors.rvdata2", apply_name_nickname_desc)
    import_array_group("Classes", "Classes.rvdata2", apply_name_desc)
    import_array_group("Skills", "Skills.rvdata2", apply_skill)
    import_array_group("Items", "Items.rvdata2", apply_name_desc)
    import_array_group("Weapons", "Weapons.rvdata2", apply_name_desc)
    import_array_group("Armors", "Armors.rvdata2", apply_name_desc)
    import_array_group("Enemies", "Enemies.rvdata2", apply_name_desc)
    import_array_group("States", "States.rvdata2", apply_state)

    # MapInfos
    mapinfo_dir = os.path.join(db_dir, "MapInfos")
    map_infos_path = os.path.join(data_dir, "MapInfos.rvdata2")
    mapinfo_compact = os.path.join(mapinfo_dir, "MapInfos.txt")
    if os.path.isfile(mapinfo_compact) and os.path.isfile(map_infos_path):
        try:
            map_infos = _load_rvdata2(map_infos_path)
        except Exception:
            map_infos = None
        if isinstance(map_infos, dict):
            changed = False
            entries = _parse_db_compact_entries(mapinfo_compact)
            for map_id, entry in entries.items():
                info = map_infos.get(map_id)
                if info is None:
                    continue
                if "Name" in entry and isinstance(entry["Name"], str):
                    old = _get_attr(info, "name", "")
                    _set_attr(info, "name", _str_like(entry["Name"], old))
                    changed = True
            if changed:
                _save_rvdata2(map_infos_path, map_infos)
                modified_files += 1

    # System + Vocab
    system_path = os.path.join(data_dir, "System.rvdata2")
    if os.path.isfile(system_path):
        try:
            system_obj = _load_rvdata2(system_path)
        except Exception:
            system_obj = None
        if system_obj is not None:
            changed = False

            system_compact = os.path.join(db_dir, "System", "System.txt")
            if os.path.isfile(system_compact):
                entry = _parse_db_file(system_compact)
                if "Name" in entry and isinstance(entry["Name"], str):
                    old = _get_attr(system_obj, "game_title", "")
                    _set_attr(system_obj, "game_title", _str_like(_unescape_inline_newlines(entry["Name"]), old))
                    changed = True

            vocab_path = os.path.join(db_dir, "Vocab.txt")
            if os.path.isfile(vocab_path):
                vocab = _parse_db_file(vocab_path)

                if "CurrencyUnit" in vocab and isinstance(vocab["CurrencyUnit"], str):
                    old = _get_attr(system_obj, "currency_unit", "")
                    _set_attr(system_obj, "currency_unit", _str_like(_unescape_inline_newlines(vocab["CurrencyUnit"]), old))
                    changed = True

                terms_obj = _get_attr(system_obj, "terms", None)
                if terms_obj is not None:
                    terms_attrs = getattr(terms_obj, "attributes", {}) or {}
                    for group_attr, idx_to_marker in _VXACE_VOCAB_MARKERS.items():
                        if _ivar(group_attr) not in terms_attrs:
                            continue
                        arr = _get_attr(terms_obj, group_attr, [])
                        if not isinstance(arr, list):
                            continue
                        group_changed = False
                        for idx, marker in idx_to_marker.items():
                            if idx < 0 or idx >= len(arr):
                                continue
                            if marker not in vocab or not isinstance(vocab[marker], str):
                                continue
                            arr[idx] = _str_like(_unescape_inline_newlines(vocab[marker]), arr[idx])
                            group_changed = True
                        if group_changed:
                            _set_attr(terms_obj, group_attr, arr)
                            changed = True

            if changed:
                _save_rvdata2(system_path, system_obj)
                modified_files += 1

    return modified_files
