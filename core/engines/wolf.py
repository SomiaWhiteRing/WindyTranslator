import base64
import csv
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
from collections import deque

from core.external import uberwolf
from core.utils import control_tokens, file_system, font_coverage, text_processing

log = logging.getLogger(__name__)

STRING_SCRIPTS_DIRNAME = "StringScripts"
STRING_SCRIPTS_ORIGIN_DIRNAME = "StringScripts_Origin"
STATE_DIRNAME = ".windy_wolf"
JSON_SNAPSHOT_DIRNAME = "original_json"
MANIFEST_FILENAME = "manifest.json"
DISABLED_ARCHIVES_DIRNAME = "disabled_archives"
WOLF_EXPORT_SCHEMA = 8
ANALYSIS_DIAGNOSTICS_FILENAME = "analysis_diagnostics.json"
FUSION_FONT_FILENAME = "fusion-pixel-12px-proportional-zh_hans.ttf"
FUSION_FONT_FAMILY = "Fusion Pixel 12px Prop zh_hans"
TRANSLATION_TRANSPORT_INSTRUCTION = """

### WOLF 内部标签保护
输入中的 `[[W数字:校验码]]`（以及上下文中的 `{{WINDY_WOLF_...}}`）是 WOLF 运行时结构标签。必须逐个原样保留，不能删除、复制、换序或换行；标签不是可翻译文本。
"""

_REQUIRED_DATA_FILES = (
    os.path.join("BasicData", "Game.dat"),
    os.path.join("BasicData", "CommonEvent.dat"),
)
_NORMAL_COMMAND_CODES = {101, 102, 122}
_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_RESOURCE_RE = re.compile(
    r"(?:^|[/\\])[^\r\n]+\.(?:png|jpe?g|bmp|gif|webp|ogg|mp3|wav|mid|midi|txt|csv|md|dat|mps|sav)$",
    re.IGNORECASE,
)
_RESOURCE_DIRECTORY_RE = re.compile(
    r"(?:^|[/\\_])(?:picture(?:_ui)?|bgm|bgs|sound|se|charachip|mapdata|"
    r"fog(?:_background)?|battleeffect|face|movie)(?:$|[/\\_])",
    re.IGNORECASE,
)
_TEXT_RESOURCE_EXTENSIONS = {".txt", ".csv", ".md"}
_SCENE_END_RE = re.compile(r"^\s*---END_SCENE---", re.MULTILINE)
_SCENE_SEPARATOR_RE = re.compile(r"^\s*---\s*$")
_SCENE_LABEL_RE = re.compile(r"^\s*#{1,2}\s+(.+?)\s*$")
_BULLET_SCENE_HEADER_RE = re.compile(r"^[ \t\u3000]*●\d+[ \t\u3000]*$")
_BULLET_SCENE_BOUNDARY_RE = re.compile(r"^[ \t\u3000]*●(?:\d+)?[ \t\u3000]*$")
_ASCII_SCENE_ID_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]*$")
_WOLF_RUBY_RE = re.compile(r"\\r\[([^,\]\r\n]*),[^\]\r\n]*\]$", re.IGNORECASE)
_WOLF_TRANSPORT_TAG_PREFIX = "{{WINDY_WOLF_"
_WOLF_TRANSPORT_TAG_RE = re.compile(r"\{\{WINDY_WOLF_[^}\r\n]*\}\}")
_WOLF_DIRECTIVE_RE = re.compile(r"^[ \t\u3000]*@\d+[ \t\u3000]*$", re.MULTILINE)
_WOLF_ALIGNMENT_RE = re.compile(r"<(?:C|L|R)>", re.IGNORECASE)
_WOLF_EMOTICON_MARKS = frozenset("ゝゞヽヾ")
_WOLF_EMOTICON_HINTS = frozenset("ωдД▽∀ﾟ・")
_DISPLAY_STRING_FORMAT_RE = re.compile(
    r"(?:\\i\[[^\]\r\n]*\]|<GRADY-[^>\r\n]*>\|\$)",
    re.IGNORECASE,
)
_COMMON_EVENT_CALLBACK_FIELD_RE = re.compile(
    r"(?:コモン|common\s*(?:event|ev)?)", re.IGNORECASE
)
_COMMON_EVENT_ARGUMENT_FIELD_RE = re.compile(
    r"(?:引数|arg(?:ument)?)(.*)", re.IGNORECASE
)
_COMMON_EVENT_NUMERIC_ID_FIELD_RE = re.compile(
    r"(?:コモン.*(?:ID|番号)|common.*id)", re.IGNORECASE
)
_COMMON_EVENT_WRAPPER_RES = (
    re.compile(r"^<動的定義>(.+?)\s*-\s*cmd\[([+-]?\d+)\]$"),
    re.compile(r"^<コモン内定義:\s*(.+?)\.([+-]?\d+)>$"),
)
_COMMON_EVENT_TYPE_REFERENCE_RE = re.compile(
    r"(?:[（(]\s*)?(UDB|CDB|SDB)\s*(.+?)\s*にて定義(?:\s*[）)])?",
    re.IGNORECASE,
)
_SCENARIO_FIELD_RE = re.compile(r"(?:シナリオ|scenario).*(?:id|ＩＤ)?", re.IGNORECASE)
_MAP_REFERENCE_RE = re.compile(r"(?:^|[/\\])([^/\\\r\n]+)\.mps(?:$|[\r\n])", re.IGNORECASE)
_MAP_FILE_FIELD_RE = re.compile(r"(?:マップ|map).*(?:ファイル|file)", re.IGNORECASE)
_DATABASE_JSON_BY_KIND = {
    0: "cdatabase.json",
    1: "sysdatabase.json",
    2: "database.json",
}
_DATABASE_READ_FLAG = 0x00001000
_DATABASE_TYPE_NAME_FLAG = 0x00010000
_DATABASE_DATA_NAME_FLAG = 0x00020000
_DATABASE_FIELD_NAME_FLAG = 0x00040000
_COMMON_CALL_ALLOWED_FLAGS = 0x0101F1FF
class WolfEngineError(RuntimeError):
    pass


def _state_path(game_path, *parts):
    return os.path.join(game_path, STATE_DIRNAME, *parts)


def _safe_join(base, relative_path):
    if not isinstance(relative_path, str) or not relative_path:
        raise WolfEngineError("WOLF 脚本包含无效的空路径")
    normalized = os.path.normpath(relative_path.replace("/", os.sep))
    if os.path.isabs(normalized):
        raise WolfEngineError(f"WOLF 脚本包含绝对路径: {relative_path}")
    base_abs = os.path.abspath(base)
    candidate = os.path.abspath(os.path.join(base_abs, normalized))
    if os.path.normcase(os.path.commonpath((base_abs, candidate))) != os.path.normcase(base_abs):
        raise WolfEngineError(f"WOLF 脚本路径越出数据目录: {relative_path}")
    return candidate


def _validate_data_dir(data_path):
    missing = [rel for rel in _REQUIRED_DATA_FILES if not os.path.isfile(os.path.join(data_path, rel))]
    if missing:
        raise WolfEngineError(f"WOLF Data 目录不完整，缺少: {', '.join(missing)}")


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _archive_layout(game_path):
    if os.path.isfile(os.path.join(game_path, "Data.wolf")):
        return "single"
    if os.path.isfile(os.path.join(game_path, "Data", "BasicData.wolf")):
        return "split"
    return None


