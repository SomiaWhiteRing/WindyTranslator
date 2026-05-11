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
import ast
import math
import os
import re
import shutil
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from core.utils import file_system

log = logging.getLogger(__name__)


STRING_SCRIPTS_DIRNAME = "StringScripts"
STRING_SCRIPTS_ORIGIN_DIRNAME = "StringScripts_Origin"
ORIGINAL_DB_STORE_FILENAME = "RTA_MVMZ_ORIGINAL_DB.json"

MESSAGE_MARKER_PREFIX = "<RTA_MVMZ_ORIGINAL_MESSAGE:"
CHOICE_MARKER_PREFIX = "<RTA_MVMZ_ORIGINAL_CHOICE:"
SCROLL_MARKER_PREFIX = "<RTA_MVMZ_ORIGINAL_SCROLL:"
PLUGIN_COMMAND_MARKER_PREFIX = "<RTA_MVMZ_ORIGINAL_PLUGIN_COMMAND:"
PLUGIN_COMMAND_TEXT_MARKER_PREFIX = "<RTA_MVMZ_PLUGIN_COMMAND_TEXT:"
PLUGIN_PARAM_TEXT_MARKER_PREFIX = "<RTA_MVMZ_PLUGIN_PARAM_TEXT:"
PLUGIN_SCRIPT_TEXT_MARKER_PREFIX = "<RTA_MVMZ_PLUGIN_SCRIPT_TEXT:"
MARKER_SUFFIX = ">"
TM_NAMEPOP_PLUGIN_NAME = "TMNamePop"
EVENT_NOTE_TM_NAMEPOP_MARKER = "EventNote_TMNamePop_namePop"
JSON_STRING_TEXT_CODEC = "json_string_text"

JAPANESE_TEXT_RE = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff\uff66-\uff9f\u4e00-\u9fff]")


class MVMZError(RuntimeError):
    pass


@dataclass
class _ParsedEntry:
    marker: str
    content: Any


@dataclass(frozen=True)
class _PluginCommandArgSpec:
    path: Tuple[str, ...]
    codec: str = "text"


@dataclass(frozen=True)
class _NoteTagSpec:
    marker: str
    tags: Tuple[str, ...]
    plugins: Tuple[str, ...]


@dataclass(frozen=True)
class _MVMZMessageLayout:
    base_font_size: float = 26.0
    no_face_units: float = 60.0
    face_units: float = 47.0
    max_lines: int = 4
    icon_units: float = 36.0 / 13.0


@dataclass(frozen=True)
class _MVMZPluginImportContext:
    active_plugins: Set[str]
    questsystem_detail_units: Optional[float] = None
    message_layout: Optional[_MVMZMessageLayout] = None


@dataclass(frozen=True)
class _MVMZTextToken:
    raw: str
    code: str = ""
    arg: Optional[str] = None


_PLUGIN_COMMAND_TEXT_SPECS: Dict[str, Dict[str, Tuple[_PluginCommandArgSpec, ...]]] = {
    "BookReader": {
        "showBook": (_PluginCommandArgSpec(("textData",), "mz_note"),),
    },
    "LL_InfoPopupWIndow": {
        "showMessage": (_PluginCommandArgSpec(("messageText",), "text"),),
    },
    "Mano_CurrencyUnit": {
        "setWalletItem": (_PluginCommandArgSpec(("unit",), "text"),),
        "setWalletVariable": (_PluginCommandArgSpec(("unit",), "text"),),
    },
    "QuestSystem": {
        "ChangeDetail": (
            _PluginCommandArgSpec(("DetailNote",), "mz_note"),
            _PluginCommandArgSpec(("Detail",), "text"),
        ),
    },
}

_NOTE_TAG_SPECS_BY_JSON: Dict[str, Tuple[_NoteTagSpec, ...]] = {
    "Skills.json": (_NoteTagSpec("Note_ExtendDescription", ("拡張説明", "ExtendDesc"), ("DescriptionExtend",)),),
    "Items.json": (_NoteTagSpec("Note_ExtendDescription", ("拡張説明", "ExtendDesc"), ("DescriptionExtend",)),),
    "Weapons.json": (_NoteTagSpec("Note_ExtendDescription", ("拡張説明", "ExtendDesc"), ("DescriptionExtend",)),),
    "Armors.json": (_NoteTagSpec("Note_ExtendDescription", ("拡張説明", "ExtendDesc"), ("DescriptionExtend",)),),
    "States.json": (_NoteTagSpec("Note_StateDescription", ("説明",), ("Mano_StateWindowOnBattle",)),),
}

_PLUGIN_PARAM_PLUGIN_KEYS: Dict[str, Set[str]] = {
    "QuestSystem": {
        "title",
        "requester",
        "difficulty",
        "place",
        "timelimit",
        "detailnote",
        "hiddendetail",
        "text",
        "nothingquesttext",
        "requestertext",
        "rewardtext",
        "difficultytext",
        "placetext",
        "timelimittext",
        "orderingcounttext",
        "questordertext",
        "questcanceltext",
        "questreporttext",
        "getrewardtext",
        "reachedlimittext",
    },
    "TA_AdventureNoteMZ": {
        "advnotemenucommandname",
        "maineventheadertext",
        "maineventtitle",
        "maineventnote",
        "subeventtitle",
        "subeventstartnote",
        "subeventclearnote",
        "subeventprogressnote",
        "lockedsubeventtext",
        "subeventcleartext",
    },
    "SceneSoundTest": {
        "commandname",
        "name",
        "description",
    },
}

_QUESTSYSTEM_DETAIL_KEYS = {"detail", "detailnote", "hiddendetail", "hiddendetailnote"}
_QUESTSYSTEM_REWARD_SECTION_COLOR_RESET_RE = re.compile(
    r"(?<!\n)(\\C\[(?:0|default)\])\n?(?=(?:直接報酬|直接报酬|報酬|报酬|奖励|獎勵)\s*[:：])",
    re.IGNORECASE,
)

_RESOURCE_KEY_FRAGMENTS = (
    "audio",
    "background",
    "battler",
    "bgm",
    "bgs",
    "character",
    "color",
    "cursor",
    "face",
    "file",
    "filename",
    "font",
    "foreground",
    "icon",
    "image",
    "jacket",
    "motion",
    "movie",
    "picture",
    "se",
    "skin",
    "sound",
)

_NON_TEXT_KEY_FRAGMENTS = (
    "height",
    "id",
    "opacity",
    "origin",
    "pan",
    "pitch",
    "priority",
    "rate",
    "scale",
    "switch",
    "variable",
    "volume",
    "width",
    "x",
    "y",
)

_LOCALE_TEXT_KEYS = {"jp", "ja", "japanese"}

_PLUGIN_SCRIPT_TEXT_SOURCE_SPECS: Dict[str, Tuple[re.Pattern, ...]] = {
    "*": (
        re.compile(
            r"(?P<prefix>\baddCommand\s*\(\s*)(?P<quote>['\"])(?P<text>(?:\\.|(?!\2).)*?)(?P=quote)(?P<suffix>\s*,)",
            re.DOTALL,
        ),
    ),
    "Mano_InputConfig": (
        re.compile(
            r"(?P<prefix>\bnew\s+MultiLanguageText\s*\(\s*['\"](?:\\.|[^'\"])*?['\"]\s*,\s*)(?P<quote>['\"])(?P<text>(?:\\.|(?!\2).)*?)(?P=quote)(?P<suffix>\s*\))",
            re.DOTALL,
        ),
    ),
}


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


def _decode_mz_note_value(value: str) -> str:
    value = _normalize_newlines(value)
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == '"' and stripped[-1] == '"':
        try:
            decoded = json.loads(stripped)
            if isinstance(decoded, str):
                return _normalize_newlines(decoded)
        except Exception:
            pass
    return value


def _encode_mz_note_value(value: str, original_value: str) -> str:
    value = _normalize_newlines(value)
    original_stripped = (original_value or "").strip()
    if len(original_stripped) >= 2 and original_stripped[0] == '"' and original_stripped[-1] == '"':
        return json.dumps(value, ensure_ascii=False)
    return value


def _looks_like_json_string_literal(value: str) -> bool:
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    return len(stripped) >= 2 and stripped[0] == '"' and stripped[-1] == '"'


def _escape_raw_json_control_chars(value: str) -> str:
    out: List[str] = []
    for ch in value:
        if ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return "".join(out)


def _decode_json_string_text_value(value: str) -> str:
    value = _normalize_newlines(value)
    if not _looks_like_json_string_literal(value):
        return value
    stripped = value.strip()
    for candidate in (stripped, _escape_raw_json_control_chars(stripped)):
        try:
            decoded = json.loads(candidate)
            if isinstance(decoded, str):
                return _normalize_newlines(decoded)
        except Exception:
            pass
    return _normalize_newlines(stripped[1:-1])


