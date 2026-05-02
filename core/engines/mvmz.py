"""
RPG Maker MV/MZ JSON support.

This adapter exports MV/MZ JSON data into the same StringScripts format used by
the existing pipeline, then imports translated StringScripts back into JSON.
It is tailored to runtime-visible text; asset filenames are intentionally left
unchanged.
"""

import base64
import copy
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
ORIGINAL_DB_STORE_FILENAME = "RTA_MVMZ_ORIGINAL_DB.json"

MESSAGE_MARKER_PREFIX = "<RTA_MVMZ_ORIGINAL_MESSAGE:"
CHOICE_MARKER_PREFIX = "<RTA_MVMZ_ORIGINAL_CHOICE:"
MARKER_SUFFIX = ">"

JAPANESE_TEXT_RE = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff\uff66-\uff9f\u4e00-\u9fff]")


class MVMZError(RuntimeError):
    pass


@dataclass
class _ParsedEntry:
    marker: str
    content: Any


def find_data_dir(game_path: str) -> Optional[str]:
    """Return the MV/MZ data directory for editor or deployed layouts."""
    candidates = [
        os.path.join(game_path, "data"),
        os.path.join(game_path, "www", "data"),
    ]
    for data_dir in candidates:
        if os.path.isfile(os.path.join(data_dir, "MapInfos.json")):
            return data_dir
    return None


def _load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def _save_json(path: str, data: Any) -> None:
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
    os.replace(tmp_path, path)


def _write_text_file(path: str, lines: List[str]) -> None:
    file_system.ensure_dir_exists(os.path.dirname(path))
    with open(path, "w", encoding="utf-8-sig", newline="\n") as f:
        f.writelines(lines)


def _normalize_newlines(text: str) -> str:
    return (text or "").replace("\r\n", "\n").replace("\r", "\n")


def _escape_inline_newlines(text: str) -> str:
    return _normalize_newlines(text).replace("\n", "\\n")


def _unescape_inline_newlines(text: str) -> str:
    return (text or "").replace("\\n", "\n")