def _archive_digest(game_path, layout):
    if layout == "single":
        return _sha256(os.path.join(game_path, "Data.wolf"))
    if layout != "split":
        return None
    data_path = os.path.join(game_path, "Data")
    archives = sorted(
        filename
        for filename in os.listdir(data_path)
        if filename.lower().endswith(".wolf") and os.path.isfile(os.path.join(data_path, filename))
    )
    if "basicdata.wolf" not in {filename.casefold() for filename in archives}:
        raise WolfEngineError(f"WOLF 分卷缺少 BasicData.wolf: {data_path}")
    digest = hashlib.sha256()
    for filename in archives:
        path = os.path.join(data_path, filename)
        digest.update(filename.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(os.path.getsize(path)).encode("ascii"))
        digest.update(b"\0")
        digest.update(_sha256(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _copy_archive_inputs(source_root, destination_root, layout):
    if layout == "single":
        shutil.copy2(
            os.path.join(source_root, "Data.wolf"),
            os.path.join(destination_root, "Data.wolf"),
        )
        return
    if layout != "split":
        raise WolfEngineError(f"未知的 WOLF 封包布局: {layout}")

    source_data = os.path.join(source_root, "Data")
    destination_data = os.path.join(destination_root, "Data")
    os.makedirs(destination_data, exist_ok=True)
    filenames = {
        entry.name.casefold()
        for entry in os.scandir(source_data)
        if entry.is_file()
    }
    for entry in os.scandir(source_data):
        destination = os.path.join(destination_data, entry.name)
        if entry.is_file():
            shutil.copy2(entry.path, destination)
        elif entry.is_dir() and f"{entry.name}.wolf".casefold() not in filenames:
            shutil.copytree(entry.path, destination)


def _active_archive_relpaths(game_path):
    result = []
    root_archive = os.path.join(game_path, "Data.wolf")
    if os.path.isfile(root_archive):
        result.append("Data.wolf")
    data_path = os.path.join(game_path, "Data")
    if os.path.isdir(data_path):
        result.extend(
            f"Data/{entry.name}"
            for entry in os.scandir(data_path)
            if entry.is_file() and entry.name.lower().endswith(".wolf")
        )
    return tuple(sorted(result, key=str.casefold))


def _validate_disabled_archives(game_path, records):
    if not isinstance(records, list):
        raise WolfEngineError("WOLF 已停用归档记录无效")
    for record in records:
        if not isinstance(record, dict):
            raise WolfEngineError("WOLF 已停用归档记录无效")
        source_rel = record.get("source_path")
        stored_rel = record.get("stored_path")
        expected_hash = record.get("sha256")
        if not all(isinstance(value, str) and value for value in (source_rel, stored_rel, expected_hash)):
            raise WolfEngineError("WOLF 已停用归档记录无效")
        if os.path.isfile(_safe_join(game_path, source_rel)):
            raise WolfEngineError(f"松散 Data 部署仍存在活动归档: {source_rel}")
        stored_path = _safe_join(game_path, stored_rel)
        if not os.path.isfile(stored_path) or _sha256(stored_path) != expected_hash:
            raise WolfEngineError(f"WOLF 已停用归档备份缺失或变化: {stored_rel}")


def _prepare_disabled_archives(game_path, previous_records):
    records = list(previous_records) if isinstance(previous_records, list) else []
    known = {
        (item.get("source_path"), item.get("sha256"))
        for item in records
        if isinstance(item, dict)
    }
    for source_rel in _active_archive_relpaths(game_path):
        source_path = _safe_join(game_path, source_rel)
        file_sha256 = _sha256(source_path)
        if (source_rel, file_sha256) in known:
            continue
        stored_rel = "/".join((STATE_DIRNAME, DISABLED_ARCHIVES_DIRNAME, file_sha256, source_rel))
        stored_path = _safe_join(game_path, stored_rel)
        os.makedirs(os.path.dirname(stored_path), exist_ok=True)
        if os.path.isfile(stored_path):
            if _sha256(stored_path) != file_sha256:
                raise WolfEngineError(f"WOLF 归档备份冲突: {stored_rel}")
        else:
            temporary = f"{stored_path}.tmp"
            shutil.copy2(source_path, temporary)
            os.replace(temporary, stored_path)
        records.append({
            "source_path": source_rel,
            "stored_path": stored_rel,
            "sha256": file_sha256,
        })
        known.add((source_rel, file_sha256))
    return records


def _strip_split_archives(data_path):
    for entry in os.scandir(data_path):
        if entry.is_file() and entry.name.lower().endswith(".wolf"):
            os.remove(entry.path)


def _prepare_json_atomic(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8", newline="\n") as output:
        json.dump(data, output, ensure_ascii=False, indent=2)
        output.write("\n")
    return temp_path


def _write_json_atomic(path, data):
    temp_path = _prepare_json_atomic(path, data)
    os.replace(temp_path, path)


def initialize_game(game_path, message_queue=None):
    data_path = os.path.join(game_path, "Data")
    game_exe = os.path.join(game_path, "Game.exe")

    manifest_path = _state_path(game_path, MANIFEST_FILENAME)
    previous_manifest = {}
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as source:
                previous_manifest = json.load(source)
        except (OSError, ValueError) as error:
            raise WolfEngineError(f"WOLF 初始化状态文件损坏: {error}") from error

    current_archive_sha256 = None
    verified_pair_data_sha256 = None
    current_archive_layout = _archive_layout(game_path)
    previous_archive_layout = previous_manifest.get("archive_layout")

    if not os.path.isfile(game_exe):
        raise WolfEngineError(f"未找到 WOLF Game.exe: {game_exe}")
    if "disabled_archives" in previous_manifest:
        _validate_disabled_archives(game_path, previous_manifest["disabled_archives"])
        active_archives = _active_archive_relpaths(game_path)
        if active_archives:
            raise WolfEngineError(f"松散 Data 部署仍存在活动归档: {', '.join(active_archives)}")
    if (
        previous_archive_layout in ("single", "split")
        and current_archive_layout
        and previous_archive_layout != current_archive_layout
    ):
        raise WolfEngineError("WOLF 封包布局已变化；请同时恢复 Data 与对应封包")
    if os.path.isdir(data_path):
        unpacked_now = False
        try:
            _validate_data_dir(data_path)
        except WolfEngineError:
            if current_archive_layout != "split":
                raise
            if message_queue:
                message_queue.put(("log", ("normal", "使用 UberWolfCli 解包 Data 目录中的 WOLF 分卷...")))
            uberwolf.unpack_game(game_path)
            _validate_data_dir(data_path)
            unpacked_now = True
        current_data_sha256 = _data_digest(data_path)
        expected_data_sha256 = previous_manifest.get("data_sha256")
        if expected_data_sha256 and current_data_sha256 != expected_data_sha256:
            raise WolfEngineError("现有 WOLF Data 已在初始化后变化；请先导入或移走 Data 后重新初始化")
        if current_archive_layout:
            current_archive_sha256 = _archive_digest(game_path, current_archive_layout)
            known_archives = {
                previous_manifest.get("archive_sha256"),
                previous_manifest.get("last_import_sha256"),
                previous_manifest.get("retained_archive_sha256"),
            } - {None}
            if known_archives and current_archive_sha256 not in known_archives:
                raise WolfEngineError("WOLF 封包已在初始化后变化，拒绝继续使用可能不同源的 Data")

            paired_data_sha256 = set()
            if current_archive_sha256 == previous_manifest.get("archive_sha256"):
                paired = previous_manifest.get("archive_data_sha256")
                if paired:
                    paired_data_sha256.add(paired)
            if current_archive_sha256 == previous_manifest.get("last_import_sha256"):
                paired = previous_manifest.get("last_import_data_sha256")
                if paired:
                    paired_data_sha256.add(paired)

            loose_data_sha256 = (
                previous_manifest.get("last_import_data_sha256")
                if previous_manifest.get("deployment_layout") == "loose"
                else None
            )
            if loose_data_sha256 and current_data_sha256 == loose_data_sha256:
                verified_pair_data_sha256 = current_data_sha256
            elif paired_data_sha256:
                if current_data_sha256 not in paired_data_sha256:
                    raise WolfEngineError("现有 Data 与 WOLF 封包不属于同一版本；请同时恢复或移走其中一份")
                verified_pair_data_sha256 = current_data_sha256
            elif unpacked_now:
                verified_pair_data_sha256 = current_data_sha256
            else:
                # ponytail: Old manifests have no archive/Data association, so pay
                # the one-time unpack cost and persist the verified pair below.
                with tempfile.TemporaryDirectory(prefix="windy-wolf-init-") as temp_root:
                    shutil.copy2(game_exe, os.path.join(temp_root, "Game.exe"))
                    _copy_archive_inputs(game_path, temp_root, current_archive_layout)
                    unpacked_data = uberwolf.unpack_game(temp_root)
                    unpacked_data_sha256 = _data_digest(unpacked_data)
                    if current_data_sha256 != unpacked_data_sha256:
                        if current_archive_sha256 != previous_manifest.get("last_import_sha256"):
                            raise WolfEngineError("现有 Data 与 WOLF 封包内容不一致；请只保留要处理的那一份")
                        if expected_data_sha256:
                            raise WolfEngineError("现有 Data 与 WOLF 封包不属于同一版本；请同时恢复或移走其中一份")
                        _replace_data_directory(data_path, unpacked_data)
                        current_data_sha256 = unpacked_data_sha256
                        if message_queue:
                            message_queue.put((
                                "log",
                                ("warning", "检测到旧版导入遗留的 Data，已按最近一次验证通过的 Data.wolf 同步。"),
                            ))
                    verified_pair_data_sha256 = current_data_sha256
        if message_queue:
            message_queue.put(("log", ("normal", "检测到已解包且完整的 WOLF Data，跳过重复解包。")))
    else:
        if current_archive_layout != "single":
            raise WolfEngineError(f"未找到 WOLF Data.wolf: {os.path.join(game_path, 'Data.wolf')}")
        if message_queue:
            message_queue.put(("log", ("normal", "使用 UberWolfCli 解包 Data.wolf...")))
        uberwolf.unpack_game(game_path)
        _validate_data_dir(data_path)
        current_data_sha256 = _data_digest(data_path)
        current_archive_sha256 = _archive_digest(game_path, current_archive_layout)
        verified_pair_data_sha256 = current_data_sha256

    archive_layout = previous_archive_layout or current_archive_layout or "loose"
    original_archive_sha256 = previous_manifest.get("archive_sha256") or current_archive_sha256
    original_archive_data_sha256 = previous_manifest.get("archive_data_sha256")
    manifest = dict(previous_manifest)
    manifest.update({
        "engine": "wolf",
        "archive_layout": archive_layout,
        "archive_sha256": original_archive_sha256,
        "archive_data_sha256": original_archive_data_sha256,
        "data_sha256": current_data_sha256,
    })
    if archive_layout == "loose":
        manifest["deployment_layout"] = "loose"
    if previous_manifest.get("last_import_sha256"):
        manifest["last_import_sha256"] = previous_manifest["last_import_sha256"]
        last_import_data_sha256 = previous_manifest.get("last_import_data_sha256")
        if current_archive_sha256 == previous_manifest["last_import_sha256"] and verified_pair_data_sha256:
            last_import_data_sha256 = verified_pair_data_sha256
        if last_import_data_sha256:
            manifest["last_import_data_sha256"] = last_import_data_sha256

    active_archives = _active_archive_relpaths(game_path)
    if active_archives:
        disabled_archives = _prepare_disabled_archives(
            game_path,
            manifest.get("disabled_archives", []),
        )
        with tempfile.TemporaryDirectory(prefix="windy-wolf-init-disable-") as temp_root:
            staging_data = None
            if any(relative.startswith("Data/") for relative in active_archives):
                staging_data = os.path.join(temp_root, "Data")
                shutil.copytree(data_path, staging_data)
                _strip_split_archives(staging_data)
                current_data_sha256 = _data_digest(staging_data)
            if current_archive_sha256 == original_archive_sha256 and verified_pair_data_sha256:
                original_archive_data_sha256 = current_data_sha256
            manifest["archive_data_sha256"] = original_archive_data_sha256
            manifest["data_sha256"] = current_data_sha256
            if current_archive_sha256 == previous_manifest.get("last_import_sha256") and verified_pair_data_sha256:
                manifest["last_import_data_sha256"] = current_data_sha256
            manifest["disabled_archives"] = disabled_archives
            manifest["deployment_layout"] = "loose"
            if current_archive_sha256:
                manifest["retained_archive_sha256"] = current_archive_sha256
            root_archive = os.path.join(game_path, "Data.wolf")
            _replace_data_and_manifest(
                data_path,
                staging_data,
                manifest_path,
                manifest,
                root_archive if os.path.isfile(root_archive) else None,
                os.path.join(temp_root, "Data.wolf.before"),
            )
        if message_queue:
            message_queue.put(("log", ("success", "WOLF 归档已备份并停用，游戏将使用解包后的 Data。")))
    else:
        if current_archive_sha256 == original_archive_sha256 and verified_pair_data_sha256:
            manifest["archive_data_sha256"] = verified_pair_data_sha256
        _write_json_atomic(manifest_path, manifest)
    return manifest


def _encode_metadata(metadata):
    payload = json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_metadata(encoded):
    encoded += "=" * (-len(encoded) % 4)
    return json.loads(base64.urlsafe_b64decode(encoded).decode("utf-8"))


def _translation_code(metadata):
    kind = str(metadata.get("kind") or "unknown").upper()
    relative = str(metadata.get("file") or "")
    if kind == "JSON":
        location = metadata.get("path")
    elif kind == "TXT":
        location = [metadata.get("start"), metadata.get("end")]
    elif kind == "CSV":
        location = [metadata.get("row"), metadata.get("column")]
    else:
        location = None
    encoded_location = json.dumps(location, ensure_ascii=False, separators=(",", ":"))
    return f"{kind}:{relative}:{encoded_location}"


def _split_text_format(text):
    newline = "\r\n" if "\r\n" in text else ("\r" if "\r" in text else "\n")
    suffix = ""
    core = text
    while core.endswith(("\r", "\n")):
        if core.endswith("\r\n"):
            core, suffix = core[:-2], "\r\n" + suffix
        else:
            core, suffix = core[:-1], core[-1:] + suffix
    core = core.replace("\r\n", "\n").replace("\r", "\n")
    return core, newline, suffix


def _restore_text_format(text, metadata):
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    newline = metadata.get("newline", "\n")
    return normalized.replace("\n", newline) + metadata.get("suffix", "")


def _looks_like_resource(text):
    stripped = text.strip()
    return any(
        bool(_RESOURCE_RE.search(line))
        or line.startswith(("Picture/", "contents/", "sound/"))
        for line in (part.strip() for part in stripped.splitlines())
        if line
    )


def _is_explicit_internal_database_text(value):
    return isinstance(value, str) and value.lstrip().startswith("#")


def _add_entry(entries, metadata, text, protect_resource=False):
    if not isinstance(text, str) or not text.strip():
        return
    metadata = dict(metadata)
    if _looks_like_resource(text):
        if not protect_resource:
            return
        metadata["marker"] = "WOLFLogic"
        metadata["logic_role"] = "resource"
    core, newline, suffix = _split_text_format(text)
    # ponytail: StringScripts uses ## as a block terminator; a sidecar format is the upgrade path.
    if any(line.strip() == "##" for line in core.splitlines()):
        raise WolfEngineError("WOLF 文本包含独立的 ## 行，无法安全写入 StringScripts")
    metadata.setdefault("wolf_code", _translation_code(metadata))
    metadata["newline"] = newline
    metadata["suffix"] = suffix
    entries.append((metadata, core))


def _command_entries(command, base_path, entries, json_rel, usage=None, owner_event_id=None):
    try:
        code = int(command.get("code", -1))
    except (TypeError, ValueError):
        return
    string_args = command.get("stringArgs")
    if not isinstance(string_args, list):
        return
    marker = "Message"
    indexes = range(len(string_args))
    call_argument_roles = {}
    call_return_roles = set()
    reachable_paths = usage.get("reachable_command_paths") if usage else None
    command_reachable = (
        not isinstance(reachable_paths, set)
        or (json_rel, tuple(base_path)) in reachable_paths
    )
    if code == 112:
        marker = "WOLFLogic"
    elif code == 150:
        int_args = command.get("intArgs") or []
        if not int_args or ((int(int_args[0]) >> 4) & 0x07) != 2:
            return
        marker = "StringPicture"
    elif code in (210, 300):
        call = _decode_common_call(command, current_event_id=owner_event_id)
        call_roles = None
        if call and usage and call["target_key"] is not None:
            role_index = (
                usage.get("common_event_roles_by_name", {})
                if code == 300
                else usage.get("common_event_roles_by_id", {})
            )
            call_roles = role_index.get(call["target_key"])
            return_index = (
                usage.get("common_event_return_roles_by_name", {})
                if code == 300
                else usage.get("common_event_return_roles_by_id", {})
            )
            call_return_roles = set(return_index.get(call["target_key"], set()))
            context_index = (
                usage.get("common_event_contexts_by_name", {})
                if code == 300
                else usage.get("common_event_contexts_by_id", {})
            )
            context = context_index.get(call["target_key"])
            if context and context["slot"] < len(call["numeric_inputs"]):
                value = _common_dispatch_context(
                    context["roles"], call["numeric_inputs"][context["slot"]]
                )
                if value in context["roles"]:
                    call_roles = context["roles"][value]
                    call_return_roles = set(context["return_roles"][value])
        if call:
            command_key = (json_rel, tuple(base_path))
            if usage and command_key in usage.get("reachable_call_return_roles", {}):
                call_return_roles = set(
                    usage["reachable_call_return_roles"][command_key]
                )
            indexes = []
            for slot, source_kind, _source in call["string_inputs"]:
                if source_kind != "literal":
                    continue
                index = 1 + slot - 5
                indexes.append(index)
                argument_key = (json_rel, tuple(base_path), index)
                call_argument_roles[index] = (
                    usage["reachable_call_argument_roles"][argument_key]
                    if usage
                    and argument_key in usage.get("reachable_call_argument_roles", {})
                    else (call_roles or {}).get(slot, set())
                )
        else:
            indexes = range(1, len(string_args))
    elif code not in _NORMAL_COMMAND_CODES:
        return

    for index in indexes:
        text = string_args[index]
        entry_marker = marker
        if code in (210, 300):
            role = set(call_argument_roles.get(index, set()))
            if (
                "return" in role
                and call
                and _string_variable_token(call["output"])
            ):
                role.update(call_return_roles)
            entry_marker = (
                "WOLFText"
                if "display" in role
                and not ({"logic", "opaque", "unknown"} & set(role))
                else "WOLFLogic"
            )
        if not command_reachable:
            entry_marker = "WOLFLogic"
        if (
            code == 122
            and usage
            and (
                text in usage["logic_command_literals"]
                or _looks_like_logic_assignment(text)
            )
        ):
            entry_marker = "WOLFLogic"
        _add_entry(
            entries,
            {
                "kind": "json",
                "file": json_rel,
                "path": [*base_path, "stringArgs", index],
                "marker": entry_marker,
                **(
                    {
                        "logic_role": (
                            "unreachable"
                            if not command_reachable
                            else "comparison" if code in (112, 122) else "call_argument"
                        )
                    }
                    if entry_marker == "WOLFLogic"
                    else {}
                ),
            },
            text,
            protect_resource=True,
        )


def _looks_like_logic_assignment(text):
    if not isinstance(text, str) or not text.strip():
        return False
    stripped = text.strip()
    return _looks_like_resource(stripped) or bool(
        stripped.startswith("^")
        or stripped.startswith("◆")
        or re.fullmatch(r"変数\d+", stripped)
        or "_dbgids=" in stripped
        or re.search(r"(?:\(\?:|\[\\[sSrRnN]\\[sSrRnN]\]|\.\*\??|---END_SCENE---)", stripped)
    )


def _string_variable_token(variable_id):
    try:
        variable_id = int(variable_id)
    except (TypeError, ValueError):
        return None
    if 1_600_000 <= variable_id < 1_700_000:
        return rf"\cself[{variable_id - 1_600_000}]"
    if 3_000_000 <= variable_id < 3_100_000:
        return rf"\s[{variable_id - 3_000_000}]"
    return None


def _database_field_access_descriptor(command, schemas=None, read=True):
    try:
        code = int(command.get("code", -1))
        int_args = command.get("intArgs") or []
        string_args = command.get("stringArgs") or []
        if code != 250 or len(int_args) < 5 or len(string_args) < 4:
            return None
        flags = int(int_args[3])
        if bool(flags & _DATABASE_READ_FLAG) != read:
            return None
        if (
            not flags & _DATABASE_DATA_NAME_FLAG
            and int(int_args[1]) in (0xFFFFFFFF, 0xFFFFFFFE)
        ):
            return None
        database_name = _DATABASE_JSON_BY_KIND.get((flags >> 8) & 0x0F)
        schema = (schemas or {}).get(database_name, {})
        type_name = string_args[1].strip() if isinstance(string_args[1], str) else ""
        type_index = (
            schema.get("types", {}).get(type_name.casefold())
            if flags & _DATABASE_TYPE_NAME_FLAG
            else int(int_args[0])
        )
        field_name = string_args[3].strip() if isinstance(string_args[3], str) else ""
        field_index = (
            schema.get("fields", {}).get(type_index, {}).get(field_name.casefold())
            if flags & _DATABASE_FIELD_NAME_FLAG
            else int(int_args[2])
        )
        destination = int(int_args[4])
    except (TypeError, ValueError, IndexError):
        return None
    if database_name is None or type_index is None or field_index is None:
        return None
    if type_index < 0 or field_index < 0:
        return None
    if schema and (
        type_index not in schema.get("fields", {})
        or field_index not in schema["fields"][type_index].values()
    ):
        return None
    return (database_name, type_index, field_index), destination


def _database_read_descriptor(command, schemas=None):
    return _database_field_access_descriptor(command, schemas, read=True)


def _database_read(command, schemas=None):
    descriptor = _database_read_descriptor(command, schemas)
    if descriptor is None:
        return None
    key, destination = descriptor
    token = _string_variable_token(destination)
    if token is None:
        return None
    return key, destination, token


def _database_write(command, schemas=None):
    descriptor = _database_field_access_descriptor(command, schemas, read=False)
    if descriptor is None:
        return None
    key, source = descriptor
    token = _string_variable_token(source)
    if token is None:
        return None
    try:
        int_args = command.get("intArgs") or []
        flags = int(int_args[3])
        data_index = int(int_args[1])
    except (TypeError, ValueError, IndexError):
        data_index = -1
        flags = _DATABASE_DATA_NAME_FLAG
    target = (
        (*key[:2], data_index, key[2])
        if not flags & _DATABASE_DATA_NAME_FLAG and 0 <= data_index < 1_000_000
        else key
    )
    return target, source, token


def _database_target_is_displayed(usage, target):
    return target in usage[
        "display_database_records"
        if len(target) == 4
        else "display_database_fields"
    ]


def _database_read_record_indexes(command, schemas, record_indexes, record_counts):
    target = _database_target(command, schemas)
    if target is None:
        return None
    try:
        int_args = command.get("intArgs") or []
        string_args = command.get("stringArgs") or []
        flags = int(int_args[3])
        namespace = target[:2]
        if flags & _DATABASE_DATA_NAME_FLAG:
            selector = string_args[2].strip().casefold()
            indexes = record_indexes.get(namespace, {}).get(selector)
            return tuple(sorted(indexes)) if indexes else None
        selector = int(int_args[1])
    except (AttributeError, TypeError, ValueError, IndexError):
        return None
    count = record_counts.get(namespace, 0)
    return (selector,) if 0 <= selector < count else None


def _database_target(command, schemas=None):
    try:
        if int(command.get("code", -1)) != 250:
            return None
        int_args = command.get("intArgs") or []
        string_args = command.get("stringArgs") or []
        if len(int_args) < 4 or len(string_args) < 3:
            return None
        flags = int(int_args[3])
        database_name = _DATABASE_JSON_BY_KIND.get((flags >> 8) & 0x0F)
        schema = (schemas or {}).get(database_name, {})
        type_name = string_args[1].strip() if isinstance(string_args[1], str) else ""
        type_index = (
            schema.get("types", {}).get(type_name.casefold())
            if flags & _DATABASE_TYPE_NAME_FLAG
            else int(int_args[0])
        )
        datum_name = (
            string_args[2].strip()
            if flags & _DATABASE_DATA_NAME_FLAG and isinstance(string_args[2], str)
            else ""
        )
    except (TypeError, ValueError, IndexError):
        return None
    if database_name is None or type_index is None or type_index < 0:
        return None
    return database_name, type_index, datum_name


def _database_schema_field_is_resource(field):
    if not isinstance(field, dict):
        return False
    return any(
        isinstance(argument, str) and bool(_RESOURCE_DIRECTORY_RE.search(argument.strip()))
        for argument in (field.get("stringArgs") or [])
    )


def _command_uses_string_variable(command, destination, token):
    strings = command.get("stringArgs") or []
    if any(isinstance(text, str) and token in text for text in strings):
        return True
    try:
        code = int(command.get("code", -1))
        int_args = command.get("intArgs") or []
        if code in (112, 122):
            inputs = int_args[1:]
        else:
            return False
        return any(int(value) == destination for value in inputs)
    except (TypeError, ValueError):
        return False


def _command_string_destination(command):
    try:
        code = int(command.get("code", -1))
        int_args = command.get("intArgs") or []
        if code == 122:
            destination = int(int_args[0])
        elif code == 250:
            flags = int(int_args[3])
            if len(int_args) < 5 or not flags & _DATABASE_READ_FLAG:
                return None
            destination = int(int_args[4])
        elif code in (210, 300):
            call = _decode_common_call(command)
            if not call or call["output"] is None:
                return None
            destination = call["output"]
        else:
            return None
    except (TypeError, ValueError, IndexError):
        return None
    return destination if _string_variable_token(destination) else None


def _decode_common_call(
    command,
    common_events_by_id=None,
    common_events_by_name=None,
    current_event_id=None,
):
    common_events_by_id = common_events_by_id or {}
    common_events_by_name = common_events_by_name or {}
    try:
        code = int(command.get("code", -1))
        if code not in (210, 300):
            return None
        int_args = [int(value) for value in (command.get("intArgs") or [])]
        strings = command.get("stringArgs") or []
        if len(int_args) < 2:
            return None
        encoded_target, flags = int_args[:2]
        numeric_count = flags & 0x0F
        string_count = (flags >> 4) & 0x0F
        literal_mask = (flags >> 12) & 0x03FF
        has_output = bool(flags & 0x01000000)
    except (TypeError, ValueError):
        return None
    if (
        flags & ~_COMMON_CALL_ALLOWED_FLAGS
        or numeric_count > 5
        or string_count > 5
        or literal_mask >> string_count
        or len(int_args) != 2 + numeric_count + string_count + int(has_output)
    ):
        return None
    values = int_args[2:2 + numeric_count + string_count]
    event = None
    target_expression = ""
    target_key = None
    if code == 300:
        target_expression = strings[0] if strings and isinstance(strings[0], str) else ""
        if not flags & 0x0100 and target_expression:
            target_key = target_expression.strip().casefold()
            event = common_events_by_name.get(target_key)
    elif 500_000 <= encoded_target < 600_000:
        target_key = encoded_target - 500_000
        event = common_events_by_id.get(target_key)
    elif encoded_target == 600_100 and current_event_id is not None:
        target_key = current_event_id
        event = common_events_by_id.get(target_key)
    slots = []
    for offset in range(string_count):
        if literal_mask & (1 << offset):
            string_index = 1 + offset
            if string_index >= len(strings) or not isinstance(strings[string_index], str):
                return None
            slots.append((offset + 5, "literal", strings[string_index]))
        else:
            slots.append((offset + 5, "variable", values[numeric_count + offset]))
    return {
        "code": code,
        "event": event,
        "target_key": target_key,
        "target_expression": target_expression,
        "numeric_inputs": tuple(values[:numeric_count]),
        "string_inputs": tuple(slots),
        "output": int_args[-1] if has_output else None,
    }


def _string_condition_is_presence_check(command, variable_id):
    try:
        if int(command.get("code", -1)) != 112:
            return False
        if any(str(value) for value in (command.get("stringArgs") or [])):
            return False
        variables = {
            int(value)
            for value in (command.get("intArgs") or [])[1:]
            if _string_variable_token(value)
        }
    except (TypeError, ValueError):
        return False
    return variables == {variable_id}


def _dynamic_jump_dispatches(commands, labels):
    dispatches = {}
    for jump_index, command in enumerate(commands):
        try:
            if int(command.get("code", -1)) != 213:
                continue
        except (TypeError, ValueError):
            continue
        strings = command.get("stringArgs") or []
        target = strings[0] if strings and isinstance(strings[0], str) else ""
        variable_match = re.fullmatch(r"\\s\[(\d+)\]", target)
        if not variable_match:
            continue
        destination = 3_000_000 + int(variable_match.group(1))
        if jump_index == 0:
            continue
        assignment_index = jump_index - 1
        while assignment_index >= 0:
            try:
                if int(commands[assignment_index].get("code", -1)) not in (0, 99, 103):
                    break
            except (TypeError, ValueError):
                break
            assignment_index -= 1
        if assignment_index < 0:
            continue
        assignment = commands[assignment_index]
        try:
            int_args = assignment.get("intArgs") or []
            if (
                int(assignment.get("code", -1)) != 122
                or len(int_args) < 2
                or int(int_args[0]) != destination
                or int(int_args[1]) != 0
            ):
                continue
        except (TypeError, ValueError):
            continue
        templates = assignment.get("stringArgs") or []
        template = templates[0] if templates and isinstance(templates[0], str) else ""
        argument_match = re.fullmatch(r"(.*)\\cself\[(\d+)\](.*)", template)
        if not argument_match:
            continue
        prefix, slot, suffix = argument_match.groups()
        pattern = re.compile(
            rf"{re.escape(prefix)}([+-]?\d+){re.escape(suffix)}"
        )
        targets = {}
        for label, label_index in labels.items():
            label_match = pattern.fullmatch(label)
            if label_match:
                targets[int(label_match.group(1))] = label_index
        if targets:
            dispatches[jump_index] = (int(slot), targets)
    return dispatches


def _command_successors(commands, numeric_arguments=None):
    if not all(isinstance(command, dict) for command in commands):
        raise WolfEngineError("WOLF 命令控制流结构无效")
    count = len(commands)
    successors = [tuple([index + 1]) if index + 1 < count else tuple() for index in range(count)]
    if not all("indent" in command and "index" in command for command in commands):
        raise WolfEngineError("WOLF 命令缺少控制流元数据；请用当前版本重新导出翻译文本")
    try:
        indexes = [int(command["index"]) for command in commands]
        codes = [int(command.get("code", -1)) for command in commands]
        indents = [int(command["indent"]) for command in commands]
    except (TypeError, ValueError) as error:
        raise WolfEngineError("WOLF 命令控制流元数据无效；请重新导出翻译文本") from error
    if indexes != list(range(count)):
        raise WolfEngineError("WOLF 命令结构不完整；请用当前版本重新导出翻译文本")

    labels = {}
    for index, command in enumerate(commands):
        if codes[index] != 212:
            continue
        strings = command.get("stringArgs") or []
        if strings and isinstance(strings[0], str):
            labels.setdefault(strings[0], index)
    dynamic_dispatches = _dynamic_jump_dispatches(commands, labels)

    for index, command in enumerate(commands):
        code = codes[index]
        if code == 213:
            strings = command.get("stringArgs") or []
            target = strings[0] if strings and isinstance(strings[0], str) else ""
            label_index = labels.get(target)
            if label_index is not None:
                successors[index] = (label_index,)
            elif target.strip().upper() == "END":
                successors[index] = tuple()
            elif index in dynamic_dispatches:
                _slot, targets = dynamic_dispatches[index]
                successors[index] = tuple(sorted({
                    *targets.values(), *successors[index]
                }))
            elif re.fullmatch(r"\\(?:s|cself)\[\d+\]", target):
                successors[index] = tuple(sorted({*labels.values(), *successors[index]}))
        elif code in (172, 174, 175):
            successors[index] = tuple()

    case_codes = {401, 402, 420, 421}
    case_exits = []
    numeric_branches = []
    for index, command in enumerate(commands):
        code = codes[index]
        indent = indents[index]
        if code not in (102, 111, 112):
            continue
        branch_end = next(
            (
                candidate
                for candidate in range(index + 1, count)
                if codes[candidate] == 499 and indents[candidate] == indent
            ),
            None,
        )
        if branch_end is None:
            continue
        cases = [
            candidate
            for candidate in range(index + 1, branch_end)
            if codes[candidate] in case_codes and indents[candidate] == indent
        ]
        if not cases:
            continue
        after_branch = branch_end + 1
        branch_targets = list(cases)
        if not any(codes[candidate] in (420, 421) for candidate in cases):
            if after_branch < count:
                branch_targets.append(after_branch)
        successors[index] = tuple(branch_targets)
        numeric_branches.append((index, cases, after_branch))
        boundaries = [*cases[1:], branch_end]
        for case_index, boundary in zip(cases, boundaries):
            body_start = case_index + 1
            if body_start >= boundary:
                successors[case_index] = (
                    (after_branch,) if after_branch < count else tuple()
                )
                continue
            successors[case_index] = (body_start,)
            case_exits.append((body_start, boundary, after_branch))

    loop_headers = {}
    for index, code in enumerate(codes):
        if code not in (170, 179):
            continue
        indent = indents[index]
        loop_end = next(
            (
                candidate
                for candidate in range(index + 1, count)
                if codes[candidate] == 498 and indents[candidate] == indent
            ),
            None,
        )
        if loop_end is None:
            raise WolfEngineError("WOLF 循环缺少结束命令；请重新导出翻译文本")
        loop_headers[index] = loop_end
        targets = [index + 1] if index + 1 < count else []
        if code == 179 and loop_end + 1 < count:
            targets.append(loop_end + 1)
        successors[index] = tuple(targets)
        successors[loop_end] = tuple(targets)

    for index, code in enumerate(codes):
        if code not in (171, 176):
            continue
        indent = indents[index]
        enclosing = [
            (header, loop_end)
            for header, loop_end in loop_headers.items()
            if header < index < loop_end
            and indents[header] < indent
        ]
        if not enclosing:
            raise WolfEngineError("WOLF 循环控制命令缺少所属循环；请重新导出翻译文本")
        _header, loop_end = max(enclosing)
        target = loop_end + 1 if code == 171 else loop_end
        successors[index] = (target,) if target < count else tuple()

    for source, targets in enumerate(successors):
        normalized = []
        for target in targets:
            while True:
                replacement = next(
                    (
                        after_branch
                        for body_start, boundary, after_branch in case_exits
                        if body_start <= source < boundary and target == boundary
                    ),
                    None,
                )
                if replacement is None:
                    break
                target = replacement
            if target < count and target not in normalized:
                normalized.append(target)
        successors[source] = tuple(normalized)

    if isinstance(numeric_arguments, dict):
        initial_values = {
            1_600_000 + int(slot): value
            for slot, value in numeric_arguments.items()
            if isinstance(slot, int) and isinstance(value, int)
        }
        while True:
            numeric_values = _numeric_values_by_command(
                commands, successors, initial_values
            )
            changed = False
            for branch_index, cases, after_branch in numeric_branches:
                if len(successors[branch_index]) <= 1:
                    continue
                int_args = commands[branch_index].get("intArgs") or []
                if (
                    codes[branch_index] != 111
                    or len(cases) != 1
                    or codes[cases[0]] != 401
                    or len(int_args) < 4
                ):
                    continue
                try:
                    condition_count = int(int_args[0])
                    variable = int(int_args[1])
                    operator = int(int_args[2])
                    operand = int(int_args[3])
                except (TypeError, ValueError):
                    continue
                value = numeric_values[branch_index].get(variable)
                if condition_count != 1 or operator != 0 or value is None:
                    continue
                target = cases[0] if value == operand else after_branch
                selected = (target,) if target < count else tuple()
                if successors[branch_index] != selected:
                    successors[branch_index] = selected
                    changed = True
            if not changed:
                break

        numeric_values = _numeric_values_by_command(
            commands, successors, initial_values
        )
        for index, (slot, targets) in dynamic_dispatches.items():
            selected = targets.get(
                numeric_values[index].get(1_600_000 + slot)
            )
            if selected is not None:
                successors[index] = (selected,)
    return successors


def _common_dispatch_context(contexts, value):
    if not isinstance(value, int):
        return None
    if 0x80000000 <= value <= 0xFFFFFFFF:
        value -= 0x100000000
    return (
        value
        if abs(value) < 1_000_000 and value in contexts
        else None
    )


def _common_context_numeric_values(event, context):
    slot = event.get("dispatch_slot") if event else None
    return (
        {1_600_000 + slot: context}
        if isinstance(slot, int) and isinstance(context, int)
        else {}
    )


def _numeric_command_destination(command):
    try:
        code = int(command.get("code", -1))
        int_args = command.get("intArgs") or []
        if code in (121, 123, 124, 125):
            return int(int_args[0])
        if code == 250:
            return int(int_args[4])
        if code in (210, 211, 300) and int(int_args[1]) & 0x01000000:
            return int(int_args[-1])
    except (TypeError, ValueError, IndexError):
        return None
    return None


def _numeric_values_by_command(commands, successors, initial_values=None):
    if not commands:
        return []
    incoming = [None] * len(commands)
    incoming[0] = dict(initial_values or {})
    queue = deque([0])
    while queue:
        index = queue.popleft()
        outgoing = dict(incoming[index])
        command = commands[index]
        try:
            code = int(command.get("code", -1))
            int_args = command.get("intArgs") or []
        except (TypeError, ValueError, IndexError):
            code = -1
            int_args = []
        destination = _numeric_command_destination(command)
        if (
            destination is not None
            and code == 121
            and len(int_args) >= 4
            and int(int_args[2]) == 0
            and int(int_args[3]) == 0
        ):
            outgoing[destination] = int(int_args[1])
        elif destination is not None:
            outgoing.pop(destination, None)
        for successor in successors[index]:
            previous = incoming[successor]
            merged = (
                outgoing
                if previous is None
                else {
                    key: value
                    for key, value in previous.items()
                    if outgoing.get(key) == value
                }
            )
            if merged != previous:
                incoming[successor] = dict(merged)
                queue.append(successor)
    return [values or {} for values in incoming]


def _common_call_role_key(call, numeric_values=None):
    event = call.get("event") if call else None
    if not event:
        return None
    slot = event.get("dispatch_slot")
    contexts = event.get("dispatch_contexts", {})
    if isinstance(slot, int) and slot < len(call["numeric_inputs"]):
        raw_value = call["numeric_inputs"][slot]
        value = _common_dispatch_context(
            contexts, (numeric_values or {}).get(raw_value, raw_value)
        )
        if value in contexts:
            return event["id"], value
    return event["id"], None


def _reachable_command_indexes(commands, successors):
    if not commands:
        return set()
    reachable = set()
    queue = deque([0])
    while queue:
        index = queue.popleft()
        if index in reachable:
            continue
        reachable.add(index)
        queue.extend(successors[index])
    return reachable


def _trace_string_variable_usage(
    commands,
    start_index,
    destination,
    schemas,
    common_events_by_id,
    common_events_by_name,
    common_event_roles,
    successors,
    current_event_id=None,
    current_context=None,
    numeric_values_by_command=None,
):
    if not commands:
        return {
            "display": False,
            "logic": False,
            "dynamic_target": False,
            "opaque": False,
            "unknown": False,
            "return": False,
            "logic_codes": set(),
            "selector_targets": set(),
            "database_writes": set(),
        }
    incoming = [set() for _ in commands]
    queue = deque()
    starts = (0,) if start_index is None else successors[start_index]
    for successor in starts:
        incoming[successor].add(destination)
        queue.append(successor)

    display_use = False
    logic_use = False
    dynamic_target_use = False
    opaque_use = False
    unknown_use = False
    return_use = False
    logic_codes = set()
    selector_targets = set()
    database_writes = set()
    if numeric_values_by_command is None:
        numeric_values_by_command = _numeric_values_by_command(
            commands,
            successors,
            _common_context_numeric_values(
                common_events_by_id.get(current_event_id), current_context
            ),
        )
    while queue:
        index = queue.popleft()
        command = commands[index]
        active = incoming[index]
        if not active:
            continue
        try:
            code = int(command.get("code", -1))
        except (TypeError, ValueError):
            code = -1
        call = (
            _decode_common_call(
                command,
                common_events_by_id,
                common_events_by_name,
                current_event_id,
            )
            if code in (210, 300)
            else None
        )
        call_event = call["event"] if call else None
        call_output = (
            call["output"]
            if call and _string_variable_token(call["output"])
            else None
        )
        call_returns = set()
        used = set()
        if code in (210, 300):
            strings = command.get("stringArgs") or []
            for variable in active:
                token = _string_variable_token(variable)
                target_use = bool(
                    call and token and token in call["target_expression"]
                )
                if target_use:
                    used.add(variable)
                    logic_use = True
                    dynamic_target_use = True
                    logic_codes.add(code)
                    continue
                if call is None:
                    try:
                        raw_values = (command.get("intArgs") or [])[2:]
                    except TypeError:
                        raw_values = []
                    if variable in raw_values or any(token and token in str(value) for value in strings[1:]):
                        used.add(variable)
                        opaque_use = True
                    continue
                if variable in call["numeric_inputs"]:
                    used.add(variable)
                    logic_use = True
                    logic_codes.add(code)
                for slot, source_kind, source in call["string_inputs"]:
                    matches = (
                        source == variable
                        if source_kind == "variable"
                        else bool(token and token in source)
                    )
                    if not matches:
                        continue
                    used.add(variable)
                    if call_event is None:
                        opaque_use = True
                        continue
                    role = common_event_roles.get(
                        _common_call_role_key(
                            call, numeric_values_by_command[index]
                        ), {}
                    ).get(slot, set())
                    display_use = display_use or "display" in role
                    dynamic_target_use = (
                        dynamic_target_use or "dynamic_target" in role
                    )
                    database_writes.update(
                        item[1:]
                        for item in role
                        if isinstance(item, tuple)
                        and len(item) in (4, 5)
                        and item[0] == "database_write"
                    )
                    if "logic" in role:
                        logic_use = True
                        logic_codes.add(code)
                    opaque_use = opaque_use or "opaque" in role
                    unknown_use = unknown_use or "unknown" in role
                    if "return" in role and call_output is not None:
                        call_returns.add(variable)
        else:
            used = {
                variable
                for variable in active
                if _command_uses_string_variable(
                    command, variable, _string_variable_token(variable)
                )
            }
        database_write = _database_write(command, schemas)
        if (
            database_write is not None
            and database_write[1] in active
        ):
            used.add(database_write[1])
            database_writes.add(database_write[0])
        selector_variables = {
            variable
            for variable in active
            if _command_database_selector_role(command, variable)
        }
        if selector_variables:
            logic_use = True
            target = _database_target(command, schemas)
            if target and target[1] is not None:
                selector_targets.add(target[:2])

        if used:
            unsafe_conditions = {
                variable
                for variable in used
                if not _string_condition_is_presence_check(command, variable)
            }
            if code in (112, 140, 213) and unsafe_conditions:
                logic_use = True
                logic_codes.add(code)
            elif code in (101, 102):
                display_use = True
            elif code == 150:
                int_args = command.get("intArgs") or []
                try:
                    picture_type = (int(int_args[0]) >> 4) & 0x07
                except (TypeError, ValueError, IndexError):
                    picture_type = -1
                if picture_type == 2:
                    display_use = True
                else:
                    unknown_use = True
            elif code == 122 and any(
                isinstance(value, str) and _looks_like_logic_assignment(value)
                for value in (command.get("stringArgs") or [])
            ):
                logic_use = True
                logic_codes.add(code)
            elif code == 122 and any(
                isinstance(value, str) and _DISPLAY_STRING_FORMAT_RE.search(value)
                for value in (command.get("stringArgs") or [])
            ):
                display_use = True
            elif code == 250 and database_write is not None:
                pass
            elif code not in (103, 106, 112, 122, 210, 300):
                unknown_use = True

        outgoing = set(active)
        target = _command_string_destination(command)
        if code == 122 and target is not None:
            outgoing.discard(target)
            if used:
                outgoing.add(target)
        elif code == 250 and target is not None:
            outgoing.discard(target)
        elif code in (210, 300) and call_output is not None:
            outgoing.discard(call_output)
            if call_returns:
                outgoing.add(call_output)

        if not successors[index] and 1_600_005 in outgoing:
            strings = command.get("stringArgs") or []
            jump_target = strings[0] if code == 213 and strings else ""
            if code != 213 or str(jump_target).strip().upper() == "END":
                return_use = True
        for successor in successors[index]:
            merged = incoming[successor] | outgoing
            if merged != incoming[successor]:
                incoming[successor] = merged
                queue.append(successor)

    return {
        "display": display_use,
        "logic": logic_use,
        "dynamic_target": dynamic_target_use,
        "opaque": opaque_use,
        "unknown": unknown_use,
        "return": return_use,
        "logic_codes": logic_codes,
        "selector_targets": selector_targets,
        "database_writes": database_writes,
    }


def _summarize_common_event_string_roles(
    common_events,
    schemas,
    common_events_by_id,
    common_events_by_name,
):
    roles = {}
    for event in common_events:
        roles[(event["id"], None)] = {slot: set() for slot in range(5, 10)}
        for context in event.get("dispatch_contexts", {}):
            roles[(event["id"], context)] = {slot: set() for slot in range(5, 10)}
    changed = True
    while changed:
        changed = False
        for event in common_events:
            contexts = [(None, event["successors"]), *event.get("dispatch_contexts", {}).items()]
            for context, successors in contexts:
                numeric_values = _numeric_values_by_command(
                    event["commands"],
                    successors,
                    _common_context_numeric_values(event, context),
                )
                for slot in range(5, 10):
                    traced = _trace_string_variable_usage(
                        event["commands"],
                        None,
                        1_600_000 + slot,
                        schemas,
                        common_events_by_id,
                        common_events_by_name,
                        roles,
                        successors,
                        event["id"],
                        context,
                        numeric_values,
                    )
                    discovered = {
                        role
                        for role in (
                            "display",
                            "logic",
                            "dynamic_target",
                            "opaque",
                            "unknown",
                            "return",
                        )
                        if traced[role]
                    }
                    discovered.update(
                        ("database_write", *target)
                        for target in traced["database_writes"]
                    )
                    current = roles[(event["id"], context)][slot]
                    if not discovered <= current:
                        current.update(discovered)
                        changed = True
    return roles


def _summarize_common_event_return_roles(
    sequences,
    common_events,
    schemas,
    common_events_by_id,
    common_events_by_name,
    common_event_roles,
):
    role_keys = {
        (event["id"], context)
        for event in common_events
        for context in (None, *event.get("dispatch_contexts", {}))
    }
    roles = {key: set() for key in role_keys}
    dependencies = {key: set() for key in role_keys}
    for _relative, _base_path, commands, successors, owner_id, owner_context in sequences:
        numeric_values = _numeric_values_by_command(
            commands,
            successors,
            _common_context_numeric_values(
                common_events_by_id.get(owner_id), owner_context
            ),
        )
        for index in _reachable_command_indexes(commands, successors):
            command = commands[index]
            call = _decode_common_call(
                command,
                common_events_by_id,
                common_events_by_name,
                owner_id,
            )
            if (
                not call
                or not call["event"]
                or call["output"] is None
                or not _string_variable_token(call["output"])
            ):
                continue
            traced = _trace_string_variable_usage(
                commands,
                index,
                call["output"],
                schemas,
                common_events_by_id,
                common_events_by_name,
                common_event_roles,
                successors,
                owner_id,
                owner_context,
                numeric_values,
            )
            target_key = _common_call_role_key(
                call, numeric_values[index]
            )
            roles[target_key].update(
                role
                for role in (
                    "display", "logic", "dynamic_target", "opaque", "unknown"
                )
                if traced[role]
            )
            roles[target_key].update(
                ("database_write", *target)
                for target in traced["database_writes"]
            )
            owner_key = (owner_id, owner_context)
            if traced["return"] and owner_key in roles:
                dependencies[target_key].add(owner_key)

    changed = True
    while changed:
        changed = False
        for role_key, targets in dependencies.items():
            discovered = set().union(*(roles[target] for target in targets)) if targets else set()
            if not discovered <= roles[role_key]:
                roles[role_key].update(discovered)
                changed = True
    return roles


def _command_database_selector_role(command, variable_id):
    try:
        if int(command.get("code", -1)) != 250:
            return None
        int_args = command.get("intArgs") or []
        if len(int_args) < 4:
            return None
        flags = int(int_args[3])
        for index, name_flag, role in (
            (0, _DATABASE_TYPE_NAME_FLAG, "type"),
            (1, _DATABASE_DATA_NAME_FLAG, "data"),
            (2, _DATABASE_FIELD_NAME_FLAG, "field"),
        ):
            if not flags & name_flag and int(int_args[index]) == variable_id:
                return role
    except (TypeError, ValueError):
        return None
    return None


def _record_selector_access(command, schemas, identifier_indexes):
    target = _database_target(command, schemas)
    if target is None:
        return None, None, None
    try:
        int_args = command.get("intArgs") or []
        flags = int(int_args[3])
        type_selector = int(int_args[0])
    except (TypeError, ValueError, IndexError):
        return target[:2], "unknown", None
    dynamic_type = (
        not flags & _DATABASE_TYPE_NAME_FLAG and type_selector >= 1_000_000
    )
    namespace = (target[0], None) if dynamic_type else target[:2]
    if not flags & _DATABASE_DATA_NAME_FLAG:
        return namespace, "numeric", None
    if dynamic_type:
        return namespace, "unknown", None
    indexes = identifier_indexes.get(namespace, {}).get(
        target[2].strip().casefold(), set()
    )
    if not indexes:
        dynamic_name = bool(re.search(r"\\[A-Za-z]+\[", target[2]))
        return namespace, "unknown" if dynamic_name else "missing", None
    return namespace, "name", tuple(sorted(indexes))


def _database_identifier_collision_policy(usage, namespace):
    policy = _database_identifier_translation_policy(usage, namespace)
    if policy == "name_closed":
        return "name_referenced"
    if policy == "unsafe":
        return "unknown"
    return "numeric_only"


def _database_identifier_translation_policy(usage, namespace):
    namespace = tuple(namespace)
    cached = usage.get("database_identifier_translation_policy", {}).get(namespace)
    if cached in ("numeric_only", "name_closed", "unsafe"):
        return cached
    profile = usage.get("database_identifier_access", {}).get(namespace, {})
    if (
        profile.get("unknown", 0)
        or not (profile.get("numeric", 0) or profile.get("name", 0))
    ):
        return "unsafe"
    return "name_closed" if profile.get("name", 0) else "numeric_only"


def _symbol_expression_tokens(value):
    if not isinstance(value, str) or not value.strip() or _looks_like_resource(value):
        return ()
    tokens = []
    for part in value.split("|"):
        token = part.strip()
        while True:
            match = re.match(r"^<[^>\r\n]+>", token)
            if not match:
                break
            token = token[match.end():].strip()
        if (
            not token
            or token == "INVALID_IGNORE"
            or re.fullmatch(r"[+-]?\d+(?:\.\d+)?", token)
        ):
            continue
        tokens.append(token)
    return tuple(tokens)


def _infer_database_symbol_references(
    databases,
    database_relatives,
    common_event_names,
    identifier_indexes,
    usage,
):
    catalog = {
        name.strip().casefold()
        for name in common_event_names
        if isinstance(name, str) and name.strip()
    }
    identifier_symbols = {}
    for database_name, database in databases.items():
        for type_index, type_data in enumerate(database.get("types", [])):
            type_name = type_data.get("name")
            if isinstance(type_name, str) and type_name.strip():
                catalog.add(type_name.strip().casefold())
            for schema_field in type_data.get("fields", []):
                field_name = schema_field.get("name") if isinstance(schema_field, dict) else None
                if isinstance(field_name, str) and field_name.strip():
                    catalog.add(field_name.strip().casefold())
            for data_index, datum in enumerate(type_data.get("data", [])):
                datum_name = datum.get("name")
                if isinstance(datum_name, str) and datum_name.strip():
                    catalog.add(datum_name.strip().casefold())
                fields = datum.get("data") or []
                if not fields or not _uses_first_string_database_id(datum, 0):
                    continue
                value = fields[0].get("value")
                if not isinstance(value, str) or not value.strip():
                    continue
                symbol = value.strip().casefold()
                catalog.add(symbol)
                namespace_targets = identifier_symbols.setdefault(symbol, {})
                namespace_targets.setdefault((database_name, type_index), set()).add(data_index)

    for database_name, database in databases.items():
        relative = database_relatives[database_name]
        for type_index, type_data in enumerate(database.get("types", [])):
            rows = type_data.get("data", [])
            field_count = max((len(row.get("data") or []) for row in rows), default=0)
            for field_index in range(field_count):
                field_key = (database_name, type_index, field_index)
                occurrences = []
                resolved_record_count = 0
                for data_index, datum in enumerate(rows):
                    fields = datum.get("data") or []
                    if field_index >= len(fields) or _uses_first_string_database_id(datum, field_index):
                        continue
                    field = fields[field_index]
                    value = field.get("value")
                    if not isinstance(value, str) or not value.strip():
                        continue
                    record_key = (database_name, type_index, data_index, field_index)
                    observed_display = (
                        field_key in usage["display_database_fields"]
                        or record_key in usage["display_database_records"]
                    )
                    observed_logic = (
                        field_key in usage["logic_database_fields"]
                        or record_key in usage["logic_database_records"]
                    )
                    if observed_display and not observed_logic:
                        continue
                    tokens = _symbol_expression_tokens(value)
                    if not tokens:
                        continue
                    resolved = tuple(
                        token for token in tokens if token.casefold() in catalog
                    )
                    if resolved:
                        resolved_record_count += 1
                    occurrences.append((data_index, value, resolved))

                if not occurrences or not resolved_record_count:
                    continue
                target_hints = usage.get(
                    "database_field_reference_targets", {}
                ).get(field_key, set())
                if not target_hints:
                    continue

                usage["symbol_reference_database_fields"].add(field_key)
                for data_index, expected_value, resolved_tokens in occurrences:
                    record_key = (database_name, type_index, data_index, field_index)
                    usage["logic_database_records"].add(record_key)
                    usage["display_database_records"].discard(record_key)
                    path = ("types", type_index, "data", data_index, "data", field_index, "value")
                    for token in resolved_tokens:
                        normalized_token = token.casefold()
                        targets = identifier_symbols.get(normalized_token, {})
                        if not targets:
                            continue
                        hinted_targets = {
                            namespace: indexes
                            for namespace, indexes in targets.items()
                            if namespace in target_hints
                        }
                        targets = hinted_targets
                        if len(targets) != 1:
                            usage["identifier_reference_namespaces"].update(targets)
                            usage["incomplete_identifier_symbols"].update(
                                (*namespace, normalized_token) for namespace in targets
                            )
                            continue
                        namespace, target_indexes = next(iter(targets.items()))
                        usage["identifier_reference_namespaces"].add(namespace)
                        usage["identifier_reference_paths"].setdefault(
                            (*namespace, normalized_token), set()
                        ).add((
                            relative,
                            path,
                            tuple(sorted(target_indexes)),
                            expected_value,
                            "database_symbol",
                        ))

    # Equality across first-string namespaces is risk evidence, not alias proof;
    # a typed runtime provenance graph is required before these names can move.
    for normalized_symbol, targets in identifier_symbols.items():
        if len(targets) < 2:
            continue
        protected = [
            namespace
            for namespace in targets
            if usage["database_identifier_access"].get(namespace, {}).get("name", 0)
        ]
        if not protected:
            continue
        candidate_namespaces = [list(namespace) for namespace in sorted(targets)]
        for namespace in protected:
            usage["incomplete_identifier_symbols"].add(
                (*namespace, normalized_symbol)
            )
            data_index = min(targets[namespace])
            usage["analysis_diagnostics"].append({
                "reason": "ambiguous_database_identifier_alias",
                "file": database_relatives[namespace[0]],
                "path": ["types", namespace[1], "data", data_index, "data", 0, "value"],
                "effect": "protected",
                "value": normalized_symbol,
                "candidate_namespaces": candidate_namespaces,
            })

    usage["display_database_fields"] -= usage["logic_database_fields"]
    usage["display_database_records"] -= usage["logic_database_records"]
    usage["identifier_symbols"] = identifier_symbols


def _common_event_argument_slot(field_name):
    match = _COMMON_EVENT_ARGUMENT_FIELD_RE.search(str(field_name or ""))
    if not match:
        return None
    numbers = [int(value) for value in re.findall(r"\d+", match.group(1))]
    return next((value - 1 for value in reversed(numbers) if 1 <= value <= 5), None)


def _database_type_reference_key(value):
    value = re.sub(
        r"^[\s\u3000◆◇●○┠└xX]*(?:\[[^\]\r\n]*\][\s\u3000]*)*",
        "",
        str(value or ""),
    )
    return re.sub(r"[\s\u3000]+", "", value).casefold()


def _database_common_event_contexts(
    databases, common_events_by_id, common_events_by_name, active_fields
):
    contexts = set()
    logic_records = set()
    opaque_records = set()
    bindings = []
    diagnostics = []
    callbacks = []

    for database_name, database in databases.items():
        for type_index, type_data in enumerate(database.get("types", [])):
            schema_fields = type_data.get("fields") or []
            for data_index, datum in enumerate(type_data.get("data", [])):
                fields = datum.get("data") or []
                for field_index, field in enumerate(fields):
                    schema_field = (
                        schema_fields[field_index]
                        if field_index < len(schema_fields)
                        else {}
                    )
                    field_name = str(
                        (schema_field or {}).get("name")
                        or field.get("name")
                        or ""
                    )
                    field_key = (database_name, type_index, field_index)
                    name_candidate = bool(
                        _COMMON_EVENT_CALLBACK_FIELD_RE.search(field_name)
                    )
                    flow_candidate = field_key in active_fields
                    if not name_candidate and not flow_candidate:
                        continue

                    record_key = (database_name, type_index, data_index, field_index)
                    logic_records.add(record_key)
                    argument_fields = []
                    for argument_index in range(
                        field_index + 1, min(field_index + 6, len(fields))
                    ):
                        argument_field = fields[argument_index]
                        argument_schema = (
                            schema_fields[argument_index]
                            if argument_index < len(schema_fields)
                            else {}
                        )
                        argument_name = str(
                            (argument_schema or {}).get("name")
                            or argument_field.get("name")
                            or ""
                        )
                        if _COMMON_EVENT_CALLBACK_FIELD_RE.search(argument_name):
                            break
                        slot = _common_event_argument_slot(argument_name)
                        if slot is None:
                            break
                        argument_fields.append((
                            slot,
                            (database_name, type_index, data_index, argument_index),
                            argument_field.get("value"),
                        ))
                    if name_candidate and not flow_candidate:
                        logic_records.update(
                            record for _slot, record, _value in argument_fields
                        )

                    value = field.get("value")
                    event = None
                    wrapper_context = None
                    if isinstance(value, str) and value.strip():
                        event_name = value.strip()
                        for wrapper_re in _COMMON_EVENT_WRAPPER_RES:
                            wrapper_match = wrapper_re.fullmatch(event_name)
                            if wrapper_match:
                                event_name = wrapper_match.group(1).strip()
                                wrapper_context = int(wrapper_match.group(2))
                                break
                        event = common_events_by_name.get(event_name.casefold())
                    elif (
                        isinstance(value, int)
                        and _COMMON_EVENT_NUMERIC_ID_FIELD_RE.search(field_name)
                    ):
                        event = common_events_by_id.get(value)

                    callback_context = None
                    if event and isinstance(event.get("dispatch_slot"), int):
                        numeric_arguments = {
                            slot: argument_value
                            for slot, _record, argument_value in argument_fields
                            if isinstance(argument_value, int)
                        }
                        dispatch_value = (
                            wrapper_context
                            if wrapper_context is not None
                            else numeric_arguments.get(event["dispatch_slot"])
                        )
                        callback_context = _common_dispatch_context(
                            event.get("dispatch_contexts", {}), dispatch_value
                        )
                    callback = {
                        "database": database_name,
                        "type": type_index,
                        "field": field_index,
                        "record": record_key,
                        "field_name": field_name,
                        "value": value,
                        "arguments": argument_fields,
                        "event": event,
                        "context": callback_context,
                        "active": flow_candidate,
                    }
                    callbacks.append(callback)

    for callback in callbacks:
        if not callback["active"]:
            continue
        event = callback["event"]
        if not event:
            opaque_records.add(callback["record"])
            logic_records.update(record for _slot, record, _value in callback["arguments"])
            opaque_records.update(record for _slot, record, _value in callback["arguments"])
            value = callback["value"]
            stripped_value = value.strip() if isinstance(value, str) else value
            target_contexts = set()
            reference_match = (
                _COMMON_EVENT_TYPE_REFERENCE_RE.search(stripped_value)
                if isinstance(stripped_value, str)
                else None
            )
            if reference_match:
                delegated_type = (
                    reference_match.group(1).upper(),
                    _database_type_reference_key(reference_match.group(2)),
                )
            else:
                delegated_type = None
            if isinstance(stripped_value, str) and stripped_value:
                diagnostics.append({
                    "database": callback["database"],
                    "type": callback["type"],
                    "field": callback["field"],
                    "value": stripped_value,
                    "candidate_event_ids": tuple(sorted({
                        event_id for event_id, _context in target_contexts
                    })),
                    "candidate_contexts": tuple(sorted(
                        target_contexts,
                        key=lambda item: (item[0], item[1] is None, item[1] or 0),
                    )),
                    "delegated_type": delegated_type,
                })
            continue

        context = callback["context"]
        contexts.add((event["id"], context))
        bindings.append((event["id"], context, callback["arguments"]))

    return contexts, logic_records, opaque_records, bindings, diagnostics


def _reachable_dynamic_callback_fields(
    sequences,
    schemas,
    common_events_by_id,
    common_events_by_name,
    common_event_roles,
):
    fields = set()
    for _relative, _base_path, commands, successors, owner_id, owner_context in sequences:
        numeric_values = _numeric_values_by_command(
            commands,
            successors,
            _common_context_numeric_values(
                common_events_by_id.get(owner_id), owner_context
            ),
        )
        for index in _reachable_command_indexes(commands, successors):
            read = _database_read(commands[index], schemas)
            if read is None:
                continue
            key, destination, _token = read
            traced = _trace_string_variable_usage(
                commands,
                index,
                destination,
                schemas,
                common_events_by_id,
                common_events_by_name,
                common_event_roles,
                successors,
                owner_id,
                owner_context,
                numeric_values,
            )
            if traced["dynamic_target"]:
                fields.add(key)
    return fields


def _analyze_json_usage(json_root, referenced_maps=None):
    referenced_maps = {
        str(filename).casefold() for filename in (referenced_maps or ())
    }
    usage = {
        "display_database_fields": set(),
        "display_database_records": set(),
        "visible_database_fields": set(),
        "visible_database_records": set(),
        "logic_database_fields": set(),
        "logic_database_records": set(),
        "unknown_database_fields": set(),
        "unknown_database_records": set(),
        "opaque_database_fields": set(),
        "opaque_database_records": set(),
        "nonselector_logic_database_fields": set(),
        "nonselector_logic_database_records": set(),
        "nonselector_logic_field_codes": {},
        "nonselector_logic_record_codes": {},
        "named_database_record_references": set(),
        "database_identifier_access": {},
        "database_identifier_translation_policy": {},
        "identifier_reference_paths": {},
        "identifier_reference_namespaces": set(),
        "incomplete_identifier_symbols": set(),
        "missing_identifier_names": {},
        "symbol_reference_database_fields": set(),
        "database_field_reference_targets": {},
        "comparison_literals": set(),
        "display_command_literals": set(),
        "logic_command_literals": set(),
        "unresolved_database_callbacks": [],
        "analysis_diagnostics": [],
        "protected_unknown_common_files": set(),
        "database_write_targets": {},
    }
    schemas = {}
    databases = {}
    database_relatives = {}
    for database_name in _DATABASE_JSON_BY_KIND.values():
        database_dir = os.path.join(json_root, "databases")
        filenames = [
            filename
            for filename in os.listdir(database_dir)
            if filename.casefold() == database_name.casefold()
        ] if os.path.isdir(database_dir) else []
        if len(filenames) > 1:
            raise WolfEngineError(f"WOLF 数据库文件无法唯一定位: {database_name}")
        if not filenames:
            continue
        relative = f"databases/{filenames[0]}"
        path = os.path.join(json_root, "databases", filenames[0])
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as source:
                database = json.load(source)
        except (OSError, ValueError) as error:
            raise WolfEngineError(f"无法分析 WOLF 数据库结构: {database_name}: {error}") from error
        databases[database_name] = database
        database_relatives[database_name] = relative
        type_names = {}
        field_names = {}
        for type_index, type_data in enumerate(database.get("types", [])):
            name = type_data.get("name")
            if isinstance(name, str) and name.strip():
                type_names.setdefault(name.strip().casefold(), type_index)
            fields = field_names.setdefault(type_index, {})
            for datum in type_data.get("data", []):
                for field_index, field in enumerate(datum.get("data", [])):
                    field_name = field.get("name")
                    if isinstance(field_name, str) and field_name.strip():
                        fields.setdefault(field_name.strip().casefold(), field_index)
        schemas[database_name] = {"types": type_names, "fields": field_names}

    identifier_indexes = {}
    record_indexes = {}
    record_counts = {}
    for database_name, database in databases.items():
        for type_index, type_data in enumerate(database.get("types", [])):
            namespace_index = identifier_indexes.setdefault((database_name, type_index), {})
            record_index = record_indexes.setdefault((database_name, type_index), {})
            records = type_data.get("data", [])
            record_counts[(database_name, type_index)] = len(records)
            for data_index, datum in enumerate(records):
                fields = datum.get("data") or []
                datum_name = datum.get("name")
                if isinstance(datum_name, str) and datum_name.strip():
                    record_index.setdefault(datum_name.strip().casefold(), set()).add(data_index)
                if not fields or not _uses_first_string_database_id(datum, 0):
                    continue
                value = fields[0].get("value")
                if isinstance(value, str) and value:
                    namespace_index.setdefault(value.strip().casefold(), set()).add(data_index)
                    record_index.setdefault(value.strip().casefold(), set()).add(data_index)

    map_sequences = []
    identifier_reference_sequences = []
    common_events = []
    common_event_names = set()
    common_events_by_id = {}
    common_events_by_name = {}
    for root, _, files in os.walk(json_root):
        parent = os.path.basename(root).lower()
        if parent not in ("common", "maps"):
            continue
        for filename in files:
            if not filename.lower().endswith(".json"):
                continue
            if parent == "maps" and referenced_maps and filename.casefold() not in referenced_maps:
                continue
            try:
                with open(os.path.join(root, filename), "r", encoding="utf-8") as source:
                    data = json.load(source)
            except (OSError, ValueError) as error:
                raise WolfEngineError(f"无法分析 WOLF JSON 用途: {filename}: {error}") from error
            if not isinstance(data, dict):
                continue
            relative = os.path.relpath(os.path.join(root, filename), json_root).replace(os.sep, "/")
            if parent == "common":
                arguments = data.get("arguments")
                activation = data.get("activation")
                if (
                    not isinstance(arguments, list)
                    or len(arguments) != 10
                    or not all(isinstance(argument, str) for argument in arguments)
                ):
                    raise WolfEngineError(
                        "WOLF 公共事件缺少参数元数据；请用当前版本重新导出翻译文本"
                    )
                if not isinstance(activation, dict):
                    raise WolfEngineError(
                        "WOLF 公共事件缺少启动元数据；请用当前版本重新导出翻译文本"
                    )
                activation_raw = activation.get("raw")
                activation_extra = activation.get("extra")
                if (
                    not isinstance(activation_raw, int)
                    or isinstance(activation_raw, bool)
                    or not 0 <= activation_raw <= 0xFFFFFFFF
                    or not isinstance(activation_extra, list)
                    or len(activation_extra) != 7
                    or not all(
                        isinstance(value, int)
                        and not isinstance(value, bool)
                        and 0 <= value <= 0xFF
                        for value in activation_extra
                    )
                ):
                    raise WolfEngineError("WOLF 公共事件启动元数据无效")
                event_id = data.get("id")
                if (
                    not isinstance(event_id, int)
                    or isinstance(event_id, bool)
                    or event_id < 0
                ):
                    raise WolfEngineError("WOLF 公共事件编号无效")
                if event_id in common_events_by_id:
                    raise WolfEngineError(f"WOLF 公共事件编号重复: {event_id}")
                name = data.get("name")
                if isinstance(name, str) and name.strip():
                    common_event_names.add(name.strip())
                commands = data.get("commands", [])
                successors = _command_successors(commands)
                labels = {}
                for command_index, command in enumerate(commands):
                    try:
                        if int(command.get("code", -1)) != 212:
                            continue
                    except (TypeError, ValueError):
                        continue
                    strings = command.get("stringArgs") or []
                    if strings and isinstance(strings[0], str):
                        labels.setdefault(strings[0], command_index)
                dispatches = _dynamic_jump_dispatches(commands, labels)
                dispatch_slots = {slot for slot, _targets in dispatches.values()}
                dispatch_slot = next(iter(dispatch_slots)) if len(dispatch_slots) == 1 else None
                dispatch_values = (
                    set().union(*(set(targets) for _slot, targets in dispatches.values()))
                    if dispatch_slot is not None and dispatches
                    else set()
                )
                event = {
                    "id": event_id,
                    "name": name.strip() if isinstance(name, str) else "",
                    "relative": relative,
                    "arguments": arguments,
                    "activation_raw": activation_raw,
                    "activation_extra": tuple(activation_extra),
                    "activation_mode": activation_raw & 0xFF,
                    "commands": commands,
                    "successors": successors,
                    "dispatch_slot": dispatch_slot,
                    "dispatch_contexts": {
                        value: _command_successors(commands, {dispatch_slot: value})
                        for value in dispatch_values
                    },
                }
                identifier_reference_sequences.append((
                    relative, ("commands",), commands
                ))
                common_events.append(event)
                common_events_by_id[event_id] = event
                if event["name"]:
                    name_key = event["name"].casefold()
                    common_events_by_name[name_key] = (
                        event if name_key not in common_events_by_name else None
                    )
            else:
                for event_index, event in enumerate(data.get("events", [])):
                    for page_index, page in enumerate(event.get("pages", [])):
                        commands = page.get("list", [])
                        sequence = (
                            relative,
                            ("events", event_index, "pages", page_index, "list"),
                            commands,
                            _command_successors(commands),
                            None,
                            None,
                        )
                        map_sequences.append(sequence)
                        identifier_reference_sequences.append(sequence[:3])

    sequences = list(map_sequences)
    common_event_roles = _summarize_common_event_string_roles(
        common_events,
        schemas,
        common_events_by_id,
        common_events_by_name,
    )
    automatic_contexts = {
        (event["id"], None)
        for event in common_events
        if event["activation_mode"] == 0x23
    }
    pending_contexts = deque(sorted(
        automatic_contexts,
        key=lambda item: (item[0], item[1] is None, item[1] or 0),
    ))
    visited_contexts = set()

    def enqueue_calls(queue, commands, successors, owner_id, owner_context=None):
        numeric_values = _numeric_values_by_command(
            commands,
            successors,
            _common_context_numeric_values(
                common_events_by_id.get(owner_id), owner_context
            ),
        )
        for command_index in _reachable_command_indexes(commands, successors):
            call = _decode_common_call(
                commands[command_index],
                common_events_by_id,
                common_events_by_name,
                owner_id,
            )
            role_key = _common_call_role_key(call, numeric_values[command_index])
            if role_key is not None:
                queue.append(role_key)

    for _relative, _base_path, commands, successors, _owner_id, _owner_context in map_sequences:
        enqueue_calls(pending_contexts, commands, successors, None)

    def consume_contexts(queue, visited, target_sequences, skip=None):
        while queue:
            event_id, context = queue.popleft()
            key = (event_id, context)
            if key in visited or key in (skip or ()):
                continue
            event = common_events_by_id[event_id]
            successors = event["dispatch_contexts"].get(context, event["successors"])
            visited.add(key)
            target_sequences.append((
                event["relative"],
                ("commands",),
                event["commands"],
                successors,
                event["id"],
                context,
            ))
            enqueue_calls(
                queue, event["commands"], successors, event["id"], context
            )

    consume_contexts(pending_contexts, visited_contexts, sequences)

    active_callback_fields = set()
    while True:
        discovered_fields = _reachable_dynamic_callback_fields(
            sequences,
            schemas,
            common_events_by_id,
            common_events_by_name,
            common_event_roles,
        )
        if discovered_fields <= active_callback_fields:
            break
        active_callback_fields.update(discovered_fields)
        database_contexts, *_unused = _database_common_event_contexts(
            databases,
            common_events_by_id,
            common_events_by_name,
            active_callback_fields,
        )
        pending_contexts.extend(sorted(
            database_contexts - visited_contexts,
            key=lambda item: (item[0], item[1] is None, item[1] or 0),
        ))
        consume_contexts(pending_contexts, visited_contexts, sequences)

    (
        _database_contexts,
        callback_logic_records,
        callback_opaque_records,
        callback_bindings,
        callback_diagnostics,
    ) = _database_common_event_contexts(
        databases,
        common_events_by_id,
        common_events_by_name,
        active_callback_fields,
    )
    usage["logic_database_records"].update(callback_logic_records)
    usage["opaque_database_records"].update(callback_opaque_records)
    usage["unresolved_database_callbacks"] = callback_diagnostics
    for diagnostic in callback_diagnostics:
        database_name = diagnostic.get("database")
        usage["analysis_diagnostics"].append({
            "reason": "unresolved_database_callback",
            "file": database_relatives.get(
                database_name, f"databases/{database_name}"
            ),
            "path": [
                "types",
                diagnostic.get("type"),
                "fields",
                diagnostic.get("field"),
            ],
            "effect": "protected",
            "details": diagnostic,
        })

    unknown_root_events = [
        event
        for event in common_events
        if event["activation_mode"] not in (0x20, 0x23)
    ]
    usage["protected_unknown_common_files"] = {
        event["relative"] for event in unknown_root_events
    }
    for event in unknown_root_events:
        usage["analysis_diagnostics"].append({
            "reason": "unknown_common_event_activation",
            "file": event["relative"],
            "path": ["activation"],
            "effect": "protected",
            "details": {
                "raw": event["activation_raw"],
                "mode": event["activation_mode"],
                "extra": list(event["activation_extra"]),
            },
        })

    common_event_return_roles = _summarize_common_event_return_roles(
        sequences,
        common_events,
        schemas,
        common_events_by_id,
        common_events_by_name,
        common_event_roles,
    )
    for event_id, context, argument_fields in callback_bindings:
        roles = common_event_roles[(event_id, context)]
        for slot, record_key, value in argument_fields:
            if not isinstance(value, str) or not value.strip():
                continue
            role = roles.get(slot + 5, set())
            unsafe_roles = {"logic", "opaque", "unknown"} & role
            if "display" in role and not unsafe_roles:
                usage["visible_database_records"].add(record_key)
                usage["display_database_records"].add(record_key)
            else:
                usage["logic_database_records"].add(record_key)
            if "opaque" in role:
                usage["opaque_database_records"].add(record_key)
            if "unknown" in role:
                usage["unknown_database_records"].add(record_key)
    usage["active_common_event_files"] = {
        common_events_by_id[event_id]["relative"]
        for event_id, _context in visited_contexts
    } | usage["protected_unknown_common_files"]
    usage["common_event_roles_by_id"] = {
        event["id"]: common_event_roles[(event["id"], None)]
        for event in common_events
    }
    usage["common_event_roles_by_name"] = {
        name: common_event_roles[(event["id"], None)] if event else None
        for name, event in common_events_by_name.items()
    }
    usage["common_event_return_roles_by_id"] = {
        event["id"]: common_event_return_roles[(event["id"], None)]
        for event in common_events
    }
    usage["common_event_return_roles_by_name"] = {
        name: common_event_return_roles[(event["id"], None)] if event else set()
        for name, event in common_events_by_name.items()
    }
    usage["common_event_contexts_by_id"] = {
        event["id"]: {
            "slot": event["dispatch_slot"],
            "roles": {
                context: common_event_roles[(event["id"], context)]
                for context in event["dispatch_contexts"]
            },
            "return_roles": {
                context: common_event_return_roles[(event["id"], context)]
                for context in event["dispatch_contexts"]
            },
        }
        for event in common_events
        if event["dispatch_contexts"]
    }
    usage["common_event_contexts_by_name"] = {
        name: usage["common_event_contexts_by_id"].get(event["id"])
        if event else None
        for name, event in common_events_by_name.items()
    }

    usage["reachable_call_argument_roles"] = {}
    usage["reachable_call_return_roles"] = {}
    for relative, base_path, commands, successors, owner_id, owner_context in sequences:
        numeric_values = _numeric_values_by_command(
            commands,
            successors,
            _common_context_numeric_values(
                common_events_by_id.get(owner_id), owner_context
            ),
        )
        for command_index in _reachable_command_indexes(commands, successors):
            call = _decode_common_call(
                commands[command_index],
                common_events_by_id,
                common_events_by_name,
                owner_id,
            )
            target_key = _common_call_role_key(call, numeric_values[command_index])
            if target_key is None:
                continue
            command_path = base_path + (command_index,)
            target_roles = common_event_roles[target_key]
            usage["reachable_call_return_roles"].setdefault(
                (relative, command_path), set()
            ).update(common_event_return_roles[target_key])
            for slot, source_kind, _source in call["string_inputs"]:
                if source_kind != "literal":
                    continue
                string_index = 1 + slot - 5
                usage["reachable_call_argument_roles"].setdefault(
                    (relative, command_path, string_index), set()
                ).update(target_roles.get(slot, set()))

    usage["reachable_command_paths"] = set()
    for relative, base_path, commands, successors, _owner_id, _owner_context in sequences:
        for command_index in _reachable_command_indexes(commands, successors):
            usage["reachable_command_paths"].add(
                (relative, base_path + (command_index,))
            )

    for relative, base_path, commands in identifier_reference_sequences:
        for command_index, command in enumerate(commands):
            namespace, access, data_indexes = _record_selector_access(
                command, schemas, identifier_indexes
            )
            if namespace is None:
                continue
            if access is None:
                continue
            if access == "missing" and namespace[1] is not None:
                target = _database_target(command, schemas)
                if target and target[2]:
                    normalized_name = target[2].strip().casefold()
                    usage["missing_identifier_names"].setdefault(
                        namespace, set()
                    ).add(normalized_name)
                    # Index alignment proves only one-hop runtime mirrors;
                    # exported database provenance is required for longer chains.
                    aliases = [
                        candidate
                        for candidate, names in identifier_indexes.items()
                        if candidate != namespace
                        and candidate[1] == namespace[1]
                        and normalized_name in names
                    ]
                    aligned_aliases = [
                        candidate
                        for candidate in aliases
                        if min(identifier_indexes[candidate][normalized_name])
                        < record_counts.get(namespace, 0)
                    ]
                    if len(aligned_aliases) == 1:
                        candidate = aligned_aliases[0]
                        candidate_indexes = tuple(sorted(
                            identifier_indexes[candidate][normalized_name]
                        ))
                        usage["identifier_reference_namespaces"].add(candidate)
                        usage["identifier_reference_paths"].setdefault(
                            (*candidate, normalized_name), set()
                        ).add((
                            relative,
                            base_path + (command_index, "stringArgs", 2),
                            candidate_indexes,
                            target[2],
                            "runtime_database_alias",
                        ))
                        usage["missing_identifier_names"].setdefault(
                            candidate, set()
                        ).update(record_indexes.get(namespace, {}))
                    elif aliases:
                        for candidate in aliases:
                            usage["incomplete_identifier_symbols"].add(
                                (*candidate, normalized_name)
                            )
                        usage["analysis_diagnostics"].append({
                            "reason": "unresolved_runtime_database_alias",
                            "file": relative,
                            "path": list(base_path + (command_index, "stringArgs", 2)),
                            "effect": "protected",
                            "value": target[2],
                            "selector_namespace": list(namespace),
                            "candidate_namespaces": [
                                list(candidate) for candidate in sorted(aliases)
                            ],
                        })
            affected_namespaces = (
                [namespace]
                if namespace[1] is not None
                else [candidate for candidate in identifier_indexes if candidate[0] == namespace[0]]
            )
            for affected in affected_namespaces:
                profile = usage["database_identifier_access"].setdefault(
                    affected, {"numeric": 0, "name": 0, "missing": 0, "unknown": 0}
                )
                profile[access] += 1
            if access == "name":
                target = _database_target(command, schemas)
                path = base_path + (command_index, "stringArgs", 2)
                usage["identifier_reference_paths"].setdefault(
                    (*namespace, target[2].strip().casefold()), set()
                ).add((relative, path, data_indexes, target[2], "database_selector"))

    for _relative, _base_path, commands in identifier_reference_sequences:
        for command in commands:
            try:
                code = int(command.get("code", -1))
            except (TypeError, ValueError):
                continue
            if code == 112:
                usage["comparison_literals"].update(
                    text
                    for text in (command.get("stringArgs") or [])
                    if isinstance(text, str) and text.strip()
                )
            elif code == 250:
                target = _database_target(command, schemas)
                if target and target[2]:
                    usage["named_database_record_references"].add(
                        (target[0], target[1], target[2].casefold())
                    )

    for _relative, _base_path, commands, successors, owner_id, owner_context in sequences:
        numeric_values = _numeric_values_by_command(
            commands,
            successors,
            _common_context_numeric_values(
                common_events_by_id.get(owner_id), owner_context
            ),
        )
        for index in _reachable_command_indexes(commands, successors):
            command = commands[index]
            read = _database_read(command, schemas)
            if read is None:
                continue
            key, destination, _token = read
            data_indexes = _database_read_record_indexes(
                command, schemas, record_indexes, record_counts
            )
            traced = _trace_string_variable_usage(
                commands,
                index,
                destination,
                schemas,
                common_events_by_id,
                common_events_by_name,
                common_event_roles,
                successors,
                owner_id,
                owner_context,
                numeric_values,
            )
            return_roles = (
                common_event_return_roles.get((owner_id, owner_context), set())
                if traced["return"]
                else set()
            )
            display_use = traced["display"] or "display" in return_roles
            logic_use = traced["logic"] or "logic" in return_roles
            unknown_flow = traced["unknown"] or "unknown" in return_roles
            opaque_flow = traced["opaque"] or "opaque" in return_roles
            nonselector_logic_codes = set(traced["logic_codes"])
            if "logic" in return_roles:
                nonselector_logic_codes.add(210)
            if traced["selector_targets"]:
                usage["database_field_reference_targets"].setdefault(
                    key, set()
                ).update(traced["selector_targets"])
            record_keys = (
                [(*key[:2], data_index, key[2]) for data_index in data_indexes]
                if data_indexes is not None
                else None
            )
            if traced["database_writes"]:
                for source_key in record_keys or (key,):
                    usage["database_write_targets"].setdefault(
                        source_key, set()
                    ).update(traced["database_writes"])
            if unknown_flow:
                if record_keys is None:
                    usage["unknown_database_fields"].add(key)
                else:
                    usage["unknown_database_records"].update(record_keys)
            if opaque_flow:
                if record_keys is None:
                    usage["opaque_database_fields"].add(key)
                else:
                    usage["opaque_database_records"].update(record_keys)
            if display_use:
                if record_keys is None:
                    usage["visible_database_fields"].add(key)
                else:
                    usage["visible_database_records"].update(record_keys)
            if logic_use:
                if record_keys is None:
                    usage["logic_database_fields"].add(key)
                else:
                    usage["logic_database_records"].update(record_keys)
                if nonselector_logic_codes:
                    if record_keys is None:
                        usage["nonselector_logic_database_fields"].add(key)
                        usage["nonselector_logic_field_codes"].setdefault(key, set()).update(
                            nonselector_logic_codes
                        )
                    else:
                        usage["nonselector_logic_database_records"].update(record_keys)
                        for record_key in record_keys:
                            usage["nonselector_logic_record_codes"].setdefault(
                                record_key, set()
                            ).update(nonselector_logic_codes)
            elif display_use:
                if record_keys is None:
                    usage["display_database_fields"].add(key)
                else:
                    usage["display_database_records"].update(record_keys)

    changed = True
    while changed:
        changed = False
        for source, targets in usage["database_write_targets"].items():
            if not any(
                _database_target_is_displayed(usage, target)
                for target in targets
            ):
                continue
            source_is_record = len(source) == 4
            display_set = (
                usage["display_database_records"]
                if source_is_record
                else usage["display_database_fields"]
            )
            if source in display_set:
                continue
            blocked = any(
                source in usage[name]
                for name in (
                    "logic_database_records" if source_is_record else "logic_database_fields",
                    "unknown_database_records" if source_is_record else "unknown_database_fields",
                    "opaque_database_records" if source_is_record else "opaque_database_fields",
                )
            )
            if not blocked:
                display_set.add(source)
                usage[
                    "visible_database_records"
                    if source_is_record
                    else "visible_database_fields"
                ].add(source)
                changed = True

    for _relative, _base_path, commands, successors, owner_id, owner_context in sequences:
        numeric_values = _numeric_values_by_command(
            commands,
            successors,
            _common_context_numeric_values(
                common_events_by_id.get(owner_id), owner_context
            ),
        )
        for index in _reachable_command_indexes(commands, successors):
            command = commands[index]
            try:
                code = int(command.get("code", -1))
                int_args = command.get("intArgs") or []
                strings = command.get("stringArgs") or []
                destination = int(int_args[0])
            except (TypeError, ValueError, IndexError):
                continue
            if code != 122 or not strings:
                continue
            if _string_variable_token(destination) is None:
                continue
            traced = _trace_string_variable_usage(
                commands,
                index,
                destination,
                schemas,
                common_events_by_id,
                common_events_by_name,
                common_event_roles,
                successors,
                owner_id,
                owner_context,
                numeric_values,
            )
            return_roles = (
                common_event_return_roles.get((owner_id, owner_context), set())
                if traced["return"]
                else set()
            )
            display_use = traced["display"] or "display" in return_roles
            display_use = display_use or any(
                _database_target_is_displayed(usage, target)
                for target in traced["database_writes"]
            )
            logic_use = traced["logic"] or "logic" in return_roles
            unknown_use = traced["unknown"] or "unknown" in return_roles
            if display_use:
                usage["display_command_literals"].update(
                    text for text in strings if isinstance(text, str) and text.strip()
                )
            if (logic_use or unknown_use) and not display_use:
                usage["logic_command_literals"].update(
                    text for text in strings if isinstance(text, str) and text.strip()
                )
    usage["logic_command_literals"].update(usage["comparison_literals"])
    usage["display_database_fields"] -= usage["logic_database_fields"]
    usage["display_database_records"] -= usage["logic_database_records"]
    # ponytail: Dynamic numbered fields are treated as one family. This can protect
    # display-only siblings; explicit selector ranges from WolfRPGText are the upgrade path.
    for database_name, type_index, field_index in list(usage["logic_database_fields"]):
        fields = schemas.get(database_name, {}).get("fields", {}).get(type_index, {})
        names = [name for name, index in fields.items() if index == field_index]
        for name in names:
            match = re.fullmatch(r"(.*?)(\d+)", name)
            if not match:
                continue
            prefix = match.group(1)
            usage["logic_database_fields"].update(
                (database_name, type_index, index)
                for candidate, index in fields.items()
                if re.fullmatch(re.escape(prefix) + r"\d+", candidate)
            )
    for database_name, type_index, data_index, field_index in list(
        usage["logic_database_records"]
    ):
        fields = schemas.get(database_name, {}).get("fields", {}).get(type_index, {})
        names = [name for name, index in fields.items() if index == field_index]
        for name in names:
            match = re.fullmatch(r"(.*?)(\d+)", name)
            if not match:
                continue
            prefix = match.group(1)
            usage["logic_database_records"].update(
                (database_name, type_index, data_index, index)
                for candidate, index in fields.items()
                if re.fullmatch(re.escape(prefix) + r"\d+", candidate)
            )
    usage["display_database_fields"] -= usage["logic_database_fields"]
    usage["display_database_records"] -= usage["logic_database_records"]
    _infer_database_symbol_references(
        databases,
        database_relatives,
        common_event_names,
        identifier_indexes,
        usage,
    )
    for relative, base_path, commands, successors, _owner_id, _owner_context in sequences:
        for command_index in _reachable_command_indexes(commands, successors):
            command = commands[command_index]
            try:
                if int(command.get("code", -1)) != 112:
                    continue
            except (TypeError, ValueError):
                continue
            for argument_index, expected_value in enumerate(command.get("stringArgs") or []):
                if not isinstance(expected_value, str) or not expected_value.strip():
                    continue
                for token in _symbol_expression_tokens(expected_value):
                    normalized_token = token.casefold()
                    targets = usage.get("identifier_symbols", {}).get(
                        normalized_token, {}
                    )
                    if not targets:
                        continue
                    if len(targets) != 1:
                        usage["identifier_reference_namespaces"].update(targets)
                        usage["incomplete_identifier_symbols"].update(
                            (*namespace, normalized_token) for namespace in targets
                        )
                        continue
                    namespace, target_indexes = next(iter(targets.items()))
                    usage["identifier_reference_namespaces"].add(namespace)
                    usage["identifier_reference_paths"].setdefault(
                        (*namespace, normalized_token), set()
                    ).add((
                        relative,
                        base_path + (command_index, "stringArgs", argument_index),
                        tuple(sorted(target_indexes)),
                        expected_value,
                        "string_comparison",
                    ))
    namespaces = {
        namespace for namespace, values in identifier_indexes.items() if values
    }
    policies = {}
    for namespace in namespaces:
        profile = usage["database_identifier_access"].get(namespace, {})
        if (
            profile.get("unknown", 0)
            or not (profile.get("numeric", 0) or profile.get("name", 0))
        ):
            policy = "unsafe"
        elif profile.get("name", 0) or namespace in usage["identifier_reference_namespaces"]:
            policy = "name_closed"
        else:
            policy = "numeric_only"
        policies[namespace] = policy

    usage["database_identifier_translation_policy"] = policies
    return usage


def _write_analysis_diagnostics(game_path, usage):
    items = sorted(
        usage.get("analysis_diagnostics", []),
        key=lambda item: (
            str(item.get("reason", "")),
            str(item.get("file", "")).casefold(),
            repr(item.get("path", [])),
        ),
    )
    reasons = {}
    for item in items:
        reason = str(item.get("reason") or "unknown")
        reasons[reason] = reasons.get(reason, 0) + 1
    payload = {
        "schema": 1,
        "wolf_export_schema": WOLF_EXPORT_SCHEMA,
        "summary": {"protected": len(items), "reasons": reasons},
        "items": items,
    }
    _write_json_atomic(
        _state_path(game_path, ANALYSIS_DIAGNOSTICS_FILENAME), payload
    )
    return payload


def _database_value_marker(
    database_name,
    type_index,
    field_index,
    field,
    usage,
    datum_name="",
    schema_field=None,
    data_index=None,
):
    value = field.get("value")
    normalized_value = value.strip().casefold() if isinstance(value, str) else ""
    normalized_datum_name = str(datum_name or "").strip().casefold()
    explicit_internal = _is_explicit_internal_database_text(value)
    marker = "WOLFLogic"
    if usage:
        key = (database_name, type_index, field_index)
        record_key = (database_name, type_index, data_index, field_index)
        schema_name = str(
            (
                schema_field.get("name")
                if isinstance(schema_field, dict)
                else None
            )
            or field.get("name")
            or ""
        ).strip()
        opaque_use = (
            key in usage.get("opaque_database_fields", set())
            or record_key in usage.get("opaque_database_records", set())
        )
        if (
            key in usage["logic_database_fields"]
            or record_key in usage.get("logic_database_records", set())
            or key in usage.get("unknown_database_fields", set())
            or record_key in usage.get("unknown_database_records", set())
            or (
                normalized_value
                and normalized_value == normalized_datum_name
                and (database_name, type_index, normalized_value)
                in usage.get("named_database_record_references", set())
            )
        ):
            return "WOLFLogic"
        if opaque_use:
            return "WOLFLogic"
        if (
            marker == "WOLFLogic"
            and not explicit_internal
            and not _database_schema_field_is_resource(schema_field)
            and (
                key in usage["display_database_fields"]
                or record_key in usage.get("display_database_records", set())
            )
        ):
            return "WOLFText"
    return marker


def _uses_first_string_database_id(datum, field_index):
    # ponytail: WolfRPGText omits WOLF's data-ID mode. A blank record name plus
    # a first string value is the observable equivalent; expose the mode to
    # WolfRPGText if a game uses a different layout.
    if field_index != 0 or str(datum.get("name") or "").strip():
        return False
    fields = datum.get("data") or []
    return bool(
        fields
        and isinstance(fields[0].get("value"), str)
        and fields[0]["value"].strip()
    )


def _first_string_database_marker(
    database_name,
    type_index,
    field,
    usage,
    schema_field=None,
    data_index=None,
):
    value = field.get("value")
    symbol = value.strip().casefold() if isinstance(value, str) else value
    translation_policy = (
        _database_identifier_translation_policy(usage, (database_name, type_index))
        if usage
        else "unsafe"
    )
    field_key = (database_name, type_index, 0)
    record_key = (database_name, type_index, data_index, 0)
    visible = bool(
        usage
        and (
            field_key in usage.get("visible_database_fields", set())
            or record_key in usage.get("visible_database_records", set())
        )
    )
    has_nonselector_logic = bool(
        usage
        and (
            field_key in usage.get("nonselector_logic_database_fields", set())
            or record_key in usage.get("nonselector_logic_database_records", set())
        )
    )
    nonselector_codes = set()
    if usage:
        nonselector_codes.update(
            usage.get("nonselector_logic_field_codes", {}).get(field_key, ())
        )
        nonselector_codes.update(
            usage.get("nonselector_logic_record_codes", {}).get(record_key, ())
        )
    nonselector_logic = has_nonselector_logic and not (
        nonselector_codes and nonselector_codes <= {112}
    )
    unknown_flow = bool(
        usage
        and (
            field_key in usage.get("unknown_database_fields", set())
            or record_key in usage.get("unknown_database_records", set())
        )
    )
    opaque_flow = bool(
        usage
        and (
            field_key in usage.get("opaque_database_fields", set())
            or record_key in usage.get("opaque_database_records", set())
        )
    )
    if (
        not usage
        or translation_policy == "unsafe"
        or not visible
        or nonselector_logic
        or unknown_flow
        or opaque_flow
        or (database_name, type_index, symbol)
        in usage.get("incomplete_identifier_symbols", set())
        or _is_explicit_internal_database_text(value)
        or _database_schema_field_is_resource(schema_field)
        or _looks_like_resource(value)
    ):
        return "WOLFLogic"
    return "WOLFText"


def _json_entries(json_root, json_path, usage=None):
    with open(json_path, "r", encoding="utf-8") as source:
        data = json.load(source)
    if not isinstance(data, dict):
        return []
    json_rel = os.path.relpath(json_path, json_root).replace(os.sep, "/")
    entries = []

    parent = os.path.basename(os.path.dirname(json_path)).lower()
    if parent == "game":
        for key in ("Title", "TitlePlus", "StartUpMsg", "TitleMsg"):
                _add_entry(entries, {"kind": "json", "file": json_rel, "path": [key]}, data.get(key))
    elif parent == "common":
        for command_index, command in enumerate(data.get("commands", [])):
            _command_entries(
                command,
                ["commands", command_index],
                entries,
                json_rel,
                usage,
                data.get("id"),
            )
    elif parent == "maps":
        for event_index, event in enumerate(data.get("events", [])):
            for page_index, page in enumerate(event.get("pages", [])):
                for command_index, command in enumerate(page.get("list", [])):
                    _command_entries(
                        command,
                        ["events", event_index, "pages", page_index, "list", command_index],
                        entries,
                        json_rel,
                        usage,
                    )
    elif parent == "databases":
        database_name = os.path.basename(json_path).lower()
        for type_index, type_data in enumerate(data.get("types", [])):
            schema_fields = type_data.get("fields") or []
            for data_index, datum in enumerate(type_data.get("data", [])):
                fields = datum.get("data", [])
                field_markers = []
                for field_index, field in enumerate(fields):
                    schema_field = schema_fields[field_index] if field_index < len(schema_fields) else None
                    field_markers.append(
                        _first_string_database_marker(
                            database_name,
                            type_index,
                            field,
                            usage,
                            schema_field,
                            data_index,
                        )
                        if _uses_first_string_database_id(datum, field_index)
                        else _database_value_marker(
                            database_name,
                            type_index,
                            field_index,
                            field,
                            usage,
                            datum.get("name"),
                            schema_field,
                            data_index,
                        )
                    )
                logic_values = {
                    field.get("value")
                    for field, field_marker in zip(fields, field_markers)
                    if isinstance(field.get("value"), str) and field_marker == "WOLFLogic"
                }
                datum_name = datum.get("name")
                datum_name_path = ["types", type_index, "data", data_index, "name"]
                normalized_datum_name = str(datum_name or "").strip().casefold()
                datum_name_is_logic = (
                    _is_explicit_internal_database_text(datum_name)
                    or datum_name in logic_values
                    or (usage and datum_name in usage["comparison_literals"])
                    or (
                        usage
                        and normalized_datum_name
                        and (database_name, type_index, normalized_datum_name)
                        in usage.get("named_database_record_references", set())
                    )
                )
                if datum_name_is_logic:
                    _add_entry(
                        entries,
                        {
                            "kind": "json",
                            "file": json_rel,
                            "path": datum_name_path,
                            "marker": "WOLFLogic",
                            "logic_role": "identifier",
                        },
                        datum_name,
                    )
                for field_index, (field, field_marker) in enumerate(zip(fields, field_markers)):
                    value = field.get("value")
                    if isinstance(value, str) and value != "INVALID_IGNORE":
                        path = ["types", type_index, "data", data_index, "data", field_index, "value"]
                        first_string_id = _uses_first_string_database_id(datum, field_index)
                        translation_policy = (
                            _database_identifier_translation_policy(
                                usage, (database_name, type_index)
                            )
                            if usage and first_string_id
                            else None
                        )
                        normalized_identifier = (
                            value.strip().casefold() if first_string_id else value
                        )
                        identifier_incomplete = bool(
                            first_string_id
                            and usage
                            and (database_name, type_index, normalized_identifier)
                            in usage.get("incomplete_identifier_symbols", set())
                        )
                        _add_entry(
                            entries,
                            {
                                "kind": "json",
                                "file": json_rel,
                                "path": path,
                                "marker": field_marker,
                                **(
                                    {
                                        "identifier_namespace": [database_name, type_index],
                                        "identifier_record": [database_name, type_index, data_index],
                                        "identifier_collision_policy": (
                                            _database_identifier_collision_policy(
                                                usage, (database_name, type_index)
                                            )
                                            if usage
                                            else "unknown"
                                        ),
                                        "identifier_translation_policy": translation_policy,
                                        "identifier_missing_names": sorted(
                                            usage.get("missing_identifier_names", {}).get(
                                                (database_name, type_index), ()
                                            )
                                        ) if usage else [],
                                        "identifier_reference_complete": (
                                            field_marker != "WOLFLogic"
                                            and translation_policy in ("numeric_only", "name_closed")
                                            and not identifier_incomplete
                                        ),
                                        "identifier_references": [
                                            {
                                                "file": relative,
                                                "path": list(reference_path),
                                                "target_data_indexes": list(target_data_indexes),
                                                "expected_original": expected_original,
                                                "reference_kind": reference_kind,
                                            }
                                            for (
                                                relative,
                                                reference_path,
                                                target_data_indexes,
                                                expected_original,
                                                reference_kind,
                                            ) in sorted(
                                                (
                                                    usage.get("identifier_reference_paths", {}).get(
                                                        (
                                                            database_name,
                                                            type_index,
                                                            normalized_identifier,
                                                        ),
                                                        (),
                                                    )
                                                    if translation_policy == "name_closed"
                                                    else ()
                                                ),
                                                key=lambda item: (
                                                    item[0].casefold(), repr(item[1]), item[2]
                                                ),
                                            )
                                        ] if usage else [],
                                    }
                                    if first_string_id
                                    else {}
                                ),
                                **(
                                    {
                                        "logic_role": (
                                            "resource"
                                            if field_index < len(schema_fields)
                                            and _database_schema_field_is_resource(schema_fields[field_index])
                                            else "identifier"
                                        )
                                    }
                                    if field_marker == "WOLFLogic"
                                    else {}
                                ),
                            },
                            value,
                            protect_resource=True,
                        )
    return entries


def _write_json_entry_groups(string_scripts_path, rel, entries):
    display_entries = [(metadata, text) for metadata, text in entries if metadata.get("marker") != "WOLFLogic"]
    logic_entries = [(metadata, text) for metadata, text in entries if metadata.get("marker") == "WOLFLogic"]
    written = 0
    if display_entries:
        _write_string_script(os.path.join(string_scripts_path, "WOLF", "Binary", rel), display_entries)
        written += 1
    if logic_entries:
        _write_string_script(os.path.join(string_scripts_path, "WOLF", "Logic", rel), logic_entries)
        written += 1
    return written


def _read_utf8(path):
    with open(path, "r", encoding="utf-8-sig", errors="strict", newline="") as source:
        return source.read()


def _iter_string_values(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_string_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_string_values(item)


def _collect_runtime_references(json_root):
    strings = []
    scene_ids = set()
    for root, _, files in os.walk(json_root):
        parent = os.path.basename(root).lower()
        for filename in files:
            if not filename.lower().endswith(".json"):
                continue
            path = os.path.join(root, filename)
            try:
                with open(path, "r", encoding="utf-8") as source:
                    data = json.load(source)
            except (OSError, ValueError) as error:
                raise WolfEngineError(f"无法分析 WOLF 运行时引用: {filename}: {error}") from error
            strings.extend(_iter_string_values(data))
            if not isinstance(data, dict):
                continue
            if parent in ("common", "maps"):
                sequences = [data.get("commands", [])] if parent == "common" else [
                    page.get("list", [])
                    for event in data.get("events", [])
                    for page in event.get("pages", [])
                ]
                for commands in sequences:
                    for command in commands:
                        try:
                            code = int(command.get("code", -1))
                        except (TypeError, ValueError):
                            continue
                        if code == 300:
                            scene_ids.update(
                                value.strip().casefold()
                                for value in (command.get("stringArgs") or [])[1:]
                                if isinstance(value, str) and value.strip()
                            )
            elif parent == "databases":
                for type_data in data.get("types", []):
                    for datum in type_data.get("data", []):
                        for field in datum.get("data", []):
                            value = field.get("value")
                            if (
                                isinstance(value, str)
                                and value.strip()
                                and _SCENARIO_FIELD_RE.search(str(field.get("name") or ""))
                            ):
                                scene_ids.add(value.strip().casefold())
    return strings, scene_ids


def _referenced_map_json_names(references):
    return {
        f"{match.group(1)}.json".casefold()
        for reference in references
        if isinstance(reference, str)
        for match in _MAP_REFERENCE_RE.finditer(reference.replace("\\", "/"))
    }


def _runtime_map_json_names(json_root, references):
    occurrences = [
        f"{match.group(1)}.json".casefold()
        for reference in references
        if isinstance(reference, str)
        for match in _MAP_REFERENCE_RE.finditer(reference.replace("\\", "/"))
    ]
    referenced = set(occurrences)
    database_dir = os.path.join(json_root, "databases")
    system_paths = [
        os.path.join(database_dir, filename)
        for filename in os.listdir(database_dir)
        if filename.casefold() == "sysdatabase.json"
    ] if os.path.isdir(database_dir) else []
    if len(system_paths) != 1:
        return referenced
    try:
        with open(system_paths[0], "r", encoding="utf-8") as source:
            system_database = json.load(source)
    except (OSError, ValueError) as error:
        raise WolfEngineError(f"无法分析 WOLF 运行地图设置: {error}") from error

    editor_only = set()
    for type_data in system_database.get("types", []):
        schema_fields = type_data.get("fields") or []
        for datum in type_data.get("data", []):
            if str(datum.get("name") or "").strip():
                continue
            fields = datum.get("data") or []
            for field_index, field in enumerate(fields):
                schema_field = (
                    schema_fields[field_index]
                    if field_index < len(schema_fields)
                    else {}
                )
                field_name = str(
                    (schema_field or {}).get("name") or field.get("name") or ""
                )
                if not _MAP_FILE_FIELD_RE.search(field_name):
                    continue
                editor_only.update(_referenced_map_json_names([field.get("value")]))

    # ponytail: A blank SysDB map-setting row is WOLF's editor/test-play slot.
    # Exporting its role from WolfRPGText removes the duplicate-reference fallback.
    literal_runtime = referenced - {
        filename for filename in editor_only if occurrences.count(filename) == 1
    }
    dynamic_map_reference = any(
        re.search(r"MapData[/\\].*\\[A-Za-z]+\[.*\.mps", reference, re.IGNORECASE)
        for reference in references
        if isinstance(reference, str)
    )
    if not dynamic_map_reference:
        return literal_runtime
    maps_dir = os.path.join(json_root, "maps")
    return {
        filename.casefold()
        for filename in os.listdir(maps_dir)
        if filename.lower().endswith(".json") and filename.casefold() not in editor_only
    } if os.path.isdir(maps_dir) else literal_runtime


def _discover_text_resources(data_path, references):
    candidates = []
    candidate_directories = set()
    for root, _, files in os.walk(data_path):
        for filename in files:
            if os.path.splitext(filename)[1].lower() not in _TEXT_RESOURCE_EXTENSIONS:
                continue
            path = os.path.join(root, filename)
            rel = os.path.relpath(path, data_path).replace(os.sep, "/")
            candidates.append((rel, path))
            parent = os.path.dirname(rel).replace(os.sep, "/")
            if parent:
                candidate_directories.add(parent.casefold())

    if not candidates:
        return [], 0

    references = [text.replace("\\", "/").casefold() for text in references]

    # ponytail: A referenced text directory is runtime-owned so dynamic WOLF
    # filenames stay complete. Mixed dev/runtime directories need string-flow tracing.
    referenced_directories = {
        directory
        for directory in candidate_directories
        if any(f"{directory}/" in reference for reference in references)
    }
    selected = []
    for rel, path in candidates:
        normalized_rel = rel.casefold()
        parent = os.path.dirname(rel).replace(os.sep, "/").casefold()
        directly_referenced = any(normalized_rel in reference for reference in references)
        if directly_referenced or parent in referenced_directories:
            selected.append(path)
    selected.sort(key=lambda path: os.path.relpath(path, data_path).casefold())
    return selected, len(candidates) - len(selected)


def _split_line_prefix(line):
    match = re.match(r"([ \t\u3000]*)(.*)", line, re.DOTALL)
    leading, body = match.groups()
    if body.startswith((":", ";", "~")):
        return leading + body[0], body[1:]
    return leading, body


def _restore_line_prefixes(text, metadata):
    prefixes = metadata.get("line_prefixes")
    if prefixes is None:
        return text
    lines = text.splitlines(keepends=True)
    if len(lines) != len(prefixes) or not all(isinstance(prefix, str) for prefix in prefixes):
        raise WolfEngineError(f"WOLF 场景文本行前缀数量不一致: {metadata.get('file')}")
    return "".join(prefix + line for prefix, line in zip(prefixes, lines))


def _scene_block_entries(data_path, path, lines, start, end):
    rel = os.path.relpath(path, data_path).replace(os.sep, "/")
    entries = []
    visible_start = None
    visible_text = []
    line_prefixes = []

    def flush():
        nonlocal visible_start, visible_text, line_prefixes
        if visible_start is not None and visible_text:
            _add_entry(
                entries,
                {
                    "kind": "txt",
                    "file": rel,
                    "start": visible_start,
                    "end": visible_start + len(visible_text),
                    "line_prefixes": line_prefixes,
                    "marker": "WOLFText",
                },
                "".join(visible_text),
            )
        visible_start = None
        visible_text = []
        line_prefixes = []

    for index in range(start, end):
        line = lines[index]
        stripped = line.lstrip()
        if stripped.startswith("@"):
            flush()
            continue
        if not line.strip() or stripped.startswith(("#", ">", "%", "---", "<")):
            flush()
            continue

        prefix, text = _split_line_prefix(line)
        if not text.strip():
            flush()
            continue
        if visible_start is None:
            visible_start = index
        visible_text.append(text)
        line_prefixes.append(prefix)
    flush()
    return entries


def _line_scenario_entries(data_path, path, content, scene_references=None):
    # ponytail: This covers WOLF's common line-oriented scene convention. A future
    # incompatible DSL needs a parser selected from its consuming common event.
    lines = content.splitlines(keepends=True)
    entries = []
    blocks = []
    start = None
    labels = []
    for index, line in enumerate(lines):
        if _SCENE_SEPARATOR_RE.match(line):
            start = None
            labels = []
            continue
        label_match = _SCENE_LABEL_RE.match(line)
        if label_match:
            if start is None:
                start = index + 1
            labels.append(label_match.group(1))
            continue
        if start is None or not _SCENE_END_RE.match(line):
            continue

        block = lines[start:index]
        has_runtime_syntax = any(
            item.lstrip().startswith(("@", "%", ":", ";", "~", "「", "《"))
            for item in block
        )
        blocks.append((start, index, labels, block, has_runtime_syntax))
        start = None
        labels = []

    label_ids = {
        label.casefold()
        for _start, _end, block_labels, _block, _runtime in blocks
        for label in block_labels
        if _ASCII_SCENE_ID_RE.fullmatch(label)
    }
    reachable = label_ids & set(scene_references or ())
    if reachable:
        graph = {label: set() for label in label_ids}
        for _start, _end, block_labels, block, _runtime in blocks:
            sources = {
                label.casefold()
                for label in block_labels
                if _ASCII_SCENE_ID_RE.fullmatch(label)
            }
            targets = {
                token.casefold()
                for line in block
                if line.lstrip().startswith("%")
                for token in re.findall(r"[A-Za-z_][A-Za-z0-9_-]*", line)
                if token.casefold() in label_ids
            }
            for source in sources:
                graph[source].update(targets)
        while True:
            expanded = reachable | set().union(*(graph[label] for label in reachable))
            if expanded == reachable:
                break
            reachable = expanded

    for block_start, block_end, block_labels, _block, has_runtime_syntax in blocks:
        block_ids = {
            label.casefold()
            for label in block_labels
            if _ASCII_SCENE_ID_RE.fullmatch(label)
        }
        if (
            (reachable and block_ids & reachable)
            or (not reachable and (has_runtime_syntax or block_ids))
        ):
            entries.extend(_scene_block_entries(data_path, path, lines, block_start, block_end))
    return entries


def _bullet_scenario_entries(data_path, path, content):
    lines = content.splitlines(keepends=True)
    entries = []
    for index, line in enumerate(lines):
        if not _BULLET_SCENE_HEADER_RE.match(line.rstrip("\r\n")):
            continue
        end = index + 1
        while end < len(lines) and not _BULLET_SCENE_BOUNDARY_RE.match(lines[end].rstrip("\r\n")):
            end += 1
        entries.extend(_scene_block_entries(data_path, path, lines, index + 1, end))
    return entries


def _has_unknown_text_structure(content):
    for line in content.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("@", "●", "■", "[")) or _SCENE_SEPARATOR_RE.match(line):
            return True
    return False


def _txt_entries(data_path, path, scene_references=None):
    content = _read_utf8(path)
    if _SCENE_END_RE.search(content):
        entries = _line_scenario_entries(data_path, path, content, scene_references)
        if entries:
            return entries
    if any(_BULLET_SCENE_HEADER_RE.match(line) for line in content.splitlines()):
        entries = _bullet_scenario_entries(data_path, path, content)
        if entries:
            return entries
    # ponytail: Unknown command-oriented text formats stay protected. Add a
    # consumer-selected parser when another runtime DSL is identified.
    if _has_unknown_text_structure(content):
        return []
    lines = content.splitlines(keepends=True)
    entries = []
    start = None
    rel = os.path.relpath(path, data_path).replace(os.sep, "/")

    for index in range(len(lines) + 1):
        if index < len(lines):
            stripped = lines[index].lstrip()
            visible = bool(lines[index].strip()) and not stripped.startswith(("<", "@"))
        else:
            visible = False
        if visible and start is None:
            start = index
        elif not visible and start is not None:
            _add_entry(
                entries,
                {"kind": "txt", "file": rel, "start": start, "end": index, "marker": "WOLFText"},
                "".join(lines[start:index]),
            )
            start = None
    return entries


def _sniff_csv(content):
    try:
        return csv.Sniffer().sniff(content[:8192], delimiters=",;\t|")
    except csv.Error:
        return csv.excel


def _csv_entries(data_path, path):
    content = _read_utf8(path)
    rows = list(csv.reader(content.splitlines(keepends=True), dialect=_sniff_csv(content)))
    rel = os.path.relpath(path, data_path).replace(os.sep, "/")
    entries = []
    for row_index, row in enumerate(rows):
        for column_index, value in enumerate(row):
            if text_processing.has_japanese_letters(value) and not _looks_like_resource(value):
                _add_entry(
                    entries,
                    {
                        "kind": "csv",
                        "file": rel,
                        "row": row_index,
                        "column": column_index,
                        "marker": "WOLFText",
                    },
                    value,
                )
    return entries


def _encode_wolf_transport(metadata, text):
    if metadata.get("marker") == "WOLFLogic":
        return metadata, text
    occurrences = [
        (item.start, item.end, item.literal, item.line, item.literal, True, item.kind)
        for item in control_tokens.iter_token_occurrences(text, include_quotes=False)
    ]
    # ponytail: Only iteration marks next to common kaomoji glyphs are decorative.
    # A future richer emoticon grammar can replace this bounded local check.
    for index, character in enumerate(text):
        if character not in _WOLF_EMOTICON_MARKS:
            continue
        line_start = text.rfind("\n", 0, index) + 1
        line_end = text.find("\n", index)
        if line_end < 0:
            line_end = len(text)
        context = text[max(line_start, index - 6):min(line_end, index + 7)]
        if any(hint in context for hint in _WOLF_EMOTICON_HINTS):
            occurrences.append((index, index + 1, character, text.count("\n", 0, index), character, True, "emoticon"))
    for match in _WOLF_DIRECTIVE_RE.finditer(text):
        occurrences.append((*match.span(), match.group(0), text.count("\n", 0, match.start()), match.group(0), True, "directive"))
    for match in _WOLF_ALIGNMENT_RE.finditer(text):
        occurrences.append((*match.span(), match.group(0), text.count("\n", 0, match.start()), match.group(0), True, "alignment"))
    for match in re.finditer("\n", text):
        occurrences.append((*match.span(), "\n", text.count("\n", 0, match.start()), "\n", True, "newline"))
    occurrences.sort(key=lambda item: item[0])
    if not occurrences:
        return metadata, text
    if _WOLF_TRANSPORT_TAG_PREFIX in text:
        raise WolfEngineError("WOLF 原文与内部控制码标签冲突")

    pieces = []
    tokens = []
    cursor = 0
    has_ruby = False
    for index, occurrence in enumerate(occurrences):
        start, end, literal, line, restore, required, kind = occurrence
        if start < cursor:
            raise WolfEngineError(f"WOLF 控制码范围重叠: {literal}")
        fingerprint = hashlib.sha256(literal.encode("utf-8")).hexdigest()[:8]
        tag = f"{_WOLF_TRANSPORT_TAG_PREFIX}{index}_{fingerprint}}}}}"
        ruby_match = _WOLF_RUBY_RE.fullmatch(literal)
        pieces.append(text[cursor:start])
        if ruby_match:
            pieces.append(ruby_match.group(1))
            restore = ""
            required = False
            kind = "ruby"
            has_ruby = True
        pieces.append(tag)
        tokens.append([tag, restore, line, required, kind])
        cursor = end
    pieces.append(text[cursor:])
    encoded = "".join(pieces)
    metadata = dict(metadata)
    transport = {"version": 2, "tokens": tokens, "encoded": encoded}
    if has_ruby:
        transport["original"] = text
    metadata["wolf_transport"] = transport
    return metadata, encoded


def _validate_wolf_transport(source_text, translated_text, metadata):
    transport = metadata.get("wolf_transport")
    if not isinstance(transport, dict):
        return True, ""
    tokens = transport.get("tokens")
    if not isinstance(tokens, list) or not tokens:
        return False, "WOLF 控制码标签元数据无效"
    expected_tags = [item[0] for item in tokens]
    actual_matches = list(_WOLF_TRANSPORT_TAG_RE.finditer(translated_text))
    actual_tags = [match.group(0) for match in actual_matches]
    if transport.get("version") == 2:
        if _WOLF_TRANSPORT_TAG_PREFIX in _WOLF_TRANSPORT_TAG_RE.sub("", translated_text):
            return False, "WOLF 控制码标签包含破损片段"
        required_tags = {
            item[0]
            for item in tokens
            if len(item) < 4 or item[3] is not False
        }
        expected_set = set(expected_tags)
        if any(tag not in expected_set for tag in actual_tags):
            return False, f"WOLF 控制码标签包含未知值: {actual_tags!r}"
        if any(actual_tags.count(tag) != 1 for tag in required_tags):
            return False, f"WOLF 必需控制码标签缺失或重复: {expected_tags!r} != {actual_tags!r}"
        if any(actual_tags.count(tag) > 1 for tag in expected_set):
            return False, f"WOLF 控制码标签重复: {actual_tags!r}"
        present_in_expected_order = [tag for tag in expected_tags if tag in actual_tags]
        if actual_tags != present_in_expected_order:
            return False, f"WOLF 控制码标签序列不一致: {expected_tags!r} != {actual_tags!r}"
        if "\n" in translated_text or "\r" in translated_text:
            return False, "WOLF 受保护文本包含标签之外的换行"
        return True, ""

    tag_pattern = re.compile("|".join(re.escape(tag) for tag in expected_tags))
    actual_matches = list(tag_pattern.finditer(translated_text))
    actual_tags = [match.group(0) for match in actual_matches]
    if actual_tags != expected_tags:
        return False, f"WOLF 控制码标签序列不一致: {expected_tags!r} != {actual_tags!r}"
    expected_lines = [int(item[2]) for item in tokens]
    actual_lines = [translated_text.count("\n", 0, match.start()) for match in actual_matches]
    if actual_lines != expected_lines:
        return False, f"WOLF 控制码标签所在行不一致: {expected_lines!r} != {actual_lines!r}"
    if source_text.count("\n") != translated_text.count("\n"):
        return False, "含 WOLF 控制码的文本行数不一致"
    return True, ""


def validate_translation_transport(source_text, translated_text, optional_tags=()):
    expected = _WOLF_TRANSPORT_TAG_RE.findall(source_text)
    if not expected:
        return True, ""
    actual = _WOLF_TRANSPORT_TAG_RE.findall(translated_text)
    if _WOLF_TRANSPORT_TAG_PREFIX in _WOLF_TRANSPORT_TAG_RE.sub("", translated_text):
        return False, "WOLF 控制码标签包含破损片段"
    optional = set(optional_tags)
    if not optional:
        if actual != expected:
            return False, f"WOLF 控制码标签序列不一致: {expected!r} != {actual!r}"
        if "\n" in translated_text or "\r" in translated_text:
            return False, "WOLF 受保护文本包含标签之外的换行"
        return True, ""
    expected_set = set(expected)
    required = {tag for tag in expected if tag not in optional}
    if any(tag not in expected_set for tag in actual):
        return False, f"WOLF 控制码标签包含未知值: {actual!r}"
    if any(actual.count(tag) != 1 for tag in required):
        return False, f"WOLF 必需控制码标签缺失或重复: {expected!r} != {actual!r}"
    if any(actual.count(tag) > 1 for tag in expected_set):
        return False, f"WOLF 控制码标签重复: {actual!r}"
    if actual != [tag for tag in expected if tag in actual]:
        return False, f"WOLF 控制码标签序列不一致: {expected!r} != {actual!r}"
    if "\n" in translated_text or "\r" in translated_text:
        return False, "WOLF 受保护文本包含标签之外的换行"
    return True, ""


def _decode_wolf_transport(text, metadata):
    transport = metadata.get("wolf_transport")
    if not isinstance(transport, dict):
        return text
    core, newline, suffix = _split_text_format(text)
    encoded = transport.get("encoded")
    original = transport.get("original")
    if isinstance(original, str) and core == encoded:
        decoded = original
    else:
        decoded = core
        for item in transport.get("tokens", []):
            tag, restore = item[0], item[1]
            count = decoded.count(tag)
            required = len(item) < 4 or item[3] is not False
            if count != 1 and (required or count != 0):
                raise WolfEngineError(f"WOLF 控制码标签缺失或重复: {tag}")
            if count:
                decoded = decoded.replace(tag, restore, 1)
    return decoded.replace("\n", newline) + suffix


def _write_string_script(path, entries):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as output:
        for metadata, text in entries:
            metadata = dict(metadata)
            metadata["wolf_export_schema"] = WOLF_EXPORT_SCHEMA
            metadata["wolf_code"] = _translation_code(metadata)
            metadata, text = _encode_wolf_transport(metadata, text)
            marker = metadata.get("marker", "Message")
            output.write(f"@WOLF {_encode_metadata(metadata)}\n#{marker}#\n")
            output.write(text)
            if text and not text.endswith("\n"):
                output.write("\n")
            output.write("##\n")


def export_to_string_scripts(game_path, message_queue=None):
    data_path = os.path.join(game_path, "Data")
    _validate_data_dir(data_path)
    snapshot_path = _state_path(game_path, JSON_SNAPSHOT_DIRNAME)
    string_scripts_path = os.path.join(game_path, STRING_SCRIPTS_DIRNAME)
    backup_path = os.path.join(game_path, STRING_SCRIPTS_ORIGIN_DIRNAME)
    diagnostics_path = _state_path(game_path, ANALYSIS_DIAGNOSTICS_FILENAME)

    for path in (snapshot_path, string_scripts_path, backup_path):
        if os.path.exists(path) and not file_system.safe_remove(path):
            raise WolfEngineError(f"无法清理旧目录: {path}")
    if os.path.exists(diagnostics_path) and not file_system.safe_remove(diagnostics_path):
        raise WolfEngineError(f"无法清理旧分析诊断: {diagnostics_path}")
    os.makedirs(snapshot_path, exist_ok=True)
    os.makedirs(string_scripts_path, exist_ok=True)

    if message_queue:
        message_queue.put(("log", ("normal", "使用 WolfRPGText 解析 WOLF 二进制数据...")))
    uberwolf.dump_text(data_path, snapshot_path)
    runtime_references, scene_references = _collect_runtime_references(snapshot_path)
    referenced_maps = _runtime_map_json_names(snapshot_path, runtime_references)
    usage = _analyze_json_usage(snapshot_path, referenced_maps)
    diagnostics = _write_analysis_diagnostics(game_path, usage)
    if message_queue:
        message_queue.put((
            "log",
            (
                "warning" if diagnostics["summary"]["protected"] else "normal",
                "WOLF 分析完成：保护了 "
                f"{diagnostics['summary']['protected']} 个无法证明用途的入口或引用；"
                f"详见 {diagnostics_path}",
            ),
        ))

    file_count = 0
    entry_count = 0
    for root, _, files in os.walk(snapshot_path):
        for filename in sorted(files):
            if not filename.lower().endswith(".json"):
                continue
            json_path = os.path.join(root, filename)
            json_relative = os.path.relpath(json_path, snapshot_path).replace(os.sep, "/")
            if (
                os.path.basename(root).lower() == "maps"
                and referenced_maps
                and filename.casefold() not in referenced_maps
            ):
                continue
            entries = _json_entries(snapshot_path, json_path, usage)
            if not entries:
                continue
            rel = os.path.relpath(json_path, snapshot_path) + ".txt"
            file_count += _write_json_entry_groups(string_scripts_path, rel, entries)
            entry_count += len(entries)

    resources, skipped_resources = _discover_text_resources(data_path, runtime_references)
    for source_path in resources:
        extension = os.path.splitext(source_path)[1].lower()
        try:
            entries = (
                _csv_entries(data_path, source_path)
                if extension == ".csv"
                else _txt_entries(data_path, source_path, scene_references)
            )
        except (UnicodeDecodeError, csv.Error) as error:
            log.warning("跳过无法安全解析的 WOLF 文本资源 %s: %s", source_path, error)
            continue
        if not entries:
            continue
        rel = os.path.relpath(source_path, data_path) + ".strings.txt"
        _write_string_script(os.path.join(string_scripts_path, "WOLF", "Resources", rel), entries)
        file_count += 1
        entry_count += len(entries)

    shutil.copytree(string_scripts_path, backup_path)
    if message_queue:
        message_queue.put((
            "log",
            (
                "warning",
                f"WOLF 已保护 {len(usage['logic_database_fields'])} 个运行时数据库字段。"
                "资源路径、引擎常量和动态拼接标识不会进入翻译。",
            ),
        ))
        message_queue.put((
            "log",
            (
                "normal",
                f"WOLF 按运行时引用扫描 {len(resources)} 个外部文本资源，"
                f"跳过 {skipped_resources} 个无引用资源。",
            ),
        ))
        message_queue.put(("log", ("success", f"WOLF 导出完成：{file_count} 个脚本文件，{entry_count} 条文本。")))
    return file_count, entry_count


def _set_json_path(data, path, value):
    if not isinstance(path, list) or not path:
        raise WolfEngineError("WOLF JSON 定位路径无效")
    current = data
    for key in path[:-1]:
        current = current[key]
    current[path[-1]] = value


def _iter_released_entries(string_scripts_path):
    for root, _, files in os.walk(string_scripts_path):
        for filename in files:
            if not filename.lower().endswith(".txt"):
                continue
            path = os.path.join(root, filename)
            script_rel = os.path.relpath(path, string_scripts_path)
            with open(path, "r", encoding="utf-8-sig", newline=None) as source:
                lines = source.readlines()
            index = 0
            while index < len(lines):
                if not lines[index].startswith("@WOLF "):
                    if lines[index].strip():
                        raise WolfEngineError(
                            f"WOLF StringScripts 缺少当前版本元数据，请重新导出: {path}"
                        )
                    index += 1
                    continue
                metadata = _decode_metadata(lines[index][6:].strip())
                if metadata.get("wolf_export_schema") != WOLF_EXPORT_SCHEMA:
                    raise WolfEngineError(
                        f"WOLF StringScripts 版本已过期，请重新导出翻译文本: {path}"
                    )
                expected_code = _translation_code(metadata)
                if metadata.get("wolf_code") != expected_code:
                    raise WolfEngineError(f"WOLF StringScripts 结构 Code 无效: {path}")
                namespace = metadata.get("identifier_namespace")
                if namespace is not None:
                    if not isinstance(namespace, list) or len(namespace) != 2:
                        raise WolfEngineError(f"WOLF 数据库标识命名空间无效: {path}")
                    policies = (
                        metadata.get("identifier_collision_policy"),
                        metadata.get("identifier_translation_policy"),
                    )
                    if policies not in {
                        ("numeric_only", "numeric_only"),
                        ("name_referenced", "name_closed"),
                        ("unknown", "unsafe"),
                    }:
                        raise WolfEngineError(f"WOLF 数据库标识策略组合无效: {path}")
                    if not isinstance(metadata.get("identifier_reference_complete"), bool):
                        raise WolfEngineError(f"WOLF 数据库标识引用状态无效: {path}")
                    if not isinstance(metadata.get("identifier_references"), list):
                        raise WolfEngineError(f"WOLF 数据库标识引用图无效: {path}")
                    missing_names = metadata.get("identifier_missing_names")
                    if (
                        not isinstance(missing_names, list)
                        or not all(isinstance(name, str) and name for name in missing_names)
                    ):
                        raise WolfEngineError(f"WOLF 数据库缺失标识保护无效: {path}")
                index += 1
                if index >= len(lines):
                    raise WolfEngineError(f"WOLF 脚本元数据后缺少文本标记: {path}")
                marker_line = lines[index].strip()
                marker_match = re.fullmatch(r"#([^#]+)#", marker_line)
                if not marker_match:
                    raise WolfEngineError(f"WOLF 脚本元数据后缺少文本标记: {path}")
                marker = marker_match.group(1)
                expected_marker = metadata.get("marker", "Message")
                if marker != expected_marker:
                    raise WolfEngineError(
                        f"WOLF 文本标记与元数据不一致: {path}: {marker!r} != {expected_marker!r}"
                    )
                index += 1
                block = []
                while index < len(lines) and lines[index].strip() != "##":
                    block.append(lines[index])
                    index += 1
                if index >= len(lines):
                    raise WolfEngineError(f"WOLF 文本块缺少 ## 结束标记: {path}")
                text = "".join(block)
                if text.endswith("\n"):
                    text = text[:-1]
                yield script_rel, metadata, _restore_text_format(text, metadata)
                index += 1


def _read_released_entries(string_scripts_path):
    for _script_rel, metadata, text in _iter_released_entries(string_scripts_path):
        yield metadata, _decode_wolf_transport(text, metadata)


def _entry_identity(script_rel, metadata):
    payload = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{script_rel}\0{payload}"


def get_logic_literals(game_path):
    origin_path = os.path.join(game_path, STRING_SCRIPTS_ORIGIN_DIRNAME)
    if not os.path.isdir(origin_path):
        return ()
    return tuple(sorted({
        _split_text_format(text)[0]
        for _script_rel, metadata, text in _iter_released_entries(origin_path)
        if metadata.get("marker") == "WOLFLogic"
    }, key=len, reverse=True))


def get_protected_logic_literals(game_path):
    origin_path = os.path.join(game_path, STRING_SCRIPTS_ORIGIN_DIRNAME)
    if not os.path.isdir(origin_path):
        return ()
    return tuple(sorted({
        _split_text_format(text)[0]
        for _script_rel, metadata, text in _iter_released_entries(origin_path)
        if metadata.get("marker") == "WOLFLogic"
        and metadata.get("logic_role", "comparison") == "comparison"
    }, key=len, reverse=True))


def get_optional_transport_tags(game_path):
    origin_path = os.path.join(game_path, STRING_SCRIPTS_ORIGIN_DIRNAME)
    if not os.path.isdir(origin_path):
        return ()
    optional = set()
    required = set()
    for _script_rel, metadata, _text in _iter_released_entries(origin_path):
        transport = metadata.get("wolf_transport")
        if not isinstance(transport, dict):
            continue
        for item in transport.get("tokens", []):
            if not isinstance(item, list) or not item or not isinstance(item[0], str):
                continue
            (optional if len(item) >= 4 and item[3] is False else required).add(item[0])
    return tuple(sorted(optional - required))


def validate_translation_release(game_path, translations):
    origin_path = os.path.join(game_path, STRING_SCRIPTS_ORIGIN_DIRNAME)
    if not os.path.isdir(origin_path):
        return ["缺少 StringScripts_Origin，无法验证 WOLF 翻译完整性"], [], {}

    errors = []
    warnings = []
    expected = {}
    logic_map = {}
    duplicate_targets = {}
    stats = {"locations": 0, "changed": 0, "unchanged": 0, "fallback": 0, "missing": 0}

    released_entries = list(_iter_released_entries(origin_path))
    expected_codes = {}
    for script_rel, metadata, full_text in released_entries:
        if metadata.get("marker") == "WOLFLogic":
            continue
        source_text = _split_text_format(full_text)[0]
        code = metadata["wolf_code"]
        expected_codes.setdefault((script_rel, source_text), set()).add(code)
    verified_codes = set()
    protected_logic_literals = {
        _split_text_format(full_text)[0]
        for _script_rel, metadata, full_text in released_entries
        if metadata.get("marker") == "WOLFLogic"
        and metadata.get("logic_role", "comparison") == "comparison"
    }
    identifier_targets = {}
    identifier_policies = {}
    for script_rel, metadata, full_text in released_entries:
        namespace = metadata.get("identifier_namespace")
        if not isinstance(namespace, list) or len(namespace) != 2:
            continue
        source_text = _split_text_format(full_text)[0]
        result = translations.get(script_rel, {}).get(source_text)
        translated = (
            result.get("text")
            if metadata.get("marker") != "WOLFLogic"
            and isinstance(result, dict)
            and isinstance(result.get("text"), str)
            and result["text"].strip()
            else source_text
        )
        namespace = tuple(namespace)
        target = identifier_targets.setdefault(namespace, {}).setdefault(
            translated.casefold(), {"translated": translated, "sources": set()}
        )
        target["sources"].add(source_text)
        identifier_policies.setdefault(namespace, set()).add(
            metadata["identifier_collision_policy"]
        )
    for script_rel, metadata, full_text in released_entries:
        source_text = _split_text_format(full_text)[0]
        marker = metadata.get("marker", "Message")
        expected.setdefault(script_rel, set()).add(source_text)
        stats["locations"] += 1
        result = translations.get(script_rel, {}).get(source_text)
        if marker == "WOLFLogic" and not isinstance(result, dict):
            stats["unchanged"] += 1
            continue
        if not isinstance(result, dict) or not isinstance(result.get("text"), str) or not result["text"].strip():
            stats["missing"] += 1
            errors.append(f"缺失译文: {script_rel}: {source_text[:80]!r}")
            continue

        code_key = (script_rel, source_text)
        if marker != "WOLFLogic" and code_key not in verified_codes:
            verified_codes.add(code_key)
            if result.get("wolf_export_schema") != WOLF_EXPORT_SCHEMA:
                errors.append(
                    f"WOLF 导出版本不匹配，译文来自旧版数据: "
                    f"{script_rel}: {source_text[:80]!r}"
                )
            actual_codes = result.get("wolf_codes")
            if not isinstance(actual_codes, list) or not all(
                isinstance(code, str) and code for code in actual_codes
            ):
                errors.append(f"缺少 WOLF 结构 Code: {script_rel}: {source_text[:80]!r}")
            elif set(actual_codes) != expected_codes.get(code_key, set()):
                errors.append(f"WOLF 结构 Code 不匹配，译文可能来自旧版导出: {script_rel}: {source_text[:80]!r}")

        translated = result["text"]
        namespace = metadata.get("identifier_namespace")
        if translated != source_text and isinstance(namespace, list) and len(namespace) == 2:
            translation_policy = metadata["identifier_translation_policy"]
            missing_names = {
                name.casefold()
                for name in metadata.get("identifier_missing_names") or []
            }
            if translated.casefold() in missing_names:
                errors.append(
                    f"WOLF 数据库译名会激活原本不存在的名称: "
                    f"{script_rel}: {translated!r}"
                )
            elif translation_policy == "unsafe":
                errors.append(
                    f"WOLF 数据库标识引用未闭合，禁止翻译: {script_rel}: {source_text[:80]!r}"
                )
            elif (
                translation_policy == "name_closed"
                and metadata.get("identifier_reference_complete") is not True
            ):
                errors.append(
                    f"WOLF 数据库标识缺少完整引用图，禁止翻译: {script_rel}: {source_text[:80]!r}"
                )
        if result.get("status") == "fallback":
            stats["fallback"] += 1
            errors.append(f"仍为 fallback: {script_rel}: {source_text[:80]!r}")
        if translated == source_text:
            stats["unchanged"] += 1
            if (
                marker != "WOLFLogic"
                and source_text not in protected_logic_literals
                and text_processing.contains_japanese_kana(source_text)
            ):
                errors.append(f"日文原样保留: {script_rel}: {source_text[:80]!r}")
            # elif marker != "WOLFLogic" and source_text not in protected_logic_literals and _CJK_RE.search(source_text):
            #     warnings.append(f"纯汉字原文未变化: {script_rel}: {source_text[:80]!r}")
        else:
            stats["changed"] += 1

        text_for_kana_check = control_tokens.strip_token_literals(translated)
        for literal in protected_logic_literals:
            if source_text == literal or any(
                f"{left}{literal}{right}" in source_text
                for left, right in (("「", "」"), ("『", "』"), ('"', '"'), ("'", "'"))
            ):
                text_for_kana_check = text_for_kana_check.replace(literal, "")
        if marker != "WOLFLogic" and text_processing.contains_japanese_kana(text_for_kana_check):
            errors.append(f"译文残留日语假名: {script_rel}: {source_text[:80]!r}")

        control_ok, control_reason = control_tokens.validate_restored_text(source_text, translated)
        if not control_ok:
            errors.append(f"控制码损坏: {script_rel}: {control_reason}")
        transport_ok, transport_reason = _validate_wolf_transport(source_text, translated, metadata)
        if not transport_ok:
            errors.append(f"WOLF 控制码损坏: {script_rel}: {transport_reason}")
        if marker == "WOLFText" and source_text.count("\n") != translated.count("\n"):
            errors.append(
                f"WOLF 文本行数不一致: {script_rel}: "
                f"{source_text.count(chr(10)) + 1}->{translated.count(chr(10)) + 1}"
            )
        if marker == "WOLFLogic":
            if translated != source_text:
                errors.append(f"逻辑字面量不得翻译: {script_rel}: {source_text[:80]!r}")
            if metadata.get("logic_role", "comparison") == "comparison":
                previous = logic_map.setdefault(source_text, translated)
                if previous != translated:
                    errors.append(f"逻辑字面量译法冲突: {source_text!r}: {previous!r} / {translated!r}")
        if marker != "WOLFLogic":
            duplicate_targets.setdefault(source_text, set()).add(translated)

    for namespace, targets in identifier_targets.items():
        for target in targets.values():
            translated = target["translated"]
            sources = target["sources"]
            if len({source.casefold() for source in sources}) > 1:
                message = (
                    f"WOLF 数据库译名碰撞: {namespace[0]} 类型{namespace[1]}: "
                    f"{sorted(sources)!r} -> {translated!r}"
                )
                if identifier_policies.get(namespace) == {"numeric_only"}:
                    warnings.append(f"{message}（该类型仅按数字 ID 读取，已允许）")
                else:
                    errors.append(message)

    for file_name, entries in translations.items():
        if file_name not in expected:
            errors.append(f"译文包含未知 WOLF 脚本文件: {file_name}")
            continue
        for source_text in entries:
            if source_text not in expected[file_name]:
                errors.append(f"译文包含未知 WOLF 原文: {file_name}: {source_text[:80]!r}")

    quote_pairs = (("「", "」"), ("『", "』"), ('"', '"'), ("'", "'"))
    for file_name, source_keys in expected.items():
        for source_text in source_keys:
            result = translations.get(file_name, {}).get(source_text)
            if not isinstance(result, dict) or not isinstance(result.get("text"), str):
                continue
            translated = result["text"]
            for literal, mapped in logic_map.items():
                if any(f"{left}{literal}{right}" in source_text for left, right in quote_pairs) and mapped not in translated:
                    errors.append(
                        f"逻辑提示与判断值不一致: {file_name}: {literal!r} 应显示为 {mapped!r}"
                    )

    conflicts = sum(1 for targets in duplicate_targets.values() if len(targets) > 1)
    if conflicts:
        warnings.append(f"存在 {conflicts} 组相同原文的不同译法，请人工复核上下文")
    return errors, warnings, stats


def _apply_resource_changes(staging_data, txt_changes, csv_changes):
    for rel, changes in txt_changes.items():
        path = _safe_join(staging_data, rel)
        content = _read_utf8(path)
        lines = content.splitlines(keepends=True)
        ordered = sorted(changes, key=lambda item: item[0])
        previous_end = 0
        for start, end, _text in ordered:
            if not isinstance(start, int) or not isinstance(end, int) or start < previous_end or end < start or end > len(lines):
                raise WolfEngineError(f"WOLF TXT 定位范围无效: {rel}:{start}-{end}")
            previous_end = end
        for start, end, text in reversed(ordered):
            lines[start:end] = [text]
        updated = "".join(lines)
        if updated != content:
            with open(path, "w", encoding="utf-8", newline="") as output:
                output.write(updated)

    for rel, changes in csv_changes.items():
        path = _safe_join(staging_data, rel)
        content = _read_utf8(path)
        dialect = _sniff_csv(content)
        rows = list(csv.reader(content.splitlines(keepends=True), dialect=dialect))
        changed = False
        for row, column, text in changes:
            if not isinstance(row, int) or not isinstance(column, int) or row < 0 or row >= len(rows) or column < 0 or column >= len(rows[row]):
                raise WolfEngineError(f"WOLF CSV 定位范围无效: {rel}:{row},{column}")
            if rows[row][column] != text:
                rows[row][column] = text
                changed = True
        if changed:
            with open(path, "w", encoding="utf-8-sig", newline="") as output:
                writer = csv.writer(output, dialect=dialect)
                writer.writerows(rows)


def _data_manifest(data_path):
    result = {}
    for root, _, files in os.walk(data_path):
        for filename in files:
            path = os.path.join(root, filename)
            rel = os.path.relpath(path, data_path).replace(os.sep, "/")
            result[rel] = (os.path.getsize(path), _sha256(path))
    return result


def _transport_residue_files(data_path, chunk_size=1024 * 1024):
    needle = _WOLF_TRANSPORT_TAG_PREFIX.encode("ascii")
    found = []
    for root, _, files in os.walk(data_path):
        for filename in files:
            path = os.path.join(root, filename)
            tail = b""
            with open(path, "rb") as source:
                for chunk in iter(lambda: source.read(chunk_size), b""):
                    combined = tail + chunk
                    if needle in combined:
                        found.append(os.path.relpath(path, data_path))
                        break
                    tail = combined[-(len(needle) - 1):]
    return found


def _data_digest(data_path):
    digest = hashlib.sha256()
    for rel, (size, file_sha256) in sorted(_data_manifest(data_path).items()):
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\0")
        digest.update(file_sha256.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _replace_data_directory(data_path, source_data_path, defer_cleanup=False):
    game_path = os.path.dirname(data_path)
    prepared = os.path.join(game_path, "Data.windy.tmp")
    displaced = os.path.join(game_path, "Data.windy.previous.tmp")
    backup = os.path.join(game_path, "Data.windy-original.bak")
    for path in (prepared, displaced):
        if os.path.exists(path) and not file_system.safe_remove(path):
            raise WolfEngineError(f"无法清理 WOLF Data 临时目录: {path}")
    shutil.copytree(source_data_path, prepared)
    if not os.path.exists(backup):
        backup_temp = f"{backup}.tmp"
        if os.path.exists(backup_temp) and not file_system.safe_remove(backup_temp):
            raise WolfEngineError(f"无法清理 WOLF Data 备份临时目录: {backup_temp}")
        try:
            shutil.copytree(data_path, backup_temp)
            os.replace(backup_temp, backup)
        except Exception:
            if os.path.exists(backup_temp) and not file_system.safe_remove(backup_temp):
                log.warning("无法清理未完成的 WOLF Data 备份: %s", backup_temp)
            raise
    # ponytail: a bounded retry covers transient scanners; persistent locks still fail fast.
    try:
        for attempt in range(6):
            try:
                os.replace(data_path, displaced)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.25 * (attempt + 1))
    except Exception as error:
        if os.path.exists(prepared) and not file_system.safe_remove(prepared):
            log.warning("无法清理未提交的 WOLF Data 临时目录: %s", prepared)
        if isinstance(error, PermissionError):
            raise WolfEngineError(
                "无法替换 WOLF Data：目录仍被占用，请关闭游戏和正在读取游戏文件的程序后重试"
            ) from error
        raise
    try:
        for attempt in range(6):
            try:
                os.replace(prepared, data_path)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.25 * (attempt + 1))
    except Exception:
        os.replace(displaced, data_path)
        raise
    if defer_cleanup:
        return displaced
    if not file_system.safe_remove(displaced):
        log.warning("无法清理已替换的 WOLF Data 临时目录: %s", displaced)
    return None


def _rollback_data_directory(data_path, displaced):
    if not displaced or not os.path.exists(displaced):
        return
    failed = f"{data_path}.windy.failed.tmp"
    if os.path.exists(failed) and not file_system.safe_remove(failed):
        raise WolfEngineError(f"无法清理 WOLF Data 回滚临时目录: {failed}")
    os.replace(data_path, failed)
    try:
        os.replace(displaced, data_path)
    except Exception:
        os.replace(failed, data_path)
        raise
    if not file_system.safe_remove(failed):
        log.warning("无法清理已回滚的 WOLF Data 临时目录: %s", failed)


def _finish_data_directory_replace(displaced):
    if displaced and os.path.exists(displaced) and not file_system.safe_remove(displaced):
        log.warning("无法清理已替换的 WOLF Data 临时目录: %s", displaced)


def _replace_data_and_manifest(
    data_path,
    staging_data,
    manifest_path=None,
    manifest=None,
    root_archive=None,
    archive_session_backup=None,
):
    archive_existed = bool(root_archive and os.path.isfile(root_archive))
    manifest_temp = None
    displaced = None
    temp_archive = f"{root_archive}.windy.tmp" if root_archive else None
    if archive_existed:
        shutil.copy2(root_archive, archive_session_backup)
    if manifest_path:
        manifest_temp = _prepare_json_atomic(manifest_path, manifest)
    try:
        if archive_existed:
            os.remove(root_archive)
        if staging_data:
            displaced = _replace_data_directory(data_path, staging_data, defer_cleanup=True)
        if manifest_temp:
            os.replace(manifest_temp, manifest_path)
            manifest_temp = None
    except Exception as commit_error:
        rollback_errors = []
        try:
            _rollback_data_directory(data_path, displaced)
        except Exception as error:
            rollback_errors.append(f"Data 回滚失败: {error}")
        try:
            if archive_existed:
                shutil.copy2(archive_session_backup, temp_archive)
                os.replace(temp_archive, root_archive)
        except Exception as error:
            rollback_errors.append(f"Data.wolf 回滚失败: {error}")
        if rollback_errors:
            raise WolfEngineError(
                f"WOLF 提交失败且未能完整回滚 ({commit_error}): {'; '.join(rollback_errors)}"
            ) from commit_error
        raise
    finally:
        for temporary in (manifest_temp, temp_archive):
            if temporary and os.path.exists(temporary) and not file_system.safe_remove(temporary):
                log.warning("无法清理 WOLF 提交临时文件: %s", temporary)
    _finish_data_directory_replace(displaced)


def _fusion_font_path():
    relative = os.path.join("modules", "WOLF", "FusionPixel", FUSION_FONT_FILENAME)
    packaged = os.path.join(file_system.get_application_path(), relative)
    if os.path.isfile(packaged):
        return packaged
    source_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    return os.path.join(source_root, relative)


def _required_font_characters(texts):
    result = set()
    for text in texts:
        visible = control_tokens.strip_token_literals(text)
        for character in visible:
            codepoint = ord(character)
            if (
                0x3040 <= codepoint <= 0x30FF
                or 0x3400 <= codepoint <= 0x9FFF
                or 0xF900 <= codepoint <= 0xFAFF
            ):
                result.add(character)
    return result


def _font_slots(game_data):
    main = game_data.get("MainFont")
    subfonts = game_data.get("SubFonts")
    main = main if isinstance(main, str) else ""
    subfonts = subfonts if isinstance(subfonts, list) else []
    result = [main, *[font if isinstance(font, str) else "" for font in subfonts[:3]]]
    return result + [""] * (4 - len(result))


def _set_font_slots(game_data, slots):
    if not isinstance(slots, list) or len(slots) != 4 or not all(isinstance(font, str) for font in slots):
        raise WolfEngineError("WOLF 字体方案必须包含四个字体槽位")
    if not slots[0].strip():
        raise WolfEngineError("WOLF 主字体不能为空")
    game_data["MainFont"] = slots[0]
    game_data["SubFonts"] = slots[1:]


def _read_game_json_slots(json_root):
    path = os.path.join(json_root, "game", "Game.json")
    try:
        with open(path, "r", encoding="utf-8") as source:
            return _font_slots(json.load(source))
    except (OSError, ValueError) as error:
        raise WolfEngineError(f"无法读取 WOLF 字体配置: {error}") from error


def _json_path_value(data, path):
    value = data
    for part in path:
        value = value[part]
    return value


def _synchronize_database_identifiers(
    original_root, patched_root, changes, analysis_usage=None
):
    if analysis_usage is None:
        analysis_usage = _analyze_json_usage(original_root)
    mappings = {}
    policies = {}
    translation_policies = {}
    missing_names_by_namespace = {}
    explicit_references = []
    for change in changes:
        if not isinstance(change, (tuple, list)) or len(change) != 7:
            raise WolfEngineError("WOLF 数据库标识同步元数据版本无效，请重新导出翻译文本")
        (
            namespace,
            original,
            translated,
            policy,
            references,
            translation_policy,
            missing_names,
        ) = change
        if (
            not isinstance(namespace, (tuple, list))
            or len(namespace) != 2
            or not isinstance(original, str)
            or not isinstance(translated, str)
            or (policy, translation_policy) not in {
                ("numeric_only", "numeric_only"),
                ("name_referenced", "name_closed"),
            }
            or not isinstance(references, list)
            or not isinstance(missing_names, list)
            or not all(isinstance(name, str) and name for name in missing_names)
        ):
            raise WolfEngineError("WOLF 数据库标识同步元数据无效")
        if _looks_like_resource(original) or _looks_like_resource(translated):
            raise WolfEngineError("WOLF 资源路径禁止参与数据库标识同步")
        key = tuple(namespace)
        if (
            *key,
            original.strip().casefold(),
        ) in analysis_usage.get("incomplete_identifier_symbols", set()):
            raise WolfEngineError(
                f"WOLF 数据库标识引用未闭合，拒绝导入: {key}: {original!r}"
            )
        policies.setdefault(key, set()).add(policy)
        translation_policies.setdefault(key, set()).add(translation_policy)
        normalized_missing = frozenset(name.casefold() for name in missing_names)
        previous_missing = missing_names_by_namespace.setdefault(
            key, normalized_missing
        )
        if previous_missing != normalized_missing:
            raise WolfEngineError("WOLF 数据库缺失标识保护不一致")
        if translated.casefold() in normalized_missing:
            raise WolfEngineError(
                f"WOLF 数据库译名会激活原本不存在的名称: {key}: {translated!r}"
            )
        previous = mappings.setdefault(key, {}).setdefault(original, translated)
        if previous != translated:
            raise WolfEngineError(
                f"WOLF 数据库标识译法冲突: {key}: {original!r}: {previous!r} / {translated!r}"
            )
        explicit_references.append((key, original, translated, references))

    original_databases = {}
    patched_databases = {}
    database_relatives = {}
    allowed = set()
    original_identifier_indexes = {}

    def database_pair(database_name):
        if database_name not in original_databases:
            database_dir = _safe_join(original_root, "databases")
            filenames = [
                filename
                for filename in os.listdir(database_dir)
                if filename.casefold() == database_name.casefold()
            ]
            if len(filenames) != 1:
                raise WolfEngineError(f"WOLF 数据库文件无法唯一定位: {database_name}")
            relative = f"databases/{filenames[0]}"
            database_relatives[database_name] = relative
            with open(_safe_join(original_root, relative), "r", encoding="utf-8") as source:
                original_databases[database_name] = json.load(source)
            with open(_safe_join(patched_root, relative), "r", encoding="utf-8") as source:
                patched_databases[database_name] = json.load(source)
        return (
            database_relatives[database_name],
            original_databases[database_name],
            patched_databases[database_name],
        )

    def identifier_indexes(database, type_index):
        try:
            data = database["types"][type_index]["data"]
        except (IndexError, KeyError, TypeError) as error:
            raise WolfEngineError(
                f"WOLF 数据库标识命名空间失效: 类型{type_index}"
            ) from error
        result = {}
        for data_index, datum in enumerate(data):
            fields = datum.get("data") or []
            if not fields or not _uses_first_string_database_id(datum, 0):
                continue
            value = fields[0].get("value")
            if isinstance(value, str) and value:
                result.setdefault(value, set()).add(data_index)
        return result

    for (database_name, type_index), mapping in mappings.items():
        relative, original_database, patched_database = database_pair(database_name)
        original_indexes = identifier_indexes(original_database, type_index)
        patched_indexes = identifier_indexes(patched_database, type_index)
        original_folded = {}
        patched_folded = {}
        for value, indexes in original_indexes.items():
            original_folded.setdefault(value.casefold(), set()).update(indexes)
        for value, indexes in patched_indexes.items():
            patched_folded.setdefault(value.casefold(), set()).update(indexes)
        original_identifier_indexes[(database_name, type_index)] = original_folded
        groups = {}
        for original in original_indexes:
            translated = mapping.get(original, original)
            groups.setdefault(translated.casefold(), set()).add(original)
        collisions = [
            (translated, sources)
            for translated, sources in groups.items()
            if len({source.casefold() for source in sources}) > 1
        ]
        if collisions and policies.get((database_name, type_index)) != {"numeric_only"}:
            translated, sources = collisions[0]
            raise WolfEngineError(
                f"WOLF 数据库译名碰撞: {database_name} 类型{type_index}: "
                f"{sorted(sources)!r} -> {translated!r}"
            )
        if translation_policies.get((database_name, type_index)) == {"name_closed"}:
            for original, translated in mapping.items():
                source_indexes = original_folded.get(original.casefold(), set())
                target_indexes = patched_folded.get(translated.casefold(), set())
                if not source_indexes or target_indexes != source_indexes:
                    raise WolfEngineError(
                        f"WOLF 数据库名称解析目标改变: {database_name} 类型{type_index}: "
                        f"{original!r} -> {translated!r}"
                    )
                for data_index in source_indexes:
                    allowed.add((
                        relative,
                        ("types", type_index, "data", data_index, "data", 0, "value"),
                    ))

    reference_edges = {}
    for namespace, original, translated, references in explicit_references:
        source_indexes = original_identifier_indexes.get(namespace, {}).get(
            original.casefold(), set()
        )
        for reference in references:
            if not isinstance(reference, dict):
                raise WolfEngineError("WOLF 数据库标识引用图无效")
            relative = reference.get("file")
            path = reference.get("path")
            target_indexes = reference.get("target_data_indexes")
            if (
                not isinstance(relative, str)
                or not isinstance(path, list)
                or not isinstance(target_indexes, list)
                or set(target_indexes) != source_indexes
            ):
                raise WolfEngineError(
                    f"WOLF 数据库标识引用目标不一致: {namespace}: {original!r}"
                )
            expected = reference.get("expected_original", original)
            if (
                not isinstance(expected, str)
                or original.casefold() not in expected.casefold()
            ):
                raise WolfEngineError(
                    f"WOLF 数据库标识引用原值无效: {relative}: {path}"
                )
            edge_key = (relative.casefold(), tuple(path))
            edge = reference_edges.setdefault(edge_key, {
                "relative": relative,
                "path": tuple(path),
                "expected": expected,
                "mappings": {},
            })
            if edge["expected"] != expected:
                raise WolfEngineError(f"WOLF 数据库标识引用路径冲突: {relative}: {path}")
            previous = edge["mappings"].setdefault(original.casefold(), translated)
            if previous != translated:
                raise WolfEngineError(f"WOLF 数据库标识引用译法冲突: {relative}: {path}")

    reference_cache = {}
    for edge in reference_edges.values():
        relative = edge["relative"]
        if relative not in reference_cache:
            with open(_safe_join(original_root, relative), "r", encoding="utf-8") as source:
                original_data = json.load(source)
            with open(_safe_join(patched_root, relative), "r", encoding="utf-8") as source:
                patched_data = json.load(source)
            reference_cache[relative] = original_data, patched_data
        original_data, patched_data = reference_cache[relative]
        path = edge["path"]
        try:
            original_value = _json_path_value(original_data, path)
            patched_value = _json_path_value(patched_data, path)
        except (IndexError, KeyError, TypeError) as error:
            raise WolfEngineError(
                f"WOLF 数据库标识引用路径失效: {relative}: {list(path)}"
            ) from error
        if original_value != edge["expected"]:
            raise WolfEngineError(
                f"WOLF 数据库标识引用原值改变: {relative}: {list(path)}"
            )
        sources = sorted(edge["mappings"], key=len, reverse=True)
        pattern = re.compile(
            "|".join(re.escape(source) for source in sources), re.IGNORECASE
        )
        translated_value = pattern.sub(
            lambda match: edge["mappings"][match.group(0).casefold()],
            original_value,
        )
        if patched_value not in (original_value, translated_value):
            raise WolfEngineError(
                f"WOLF 数据库标识引用联动冲突: {relative}: {list(path)}"
            )
        _set_json_path(patched_data, list(path), translated_value)
        allowed.add((relative, path))

    for relative, (_original_data, patched_data) in reference_cache.items():
        _write_json_atomic(_safe_join(patched_root, relative), patched_data)

    return allowed


def _verify_logic_json_unchanged(
    original_root, patched_root, allowed_changes=None, usage=None
):
    if usage is None:
        runtime_references, _scene_references = _collect_runtime_references(original_root)
        usage = _analyze_json_usage(
            original_root,
            _runtime_map_json_names(original_root, runtime_references),
        )
    allowed_changes = set(allowed_changes or ())
    protected = set()
    for root, _, files in os.walk(original_root):
        for filename in files:
            if not filename.lower().endswith(".json"):
                continue
            source_path = os.path.join(root, filename)
            for metadata, _text in _json_entries(original_root, source_path, usage):
                if metadata.get("kind") == "json" and metadata.get("marker") == "WOLFLogic":
                    protected.add((metadata["file"], tuple(metadata["path"])))

    cache = {}
    for relative, path in sorted(protected, key=lambda item: (item[0].casefold(), repr(item[1]))):
        if (relative, path) in allowed_changes:
            continue
        if relative not in cache:
            try:
                with open(_safe_join(original_root, relative), "r", encoding="utf-8") as source:
                    original = json.load(source)
                with open(_safe_join(patched_root, relative), "r", encoding="utf-8") as source:
                    patched = json.load(source)
            except (OSError, ValueError) as error:
                raise WolfEngineError(f"无法验证 WOLF 逻辑字段 {relative}: {error}") from error
            cache[relative] = original, patched
        original, patched = cache[relative]
        try:
            unchanged = _json_path_value(original, path) == _json_path_value(patched, path)
        except (IndexError, KeyError, TypeError) as error:
            raise WolfEngineError(f"WOLF 逻辑字段路径失效: {relative}: {list(path)}") from error
        if not unchanged:
            raise WolfEngineError(f"WOLF 逻辑字段被翻译，拒绝导入: {relative}: {list(path)}")


def _verify_json_dump_matches(expected_root, actual_root):
    def json_files(root):
        return {
            os.path.relpath(os.path.join(directory, filename), root).replace(os.sep, "/")
            for directory, _, filenames in os.walk(root)
            for filename in filenames
            if filename.lower().endswith(".json")
        }

    expected_files = json_files(expected_root)
    actual_files = json_files(actual_root)
    if expected_files != actual_files:
        missing = sorted(expected_files - actual_files, key=str.casefold)
        extra = sorted(actual_files - expected_files, key=str.casefold)
        raise WolfEngineError(f"WOLF Data 重读文件集合不一致: 缺少 {missing[:5]}，多出 {extra[:5]}")
    for relative in sorted(expected_files, key=str.casefold):
        expected_path = _safe_join(expected_root, relative)
        actual_path = _safe_join(actual_root, relative)
        try:
            with open(expected_path, "r", encoding="utf-8") as source:
                expected = json.load(source)
            with open(actual_path, "r", encoding="utf-8") as source:
                actual = json.load(source)
        except (OSError, ValueError) as error:
            raise WolfEngineError(f"无法比较 WOLF Data 重读结果 {relative}: {error}") from error
        if actual != expected:
            raise WolfEngineError(f"WOLF Data 重读内容不一致: {relative}")


def _manifest_font_slots(manifest, fallback_slots):
    revision = manifest.get("font_revision")
    if isinstance(revision, dict):
        applied = revision.get("applied_slots")
        if isinstance(applied, list) and len(applied) == 4:
            families = [
                item.get("family", "") if isinstance(item, dict) else item
                for item in applied
            ]
            if all(isinstance(family, str) for family in families) and families[0]:
                return families
    legacy = manifest.get("font")
    if isinstance(legacy, dict) and isinstance(legacy.get("family"), str):
        return [legacy["family"], *fallback_slots[1:]]
    return list(fallback_slots)


def get_font_revision_context(game_path):
    data_path = os.path.join(game_path, "Data")
    _validate_data_dir(data_path)
    manifest_path = _state_path(game_path, MANIFEST_FILENAME)
    try:
        with open(manifest_path, "r", encoding="utf-8") as source:
            manifest = json.load(source)
    except (OSError, ValueError) as error:
        raise WolfEngineError(f"无法读取 WOLF 初始化状态: {error}") from error

    snapshot_path = _state_path(game_path, JSON_SNAPSHOT_DIRNAME)
    if os.path.isfile(os.path.join(snapshot_path, "game", "Game.json")):
        snapshot_slots = _read_game_json_slots(snapshot_path)
    else:
        with tempfile.TemporaryDirectory(prefix="windy-wolf-font-read-") as temp_root:
            uberwolf.dump_text(data_path, temp_root)
            snapshot_slots = _read_game_json_slots(temp_root)

    revision = manifest.get("font_revision") if isinstance(manifest.get("font_revision"), dict) else {}
    original_slots = revision.get("original_slots")
    if not isinstance(original_slots, list) or len(original_slots) != 4:
        original_slots = list(snapshot_slots)
        legacy = manifest.get("font")
        if isinstance(legacy, dict) and isinstance(legacy.get("original_main"), str):
            original_slots[0] = legacy["original_main"]
    applied_slots = _manifest_font_slots(manifest, snapshot_slots)
    selected_slots = revision.get("selected_slots")
    if not isinstance(selected_slots, list) or len(selected_slots) != 4:
        selected_slots = [{"family": family, "source": "current", "files": []} for family in applied_slots]
    return {
        "original_slots": original_slots,
        "applied_slots": applied_slots,
        "selected_slots": selected_slots,
        "copied_files": revision.get("copied_files", []) or (
            [{"filename": manifest["font"]["filename"], "sha256": manifest["font"].get("sha256")}]
            if isinstance(manifest.get("font"), dict) and manifest["font"].get("filename")
            else []
        ),
        "system_font_copy_ack": revision.get("system_font_copy_ack", []),
    }


def try_get_font_revision_context(game_path):
    try:
        return get_font_revision_context(game_path)
    except (OSError, WolfEngineError, uberwolf.UberWolfError):
        return None


def _font_selection_files(selection):
    files = selection.get("files") if isinstance(selection, dict) else None
    if not isinstance(files, list):
        return []
    result = []
    for item in files:
        if not isinstance(item, dict):
            raise WolfEngineError("WOLF 字体文件记录无效")
        path = item.get("path")
        expected_hash = item.get("sha256")
        if not isinstance(path, str) or not os.path.isfile(path):
            raise WolfEngineError(f"WOLF 字体文件不存在: {path}")
        actual_hash = _sha256(path)
        if expected_hash and actual_hash.lower() != str(expected_hash).lower():
            raise WolfEngineError(f"WOLF 字体文件已变化，请刷新字体列表: {path}")
        result.append({
            "path": path,
            "filename": os.path.basename(path),
            "sha256": actual_hash,
            **({"relative": item["relative"]} if isinstance(item.get("relative"), str) else {}),
        })
    return result


def prepare_font_revision(candidates):
    if not isinstance(candidates, list) or len(candidates) != 4:
        raise WolfEngineError("WOLF 字体方案必须包含四个字体槽位")
    prepared = []
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            raise WolfEngineError(f"WOLF 字体槽位 {index} 无效")
        family = candidate.get("family", "")
        source = candidate.get("source", "current")
        if not isinstance(family, str) or not isinstance(source, str):
            raise WolfEngineError(f"WOLF 字体槽位 {index} 无效")
        if index == 0 and not family.strip():
            raise WolfEngineError("WOLF 主字体不能为空")
        files = _font_selection_files(candidate) if source in ("module", "system") else []
        if source in ("module", "system") and not files:
            raise WolfEngineError(f"字体 {family} 无法定位可复制的字体文件")
        prepared.append({"family": family, "source": source, "files": files})
    return prepared


def font_revision_missing_characters(revision, characters):
    required = set(characters)
    result = []
    for index, selection in enumerate(revision):
        files = [item["path"] for item in selection.get("files", []) if os.path.isfile(item.get("path", ""))]
        missing = set()
        error = None
        if selection.get("family") and files:
            try:
                missing = font_coverage.missing_characters_in_files(files, required)
            except (OSError, font_coverage.FontCoverageError) as caught:
                error = str(caught)
        result.append({"slot": index, "family": selection.get("family", ""), "missing": missing, "error": error})
    return result


def font_revision_required_characters(game_path, sample_text=""):
    scripts_path = os.path.join(game_path, STRING_SCRIPTS_DIRNAME)
    origin_path = os.path.join(game_path, STRING_SCRIPTS_ORIGIN_DIRNAME)
    if not os.path.isdir(scripts_path) or not os.path.isdir(origin_path):
        return _required_font_characters([sample_text]), False
    try:
        origin_entries = {
            _entry_identity(script_rel, metadata): text
            for script_rel, metadata, text in _iter_released_entries(origin_path)
        }
        texts = []
        has_translation = False
        for script_rel, metadata, text in _iter_released_entries(scripts_path):
            if metadata.get("marker") == "WOLFLogic":
                continue
            original = origin_entries.get(_entry_identity(script_rel, metadata))
            if original is None:
                continue
            has_translation = has_translation or text != original
            texts.append(_decode_wolf_transport(text, metadata))
    except (OSError, WolfEngineError):
        return _required_font_characters([sample_text]), False
    if not has_translation:
        return _required_font_characters([sample_text]), False
    return _required_font_characters(texts), True


def _serialized_font_selection(selection):
    return {
        "family": selection["family"],
        "source": selection["source"],
        "files": [
            {
                "filename": item["filename"],
                "sha256": item["sha256"],
                **({"relative": item["relative"]} if "relative" in item else {}),
            }
            for item in selection.get("files", [])
        ],
    }


def _check_font_destinations(game_path, revision, managed_files):
    managed = {
        item.get("filename"): item.get("sha256")
        for item in managed_files
        if isinstance(item, dict)
    }
    copies = {}
    for selection in revision:
        if selection["source"] not in ("module", "system"):
            continue
        for item in selection["files"]:
            previous = copies.get(item["filename"])
            if previous and previous["sha256"] != item["sha256"]:
                raise WolfEngineError(f"两个字体文件同名但内容不同: {item['filename']}")
            copies[item["filename"]] = item
    for filename, item in copies.items():
        destination = os.path.join(game_path, filename)
        if not os.path.isfile(destination) or _sha256(destination) == item["sha256"]:
            continue
        backup = f"{destination}.windy-original.bak"
        if os.path.exists(backup) and managed.get(filename) != _sha256(destination):
            raise WolfEngineError(f"字体文件及其备份均已存在，拒绝覆盖: {filename}")
    return copies


def _commit_font_files(game_path, copies):
    rollback = []
    try:
        for filename, item in copies.items():
            destination = os.path.join(game_path, filename)
            if os.path.isfile(destination) and _sha256(destination) == item["sha256"]:
                continue
            existed = os.path.isfile(destination)
            session_backup = f"{destination}.windy-session.bak"
            if existed:
                shutil.copy2(destination, session_backup)
                permanent_backup = f"{destination}.windy-original.bak"
                if not os.path.exists(permanent_backup):
                    shutil.copy2(destination, permanent_backup)
            rollback.append((destination, session_backup if existed else None))
            temp_destination = f"{destination}.windy.tmp"
            shutil.copy2(item["path"], temp_destination)
            os.replace(temp_destination, destination)
        return rollback
    except Exception:
        _rollback_font_files(rollback)
        raise


def _rollback_font_files(rollback):
    for destination, backup in reversed(rollback):
        if backup and os.path.isfile(backup):
            os.replace(backup, destination)
        elif os.path.isfile(destination):
            os.remove(destination)
    for _destination, backup in rollback:
        if backup and os.path.isfile(backup):
            os.remove(backup)


def _finish_font_files(rollback):
    for _destination, backup in rollback:
        if backup and os.path.isfile(backup):
            try:
                os.remove(backup)
            except OSError as error:
                log.warning("无法清理 WOLF 字体会话备份 %s: %s", backup, error)


def _remove_stale_managed_fonts(game_path, previous_files, copies, active_families):
    active = {family.casefold() for family in active_families if family}
    for item in previous_files:
        if not isinstance(item, dict) or item.get("filename") in copies or not item.get("sha256"):
            continue
        path = os.path.join(game_path, item["filename"])
        if not os.path.isfile(path) or _sha256(path).lower() != str(item["sha256"]).lower():
            continue
        try:
            if active.intersection(family.casefold() for family in font_coverage.font_families(path)):
                continue
            os.remove(path)
        except (OSError, font_coverage.FontCoverageError) as error:
            log.warning("无法清理不再使用的 WOLF 字体 %s: %s", path, error)


def apply_font_revision(game_path, revision, message_queue=None):
    data_path = os.path.join(game_path, "Data")
    manifest_path = _state_path(game_path, MANIFEST_FILENAME)
    _validate_data_dir(data_path)
    try:
        with open(manifest_path, "r", encoding="utf-8") as source:
            manifest = json.load(source)
    except (OSError, ValueError) as error:
        raise WolfEngineError(f"无法读取 WOLF 初始化状态: {error}") from error
    context = get_font_revision_context(game_path)
    prepared = prepare_font_revision(revision.get("slots") if isinstance(revision, dict) else revision)
    system_families = {item["family"] for item in prepared if item["source"] == "system"}
    acknowledgements = set(revision.get("system_font_copy_ack", [])) if isinstance(revision, dict) else set()
    if system_families - acknowledgements:
        raise WolfEngineError("复制系统字体前必须确认字体授权风险")
    copies = _check_font_destinations(game_path, prepared, context["copied_files"])
    serialized = [_serialized_font_selection(item) for item in prepared]
    copied_files = [
        {"filename": filename, "sha256": item["sha256"]}
        for filename, item in sorted(copies.items())
    ]

    with tempfile.TemporaryDirectory(prefix="windy-wolf-font-apply-") as temp_root:
        patch_path = os.path.join(temp_root, "json")
        staging_data = os.path.join(temp_root, "Data")
        if message_queue:
            message_queue.put(("log", ("normal", "解析当前 WOLF 数据并应用四槽位字体方案...")))
        uberwolf.dump_text(data_path, patch_path)
        shutil.copytree(data_path, staging_data)
        game_json_path = os.path.join(patch_path, "game", "Game.json")
        with open(game_json_path, "r", encoding="utf-8") as source:
            game_data = json.load(source)
        _set_font_slots(game_data, [item["family"] for item in prepared])
        _write_json_atomic(game_json_path, game_data)
        uberwolf.apply_text(data_path, patch_path, staging_data)
        active_archive_layout = _archive_layout(game_path)
        disabled_archives = _prepare_disabled_archives(
            game_path,
            manifest.get("disabled_archives", []),
        )
        retained_archive_sha256 = (
            _archive_digest(game_path, active_archive_layout)
            if active_archive_layout in ("single", "split")
            else manifest.get("retained_archive_sha256")
        )
        _strip_split_archives(staging_data)
        verification_json = os.path.join(temp_root, "verify-json")
        uberwolf.dump_text(staging_data, verification_json)
        expected_slots = [item["family"] for item in prepared]
        if _read_game_json_slots(verification_json) != expected_slots:
            raise WolfEngineError("字体修订后的 Data 重读结果不一致，拒绝替换")

        staged_data_sha256 = _data_digest(staging_data)
        next_manifest = dict(manifest)
        if retained_archive_sha256:
            next_manifest["retained_archive_sha256"] = retained_archive_sha256
        next_manifest["disabled_archives"] = disabled_archives
        next_manifest["deployment_layout"] = "loose"
        next_manifest["last_import_data_sha256"] = staged_data_sha256
        next_manifest["data_sha256"] = staged_data_sha256
        next_manifest["font_revision"] = {
            "original_slots": context["original_slots"],
            "selected_slots": serialized,
            "applied_slots": serialized,
            "copied_files": copied_files,
            "system_font_copy_ack": sorted(acknowledgements),
        }
        font_rollback = _commit_font_files(game_path, copies)
        try:
            root_archive = os.path.join(game_path, "Data.wolf")
            _replace_data_and_manifest(
                data_path,
                staging_data,
                manifest_path,
                next_manifest,
                root_archive if os.path.isfile(root_archive) else None,
                os.path.join(temp_root, "Data.wolf.before"),
            )
        except Exception:
            _rollback_font_files(font_rollback)
            raise
        _finish_font_files(font_rollback)

    _remove_stale_managed_fonts(
        game_path,
        context["copied_files"],
        copies,
        [item["family"] for item in prepared],
    )
    if message_queue:
        message_queue.put(("log", ("success", "WOLF 字体修订已应用并通过 Data 重读校验。")))
        message_queue.put(("font_revision_applied", game_path))
    return True


def _overlay_applied_font_slots(patch_path, manifest):
    game_json_path = os.path.join(patch_path, "game", "Game.json")
    with open(game_json_path, "r", encoding="utf-8") as source:
        game_data = json.load(source)
    current = _font_slots(game_data)
    applied = _manifest_font_slots(manifest, current)
    if applied != current:
        _set_font_slots(game_data, applied)
        _write_json_atomic(game_json_path, game_data)


def _font_coverage_warnings(game_path, slots, texts):
    required = _required_font_characters(texts)
    if not required:
        return []
    module_root = os.path.join(file_system.get_application_path(), "modules", "WOLF")
    candidates = font_coverage.discover_font_candidates(module_root, game_path)
    by_family = {}
    for candidate in candidates:
        by_family.setdefault(candidate["family"].casefold(), []).extend(
            item["path"] for item in candidate["files"]
        )
    warnings = []
    for index, family in enumerate(slots):
        if not family:
            continue
        paths = sorted(set(by_family.get(family.casefold(), [])))
        if not paths:
            warnings.append(f"字体槽位 {index}（{family}）无法定位字体文件")
            continue
        try:
            missing = font_coverage.missing_characters_in_files(paths, required)
        except (OSError, font_coverage.FontCoverageError) as error:
            warnings.append(f"字体槽位 {index}（{family}）无法检查: {error}")
            continue
        if missing:
            warnings.append(
                f"字体槽位 {index}（{family}）缺少 {len(missing)} 个译文字符: "
                f"{''.join(sorted(missing))[:30]}"
            )
    return warnings


def import_from_string_scripts(game_path, message_queue=None):
    data_path = os.path.join(game_path, "Data")
    scripts_path = os.path.join(game_path, STRING_SCRIPTS_DIRNAME)
    snapshot_path = _state_path(game_path, JSON_SNAPSHOT_DIRNAME)
    manifest_path = _state_path(game_path, MANIFEST_FILENAME)
    _validate_data_dir(data_path)
    if not os.path.isdir(scripts_path) or not os.path.isdir(snapshot_path):
        raise WolfEngineError("缺少 WOLF StringScripts 或原始 JSON 快照，请先导出文本")
    origin_path = os.path.join(game_path, STRING_SCRIPTS_ORIGIN_DIRNAME)
    if not os.path.isdir(origin_path):
        raise WolfEngineError("缺少 WOLF StringScripts_Origin，请先导出文本")
    with open(manifest_path, "r", encoding="utf-8") as source:
        manifest = json.load(source)

    origin_entries = {
        _entry_identity(script_rel, metadata): text
        for script_rel, metadata, text in _iter_released_entries(
            origin_path
        )
    }
    if not origin_entries:
        raise WolfEngineError("WOLF StringScripts_Origin 不含当前版本条目，请重新导出文本")
    released_entries = list(_iter_released_entries(scripts_path))
    if not released_entries:
        raise WolfEngineError("WOLF StringScripts 不含当前版本条目，请重新导出文本")
    with tempfile.TemporaryDirectory(prefix="windy-wolf-import-") as temp_root:
        patch_path = os.path.join(temp_root, "json")
        staging_data = os.path.join(temp_root, "Data")
        shutil.copytree(snapshot_path, patch_path)
        shutil.copytree(data_path, staging_data)
        _overlay_applied_font_slots(patch_path, manifest)

        json_cache = {}
        txt_changes = {}
        csv_changes = {}
        applied = 0
        changed = 0
        font_texts = []
        identifier_changes = []
        seen_identities = set()
        for script_rel, metadata, text in released_entries:
            identity = _entry_identity(script_rel, metadata)
            seen_identities.add(identity)
            if identity not in origin_entries:
                raise WolfEngineError(f"WOLF StringScripts 包含未知条目: {script_rel}")
            original_text = origin_entries[identity]
            if metadata.get("marker") == "WOLFLogic" and text != original_text:
                raise WolfEngineError(f"WOLF 逻辑字面量被修改: {script_rel}")
            control_ok, control_reason = control_tokens.validate_restored_text(original_text, text)
            if not control_ok:
                raise WolfEngineError(f"WOLF 控制码损坏: {script_rel}: {control_reason}")
            original_core = _split_text_format(original_text)[0]
            translated_core = _split_text_format(text)[0]
            transport_ok, transport_reason = _validate_wolf_transport(original_core, translated_core, metadata)
            if not transport_ok:
                raise WolfEngineError(f"WOLF 控制码损坏: {script_rel}: {transport_reason}")
            if metadata.get("marker") == "WOLFText" and original_text.count("\n") != text.count("\n"):
                raise WolfEngineError(f"WOLF 外部文本行数不一致: {script_rel}")
            if text != original_text:
                changed += 1
            target_text = _decode_wolf_transport(text, metadata)
            original_target_text = _decode_wolf_transport(original_text, metadata)
            namespace = metadata.get("identifier_namespace")
            if target_text != original_target_text and isinstance(namespace, list) and len(namespace) == 2:
                translation_policy = metadata["identifier_translation_policy"]
                if translation_policy == "unsafe":
                    raise WolfEngineError(f"WOLF 数据库标识引用未闭合，拒绝导入: {script_rel}")
                if (
                    translation_policy == "name_closed"
                    and metadata.get("identifier_reference_complete") is not True
                ):
                    raise WolfEngineError(f"WOLF 数据库标识缺少完整引用图，拒绝导入: {script_rel}")
                references = metadata.get("identifier_references") or []
                if not all(
                    isinstance(reference, dict)
                    and isinstance(reference.get("file"), str)
                    and isinstance(reference.get("path"), list)
                    and isinstance(reference.get("target_data_indexes"), list)
                    and all(
                        isinstance(index, int) and index >= 0
                        for index in reference["target_data_indexes"]
                    )
                    for reference in references
                ):
                    raise WolfEngineError(f"WOLF 数据库标识引用图无效: {script_rel}")
                if translation_policy != "name_closed":
                    references = []
                collision_policy = metadata["identifier_collision_policy"]
                identifier_changes.append(
                    (
                        namespace,
                        original_target_text,
                        target_text,
                        collision_policy,
                        references,
                        translation_policy,
                        metadata.get("identifier_missing_names") or [],
                    )
                )
            if metadata.get("marker") != "WOLFLogic":
                font_texts.append(target_text)
            if target_text != original_target_text:
                kind = metadata.get("kind")
                rel = metadata.get("file")
                if kind == "json":
                    target = _safe_join(patch_path, rel)
                    if target not in json_cache:
                        with open(target, "r", encoding="utf-8") as source:
                            json_cache[target] = json.load(source)
                    _set_json_path(json_cache[target], metadata["path"], target_text)
                elif kind == "txt":
                    restored_text = _restore_line_prefixes(target_text, metadata)
                    txt_changes.setdefault(rel, []).append((
                        metadata["start"], metadata["end"], restored_text
                    ))
                elif kind == "csv":
                    csv_changes.setdefault(rel, []).append((
                        metadata["row"], metadata["column"], target_text
                    ))
                else:
                    raise WolfEngineError(f"未知的 WOLF 文本类型: {kind}")
            applied += 1

        missing_identities = set(origin_entries) - seen_identities
        if missing_identities:
            raise WolfEngineError(f"WOLF StringScripts 缺少 {len(missing_identities)} 个原始条目")

        for path, data in json_cache.items():
            _write_json_atomic(path, data)
        runtime_references, _scene_references = _collect_runtime_references(snapshot_path)
        verification_usage = _analyze_json_usage(
            snapshot_path,
            _runtime_map_json_names(snapshot_path, runtime_references),
        )
        allowed_identifier_changes = _synchronize_database_identifiers(
            snapshot_path, patch_path, identifier_changes, verification_usage
        ) if identifier_changes else set()
        _verify_logic_json_unchanged(
            snapshot_path, patch_path, allowed_identifier_changes, verification_usage
        )

        applied_font_slots = _read_game_json_slots(patch_path)
        for warning in _font_coverage_warnings(game_path, applied_font_slots, font_texts):
            if message_queue:
                message_queue.put((
                    "log",
                    ("warning", f"{warning}。请在“字体修订”页检查并替换字体。"),
                ))

        if message_queue:
            message_queue.put(("log", ("normal", "将 StringScripts 写回 WOLF 数据并执行结构校验...")))
        uberwolf.apply_text(data_path, patch_path, staging_data)
        _apply_resource_changes(staging_data, txt_changes, csv_changes)
        residue_files = _transport_residue_files(staging_data)
        if residue_files:
            raise WolfEngineError(
                "WOLF 写回数据仍含内部控制标签，拒绝替换 Data: "
                + ", ".join(residue_files[:5])
            )
        active_archive_layout = _archive_layout(game_path)
        disabled_archives = _prepare_disabled_archives(
            game_path,
            manifest.get("disabled_archives", []),
        )
        retained_archive_sha256 = (
            _archive_digest(game_path, active_archive_layout)
            if active_archive_layout in ("single", "split")
            else manifest.get("retained_archive_sha256")
        )
        _strip_split_archives(staging_data)
        verification_json = os.path.join(temp_root, "verify-json")
        uberwolf.dump_text(staging_data, verification_json)
        _verify_json_dump_matches(patch_path, verification_json)

        staged_data_sha256 = _data_digest(staging_data)
        next_manifest = dict(manifest)
        if retained_archive_sha256:
            next_manifest["retained_archive_sha256"] = retained_archive_sha256
        next_manifest["disabled_archives"] = disabled_archives
        next_manifest["deployment_layout"] = "loose"
        next_manifest["last_import_data_sha256"] = staged_data_sha256
        next_manifest["data_sha256"] = staged_data_sha256
        root_archive = os.path.join(game_path, "Data.wolf")
        _replace_data_and_manifest(
            data_path,
            staging_data,
            manifest_path,
            next_manifest,
            root_archive if os.path.isfile(root_archive) else None,
            os.path.join(temp_root, "Data.wolf.before"),
        )

    if message_queue:
        message_queue.put((
            "log",
            ("success", f"WOLF 导入完成并通过 Data 重读校验：共 {applied} 条，实际变化 {changed} 条。"),
        ))
        message_queue.put(("wolf_translation_imported", game_path))
    return changed