def _json_string_literal_needs_control_repair(value: str) -> bool:
    if not _looks_like_json_string_literal(value):
        return False
    stripped = value.strip()
    try:
        return not isinstance(json.loads(stripped), str)
    except Exception:
        repaired = _escape_raw_json_control_chars(stripped)
        if repaired == stripped:
            return False
        try:
            return isinstance(json.loads(repaired), str)
        except Exception:
            return False


def _decode_text_codec(value: str, codec: str) -> str:
    if codec == JSON_STRING_TEXT_CODEC:
        return _decode_json_string_text_value(value)
    if codec == "mz_note":
        return _decode_mz_note_value(value)
    return _normalize_newlines(value)


def _encode_text_codec(value: str, original_value: str, codec: str) -> str:
    if codec == JSON_STRING_TEXT_CODEC:
        return json.dumps(_normalize_newlines(value), ensure_ascii=False)
    if codec == "mz_note":
        return _encode_mz_note_value(value, original_value)
    return _normalize_newlines(value)


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


def _get_original_text_for_codec(
    store: Dict[str, Any],
    key: str,
    current_value: str,
    codec: str,
) -> Tuple[str, bool]:
    original, created = _get_original_text(store, key, current_value)
    if (
        not created
        and codec == JSON_STRING_TEXT_CODEC
        and _looks_like_json_string_literal(original)
        and not _looks_like_json_string_literal(current_value)
    ):
        normalized = _decode_json_string_text_value(original)
        if normalized and normalized != original:
            store[key] = normalized
            return normalized, True
    return original, created


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


def _encode_scroll_marker(original_text: str) -> str:
    return _encode_marker(SCROLL_MARKER_PREFIX, _normalize_newlines(original_text))


def _decode_scroll_marker(comment: str) -> Optional[str]:
    decoded = _decode_marker(comment, SCROLL_MARKER_PREFIX)
    return decoded if isinstance(decoded, str) else None


def _encode_plugin_command_marker(original_values: Dict[str, str], codecs: Optional[Dict[str, str]] = None) -> str:
    return _encode_marker(
        PLUGIN_COMMAND_MARKER_PREFIX,
        {
            "values": {str(k): _normalize_newlines(v) for k, v in original_values.items() if isinstance(v, str)},
            "codecs": {str(k): str(v) for k, v in (codecs or {}).items() if isinstance(v, str)},
        },
    )


def _decode_plugin_command_marker(comment: str) -> Optional[Dict[str, Any]]:
    decoded = _decode_marker(comment, PLUGIN_COMMAND_MARKER_PREFIX)
    if isinstance(decoded, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in decoded.items()):
        return {"values": decoded, "codecs": {}}
    if isinstance(decoded, dict) and isinstance(decoded.get("values"), dict):
        values = decoded.get("values")
        codecs = decoded.get("codecs", {})
        if (
            isinstance(values, dict)
            and all(isinstance(k, str) and isinstance(v, str) for k, v in values.items())
            and isinstance(codecs, dict)
            and all(isinstance(k, str) and isinstance(v, str) for k, v in codecs.items())
        ):
            return {"values": values, "codecs": codecs}
    return None


def _encode_plugin_command_text_marker(payload: Any) -> str:
    return _encode_marker(PLUGIN_COMMAND_TEXT_MARKER_PREFIX, payload)


def _decode_plugin_command_text_marker(marker: str) -> Optional[Any]:
    return _decode_marker(marker, PLUGIN_COMMAND_TEXT_MARKER_PREFIX)


def _encode_plugin_param_text_marker(payload: Any) -> str:
    return _encode_marker(PLUGIN_PARAM_TEXT_MARKER_PREFIX, payload)


def _decode_plugin_param_text_marker(marker: str) -> Optional[Any]:
    return _decode_marker(marker, PLUGIN_PARAM_TEXT_MARKER_PREFIX)


def _encode_plugin_script_text_marker(payload: Any) -> str:
    return _encode_marker(PLUGIN_SCRIPT_TEXT_MARKER_PREFIX, payload)


def _decode_plugin_script_text_marker(marker: str) -> Optional[Any]:
    return _decode_marker(marker, PLUGIN_SCRIPT_TEXT_MARKER_PREFIX)


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


def _scroll_lines_from_commands(cmds: List[Any]) -> List[str]:
    lines: List[str] = []
    for cmd in cmds:
        code, _indent, params = _event_command_fields(cmd)
        if code != 405:
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


def _emit_multiline_entry(out_lines: List[str], marker: str, text: str) -> None:
    text = _normalize_newlines(text)
    if not text:
        return
    out_lines.append(f"#{marker}#\n")
    for line in text.split("\n"):
        out_lines.append(f"{line}\n")
    out_lines.append("##\n")


def _emit_plugin_command_lines(out_lines: List[str], marker: str, text: str) -> None:
    _emit_multiline_entry(out_lines, marker, text)