def _load_json_if_exists(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        data = _load_json(path)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_store(path: str, data: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _get_original_text(store: Dict[str, Any], key: str, current_value: str) -> Tuple[str, bool]:
    if key in store and isinstance(store[key], str):
        return store[key], False
    store[key] = current_value
    return current_value, True


def _append_single_entry(
    lines: List[str],
    marker: str,
    text: str,
    *,
    multiline_inline: bool = False,
) -> None:
    text = _normalize_newlines(text)
    if text == "":
        return
    lines.append(f"#{marker}#\n")
    lines.append(f"{_escape_inline_newlines(text) if multiline_inline else text}\n")


def _string_scripts_face_line(face_name: str, face_index: int) -> str:
    face_name = _normalize_newlines(face_name).strip()
    if not face_name:
        return "{{ Select Face Graphic: Erase }}\n"
    return f"{{{{ Select Face Graphic: {face_name}, {int(face_index)} }}}}\n"


def _event_command_fields(cmd: Any) -> Tuple[int, int, List[Any]]:
    if not isinstance(cmd, dict):
        return 0, 0, []
    try:
        code = int(cmd.get("code", 0))
    except Exception:
        code = 0
    try:
        indent = int(cmd.get("indent", 0) or 0)
    except Exception:
        indent = 0
    params = cmd.get("parameters", [])
    if not isinstance(params, list):
        params = []
    return code, indent, params


def _new_event_command(code: int, indent: int, parameters: List[Any]) -> Dict[str, Any]:
    return {"code": int(code), "indent": int(indent), "parameters": parameters}


def _encode_marker(prefix: str, payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    encoded = base64.b64encode(raw.encode("utf-8")).decode("ascii")
    return f"{prefix}{encoded}{MARKER_SUFFIX}"


def _decode_marker(text: str, prefix: str) -> Optional[Any]:
    if not isinstance(text, str):
        return None
    if not (text.startswith(prefix) and text.endswith(MARKER_SUFFIX)):
        return None
    payload = text[len(prefix) : -len(MARKER_SUFFIX)]
    try:
        raw = base64.b64decode(payload).decode("utf-8", errors="strict")
        return json.loads(raw)
    except Exception:
        return None


def _encode_message_marker(original_text: str, original_speaker: str) -> str:
    return _encode_marker(
        MESSAGE_MARKER_PREFIX,
        {"text": _normalize_newlines(original_text), "speaker": _normalize_newlines(original_speaker)},
    )


def _decode_message_marker(comment: str) -> Optional[Dict[str, str]]:
    decoded = _decode_marker(comment, MESSAGE_MARKER_PREFIX)
    if not isinstance(decoded, dict):
        return None
    text = decoded.get("text", "")
    speaker = decoded.get("speaker", "")
    if isinstance(text, str) and isinstance(speaker, str):
        return {"text": text, "speaker": speaker}
    return None


def _encode_choice_marker(original_choices: List[str]) -> str:
    return _encode_marker(CHOICE_MARKER_PREFIX, [_normalize_newlines(x) for x in original_choices])


def _decode_choice_marker(comment: str) -> Optional[List[str]]:
    decoded = _decode_marker(comment, CHOICE_MARKER_PREFIX)
    if isinstance(decoded, list) and all(isinstance(x, str) for x in decoded):
        return decoded
    return None


def _get_speaker_name(params: List[Any]) -> str:
    if len(params) > 4 and isinstance(params[4], str):
        return _normalize_newlines(params[4])
    return ""


def _set_speaker_name(params: List[Any], speaker: str) -> List[Any]:
    new_params = list(params)
    if len(new_params) > 4:
        new_params[4] = speaker
    return new_params


def _message_lines_from_commands(cmds: List[Any]) -> List[str]:
    lines: List[str] = []
    for cmd in cmds:
        code, _indent, params = _event_command_fields(cmd)
        if code != 401:
            continue
        lines.append(_normalize_newlines(str(params[0])) if params else "")
    return lines


def _emit_message_lines(
    out_lines: List[str],
    *,
    face_name: str = "",
    face_index: int = 0,
    speaker_name: str = "",
    message_text: str = "",
) -> None:
    out_lines.append(_string_scripts_face_line(face_name, face_index))
    if speaker_name:
        _append_single_entry(out_lines, "SpeakerName", speaker_name)
    message_text = _normalize_newlines(message_text)
    if message_text:
        out_lines.append("#Message#\n")
        for line in message_text.split("\n"):
            out_lines.append(f"{line}\n")
        out_lines.append("##\n")


def _export_command_list_to_lines(cmd_list: Any) -> List[str]:
    if not isinstance(cmd_list, list):
        return []

    lines: List[str] = []
    pending_message_marker: Optional[Dict[str, str]] = None
    pending_choice_marker: Optional[List[str]] = None
    i = 0
    while i < len(cmd_list):
        cmd = cmd_list[i]
        code, indent, params = _event_command_fields(cmd)

        if code == 108 and params:
            comment = str(params[0])
            decoded_message = _decode_message_marker(comment)
            if decoded_message is not None:
                pending_message_marker = decoded_message
                i += 1
                continue
            decoded_choices = _decode_choice_marker(comment)
            if decoded_choices is not None:
                pending_choice_marker = decoded_choices
                i += 1
                continue

        if code == 101:
            face_name = str(params[0]) if params else ""
            try:
                face_index = int(params[1]) if len(params) > 1 else 0
            except Exception:
                face_index = 0
            speaker_name = _get_speaker_name(params)

            j = i + 1
            text_cmds: List[Any] = []
            while j < len(cmd_list):
                next_code, _next_indent, _next_params = _event_command_fields(cmd_list[j])
                if next_code != 401:
                    break
                text_cmds.append(cmd_list[j])
                j += 1

            message_text = "\n".join(_message_lines_from_commands(text_cmds))
            if pending_message_marker is not None:
                message_text = pending_message_marker.get("text", "")
                speaker_name = pending_message_marker.get("speaker", "")
                pending_message_marker = None

            _emit_message_lines(
                lines,
                face_name=face_name,
                face_index=face_index,
                speaker_name=speaker_name,
                message_text=message_text,
            )
            i = j
            continue

        if code == 401:
            text_cmds = [cmd]
            j = i + 1
            while j < len(cmd_list):
                next_code, _next_indent, _next_params = _event_command_fields(cmd_list[j])
                if next_code != 401:
                    break
                text_cmds.append(cmd_list[j])
                j += 1

            message_text = "\n".join(_message_lines_from_commands(text_cmds))
            speaker_name = ""
            if pending_message_marker is not None:
                message_text = pending_message_marker.get("text", "")
                speaker_name = pending_message_marker.get("speaker", "")
                pending_message_marker = None

            _emit_message_lines(lines, speaker_name=speaker_name, message_text=message_text)
            i = j
            continue

        if code == 102:
            choices_raw = pending_choice_marker if pending_choice_marker is not None else (params[0] if params else [])
            pending_choice_marker = None
            if isinstance(choices_raw, list):
                choices = [_normalize_newlines(str(x)) for x in choices_raw if str(x) != ""]
                if choices:
                    lines.append("#Choice#\n")
                    for choice in choices:
                        lines.append(f"{choice}\n")
                    lines.append("##\n")
            i += 1
            continue

        i += 1

    return lines


def export_to_string_scripts(game_path: str, message_queue) -> None:
    data_dir = find_data_dir(game_path)
    if not data_dir:
        raise MVMZError("未找到 MV/MZ 数据目录（data/MapInfos.json 或 www/data/MapInfos.json）。")

    string_scripts_path = os.path.join(game_path, STRING_SCRIPTS_DIRNAME)
    backup_path = os.path.join(game_path, STRING_SCRIPTS_ORIGIN_DIRNAME)

    if os.path.exists(string_scripts_path):
        file_system.safe_remove(string_scripts_path)
    if os.path.exists(backup_path):
        file_system.safe_remove(backup_path)
    file_system.ensure_dir_exists(string_scripts_path)

    message_queue.put(("log", ("normal", "读取 MV/MZ JSON 数据并生成 StringScripts...")))

    store_path = os.path.join(game_path, ORIGINAL_DB_STORE_FILENAME)
    original_store = _load_json_if_exists(store_path)
    original_store_modified = False

    map_infos_path = os.path.join(data_dir, "MapInfos.json")
    map_infos = _load_json(map_infos_path)

    exported_map_files = 0
    for map_id in _iter_map_ids(data_dir, map_infos):
        map_path = os.path.join(data_dir, f"Map{map_id:03d}.json")
        if not os.path.isfile(map_path):
            continue
        try:
            map_obj = _load_json(map_path)
        except Exception as e:
            log.warning(f"读取地图失败: {map_path} - {e}")
            continue

        events = map_obj.get("events") if isinstance(map_obj, dict) else None
        if not isinstance(events, list):
            continue

        out_lines: List[str] = []
        for ev in events:
            if not isinstance(ev, dict):
                continue
            ev_id = ev.get("id")
            pages = ev.get("pages")
            if not isinstance(ev_id, int) or not isinstance(pages, list):
                continue

            entry_lines: List[str] = [f"*****Entry{ev_id}*****\n"]
            for page_idx, page in enumerate(pages):
                if not isinstance(page, dict):
                    continue
                cmd_list = page.get("list", [])
                page_lines = _export_command_list_to_lines(cmd_list)
                if page_lines:
                    entry_lines.append(f"-----Page{page_idx + 1}-----\n")
                    entry_lines.extend(page_lines)

            if any(line.startswith("#") for line in entry_lines):
                out_lines.extend(entry_lines)

        if out_lines:
            _write_text_file(os.path.join(string_scripts_path, f"Map{map_id:03d}.txt"), out_lines)
            exported_map_files += 1

    message_queue.put(("log", ("success", f"地图对话导出完成：{exported_map_files} 个文件。")))

    common_path = os.path.join(data_dir, "CommonEvents.json")
    if os.path.isfile(common_path):
        common_events = _load_json(common_path)
        common_lines = _export_common_events(common_events)
        if common_lines:
            _write_text_file(os.path.join(string_scripts_path, "CommonEvents.txt"), common_lines)
            message_queue.put(("log", ("success", "公共事件对话导出完成：CommonEvents.txt")))

    db_modified, db_files = _export_database(
        game_path=game_path,
        data_dir=data_dir,
        map_infos=map_infos,
        out_root=os.path.join(string_scripts_path, "Database"),
        original_store=original_store,
    )
    original_store_modified = original_store_modified or db_modified
    message_queue.put(("log", ("success", f"数据库/系统导出完成：{db_files} 个文件。")))

    if original_store_modified:
        _save_store(store_path, original_store)

    if os.path.exists(backup_path):
        file_system.safe_remove(backup_path)
    shutil.copytree(string_scripts_path, backup_path)
    message_queue.put(("log", ("success", "已备份原始 StringScripts 到 StringScripts_Origin。")))


def _iter_map_ids(data_dir: str, map_infos: Any) -> List[int]:
    ids = set()
    if isinstance(map_infos, list):
        for entry in map_infos:
            if isinstance(entry, dict) and isinstance(entry.get("id"), int):
                ids.add(entry["id"])
    elif isinstance(map_infos, dict):
        for key in map_infos.keys():
            try:
                ids.add(int(key))
            except Exception:
                pass

    for filename in os.listdir(data_dir):
        m = re.match(r"Map(\d{3})\.json$", filename, re.IGNORECASE)
        if m:
            ids.add(int(m.group(1)))
    return sorted(x for x in ids if x > 0)


def _export_common_events(common_events: Any) -> List[str]:
    if not isinstance(common_events, list):
        return []
    out_lines: List[str] = []
    for common_event in common_events:
        if not isinstance(common_event, dict):
            continue
        ce_id = common_event.get("id")
        cmd_list = common_event.get("list", [])
        page_lines = _export_command_list_to_lines(cmd_list)
        if page_lines and isinstance(ce_id, int):
            out_lines.append(f"*****Entry{ce_id}*****\n")
            out_lines.append("-----Page1-----\n")
            out_lines.extend(page_lines)
    return out_lines


def _export_database(
    *,
    game_path: str,
    data_dir: str,
    map_infos: Any,
    out_root: str,
    original_store: Dict[str, Any],
) -> Tuple[bool, int]:
    file_system.ensure_dir_exists(out_root)
    original_store_modified = False
    exported_files = 0

    def add_entry_line(lines: List[str], marker: str, store_key: str, current_val: str, multiline_inline: bool = False):
        nonlocal original_store_modified
        original, created = _get_original_text(original_store, store_key, _normalize_newlines(current_val))
        if created:
            original_store_modified = True
        _append_single_entry(lines, marker, original, multiline_inline=multiline_inline)

    def export_array_table(folder_name: str, json_name: str, fields: List[Tuple[str, str, bool]]) -> int:
        path = os.path.join(data_dir, json_name)
        if not os.path.isfile(path):
            return 0
        try:
            data = _load_json(path)
        except Exception as e:
            log.warning(f"读取数据库失败: {path} - {e}")
            return 0
        if not isinstance(data, list):
            return 0

        out_lines: List[str] = []
        for idx, obj in enumerate(data):
            if idx == 0 or not isinstance(obj, dict):
                continue
            entry_lines: List[str] = [f"*****Entry{idx}*****\n"]
            for marker, attr_name, multiline_inline in fields:
                val = obj.get(attr_name, "")
                if not isinstance(val, str):
                    continue
                add_entry_line(entry_lines, marker, f"{json_name}:{idx}:{attr_name}", val, multiline_inline)
            if any(line.startswith("#") for line in entry_lines):
                out_lines.extend(entry_lines)
                out_lines.append("\n")

        if not out_lines:
            return 0
        out_dir = os.path.join(out_root, folder_name)
        _write_text_file(os.path.join(out_dir, f"{folder_name}.txt"), out_lines)
        return 1

    exported_files += export_array_table("Actors", "Actors.json", [("Name", "name", False), ("Nickname", "nickname", False), ("Profile", "profile", True)])
    exported_files += export_array_table("Classes", "Classes.json", [("Name", "name", False)])
    exported_files += export_array_table(
        "Skills",
        "Skills.json",
        [("Name", "name", False), ("Description", "description", True), ("Message1", "message1", False), ("Message2", "message2", False)],
    )
    exported_files += export_array_table("Items", "Items.json", [("Name", "name", False), ("Description", "description", True)])
    exported_files += export_array_table("Weapons", "Weapons.json", [("Name", "name", False), ("Description", "description", True)])
    exported_files += export_array_table("Armors", "Armors.json", [("Name", "name", False), ("Description", "description", True)])
    exported_files += export_array_table("Enemies", "Enemies.json", [("Name", "name", False)])
    exported_files += export_array_table(
        "States",
        "States.json",
        [("Name", "name", False), ("Message1", "message1", False), ("Message2", "message2", False), ("Message3", "message3", False), ("Message4", "message4", False)],
    )

    mapinfo_lines: List[str] = []
    if isinstance(map_infos, list):
        for entry in map_infos:
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), int):
                continue
            map_id = int(entry["id"])
            name = entry.get("name", "")
            if isinstance(name, str):
                entry_lines = [f"*****Entry{map_id}*****\n"]
                add_entry_line(entry_lines, "Name", f"MapInfos.json:{map_id}:name", name)
                if any(line.startswith("#") for line in entry_lines):
                    mapinfo_lines.extend(entry_lines)
                    mapinfo_lines.append("\n")
    if mapinfo_lines:
        _write_text_file(os.path.join(out_root, "MapInfos", "MapInfos.txt"), mapinfo_lines)
        exported_files += 1

    map_display_files = _export_map_display_names(data_dir, out_root, original_store)
    if map_display_files[0]:
        original_store_modified = True
    exported_files += map_display_files[1]

    system_files = _export_system(data_dir, out_root, original_store)
    if system_files[0]:
        original_store_modified = True
    exported_files += system_files[1]

    plugin_files = _export_plugins(game_path, out_root, original_store)
    if plugin_files[0]:
        original_store_modified = True
    exported_files += plugin_files[1]

    metadata_files = _export_metadata(game_path, out_root, original_store)
    if metadata_files[0]:
        original_store_modified = True
    exported_files += metadata_files[1]

    return original_store_modified, exported_files


def _export_map_display_names(data_dir: str, out_root: str, original_store: Dict[str, Any]) -> Tuple[bool, int]:
    modified = False
    out_lines: List[str] = []
    for map_id in _iter_map_ids(data_dir, None):
        path = os.path.join(data_dir, f"Map{map_id:03d}.json")
        try:
            map_obj = _load_json(path)
        except Exception:
            continue
        if not isinstance(map_obj, dict):
            continue
        display_name = map_obj.get("displayName", "")
        if not isinstance(display_name, str) or display_name == "":
            continue
        original, created = _get_original_text(original_store, f"Map{map_id:03d}.json:displayName", display_name)
        modified = modified or created
        entry_lines = [f"*****Entry{map_id}*****\n"]
        _append_single_entry(entry_lines, "Name", original)
        if any(line.startswith("#") for line in entry_lines):
            out_lines.extend(entry_lines)
            out_lines.append("\n")
    if out_lines:
        _write_text_file(os.path.join(out_root, "MapDisplayNames", "MapDisplayNames.txt"), out_lines)
        return modified, 1
    return modified, 0


def _export_system(data_dir: str, out_root: str, original_store: Dict[str, Any]) -> Tuple[bool, int]:
    system_path = os.path.join(data_dir, "System.json")
    if not os.path.isfile(system_path):
        return False, 0
    try:
        system = _load_json(system_path)
    except Exception as e:
        log.warning(f"读取 System 失败: {system_path} - {e}")
        return False, 0
    if not isinstance(system, dict):
        return False, 0

    modified = False
    out_lines: List[str] = []

    def add(marker: str, store_key: str, value: str) -> None:
        nonlocal modified
        original, created = _get_original_text(original_store, store_key, _normalize_newlines(value))
        modified = modified or created
        _append_single_entry(out_lines, marker, original, multiline_inline=True)

    if isinstance(system.get("gameTitle"), str):
        add("Name", "System.json:gameTitle", system["gameTitle"])
    if isinstance(system.get("currencyUnit"), str):
        add("CurrencyUnit", "System.json:currencyUnit", system["currencyUnit"])

    for json_key, marker_prefix in [
        ("armorTypes", "ArmorType"),
        ("elements", "Element"),
        ("equipTypes", "EquipType"),
        ("skillTypes", "SkillType"),
        ("weaponTypes", "WeaponType"),
    ]:
        arr = system.get(json_key)
        if not isinstance(arr, list):
            continue
        for idx, value in enumerate(arr):
            if isinstance(value, str) and value:
                add(f"{marker_prefix}{idx}", f"System.json:{json_key}:{idx}", value)

    terms = system.get("terms")
    if isinstance(terms, dict):
        for term_key, marker_prefix in [("basic", "TermBasic"), ("commands", "TermCommand"), ("params", "TermParam")]:
            arr = terms.get(term_key)
            if not isinstance(arr, list):
                continue
            for idx, value in enumerate(arr):
                if isinstance(value, str) and value:
                    add(f"{marker_prefix}{idx}", f"System.json:terms.{term_key}:{idx}", value)

        messages = terms.get("messages")
        if isinstance(messages, dict):
            for key in sorted(messages.keys()):
                value = messages.get(key)
                if isinstance(value, str) and value:
                    add(f"TermMessage_{key}", f"System.json:terms.messages:{key}", value)

    if out_lines:
        _write_text_file(os.path.join(out_root, "System", "System.txt"), out_lines)
        return modified, 1
    return modified, 0


def _load_plugins_js(path: str) -> Tuple[str, str, List[Any]]:
    text = open(path, "r", encoding="utf-8-sig", errors="replace").read()
    start = text.find("[")
    end = text.rfind("];")
    if start < 0 or end < start:
        raise MVMZError(f"plugins.js 格式异常: {path}")
    prefix = text[:start]
    suffix = text[end + 1 :]
    payload = text[start : end + 1]
    plugins = json.loads(payload)
    if not isinstance(plugins, list):
        raise MVMZError(f"plugins.js 内容异常: {path}")
    return prefix, suffix, plugins


def _save_plugins_js(path: str, prefix: str, suffix: str, plugins: List[Any]) -> None:
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(prefix)
        f.write(json.dumps(plugins, ensure_ascii=False, indent=4))
        f.write(suffix if suffix else ";\n")
    os.replace(tmp_path, path)


def _is_translatable_plugin_value(value: str) -> bool:
    if not isinstance(value, str) or value == "":
        return False
    stripped = value.strip()
    if stripped.lower() in {"true", "false", "on", "off", "left", "right", "center"}:
        return False
    try:
        float(stripped)
        return False
    except Exception:
        pass
    if stripped.startswith(("http://", "https://")):
        return False
    return JAPANESE_TEXT_RE.search(stripped) is not None


def _safe_marker_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", text).strip("_") or "Value"


def _export_plugins(game_path: str, out_root: str, original_store: Dict[str, Any]) -> Tuple[bool, int]:
    plugins_path = os.path.join(game_path, "js", "plugins.js")
    if not os.path.isfile(plugins_path):
        return False, 0
    try:
        _prefix, _suffix, plugins = _load_plugins_js(plugins_path)
    except Exception as e:
        log.warning(f"读取 plugins.js 失败: {plugins_path} - {e}")
        return False, 0

    modified = False
    out_lines: List[str] = []
    for idx, plugin in enumerate(plugins):
        if not isinstance(plugin, dict):
            continue
        params = plugin.get("parameters")
        if not isinstance(params, dict):
            continue
        entry_lines: List[str] = [f"*****Entry{idx}*****\n"]
        for param_name, value in params.items():
            if not _is_translatable_plugin_value(value):
                continue
            marker = f"Param_{_safe_marker_name(str(param_name))}"
            original, created = _get_original_text(original_store, f"plugins.js:{idx}:parameters.{param_name}", value)
            modified = modified or created
            _append_single_entry(entry_lines, marker, original, multiline_inline=True)
        if any(line.startswith("#") for line in entry_lines):
            out_lines.extend(entry_lines)
            out_lines.append("\n")

    if out_lines:
        _write_text_file(os.path.join(out_root, "Plugins", "Plugins.txt"), out_lines)
        return modified, 1
    return modified, 0


def _export_metadata(game_path: str, out_root: str, original_store: Dict[str, Any]) -> Tuple[bool, int]:
    modified = False
    out_lines: List[str] = []

    package_path = os.path.join(game_path, "package.json")
    if os.path.isfile(package_path):
        try:
            package = _load_json(package_path)
            title = package.get("window", {}).get("title") if isinstance(package, dict) else None
            if isinstance(title, str) and title:
                original, created = _get_original_text(original_store, "package.json:window.title", title)
                modified = modified or created
                _append_single_entry(out_lines, "PackageTitle", original, multiline_inline=True)
        except Exception as e:
            log.warning(f"读取 package.json 失败: {package_path} - {e}")

    index_path = os.path.join(game_path, "index.html")
    if os.path.isfile(index_path):
        try:
            text = open(index_path, "r", encoding="utf-8-sig", errors="replace").read()
            m = re.search(r"<title>(.*?)</title>", text, flags=re.IGNORECASE | re.DOTALL)
            if m:
                title = _normalize_newlines(m.group(1).strip())
                if title:
                    original, created = _get_original_text(original_store, "index.html:title", title)
                    modified = modified or created
                    _append_single_entry(out_lines, "HtmlTitle", original, multiline_inline=True)
        except Exception as e:
            log.warning(f"读取 index.html 失败: {index_path} - {e}")

    if out_lines:
        _write_text_file(os.path.join(out_root, "Metadata", "Metadata.txt"), out_lines)
        return modified, 1
    return modified, 0


_MARKER_RE = re.compile(r"#(.+)#")


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

        if i < len(lines):
            entries.append(_ParsedEntry(marker=marker, content=_normalize_newlines(lines[i].rstrip("\n"))))
            i += 1
        else:
            entries.append(_ParsedEntry(marker=marker, content=""))
    return entries


def _build_translation_maps(origin_text: str, translated_text: str) -> Dict[str, Dict[str, str]]:
    origin_entries = _parse_string_scripts_text(origin_text)
    translated_entries = _parse_string_scripts_text(translated_text)
    maps: Dict[str, Dict[str, str]] = {}
    for idx in range(min(len(origin_entries), len(translated_entries))):
        o = origin_entries[idx]
        t = translated_entries[idx]
        if o.marker != t.marker:
            continue
        marker_map = maps.setdefault(o.marker, {})
        if o.marker == "Choice":
            if isinstance(o.content, list) and isinstance(t.content, list):
                for j in range(min(len(o.content), len(t.content))):
                    marker_map[o.content[j]] = t.content[j]
        elif isinstance(o.content, str) and isinstance(t.content, str):
            marker_map[o.content] = t.content
    return maps


def _translation_for(maps: Dict[str, Dict[str, str]], marker: str, original: str) -> Optional[str]:
    mapped = maps.get(marker, {}).get(original)
    if isinstance(mapped, str) and mapped.strip() != "" and mapped != original:
        return _normalize_newlines(mapped)
    return None


def _is_our_marker_command(cmd: Any) -> Tuple[Optional[str], Any]:
    code, _indent, params = _event_command_fields(cmd)
    if code != 108 or not params:
        return None, None
    comment = str(params[0])
    decoded_message = _decode_message_marker(comment)
    if decoded_message is not None:
        return "message", decoded_message
    decoded_choices = _decode_choice_marker(comment)
    if decoded_choices is not None:
        return "choice", decoded_choices
    return None, None


def _update_event_command_list(cmd_list: Any, translation_maps: Dict[str, Dict[str, str]]) -> bool:
    if not isinstance(cmd_list, list):
        return False

    modified = False
    new_list: List[Any] = []
    i = 0
    while i < len(cmd_list):
        pending_kind, pending_payload = _is_our_marker_command(cmd_list[i])
        marker_cmd = cmd_list[i] if pending_kind else None
        if pending_kind:
            i += 1
            if i >= len(cmd_list):
                modified = True
                break

        cmd = cmd_list[i]
        code, indent, params = _event_command_fields(cmd)

        if code == 101:
            j = i + 1
            text_cmds: List[Any] = []
            while j < len(cmd_list):
                next_code, _next_indent, _next_params = _event_command_fields(cmd_list[j])
                if next_code != 401:
                    break
                text_cmds.append(cmd_list[j])
                j += 1

            original_text = "\n".join(_message_lines_from_commands(text_cmds))
            original_speaker = _get_speaker_name(params)
            had_marker = pending_kind == "message" and isinstance(pending_payload, dict)
            if had_marker:
                original_text = _normalize_newlines(pending_payload.get("text", ""))
                original_speaker = _normalize_newlines(pending_payload.get("speaker", ""))

            new_text = _translation_for(translation_maps, "Message", original_text)
            new_speaker = _translation_for(translation_maps, "SpeakerName", original_speaker)
            if new_text is not None or new_speaker is not None:
                marker = _new_event_command(108, indent, [_encode_message_marker(original_text, original_speaker)])
                new_list.append(marker)

                new_101 = copy.deepcopy(cmd)
                new_params = _set_speaker_name(params, new_speaker if new_speaker is not None else original_speaker)
                new_101["parameters"] = new_params
                new_list.append(new_101)

                final_text = new_text if new_text is not None else original_text
                if final_text != "" or text_cmds:
                    for line in final_text.split("\n"):
                        new_list.append(_new_event_command(401, indent, [line]))
                modified = True
            elif had_marker:
                restored_101 = copy.deepcopy(cmd)
                restored_101["parameters"] = _set_speaker_name(params, original_speaker)
                new_list.append(restored_101)
                if original_text != "" or text_cmds:
                    for line in original_text.split("\n"):
                        new_list.append(_new_event_command(401, indent, [line]))
                modified = True
            else:
                if marker_cmd is not None:
                    new_list.append(marker_cmd)
                new_list.append(cmd)
                new_list.extend(text_cmds)

            i = j
            continue

        if code == 401:
            text_cmds = [cmd]
            j = i + 1
            while j < len(cmd_list):
                next_code, _next_indent, _next_params = _event_command_fields(cmd_list[j])
                if next_code != 401:
                    break
                text_cmds.append(cmd_list[j])
                j += 1
            original_text = "\n".join(_message_lines_from_commands(text_cmds))
            had_marker = pending_kind == "message" and isinstance(pending_payload, dict)
            if had_marker:
                original_text = _normalize_newlines(pending_payload.get("text", ""))
            new_text = _translation_for(translation_maps, "Message", original_text)
            if new_text is not None:
                new_list.append(_new_event_command(108, indent, [_encode_message_marker(original_text, "")]))
                for line in new_text.split("\n"):
                    new_list.append(_new_event_command(401, indent, [line]))
                modified = True
            elif had_marker:
                for line in original_text.split("\n"):
                    new_list.append(_new_event_command(401, indent, [line]))
                modified = True
            else:
                if marker_cmd is not None:
                    new_list.append(marker_cmd)
                new_list.extend(text_cmds)
            i = j
            continue

        if code == 102:
            current_choices = params[0] if params else []
            if not isinstance(current_choices, list):
                current_choices = []
            had_marker = pending_kind == "choice" and isinstance(pending_payload, list)
            original_choices = [_normalize_newlines(str(x)) for x in (pending_payload if had_marker else current_choices)]

            new_choices: List[str] = []
            changed = False
            for choice in original_choices:
                mapped = _translation_for(translation_maps, "Choice", choice)
                if mapped is not None:
                    new_choices.append(mapped)
                    changed = True
                else:
                    new_choices.append(choice)

            if changed:
                new_list.append(_new_event_command(108, indent, [_encode_choice_marker(original_choices)]))
                new_102 = copy.deepcopy(cmd)
                new_params = list(params)
                if new_params:
                    new_params[0] = new_choices
                new_102["parameters"] = new_params
                new_list.append(new_102)
                modified = True
            elif had_marker:
                restored_102 = copy.deepcopy(cmd)
                restored_params = list(params)
                if restored_params:
                    restored_params[0] = original_choices
                restored_102["parameters"] = restored_params
                new_list.append(restored_102)
                modified = True
            else:
                if marker_cmd is not None:
                    new_list.append(marker_cmd)
                new_list.append(cmd)
            i += 1
            continue

        if marker_cmd is not None:
            # Drop stale adapter markers that are not attached to a supported command.
            modified = True
        new_list.append(cmd)
        i += 1

    if modified:
        cmd_list[:] = new_list
    return modified


def import_from_string_scripts(game_path: str, message_queue) -> int:
    data_dir = find_data_dir(game_path)
    if not data_dir:
        raise MVMZError("未找到 MV/MZ 数据目录（data/MapInfos.json 或 www/data/MapInfos.json）。")

    string_scripts_path = os.path.join(game_path, STRING_SCRIPTS_DIRNAME)
    backup_path = os.path.join(game_path, STRING_SCRIPTS_ORIGIN_DIRNAME)
    if not os.path.isdir(string_scripts_path) or not os.path.isdir(backup_path):
        raise MVMZError("未找到 StringScripts 或 StringScripts_Origin，请先执行步骤 1 导出文本。")

    modified_files = 0

    for filename in os.listdir(backup_path):
        if not re.match(r"^Map\d{3}\.txt$", filename, re.IGNORECASE):
            continue
        origin_file = os.path.join(backup_path, filename)
        translated_file = os.path.join(string_scripts_path, filename)
        if not os.path.isfile(translated_file):
            continue
        maps = _load_translation_maps_for_pair(origin_file, translated_file)
        if not maps:
            continue

        map_id = int(filename[3:6])
        map_path = os.path.join(data_dir, f"Map{map_id:03d}.json")
        if not os.path.isfile(map_path):
            continue
        map_obj = _load_json(map_path)
        events = map_obj.get("events") if isinstance(map_obj, dict) else None
        if not isinstance(events, list):
            continue

        touched = False
        for ev in events:
            if not isinstance(ev, dict):
                continue
            pages = ev.get("pages")
            if not isinstance(pages, list):
                continue
            for page in pages:
                if isinstance(page, dict) and _update_event_command_list(page.get("list", []), maps):
                    touched = True

        if touched:
            _save_json(map_path, map_obj)
            modified_files += 1

    common_origin = os.path.join(backup_path, "CommonEvents.txt")
    common_translated = os.path.join(string_scripts_path, "CommonEvents.txt")
    if os.path.isfile(common_origin) and os.path.isfile(common_translated):
        maps = _load_translation_maps_for_pair(common_origin, common_translated)
        if maps:
            common_path = os.path.join(data_dir, "CommonEvents.json")
            if os.path.isfile(common_path):
                common_events = _load_json(common_path)
                touched = False
                if isinstance(common_events, list):
                    for common_event in common_events:
                        if isinstance(common_event, dict) and _update_event_command_list(common_event.get("list", []), maps):
                            touched = True
                if touched:
                    _save_json(common_path, common_events)
                    modified_files += 1

    db_dir = os.path.join(string_scripts_path, "Database")
    if os.path.isdir(db_dir):
        modified_files += _import_database(game_path, data_dir, db_dir)

    return modified_files


def _load_translation_maps_for_pair(origin_file: str, translated_file: str) -> Dict[str, Dict[str, str]]:
    try:
        origin_text = open(origin_file, "r", encoding="utf-8-sig", errors="replace").read()
        translated_text = open(translated_file, "r", encoding="utf-8-sig", errors="replace").read()
    except Exception:
        return {}
    return _build_translation_maps(origin_text, translated_text)


def _parse_db_file(path: str) -> Dict[str, Any]:
    try:
        text = open(path, "r", encoding="utf-8-sig", errors="replace").read()
    except Exception:
        return {}
    result: Dict[str, Any] = {}
    for entry in _parse_string_scripts_text(text):
        result[entry.marker] = entry.content
    return result


_DB_ENTRY_SEPARATOR_RE = re.compile(r"^\*{5,}Entry(\d+)\*{5,}$", re.IGNORECASE)


def _parse_db_compact_entries(path: str) -> Dict[int, Dict[str, Any]]:
    try:
        lines = open(path, "r", encoding="utf-8-sig", errors="replace").read().splitlines(keepends=True)
    except Exception:
        return {}

    result: Dict[int, Dict[str, Any]] = {}
    current_id: Optional[int] = None
    buf: List[str] = []

    def flush() -> None:
        nonlocal buf, current_id
        if current_id is None:
            return
        text = "".join(buf)
        entry: Dict[str, Any] = {}
        for parsed in _parse_string_scripts_text(text):
            entry[parsed.marker] = parsed.content
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
        if current_id is not None:
            buf.append(line)

    flush()
    return result


def _import_database(game_path: str, data_dir: str, db_dir: str) -> int:
    modified_files = 0

    def import_array_table(folder_name: str, json_name: str, marker_to_attr: Dict[str, Tuple[str, bool]]) -> None:
        nonlocal modified_files
        compact_path = os.path.join(db_dir, folder_name, f"{folder_name}.txt")
        json_path = os.path.join(data_dir, json_name)
        if not os.path.isfile(compact_path) or not os.path.isfile(json_path):
            return
        entries = _parse_db_compact_entries(compact_path)
        if not entries:
            return
        data = _load_json(json_path)
        if not isinstance(data, list):
            return

        touched = False
        for idx, entry in entries.items():
            if idx <= 0 or idx >= len(data) or not isinstance(data[idx], dict):
                continue
            for marker, (attr, multiline_inline) in marker_to_attr.items():
                value = entry.get(marker)
                if isinstance(value, str):
                    new_value = _unescape_inline_newlines(value) if multiline_inline else value
                    if data[idx].get(attr) != new_value:
                        data[idx][attr] = new_value
                        touched = True
        if touched:
            _save_json(json_path, data)
            modified_files += 1

    import_array_table("Actors", "Actors.json", {"Name": ("name", False), "Nickname": ("nickname", False), "Profile": ("profile", True)})
    import_array_table("Classes", "Classes.json", {"Name": ("name", False)})
    import_array_table("Skills", "Skills.json", {"Name": ("name", False), "Description": ("description", True), "Message1": ("message1", False), "Message2": ("message2", False)})
    import_array_table("Items", "Items.json", {"Name": ("name", False), "Description": ("description", True)})
    import_array_table("Weapons", "Weapons.json", {"Name": ("name", False), "Description": ("description", True)})
    import_array_table("Armors", "Armors.json", {"Name": ("name", False), "Description": ("description", True)})
    import_array_table("Enemies", "Enemies.json", {"Name": ("name", False)})
    import_array_table(
        "States",
        "States.json",
        {"Name": ("name", False), "Message1": ("message1", False), "Message2": ("message2", False), "Message3": ("message3", False), "Message4": ("message4", False)},
    )

    modified_files += _import_map_infos(data_dir, db_dir)
    modified_files += _import_map_display_names(data_dir, db_dir)
    modified_files += _import_system(data_dir, db_dir)
    modified_files += _import_plugins(game_path, db_dir)
    modified_files += _import_metadata(game_path, db_dir)
    return modified_files


def _import_map_infos(data_dir: str, db_dir: str) -> int:
    path = os.path.join(db_dir, "MapInfos", "MapInfos.txt")
    json_path = os.path.join(data_dir, "MapInfos.json")
    if not os.path.isfile(path) or not os.path.isfile(json_path):
        return 0
    entries = _parse_db_compact_entries(path)
    if not entries:
        return 0
    map_infos = _load_json(json_path)
    if not isinstance(map_infos, list):
        return 0
    by_id = {entry.get("id"): entry for entry in map_infos if isinstance(entry, dict)}
    touched = False
    for map_id, entry in entries.items():
        info = by_id.get(map_id)
        if isinstance(info, dict) and isinstance(entry.get("Name"), str):
            if info.get("name") != entry["Name"]:
                info["name"] = entry["Name"]
                touched = True
    if touched:
        _save_json(json_path, map_infos)
        return 1
    return 0


def _import_map_display_names(data_dir: str, db_dir: str) -> int:
    path = os.path.join(db_dir, "MapDisplayNames", "MapDisplayNames.txt")
    if not os.path.isfile(path):
        return 0
    entries = _parse_db_compact_entries(path)
    modified = 0
    for map_id, entry in entries.items():
        name = entry.get("Name")
        if not isinstance(name, str):
            continue
        map_path = os.path.join(data_dir, f"Map{map_id:03d}.json")
        if not os.path.isfile(map_path):
            continue
        map_obj = _load_json(map_path)
        if isinstance(map_obj, dict):
            if map_obj.get("displayName") != name:
                map_obj["displayName"] = name
                _save_json(map_path, map_obj)
                modified += 1
    return modified


def _import_system(data_dir: str, db_dir: str) -> int:
    path = os.path.join(db_dir, "System", "System.txt")
    system_path = os.path.join(data_dir, "System.json")
    if not os.path.isfile(path) or not os.path.isfile(system_path):
        return 0
    entry = _parse_db_file(path)
    if not entry:
        return 0
    system = _load_json(system_path)
    if not isinstance(system, dict):
        return 0

    touched = False

    def apply(marker: str, setter) -> None:
        nonlocal touched
        value = entry.get(marker)
        if isinstance(value, str):
            touched = setter(_unescape_inline_newlines(value)) or touched

    def set_dict_value(obj: Dict[str, Any], key: str, value: str) -> bool:
        if obj.get(key) == value:
            return False
        obj[key] = value
        return True

    def set_list_value(arr: List[Any], idx: int, value: str) -> bool:
        if idx < 0 or idx >= len(arr) or arr[idx] == value:
            return False
        arr[idx] = value
        return True

    apply("Name", lambda v: set_dict_value(system, "gameTitle", v))
    apply("CurrencyUnit", lambda v: set_dict_value(system, "currencyUnit", v))

    for json_key, marker_prefix in [
        ("armorTypes", "ArmorType"),
        ("elements", "Element"),
        ("equipTypes", "EquipType"),
        ("skillTypes", "SkillType"),
        ("weaponTypes", "WeaponType"),
    ]:
        arr = system.get(json_key)
        if not isinstance(arr, list):
            continue
        for idx in range(len(arr)):
            apply(f"{marker_prefix}{idx}", lambda v, arr=arr, idx=idx: set_list_value(arr, idx, v))

    terms = system.get("terms")
    if isinstance(terms, dict):
        for term_key, marker_prefix in [("basic", "TermBasic"), ("commands", "TermCommand"), ("params", "TermParam")]:
            arr = terms.get(term_key)
            if not isinstance(arr, list):
                continue
            for idx in range(len(arr)):
                apply(f"{marker_prefix}{idx}", lambda v, arr=arr, idx=idx: set_list_value(arr, idx, v))

        messages = terms.get("messages")
        if isinstance(messages, dict):
            for key in list(messages.keys()):
                apply(f"TermMessage_{key}", lambda v, messages=messages, key=key: set_dict_value(messages, key, v))

    if touched:
        _save_json(system_path, system)
        return 1
    return 0


def _import_plugins(game_path: str, db_dir: str) -> int:
    path = os.path.join(db_dir, "Plugins", "Plugins.txt")
    plugins_path = os.path.join(game_path, "js", "plugins.js")
    if not os.path.isfile(path) or not os.path.isfile(plugins_path):
        return 0
    entries = _parse_db_compact_entries(path)
    if not entries:
        return 0
    prefix, suffix, plugins = _load_plugins_js(plugins_path)
    touched = False
    for idx, entry in entries.items():
        if idx < 0 or idx >= len(plugins) or not isinstance(plugins[idx], dict):
            continue
        params = plugins[idx].get("parameters")
        if not isinstance(params, dict):
            continue
        for param_name in list(params.keys()):
            marker = f"Param_{_safe_marker_name(str(param_name))}"
            value = entry.get(marker)
            if isinstance(value, str):
                new_value = _unescape_inline_newlines(value)
                if params.get(param_name) != new_value:
                    params[param_name] = new_value
                    touched = True
    if touched:
        _save_plugins_js(plugins_path, prefix, suffix, plugins)
        return 1
    return 0


def _import_metadata(game_path: str, db_dir: str) -> int:
    path = os.path.join(db_dir, "Metadata", "Metadata.txt")
    if not os.path.isfile(path):
        return 0
    entry = _parse_db_file(path)
    if not entry:
        return 0
    modified_files = 0

    package_title = entry.get("PackageTitle")
    package_path = os.path.join(game_path, "package.json")
    if isinstance(package_title, str) and os.path.isfile(package_path):
        package = _load_json(package_path)
        if isinstance(package, dict):
            window = package.setdefault("window", {})
            if isinstance(window, dict):
                new_title = _unescape_inline_newlines(package_title)
                if window.get("title") != new_title:
                    window["title"] = new_title
                    _save_json(package_path, package)
                    modified_files += 1

    html_title = entry.get("HtmlTitle")
    index_path = os.path.join(game_path, "index.html")
    if isinstance(html_title, str) and os.path.isfile(index_path):
        html = open(index_path, "r", encoding="utf-8-sig", errors="replace").read()
        new_html, count = re.subn(
            r"(<title>)(.*?)(</title>)",
            lambda m: f"{m.group(1)}{_unescape_inline_newlines(html_title)}{m.group(3)}",
            html,
            count=1,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if count and new_html != html:
            with open(index_path, "w", encoding="utf-8", newline="\n") as f:
                f.write(new_html)
            modified_files += 1

    return modified_files