def _export_command_list_to_lines(cmd_list: Any, active_plugins: Optional[Set[str]] = None) -> List[str]:
    if not isinstance(cmd_list, list):
        return []

    lines: List[str] = []
    pending_message_marker: Optional[Dict[str, str]] = None
    pending_choice_marker: Optional[List[str]] = None
    pending_scroll_marker: Optional[str] = None
    pending_plugin_command_marker: Optional[Dict[str, str]] = None
    at_leading_comments = True
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
            decoded_scroll = _decode_scroll_marker(comment)
            if decoded_scroll is not None:
                pending_scroll_marker = decoded_scroll
                i += 1
                continue
            decoded_plugin_command = _decode_plugin_command_marker(comment)
            if decoded_plugin_command is not None:
                pending_plugin_command_marker = decoded_plugin_command
                i += 1
                continue

        if code in (108, 408) and params and at_leading_comments and _tm_namepop_enabled(active_plugins):
            namepop_name = _extract_tm_namepop_name_from_note(str(params[0]))
            if isinstance(namepop_name, str) and namepop_name and JAPANESE_TEXT_RE.search(namepop_name):
                _append_single_entry(lines, EVENT_NOTE_TM_NAMEPOP_MARKER, namepop_name)

        if code == 101:
            at_leading_comments = False
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
            at_leading_comments = False
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
            at_leading_comments = False
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

        if code == 105:
            at_leading_comments = False
            j = i + 1
            text_cmds: List[Any] = []
            while j < len(cmd_list):
                next_code, _next_indent, _next_params = _event_command_fields(cmd_list[j])
                if next_code != 405:
                    break
                text_cmds.append(cmd_list[j])
                j += 1
            scroll_text = "\n".join(_scroll_lines_from_commands(text_cmds))
            if pending_scroll_marker is not None:
                scroll_text = pending_scroll_marker
                pending_scroll_marker = None
            _emit_multiline_entry(lines, "ScrollText", scroll_text)
            i = j
            continue

        if code == 357:
            at_leading_comments = False
            plugin_values = _plugin_command_translatable_values(params, active_plugins)
            if pending_plugin_command_marker is not None:
                pending_values = pending_plugin_command_marker.get("values", {})
                pending_codecs = pending_plugin_command_marker.get("codecs", {})
                if not isinstance(pending_values, dict):
                    pending_values = {}
                if not isinstance(pending_codecs, dict):
                    pending_codecs = {}
                for key, original in pending_values.items():
                    if not isinstance(key, str) or not isinstance(original, str):
                        continue
                    if key in plugin_values:
                        codec = plugin_values[key][1]
                    else:
                        codec = pending_codecs.get(key, "text") if isinstance(pending_codecs.get(key), str) else "text"
                    marker = _plugin_command_marker_name(params, key)
                    _emit_plugin_command_lines(lines, marker, _decode_text_codec(original, codec))
                pending_plugin_command_marker = None
            else:
                for key, (value, codec) in plugin_values.items():
                    marker = _plugin_command_marker_name(params, key)
                    _emit_plugin_command_lines(lines, marker, _decode_text_codec(value, codec))
            i += 1
            continue

        if code not in (108, 408):
            at_leading_comments = False
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
    active_plugins = _active_plugin_names(game_path)

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
            note = ev.get("note")
            if _tm_namepop_enabled(active_plugins) and isinstance(note, str):
                namepop_name = _extract_tm_namepop_name_from_note(note)
                if isinstance(namepop_name, str) and namepop_name:
                    original, created = _get_original_text(
                        original_store,
                        f"Map{map_id:03d}.json:events:{ev_id}:note:namePop",
                        namepop_name,
                    )
                    original_store_modified = original_store_modified or created
                    _append_single_entry(entry_lines, EVENT_NOTE_TM_NAMEPOP_MARKER, original)
            for page_idx, page in enumerate(pages):
                if not isinstance(page, dict):
                    continue
                cmd_list = page.get("list", [])
                page_lines = _export_command_list_to_lines(cmd_list, active_plugins)
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
        common_lines = _export_common_events(common_events, active_plugins)
        if common_lines:
            _write_text_file(os.path.join(string_scripts_path, "CommonEvents.txt"), common_lines)
            message_queue.put(("log", ("success", "公共事件对话导出完成：CommonEvents.txt")))

    troops_path = os.path.join(data_dir, "Troops.json")
    if os.path.isfile(troops_path):
        troops = _load_json(troops_path)
        troop_lines = _export_troops(troops, active_plugins)
        if troop_lines:
            _write_text_file(os.path.join(string_scripts_path, "Troops.txt"), troop_lines)
            message_queue.put(("log", ("success", "战斗事件对话导出完成：Troops.txt")))

    db_modified, db_files = _export_database(
        game_path=game_path,
        data_dir=data_dir,
        map_infos=map_infos,
        out_root=os.path.join(string_scripts_path, "Database"),
        original_store=original_store,
        active_plugins=active_plugins,
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


def _export_common_events(common_events: Any, active_plugins: Optional[Set[str]] = None) -> List[str]:
    if not isinstance(common_events, list):
        return []
    out_lines: List[str] = []
    for common_event in common_events:
        if not isinstance(common_event, dict):
            continue
        ce_id = common_event.get("id")
        cmd_list = common_event.get("list", [])
        page_lines = _export_command_list_to_lines(cmd_list, active_plugins)
        if page_lines and isinstance(ce_id, int):
            out_lines.append(f"*****Entry{ce_id}*****\n")
            out_lines.append("-----Page1-----\n")
            out_lines.extend(page_lines)
    return out_lines


def _export_troops(troops: Any, active_plugins: Optional[Set[str]] = None) -> List[str]:
    if not isinstance(troops, list):
        return []
    out_lines: List[str] = []
    for troop in troops:
        if not isinstance(troop, dict):
            continue
        troop_id = troop.get("id")
        pages = troop.get("pages")
        if not isinstance(troop_id, int) or not isinstance(pages, list):
            continue
        entry_lines: List[str] = [f"*****Entry{troop_id}*****\n"]
        for page_idx, page in enumerate(pages):
            if not isinstance(page, dict):
                continue
            page_lines = _export_command_list_to_lines(page.get("list", []), active_plugins)
            if page_lines:
                entry_lines.append(f"-----Page{page_idx + 1}-----\n")
                entry_lines.extend(page_lines)
        if any(line.startswith("#") for line in entry_lines):
            out_lines.extend(entry_lines)
    return out_lines


def _export_database(
    *,
    game_path: str,
    data_dir: str,
    map_infos: Any,
    out_root: str,
    original_store: Dict[str, Any],
    active_plugins: Set[str],
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
            note = obj.get("note")
            if isinstance(note, str):
                for marker, text in _extract_note_texts(json_name, note, active_plugins):
                    add_entry_line(
                        entry_lines,
                        marker,
                        f"{json_name}:{idx}:note:{marker}",
                        text,
                        True,
                    )
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

    plugin_script_files = _export_plugin_scripts(game_path, out_root, original_store, active_plugins)
    if plugin_script_files[0]:
        original_store_modified = True
    exported_files += plugin_script_files[1]

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


def _try_parse_json_string(value: str) -> Optional[Any]:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped or stripped[0] not in "[{\"":
        return None
    try:
        return json.loads(stripped)
    except Exception:
        return None


def _plugin_param_allowed_leaf(plugin_name: str, path: Sequence[str]) -> bool:
    if not path:
        return False
    leaf = path[-1].lower()
    full = ".".join(path).lower()
    allowed = _PLUGIN_PARAM_PLUGIN_KEYS.get(plugin_name, set())
    if leaf in _LOCALE_TEXT_KEYS:
        return True
    if leaf in allowed or full in allowed:
        return True
    if leaf.endswith(("text", "message", "description", "desc", "title", "name", "note", "help")):
        if any(fragment in leaf for fragment in _RESOURCE_KEY_FRAGMENTS):
            return False
        return True
    return False


def _plugin_param_excluded_leaf(path: Sequence[str]) -> bool:
    if not path:
        return True
    leaf = path[-1].lower()
    if any(fragment in leaf for fragment in _RESOURCE_KEY_FRAGMENTS):
        return True
    if any(fragment == leaf or leaf.endswith(fragment) for fragment in _NON_TEXT_KEY_FRAGMENTS):
        return True
    return False


def _plugin_param_excluded_path(plugin_name: str, path: Sequence[str]) -> bool:
    if _plugin_param_excluded_leaf(path):
        return True
    normalized = tuple(str(part).lower() for part in path)
    if not normalized:
        return True
    leaf = normalized[-1]

    # Font registration names are runtime identifiers. Plugins such as
    # FontLoad store them as generic "name" fields under a font list, so the
    # leaf alone looks translatable even though changing it breaks font lookup.
    if leaf in {"name", "family"} and any("font" in part for part in normalized[:-1]):
        return True
    if plugin_name == "FontLoad" and leaf in {"name", "family"}:
        return True
    return False


def _plugin_param_key_path_from_marker(param_name: str, value_path: Sequence[Any]) -> Tuple[str, ...]:
    path: List[str] = [str(param_name)] if param_name else []
    for step in value_path:
        if isinstance(step, str) and step != "{}":
            path.append(step)
    return tuple(path)


def _extract_plugin_param_texts(plugin_name: str, param_name: str, value: str) -> List[Tuple[Tuple[Any, ...], str, str]]:
    result: List[Tuple[Tuple[Any, ...], str, str]] = []
    seen: Set[Tuple[Any, ...]] = set()
    root_key_path = (str(param_name),) if param_name else tuple()

    def add(path: Tuple[Any, ...], text: str, codec: str = "text") -> None:
        if path in seen:
            return
        text = _decode_text_codec(text, codec)
        if text and JAPANESE_TEXT_RE.search(text):
            seen.add(path)
            result.append((path, text, codec))

    def walk(obj: Any, path: Tuple[Any, ...], key_path: Tuple[str, ...]) -> None:
        if isinstance(obj, dict):
            for key, child in obj.items():
                key_text = str(key)
                walk(child, path + (key_text,), key_path + (key_text,))
            return
        if isinstance(obj, list):
            for idx, child in enumerate(obj):
                walk(child, path + (idx,), key_path)
            return
        if not isinstance(obj, str):
            return

        nested = _try_parse_json_string(obj)
        if isinstance(nested, (dict, list)):
            walk(nested, path + ("{}",), key_path)
            return
        if isinstance(nested, str) or (nested is None and _looks_like_json_string_literal(obj)):
            decoded = _decode_json_string_text_value(obj)
            if not _is_translatable_plugin_value(decoded):
                return
            if _plugin_param_excluded_path(plugin_name, key_path):
                return
            if _plugin_param_allowed_leaf(plugin_name, key_path):
                add(path, obj, JSON_STRING_TEXT_CODEC)
            return

        if not _is_translatable_plugin_value(obj):
            return
        if _plugin_param_excluded_path(plugin_name, key_path):
            return
        if _plugin_param_allowed_leaf(plugin_name, key_path):
            codec = "mz_note" if key_path and key_path[-1].lower().endswith("note") else "text"
            add(path, obj, codec)

    parsed = _try_parse_json_string(value)
    if isinstance(parsed, (dict, list)):
        walk(parsed, tuple(), root_key_path)
    elif _is_translatable_plugin_value(value):
        if not _plugin_param_excluded_path(plugin_name, root_key_path):
            add(tuple(), value)
    return result


def _decode_plugin_param_value(value: str, path: Sequence[Any]) -> Optional[str]:
    def walk(current: Any, remaining: Sequence[Any]) -> Optional[str]:
        if not remaining:
            return current if isinstance(current, str) else None
        step = remaining[0]
        rest = remaining[1:]
        if step == "{}":
            if not isinstance(current, str):
                return None
            parsed = _try_parse_json_string(current)
            if parsed is None:
                return None
            return walk(parsed, rest)
        if isinstance(step, int):
            if isinstance(current, list) and 0 <= step < len(current):
                return walk(current[step], rest)
            return None
        if isinstance(current, dict):
            return walk(current.get(str(step)), rest)
        return None

    if not path:
        return value
    parsed_root = _try_parse_json_string(value)
    if parsed_root is None:
        return None
    return walk(parsed_root, path)


def _replace_plugin_param_value(value: str, path: Sequence[Any], new_text: str) -> Optional[str]:
    if not path:
        return new_text

    def walk(current: Any, remaining: Sequence[Any]) -> Tuple[Any, bool]:
        if not remaining:
            if isinstance(current, str) and current != new_text:
                return new_text, True
            return current, False
        step = remaining[0]
        rest = remaining[1:]
        if step == "{}":
            if not isinstance(current, str):
                return current, False
            parsed = _try_parse_json_string(current)
            if parsed is None:
                return current, False
            updated, changed = walk(parsed, rest)
            if not changed:
                return current, False
            return json.dumps(updated, ensure_ascii=False, separators=(",", ":")), True
        if isinstance(step, int):
            if not isinstance(current, list) or step < 0 or step >= len(current):
                return current, False
            updated_child, changed = walk(current[step], rest)
            if not changed:
                return current, False
            new_list = list(current)
            new_list[step] = updated_child
            return new_list, True
        if not isinstance(current, dict) or str(step) not in current:
            return current, False
        updated_child, changed = walk(current[str(step)], rest)
        if not changed:
            return current, False
        new_dict = dict(current)
        new_dict[str(step)] = updated_child
        return new_dict, True

    parsed_root = _try_parse_json_string(value)
    if parsed_root is None:
        return None
    updated_root, changed = walk(parsed_root, path)
    if not changed:
        return value
    return json.dumps(updated_root, ensure_ascii=False, separators=(",", ":"))


def _repair_plugin_param_json_string_literals(value: str) -> Tuple[str, bool]:
    parsed_root = _try_parse_json_string(value)
    if parsed_root is None:
        if _json_string_literal_needs_control_repair(value):
            return json.dumps(_decode_json_string_text_value(value), ensure_ascii=False), True
        return value, False

    def walk(current: Any) -> Tuple[Any, bool]:
        if isinstance(current, dict):
            changed = False
            updated: Dict[Any, Any] = {}
            for key, child in current.items():
                updated_child, child_changed = walk(child)
                updated[key] = updated_child
                changed = changed or child_changed
            return (updated if changed else current), changed
        if isinstance(current, list):
            changed = False
            updated_list: List[Any] = []
            for child in current:
                updated_child, child_changed = walk(child)
                updated_list.append(updated_child)
                changed = changed or child_changed
            return (updated_list if changed else current), changed
        if not isinstance(current, str):
            return current, False

        nested = _try_parse_json_string(current)
        if isinstance(nested, (dict, list)):
            updated_nested, changed = walk(nested)
            if changed:
                return json.dumps(updated_nested, ensure_ascii=False, separators=(",", ":")), True
            return current, False
        if _json_string_literal_needs_control_repair(current):
            return json.dumps(_decode_json_string_text_value(current), ensure_ascii=False), True
        return current, False

    updated_root, changed = walk(parsed_root)
    if not changed:
        return value, False
    return json.dumps(updated_root, ensure_ascii=False, separators=(",", ":")), True


def _replace_plugin_param_translation(value: str, path: Sequence[Any], translated_text: str, codec: str) -> Optional[str]:
    original_raw = _decode_plugin_param_value(value, path)
    if original_raw is None:
        return None
    effective_codec = codec
    if codec == "text" and _looks_like_json_string_literal(original_raw):
        effective_codec = JSON_STRING_TEXT_CODEC
        if _looks_like_json_string_literal(translated_text):
            translated_text = _decode_json_string_text_value(translated_text)
    encoded = _encode_text_codec(translated_text, original_raw, effective_codec)
    return _replace_plugin_param_value(value, path, encoded)


def _plugin_param_leaf_from_path(path: Sequence[Any]) -> str:
    for step in reversed(path):
        if isinstance(step, str) and step != "{}":
            return step.lower()
    return ""


def _normalize_questsystem_detail_for_wrap(text: str) -> str:
    normalized = _normalize_newlines(text)
    return _QUESTSYSTEM_REWARD_SECTION_COLOR_RESET_RE.sub(r"\n\1", normalized)


def _wrap_questsystem_detail_text(
    text: str,
    *,
    limit_units: Optional[float],
    layout: Optional[_MVMZMessageLayout],
) -> str:
    normalized = _normalize_questsystem_detail_for_wrap(text)
    if not limit_units or limit_units <= 0:
        return normalized
    effective_layout = layout or _default_mvmz_message_layout()
    wrapped_lines: List[str] = []
    for line in normalized.split("\n"):
        wrapped_lines.extend(_wrap_mvmz_message_line(line, limit_units=limit_units, layout=effective_layout))
    return "\n".join(wrapped_lines)


def _postprocess_plugin_param_translation(
    plugin_name: str,
    path: Sequence[Any],
    translated_text: str,
    context: Optional[_MVMZPluginImportContext] = None,
) -> str:
    if (
        plugin_name == "QuestSystem"
        and _plugin_param_leaf_from_path(path) in _QUESTSYSTEM_DETAIL_KEYS
        and (context is None or plugin_name in context.active_plugins)
    ):
        return _wrap_questsystem_detail_text(
            translated_text,
            limit_units=context.questsystem_detail_units if context else None,
            layout=context.message_layout if context else None,
        )
    return translated_text


def _active_plugin_names(game_path: str) -> Set[str]:
    plugins_path = os.path.join(game_path, "js", "plugins.js")
    if not os.path.isfile(plugins_path):
        return set()
    try:
        _prefix, _suffix, plugins = _load_plugins_js(plugins_path)
    except Exception:
        return set()
    names: Set[str] = set()
    for plugin in plugins:
        if not isinstance(plugin, dict) or plugin.get("status") is False:
            continue
        name = plugin.get("name")
        if isinstance(name, str) and name:
            names.add(name)
    return names


def _get_nested_value(obj: Any, path: Sequence[str]) -> Optional[str]:
    current = obj
    for part in path:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current if isinstance(current, str) else None


def _set_nested_value(obj: Any, path: Sequence[str], value: str) -> bool:
    current = obj
    for part in path[:-1]:
        if not isinstance(current, dict):
            return False
        current = current.get(part)
    if not isinstance(current, dict) or not path:
        return False
    key = path[-1]
    if current.get(key) == value:
        return False
    current[key] = value
    return True


def _plugin_command_specs(plugin_name: str, command_name: str, active_plugins: Optional[Set[str]] = None) -> Tuple[_PluginCommandArgSpec, ...]:
    if active_plugins is not None and plugin_name not in active_plugins:
        return ()
    by_command = _PLUGIN_COMMAND_TEXT_SPECS.get(plugin_name)
    if not by_command:
        return ()
    return by_command.get(command_name, ())


def _plugin_command_translatable_values(params: List[Any], active_plugins: Optional[Set[str]] = None) -> Dict[str, Tuple[str, str]]:
    plugin_name = str(params[0]) if len(params) > 0 and isinstance(params[0], str) else ""
    command_name = str(params[1]) if len(params) > 1 and isinstance(params[1], str) else ""
    args = params[3] if len(params) > 3 and isinstance(params[3], dict) else {}
    if not plugin_name or not command_name or not isinstance(args, dict):
        return {}
    result: Dict[str, Tuple[str, str]] = {}
    for spec in _plugin_command_specs(plugin_name, command_name, active_plugins):
        raw = _get_nested_value(args, spec.path)
        if not isinstance(raw, str) or raw == "":
            continue
        text = _decode_text_codec(raw, spec.codec)
        if JAPANESE_TEXT_RE.search(text):
            key = ".".join(spec.path)
            result[key] = (raw, spec.codec)
    return result


def _plugin_command_marker_name(params: List[Any], arg_key: str) -> str:
    plugin_name = str(params[0]) if len(params) > 0 and isinstance(params[0], str) else "Plugin"
    command_name = str(params[1]) if len(params) > 1 and isinstance(params[1], str) else "Command"
    return f"PluginCommand_{_safe_marker_name(plugin_name)}_{_safe_marker_name(command_name)}_{_safe_marker_name(arg_key)}"


def _js_string_literal_value(raw: str, quote: str) -> Optional[str]:
    if quote not in {"'", '"'}:
        return None
    try:
        return ast.literal_eval(f"{quote}{raw}{quote}")
    except Exception:
        return None


def _js_quote_string(value: str, quote: str) -> str:
    quote = quote if quote in {"'", '"'} else '"'
    escaped = _normalize_newlines(value)
    escaped = escaped.replace("\\", "\\\\")
    escaped = escaped.replace("\n", "\\n")
    escaped = escaped.replace("\t", "\\t")
    escaped = escaped.replace("\r", "\\r")
    escaped = escaped.replace(quote, "\\" + quote)
    return f"{quote}{escaped}{quote}"


def _plugin_script_text_specs(plugin_name: str) -> Tuple[re.Pattern, ...]:
    specs = list(_PLUGIN_SCRIPT_TEXT_SOURCE_SPECS.get("*", ()))
    specs.extend(_PLUGIN_SCRIPT_TEXT_SOURCE_SPECS.get(plugin_name, ()))
    return tuple(specs)


def _extract_plugin_script_texts(plugin_name: str, source: str) -> List[Tuple[int, str, str]]:
    result: List[Tuple[int, str, str]] = []
    seen: Set[Tuple[int, str]] = set()
    for regex in _plugin_script_text_specs(plugin_name):
        for match in regex.finditer(source):
            raw = match.group("text")
            quote = match.group("quote")
            text = _js_string_literal_value(raw, quote)
            if not isinstance(text, str) or not _is_translatable_plugin_value(text):
                continue
            key = (match.start("text"), text)
            if key in seen:
                continue
            seen.add(key)
            result.append((match.start("text"), text, quote))
    result.sort(key=lambda item: item[0])
    return result


def _replace_plugin_script_text_at(source: str, offset: int, original: str, translated: str) -> Optional[str]:
    if offset < 1 or offset >= len(source):
        return None
    quote = source[offset - 1]
    if quote not in {"'", '"'}:
        return None
    idx = offset
    escaped = False
    while idx < len(source):
        ch = source[idx]
        if escaped:
            escaped = False
            idx += 1
            continue
        if ch == "\\":
            escaped = True
            idx += 1
            continue
        if ch == quote:
            raw = source[offset:idx]
            decoded = _js_string_literal_value(raw, quote)
            if decoded != original and decoded == translated:
                return source
            if decoded != original:
                return None
            return source[: offset - 1] + _js_quote_string(translated, quote) + source[idx + 1 :]
        idx += 1
    return None


def _replace_plugin_script_text_near(source: str, plugin_name: str, offset: int, original: str, translated: str) -> Optional[str]:
    candidates: List[Tuple[int, int]] = []
    already_translated = False
    for regex in _plugin_script_text_specs(plugin_name):
        for match in regex.finditer(source):
            raw = match.group("text")
            quote = match.group("quote")
            text = _js_string_literal_value(raw, quote)
            if text == original:
                candidates.append((abs(match.start("text") - offset), match.start("text")))
            elif text == translated:
                already_translated = True
    if not candidates:
        return source if already_translated else None
    _distance, candidate_offset = min(candidates, key=lambda item: item[0])
    return _replace_plugin_script_text_at(source, candidate_offset, original, translated)


def _as_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if math.isfinite(float(value)):
            return float(value)
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            parsed = float(stripped)
        except Exception:
            return None
        if math.isfinite(parsed):
            return parsed
    return None


def _simple_js_numeric_expr(expr: str, names: Dict[str, float]) -> Optional[float]:
    if not isinstance(expr, str) or not expr.strip():
        return None

    def name_from_node(node: ast.AST) -> Optional[str]:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            base = name_from_node(node.value)
            return f"{base}.{node.attr}" if base else None
        return None

    def eval_node(node: ast.AST) -> Optional[float]:
        if isinstance(node, ast.Expression):
            return eval_node(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return float(node.value)
        if isinstance(node, (ast.Name, ast.Attribute)):
            key = name_from_node(node)
            return names.get(key or "")
        if isinstance(node, ast.UnaryOp):
            operand = eval_node(node.operand)
            if operand is None:
                return None
            if isinstance(node.op, ast.UAdd):
                return operand
            if isinstance(node.op, ast.USub):
                return -operand
            return None
        if isinstance(node, ast.BinOp):
            left = eval_node(node.left)
            right = eval_node(node.right)
            if left is None or right is None:
                return None
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return None if right == 0 else left / right
            return None
        return None

    try:
        parsed = ast.parse(expr, mode="eval")
    except SyntaxError:
        return _as_float(expr)
    result = eval_node(parsed)
    return result if result is not None and math.isfinite(result) else None


def _mvmz_graphics_box_width(system: Dict[str, Any]) -> float:
    advanced = system.get("advanced") if isinstance(system.get("advanced"), dict) else {}
    ui_width = _as_float(advanced.get("uiAreaWidth")) if isinstance(advanced, dict) else None
    screen_width = _as_float(advanced.get("screenWidth")) if isinstance(advanced, dict) else None
    if ui_width is None:
        ui_width = _as_float(system.get("screenWidth"))
    if screen_width is None:
        screen_width = _as_float(system.get("screenWidth"))
    if ui_width is not None and ui_width > 8:
        return ui_width - 8
    if screen_width is not None and screen_width > 0:
        return screen_width
    return 808.0


def _message_units_from_window_width(
    window_width: float,
    *,
    base_font_size: float,
    face_size: float,
    padding: float = 12.0,
) -> Tuple[float, float]:
    half_unit_pixels = max(base_font_size / 2.0, 1.0)
    contents_width = max(window_width - padding * 2.0, 1.0)
    no_face_pixels = max(contents_width - 4.0, 1.0)
    face_pixels = max(contents_width - (face_size + 20.0), 1.0)
    return (
        max(1.0, math.floor(no_face_pixels / half_unit_pixels)),
        max(1.0, math.floor(face_pixels / half_unit_pixels)),
    )


def _default_mvmz_message_layout() -> _MVMZMessageLayout:
    base_font_size = 26.0
    return _MVMZMessageLayout(
        base_font_size=base_font_size,
        no_face_units=60.0,
        face_units=47.0,
        max_lines=4,
        icon_units=36.0 / (base_font_size / 2.0),
    )


def _load_mvmz_message_layout(game_path: str, data_dir: str) -> _MVMZMessageLayout:
    layout = _default_mvmz_message_layout()
    system_path = os.path.join(data_dir, "System.json")
    system: Dict[str, Any] = {}
    if os.path.isfile(system_path):
        try:
            loaded = _load_json(system_path)
            if isinstance(loaded, dict):
                system = loaded
        except Exception as e:
            log.warning(f"读取 System.json 失败，使用默认消息窗口宽度: {system_path} - {e}")

    advanced = system.get("advanced") if isinstance(system.get("advanced"), dict) else {}
    editor = system.get("editor") if isinstance(system.get("editor"), dict) else {}
    base_font_size = _as_float(advanced.get("fontSize")) if isinstance(advanced, dict) else None
    base_font_size = base_font_size if base_font_size and base_font_size > 0 else layout.base_font_size
    no_face_units = _as_float(editor.get("messageWidth1")) if isinstance(editor, dict) else None
    face_units = _as_float(editor.get("messageWidth2")) if isinstance(editor, dict) else None
    no_face_units = no_face_units if no_face_units and no_face_units > 0 else layout.no_face_units
    face_units = face_units if face_units and face_units > 0 else layout.face_units
    face_size = _as_float(system.get("faceSize")) or 144.0
    icon_size = _as_float(system.get("iconSize")) or 32.0

    plugins_path = os.path.join(game_path, "js", "plugins.js")
    if os.path.isfile(plugins_path):
        try:
            _prefix, _suffix, plugins = _load_plugins_js(plugins_path)
        except Exception as e:
            log.warning(f"读取 plugins.js 失败，跳过消息窗口插件适配: {plugins_path} - {e}")
            plugins = []

        for plugin in plugins:
            if not isinstance(plugin, dict) or plugin.get("status") is False:
                continue
            if plugin.get("name") != "NRP_MessageWindow":
                continue
            params = plugin.get("parameters")
            if not isinstance(params, dict):
                continue
            plugin_font_size = _as_float(params.get("MessageFontSize"))
            if plugin_font_size and plugin_font_size > 0:
                base_font_size = plugin_font_size
            window_width_expr = params.get("WindowWidth")
            window_width = None
            if isinstance(window_width_expr, str) and window_width_expr.strip():
                box_width = _mvmz_graphics_box_width(system)
                screen_width = _as_float(advanced.get("screenWidth")) if isinstance(advanced, dict) else None
                screen_width = screen_width or box_width
                window_width = _simple_js_numeric_expr(
                    window_width_expr,
                    {
                        "Graphics.boxWidth": box_width,
                        "Graphics._boxWidth": box_width,
                        "Graphics.width": screen_width,
                        "Graphics._width": screen_width,
                    },
                )
            if window_width and window_width > 0:
                no_face_units, face_units = _message_units_from_window_width(
                    window_width,
                    base_font_size=base_font_size,
                    face_size=face_size,
                )
            break

    return _MVMZMessageLayout(
        base_font_size=base_font_size,
        no_face_units=float(no_face_units),
        face_units=float(face_units),
        max_lines=layout.max_lines,
        icon_units=(icon_size + 4.0) / max(base_font_size / 2.0, 1.0),
    )


_MVMZ_ESCAPE_RE = re.compile(r"\\([A-Za-z][A-Za-z0-9_]*)(?:\[([^\]\r\n]*)\])?|\\([\\$.!<>|^{}])")
_MVMZ_BREAK_AFTER_CHARS = set("。！？!?，,、；;：:…")
_MVMZ_NO_LINE_START_CHARS = set("，,。！？!?、；;：:…」』”’）)]】》〉")


def _tokenize_mvmz_message_text(text: str) -> List[_MVMZTextToken]:
    tokens: List[_MVMZTextToken] = []
    i = 0
    while i < len(text):
        if text[i] == "\\":
            m = _MVMZ_ESCAPE_RE.match(text, i)
            if m:
                code = m.group(1) or m.group(3) or ""
                tokens.append(_MVMZTextToken(m.group(0), code.upper(), m.group(2)))
                i = m.end()
                continue
        tokens.append(_MVMZTextToken(text[i]))
        i += 1
    return tokens


def _mvmz_char_units(ch: str, font_size: float, layout: _MVMZMessageLayout) -> float:
    if not ch:
        return 0.0
    if unicodedata.combining(ch):
        return 0.0
    if ch == "\t":
        base_units = 4.0
    else:
        east_asian_width = unicodedata.east_asian_width(ch)
        base_units = 2.0 if east_asian_width in {"F", "W"} or (east_asian_width == "A" and ord(ch) > 127) else 1.0
    return base_units * max(font_size, 1.0) / max(layout.base_font_size, 1.0)


def _apply_mvmz_font_escape(token: _MVMZTextToken, font_size: float, layout: _MVMZMessageLayout) -> float:
    if token.code == "FS":
        parsed = _as_float(token.arg)
        if parsed and parsed > 0:
            return parsed
    if token.code == "{":
        return font_size + 12.0
    if token.code == "}":
        return max(font_size - 12.0, 1.0)
    return font_size


def _mvmz_token_units(token: _MVMZTextToken, font_size: float, layout: _MVMZMessageLayout) -> float:
    if token.code:
        if token.code == "I":
            return layout.icon_units
        return 0.0
    return _mvmz_char_units(token.raw, font_size, layout)


def _measure_mvmz_tokens(
    tokens: Sequence[_MVMZTextToken],
    layout: _MVMZMessageLayout,
    start_font_size: Optional[float] = None,
) -> Tuple[float, float, bool]:
    font_size = start_font_size if start_font_size and start_font_size > 0 else layout.base_font_size
    units = 0.0
    has_visible = False
    for token in tokens:
        units += _mvmz_token_units(token, font_size, layout)
        if not token.code and token.raw.strip():
            has_visible = True
        elif token.code == "I":
            has_visible = True
        font_size = _apply_mvmz_font_escape(token, font_size, layout)
    return units, font_size, has_visible


def _mvmz_line_units(text: str, layout: Optional[_MVMZMessageLayout] = None) -> float:
    effective_layout = layout or _default_mvmz_message_layout()
    units, _font_size, _visible = _measure_mvmz_tokens(_tokenize_mvmz_message_text(text), effective_layout)
    return units


def _join_mvmz_tokens(tokens: Sequence[_MVMZTextToken]) -> str:
    return "".join(token.raw for token in tokens)


def _strip_wrapped_leading_spaces(tokens: Sequence[_MVMZTextToken]) -> List[_MVMZTextToken]:
    result = list(tokens)
    while result and not result[0].code and result[0].raw in {" ", "\t"}:
        result.pop(0)
    return result


def _best_mvmz_break_position(tokens: Sequence[_MVMZTextToken]) -> Optional[int]:
    best: Optional[int] = None
    for idx, token in enumerate(tokens):
        if token.code:
            continue
        if token.raw in _MVMZ_BREAK_AFTER_CHARS or token.raw in {" ", "\t"}:
            candidate = idx + 1
            while (
                candidate < len(tokens)
                and not tokens[candidate].code
                and tokens[candidate].raw in _MVMZ_NO_LINE_START_CHARS
            ):
                candidate += 1
            if candidate < len(tokens):
                best = candidate
    return best


def _wrap_mvmz_message_line(
    line: str,
    *,
    limit_units: float,
    layout: _MVMZMessageLayout,
) -> List[str]:
    tokens = _tokenize_mvmz_message_text(line)
    if not tokens:
        return [""]
    measured, _font, _visible = _measure_mvmz_tokens(tokens, layout)
    if measured <= limit_units:
        return [line]

    wrapped: List[str] = []
    current: List[_MVMZTextToken] = []
    line_start_font = layout.base_font_size
    for token in tokens:
        current.append(token)
        current_units, _current_end_font, current_visible = _measure_mvmz_tokens(current, layout, line_start_font)
        if current_units <= limit_units or not current_visible:
            continue

        break_pos = _best_mvmz_break_position(current)
        if break_pos is None or break_pos >= len(current):
            prefix = current[:-1]
            prefix_units, _prefix_end_font, prefix_visible = _measure_mvmz_tokens(prefix, layout, line_start_font)
            if prefix and prefix_visible and prefix_units > 0:
                break_pos = len(current) - 1
                while (
                    break_pos > 1
                    and break_pos < len(current)
                    and not current[break_pos].code
                    and current[break_pos].raw in _MVMZ_NO_LINE_START_CHARS
                ):
                    break_pos -= 1
            else:
                continue

        line_tokens = current[:break_pos]
        if not line_tokens:
            continue
        wrapped.append(_join_mvmz_tokens(line_tokens).rstrip(" \t"))
        _line_units, line_start_font, _line_visible = _measure_mvmz_tokens(line_tokens, layout, line_start_font)
        current = _strip_wrapped_leading_spaces(current[break_pos:])

    if current:
        wrapped.append(_join_mvmz_tokens(current).rstrip(" \t"))
    return wrapped or [line]


def _wrap_mvmz_message_text(
    text: str,
    *,
    has_face: bool = False,
    layout: Optional[_MVMZMessageLayout] = None,
) -> str:
    effective_layout = layout or _default_mvmz_message_layout()
    limit_units = effective_layout.face_units if has_face else effective_layout.no_face_units
    if limit_units <= 0:
        return _normalize_newlines(text)
    wrapped_lines: List[str] = []
    for line in _normalize_newlines(text).split("\n"):
        wrapped_lines.extend(_wrap_mvmz_message_line(line, limit_units=limit_units, layout=effective_layout))
    return "\n".join(wrapped_lines)


def _mvmz_units_from_pixels(pixels: float, layout: _MVMZMessageLayout) -> float:
    return max(float(pixels), 1.0) / max(layout.base_font_size / 2.0, 1.0)


def _load_questsystem_detail_units(
    game_path: str,
    data_dir: str,
    layout: Optional[_MVMZMessageLayout] = None,
) -> Optional[float]:
    plugins_path = os.path.join(game_path, "js", "plugins.js")
    if not os.path.isfile(plugins_path):
        return None
    try:
        _prefix, _suffix, plugins = _load_plugins_js(plugins_path)
    except Exception:
        return None

    quest_params: Optional[Dict[str, Any]] = None
    for plugin in plugins:
        if not isinstance(plugin, dict) or plugin.get("status") is False or plugin.get("name") != "QuestSystem":
            continue
        params = plugin.get("parameters")
        if isinstance(params, dict):
            quest_params = params
        break
    if quest_params is None:
        return None

    system_path = os.path.join(data_dir, "System.json")
    system: Dict[str, Any] = {}
    if os.path.isfile(system_path):
        try:
            loaded = _load_json(system_path)
            if isinstance(loaded, dict):
                system = loaded
        except Exception:
            system = {}

    window_size_raw = quest_params.get("WindowSize")
    window_size = _try_parse_json_string(window_size_raw) if isinstance(window_size_raw, str) else None
    command_width = 300.0
    if isinstance(window_size, dict):
        command_width = _as_float(window_size.get("CommandWindowWidth")) or command_width

    effective_layout = layout or _load_mvmz_message_layout(game_path, data_dir)
    detail_window_width = max(_mvmz_graphics_box_width(system) - command_width, 1.0)
    # Mirrors QuestSystem.Window_QuestDetail.drawDetail:
    # this.width - this.padding * 2 - 24, with MZ default window padding 12.
    draw_width_pixels = max(detail_window_width - 48.0, 1.0)
    return _mvmz_units_from_pixels(draw_width_pixels, effective_layout)


def _note_tag_specs(json_name: str, active_plugins: Set[str]) -> Tuple[_NoteTagSpec, ...]:
    specs = _NOTE_TAG_SPECS_BY_JSON.get(json_name, ())
    return tuple(spec for spec in specs if all(plugin in active_plugins for plugin in spec.plugins))


def _extract_note_texts(json_name: str, note: str, active_plugins: Set[str]) -> List[Tuple[str, str]]:
    note = _normalize_newlines(note)
    result: List[Tuple[str, str]] = []
    for spec in _note_tag_specs(json_name, active_plugins):
        for tag in spec.tags:
            value = _find_note_tag_value(note, tag)
            if isinstance(value, str) and value and JAPANESE_TEXT_RE.search(value):
                result.append((spec.marker, value))
                break
    return result


def _find_note_tag_value(note: str, tag: str) -> Optional[str]:
    pattern = re.compile(rf"<{re.escape(tag)}(?::(?P<value>.*?))?>", re.DOTALL)
    m = pattern.search(note)
    if not m:
        return None
    value = m.group("value")
    return _normalize_newlines(value.strip()) if isinstance(value, str) else None


def _replace_note_tag_value(note: str, tags: Sequence[str], new_value: str) -> Tuple[str, bool]:
    note = _normalize_newlines(note)
    for tag in tags:
        pattern = re.compile(rf"<{re.escape(tag)}(?::(?P<value>.*?))?>", re.DOTALL)
        m = pattern.search(note)
        if not m or m.group("value") is None:
            continue
        replacement = f"<{tag}:{_normalize_newlines(new_value)}>"
        new_note = note[: m.start()] + replacement + note[m.end() :]
        return new_note, new_note != note
    return note, False


def _tm_namepop_enabled(active_plugins: Optional[Set[str]]) -> bool:
    return bool(active_plugins and TM_NAMEPOP_PLUGIN_NAME in active_plugins)


def _split_tm_namepop_value(value: str) -> Optional[Tuple[str, str]]:
    value = _normalize_newlines(value).strip()
    if not value:
        return None
    parts = value.split(" ", 1)
    name = parts[0].strip()
    if not name:
        return None
    suffix = f" {parts[1]}" if len(parts) > 1 else ""
    return name, suffix


def _extract_tm_namepop_name_from_note(note: str) -> Optional[str]:
    value = _find_note_tag_value(note, "namePop")
    if not isinstance(value, str):
        return None
    parsed = _split_tm_namepop_value(value)
    if parsed is None:
        return None
    return parsed[0]


def _replace_tm_namepop_name_in_note(note: str, new_name: str) -> Tuple[str, bool]:
    current_value = _find_note_tag_value(note, "namePop")
    if not isinstance(current_value, str):
        return note, False
    parsed = _split_tm_namepop_value(current_value)
    if parsed is None:
        return note, False
    _old_name, suffix = parsed
    return _replace_note_tag_value(note, ("namePop",), f"{_normalize_newlines(new_name).strip()}{suffix}")


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
            if not isinstance(value, str):
                continue
            for value_path, text, codec in _extract_plugin_param_texts(str(plugin.get("name") or ""), str(param_name), value):
                payload = {"param": str(param_name), "path": list(value_path), "codec": codec}
                marker = _encode_plugin_param_text_marker(payload)
                store_path = ".".join(str(x) for x in value_path) if value_path else "$"
                original, created = _get_original_text_for_codec(
                    original_store,
                    f"plugins.js:{idx}:parameters.{param_name}:{store_path}",
                    text,
                    codec,
                )
                modified = modified or created
                _append_single_entry(entry_lines, marker, original, multiline_inline=True)
        if any(line.startswith("#") for line in entry_lines):
            out_lines.extend(entry_lines)
            out_lines.append("\n")

    if out_lines:
        _write_text_file(os.path.join(out_root, "Plugins", "Plugins.txt"), out_lines)
        return modified, 1
    return modified, 0


def _export_plugin_scripts(
    game_path: str,
    out_root: str,
    original_store: Dict[str, Any],
    active_plugins: Set[str],
) -> Tuple[bool, int]:
    plugins_dir = os.path.join(game_path, "js", "plugins")
    if not os.path.isdir(plugins_dir):
        return False, 0

    modified = False
    exported_files = 0
    output_dir = os.path.join(out_root, "PluginScripts")
    for plugin_name in sorted(active_plugins):
        safe_name = _safe_marker_name(plugin_name)
        plugin_path = os.path.join(plugins_dir, f"{plugin_name}.js")
        if not os.path.isfile(plugin_path):
            continue
        try:
            source = open(plugin_path, "r", encoding="utf-8-sig", errors="replace").read()
        except Exception as e:
            log.warning(f"读取插件脚本失败: {plugin_path} - {e}")
            continue

        out_lines: List[str] = []
        for index, (offset, text, _quote) in enumerate(_extract_plugin_script_texts(plugin_name, source)):
            payload = {"plugin": plugin_name, "offset": offset, "kind": "addCommand", "index": index}
            marker = _encode_plugin_script_text_marker(payload)
            original, created = _get_original_text(
                original_store,
                f"plugin_script:{plugin_name}:{offset}:addCommand",
                text,
            )
            modified = modified or created
            _append_single_entry(out_lines, marker, original, multiline_inline=True)

        if out_lines:
            _write_text_file(os.path.join(output_dir, f"{safe_name}.txt"), out_lines)
            exported_files += 1

    return modified, exported_files


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

        if marker in ("Message", "StringPicture", "ScrollText") or marker.startswith("PluginCommand_"):
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
                    original_choice = o.content[j]
                    translated_choice = t.content[j]
                    existing = marker_map.get(original_choice)
                    if existing is None or existing == original_choice:
                        marker_map[original_choice] = translated_choice
        elif isinstance(o.content, str) and isinstance(t.content, str):
            existing = marker_map.get(o.content)
            if existing is None or existing == o.content:
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
    decoded_scroll = _decode_scroll_marker(comment)
    if decoded_scroll is not None:
        return "scroll", decoded_scroll
    decoded_plugin_command = _decode_plugin_command_marker(comment)
    if decoded_plugin_command is not None:
        return "plugin_command", decoded_plugin_command
    return None, None


def _update_event_command_list(
    cmd_list: Any,
    translation_maps: Dict[str, Dict[str, str]],
    message_layout: Optional[_MVMZMessageLayout] = None,
) -> bool:
    if not isinstance(cmd_list, list):
        return False

    modified = False
    new_list: List[Any] = []
    at_leading_comments = True
    plugin_annotation_replacements: List[Tuple[str, str]] = []
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

        if code != 657:
            plugin_annotation_replacements = []

        if code == 657 and plugin_annotation_replacements:
            new_cmd = copy.deepcopy(cmd)
            new_params = list(params)
            changed_annotation = False
            for idx, value in enumerate(new_params):
                if not isinstance(value, str):
                    continue
                updated_value = value
                for old, new in plugin_annotation_replacements:
                    if old and old in updated_value:
                        updated_value = updated_value.replace(old, new)
                if updated_value != value:
                    new_params[idx] = updated_value
                    changed_annotation = True
            if changed_annotation:
                new_cmd["parameters"] = new_params
                new_list.append(new_cmd)
                modified = True
            else:
                new_list.append(cmd)
            i += 1
            continue

        if code in (108, 408) and params and at_leading_comments:
            original_name = _extract_tm_namepop_name_from_note(str(params[0]))
            if isinstance(original_name, str):
                translated_name = _translation_for(translation_maps, EVENT_NOTE_TM_NAMEPOP_MARKER, original_name)
                if translated_name is not None:
                    new_cmd = copy.deepcopy(cmd)
                    new_note, changed = _replace_tm_namepop_name_in_note(str(params[0]), translated_name)
                    if changed:
                        new_params = list(params)
                        new_params[0] = new_note
                        new_cmd["parameters"] = new_params
                        new_list.append(new_cmd)
                        modified = True
                        i += 1
                        continue

        if code == 101:
            at_leading_comments = False
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
                if new_text is not None:
                    final_text = _wrap_mvmz_message_text(
                        final_text,
                        has_face=bool(str(new_params[0]).strip()) if new_params else False,
                        layout=message_layout,
                    )
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
            at_leading_comments = False
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
                new_text = _wrap_mvmz_message_text(new_text, has_face=False, layout=message_layout)
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
            at_leading_comments = False
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

        if code == 105:
            at_leading_comments = False
            j = i + 1
            text_cmds: List[Any] = []
            while j < len(cmd_list):
                next_code, _next_indent, _next_params = _event_command_fields(cmd_list[j])
                if next_code != 405:
                    break
                text_cmds.append(cmd_list[j])
                j += 1
            original_text = "\n".join(_scroll_lines_from_commands(text_cmds))
            had_marker = pending_kind == "scroll" and isinstance(pending_payload, str)
            if had_marker:
                original_text = _normalize_newlines(pending_payload)
            new_text = _translation_for(translation_maps, "ScrollText", original_text)
            if new_text is not None:
                new_list.append(_new_event_command(108, indent, [_encode_scroll_marker(original_text)]))
                new_list.append(cmd)
                for line in new_text.split("\n"):
                    new_list.append(_new_event_command(405, indent, [line]))
                modified = True
            elif had_marker:
                new_list.append(cmd)
                for line in original_text.split("\n"):
                    new_list.append(_new_event_command(405, indent, [line]))
                modified = True
            else:
                if marker_cmd is not None:
                    new_list.append(marker_cmd)
                new_list.append(cmd)
                new_list.extend(text_cmds)
            i = j
            continue

        if code == 357:
            at_leading_comments = False
            current_values = _plugin_command_translatable_values(params)
            had_marker = pending_kind == "plugin_command" and isinstance(pending_payload, dict)
            original_values: Dict[str, str] = {}
            original_codecs: Dict[str, str] = {}
            for key, (value, _codec) in current_values.items():
                original_values[key] = value
                original_codecs[key] = _codec
            if had_marker:
                marker_values = pending_payload.get("values", {})
                marker_codecs = pending_payload.get("codecs", {})
                if not isinstance(marker_values, dict):
                    marker_values = {}
                if not isinstance(marker_codecs, dict):
                    marker_codecs = {}
                for key, value in marker_values.items():
                    if isinstance(key, str) and isinstance(value, str) and key in current_values:
                        original_values[key] = value
                        if isinstance(marker_codecs.get(key), str):
                            original_codecs[key] = marker_codecs[key]

            new_cmd = copy.deepcopy(cmd)
            new_params = list(params)
            args = copy.deepcopy(new_params[3]) if len(new_params) > 3 and isinstance(new_params[3], dict) else None
            changed_values = False
            translated_values = False
            annotation_replacements: List[Tuple[str, str]] = []
            if isinstance(args, dict):
                for key, original_raw in original_values.items():
                    if key in current_values:
                        _current_raw, codec = current_values[key]
                    else:
                        codec = original_codecs.get(key, "text")
                    original_text = _decode_text_codec(original_raw, codec)
                    marker = _plugin_command_marker_name(params, key)
                    translated = _translation_for(translation_maps, marker, original_text)
                    if translated is None:
                        continue
                    translated_values = True
                    annotation_replacements.append((original_text, translated))
                    encoded = _encode_text_codec(translated, original_raw, codec)
                    if _set_nested_value(args, key.split("."), encoded):
                        changed_values = True
                if changed_values:
                    new_params[3] = args
                    new_cmd["parameters"] = new_params

            if changed_values:
                new_list.append(_new_event_command(108, indent, [_encode_plugin_command_marker(original_values, original_codecs)]))
                new_list.append(new_cmd)
                plugin_annotation_replacements = annotation_replacements
                modified = True
            elif translated_values:
                if marker_cmd is not None:
                    new_list.append(marker_cmd)
                else:
                    new_list.append(_new_event_command(108, indent, [_encode_plugin_command_marker(original_values, original_codecs)]))
                    modified = True
                new_list.append(new_cmd)
                plugin_annotation_replacements = annotation_replacements
            elif had_marker:
                restored_cmd = copy.deepcopy(cmd)
                restored_params = list(params)
                restored_args = copy.deepcopy(restored_params[3]) if len(restored_params) > 3 and isinstance(restored_params[3], dict) else None
                restored_changed = False
                if isinstance(restored_args, dict):
                    for key, original_raw in original_values.items():
                        if _set_nested_value(restored_args, key.split("."), original_raw):
                            restored_changed = True
                    if restored_changed:
                        restored_params[3] = restored_args
                        restored_cmd["parameters"] = restored_params
                new_list.append(restored_cmd)
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
        if code not in (108, 408):
            at_leading_comments = False
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
    message_layout = _load_mvmz_message_layout(game_path, data_dir)

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
            note = ev.get("note")
            if isinstance(note, str):
                original_name = _extract_tm_namepop_name_from_note(note)
                if isinstance(original_name, str):
                    translated_name = _translation_for(maps, EVENT_NOTE_TM_NAMEPOP_MARKER, original_name)
                    if translated_name is not None:
                        new_note, changed = _replace_tm_namepop_name_in_note(note, translated_name)
                        if changed:
                            ev["note"] = new_note
                            touched = True
            pages = ev.get("pages")
            if not isinstance(pages, list):
                continue
            for page in pages:
                if isinstance(page, dict) and _update_event_command_list(page.get("list", []), maps, message_layout):
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
                        if isinstance(common_event, dict) and _update_event_command_list(common_event.get("list", []), maps, message_layout):
                            touched = True
                if touched:
                    _save_json(common_path, common_events)
                    modified_files += 1

    troops_origin = os.path.join(backup_path, "Troops.txt")
    troops_translated = os.path.join(string_scripts_path, "Troops.txt")
    if os.path.isfile(troops_origin) and os.path.isfile(troops_translated):
        maps = _load_translation_maps_for_pair(troops_origin, troops_translated)
        if maps:
            troops_path = os.path.join(data_dir, "Troops.json")
            if os.path.isfile(troops_path):
                troops = _load_json(troops_path)
                touched = False
                if isinstance(troops, list):
                    for troop in troops:
                        if not isinstance(troop, dict):
                            continue
                        pages = troop.get("pages")
                        if not isinstance(pages, list):
                            continue
                        for page in pages:
                            if isinstance(page, dict) and _update_event_command_list(page.get("list", []), maps, message_layout):
                                touched = True
                if touched:
                    _save_json(troops_path, troops)
                    modified_files += 1

    db_dir = os.path.join(string_scripts_path, "Database")
    if os.path.isdir(db_dir):
        origin_db_dir = os.path.join(backup_path, "Database")
        modified_files += _import_database(game_path, data_dir, db_dir, origin_db_dir)

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


def _import_database(game_path: str, data_dir: str, db_dir: str, origin_db_dir: Optional[str] = None) -> int:
    modified_files = 0
    active_plugins = _active_plugin_names(game_path)
    message_layout = _load_mvmz_message_layout(game_path, data_dir)
    plugin_context = _MVMZPluginImportContext(
        active_plugins=active_plugins,
        questsystem_detail_units=_load_questsystem_detail_units(game_path, data_dir, message_layout)
        if "QuestSystem" in active_plugins
        else None,
        message_layout=message_layout,
    )

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
            note = data[idx].get("note")
            if isinstance(note, str):
                for spec in _NOTE_TAG_SPECS_BY_JSON.get(json_name, ()):
                    value = entry.get(spec.marker)
                    if not isinstance(value, str):
                        continue
                    new_value = _unescape_inline_newlines(value)
                    if any(_find_note_tag_value(note, tag) == new_value for tag in spec.tags):
                        continue
                    new_note, changed = _replace_note_tag_value(note, spec.tags, new_value)
                    if changed:
                        note = new_note
                        data[idx]["note"] = new_note
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
    modified_files += _import_plugins(game_path, db_dir, plugin_context)
    modified_files += _import_plugin_scripts(game_path, db_dir, origin_db_dir)
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


def _import_plugins(
    game_path: str,
    db_dir: str,
    context: Optional[_MVMZPluginImportContext] = None,
) -> int:
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
        for param_name, current_value in list(params.items()):
            if not isinstance(current_value, str):
                continue
            repaired_value, repaired = _repair_plugin_param_json_string_literals(current_value)
            if repaired:
                params[param_name] = repaired_value
                touched = True
        for marker, value in entry.items():
            if not isinstance(value, str):
                continue
            payload = _decode_plugin_param_text_marker(marker)
            if not isinstance(payload, dict):
                continue
            param_name = payload.get("param")
            value_path = payload.get("path", [])
            codec = payload.get("codec", "text")
            if not isinstance(param_name, str) or param_name not in params:
                continue
            if not isinstance(value_path, list):
                value_path = []
            if not isinstance(codec, str):
                codec = "text"
            plugin_name = str(plugins[idx].get("name") or "")
            if _plugin_param_excluded_path(plugin_name, _plugin_param_key_path_from_marker(param_name, value_path)):
                continue
            current_value = params.get(param_name)
            if not isinstance(current_value, str):
                continue
            translated_value = _postprocess_plugin_param_translation(
                plugin_name,
                value_path,
                _unescape_inline_newlines(value),
                context,
            )
            new_param_value = _replace_plugin_param_translation(
                current_value,
                value_path,
                translated_value,
                codec,
            )
            if isinstance(new_param_value, str) and new_param_value != current_value:
                params[param_name] = new_param_value
                touched = True
    if touched:
        _save_plugins_js(plugins_path, prefix, suffix, plugins)
        return 1
    return 0


def _import_plugin_scripts(game_path: str, db_dir: str, origin_db_dir: Optional[str] = None) -> int:
    scripts_dir = os.path.join(db_dir, "PluginScripts")
    plugins_dir = os.path.join(game_path, "js", "plugins")
    origin_scripts_dir = os.path.join(origin_db_dir, "PluginScripts") if origin_db_dir else ""
    if not os.path.isdir(scripts_dir) or not os.path.isdir(plugins_dir) or not os.path.isdir(origin_scripts_dir):
        return 0

    modified = 0
    for file_name in sorted(os.listdir(scripts_dir)):
        if not file_name.lower().endswith(".txt"):
            continue
        origin_path = os.path.join(origin_scripts_dir, file_name)
        translated_path = os.path.join(scripts_dir, file_name)
        if not os.path.isfile(origin_path):
            continue
        maps = _load_translation_maps_for_pair(origin_path, translated_path)
        if not maps:
            continue
        changes_by_plugin: Dict[str, List[Tuple[int, str, str]]] = {}
        for marker, original_map in maps.items():
            payload = _decode_plugin_script_text_marker(marker)
            if not isinstance(payload, dict):
                continue
            plugin_name = payload.get("plugin")
            offset = payload.get("offset")
            if not isinstance(plugin_name, str) or not isinstance(offset, int):
                continue
            if not isinstance(original_map, dict):
                continue
            for original, translated in original_map.items():
                if isinstance(original, str) and isinstance(translated, str):
                    changes_by_plugin.setdefault(plugin_name, []).append((offset, original, translated))

        for plugin_name, changes in changes_by_plugin.items():
            plugin_path = os.path.join(plugins_dir, f"{plugin_name}.js")
            if not os.path.isfile(plugin_path):
                continue
            try:
                source = open(plugin_path, "r", encoding="utf-8-sig", errors="replace").read()
            except Exception as e:
                log.warning(f"读取插件脚本失败: {plugin_path} - {e}")
                continue
            updated = source
            touched = False
            for offset, original, translated in sorted(changes, key=lambda item: item[0], reverse=True):
                if not translated or translated == original:
                    continue
                original_text = _unescape_inline_newlines(original)
                translated_text = _unescape_inline_newlines(translated)
                candidate = _replace_plugin_script_text_at(updated, offset, original_text, translated_text)
                if candidate is None:
                    candidate = _replace_plugin_script_text_near(
                        updated,
                        plugin_name,
                        offset,
                        original_text,
                        translated_text,
                    )
                if candidate is not None and candidate != updated:
                    updated = candidate
                    touched = True
            if touched:
                tmp_path = f"{plugin_path}.tmp"
                with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
                    f.write(updated)
                os.replace(tmp_path, plugin_path)
                modified += 1

    return modified


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
