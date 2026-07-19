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

from core.external import uberwolf
from core.utils import control_tokens, file_system, font_coverage, text_processing

log = logging.getLogger(__name__)

STRING_SCRIPTS_DIRNAME = "StringScripts"
STRING_SCRIPTS_ORIGIN_DIRNAME = "StringScripts_Origin"
STATE_DIRNAME = ".windy_wolf"
JSON_SNAPSHOT_DIRNAME = "original_json"
MANIFEST_FILENAME = "manifest.json"
DISABLED_ARCHIVES_DIRNAME = "disabled_archives"
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
_INTERNAL_DATABASE_FIELD_RE = re.compile(
    r"(?:識別|识别|ファイル(?:名|パス)?|file\s*path|コモン|"
    r"アドレス|参照(?:名|先)|読み込み先|読込先|作中には使わない|"
    r"(?:^|[_#【\s])メモ(?:$|[】\d_\s]))",
    re.IGNORECASE,
)
_SCENARIO_FIELD_RE = re.compile(r"(?:シナリオ|scenario).*(?:id|ＩＤ)?", re.IGNORECASE)
_MAP_REFERENCE_RE = re.compile(r"(?:^|[/\\])([^/\\\r\n]+)\.mps(?:$|[\r\n])", re.IGNORECASE)
_DATABASE_JSON_BY_KIND = {
    0: "cdatabase.json",
    1: "sysdatabase.json",
    2: "database.json",
}
_DISPLAY_COMMON_EVENT_RE = re.compile(
    r"(?:文章|メッセージ|ログ|文字|テキスト|tips?|ボタン|説明|攻略|"
    r"エフェクト|オブジェ生成|おまけ|見る)",
    re.IGNORECASE,
)
_DEVELOPMENT_NAME_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:bk\d*|backup|old|test|sample)(?![A-Za-z0-9])|"
    r"(?:サンプル|デバッグ|検証用|案\d+$|一時専用)",
    re.IGNORECASE,
)


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


def _database_field_marker(field_name):
    if isinstance(field_name, str) and _INTERNAL_DATABASE_FIELD_RE.search(field_name):
        return "WOLFLogic"
    return "WOLFText"


def _is_explicit_internal_database_text(value):
    return isinstance(value, str) and (
        value.lstrip().startswith("#")
        or _DEVELOPMENT_NAME_RE.search(value)
    )


def _common_event_displays_text(name):
    return isinstance(name, str) and bool(_DISPLAY_COMMON_EVENT_RE.search(name))


def _add_entry(entries, metadata, text):
    if not isinstance(text, str) or not text.strip() or _looks_like_resource(text):
        return
    core, newline, suffix = _split_text_format(text)
    # ponytail: StringScripts uses ## as a block terminator; a sidecar format is the upgrade path.
    if any(line.strip() == "##" for line in core.splitlines()):
        raise WolfEngineError("WOLF 文本包含独立的 ## 行，无法安全写入 StringScripts")
    metadata = dict(metadata)
    metadata["newline"] = newline
    metadata["suffix"] = suffix
    entries.append((metadata, core))


def _command_entries(command, base_path, entries, json_rel, usage=None):
    try:
        code = int(command.get("code", -1))
    except (TypeError, ValueError):
        return
    string_args = command.get("stringArgs")
    if not isinstance(string_args, list):
        return
    marker = "Message"
    indexes = range(len(string_args))
    if code == 112:
        marker = "WOLFLogic"
    elif code == 150:
        int_args = command.get("intArgs") or []
        if not int_args or ((int(int_args[0]) >> 4) & 0x07) != 2:
            return
        marker = "StringPicture"
    elif code == 300:
        # ponytail: WOLF's dump has no common-event argument schema. This conservative
        # sentence heuristic skips identifier-like arguments; expose argument roles in
        # WolfRPGText before translating every code-300 parameter.
        display_event = bool(string_args) and _common_event_displays_text(string_args[0])
        indexes = [
            index for index, text in enumerate(string_args[1:], start=1)
            if display_event or _looks_like_display_parameter(text)
        ]
    elif code not in _NORMAL_COMMAND_CODES:
        return

    for index in indexes:
        text = string_args[index]
        entry_marker = marker
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
                "marker": "WOLFText" if code == 300 else entry_marker,
                **(
                    {"logic_role": "comparison"}
                    if entry_marker == "WOLFLogic"
                    else {}
                ),
            },
            text,
        )


def _looks_like_display_parameter(text):
    if not isinstance(text, str) or not text.strip() or _looks_like_resource(text):
        return False
    stripped = text.strip()
    if "\n" in stripped or "\r" in stripped:
        return True
    return bool(re.search(r"(?:[。！？…]|・・・|ました|ません|ください|下さい|です|ます)$", stripped))


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


def _database_read(command, schemas=None):
    try:
        code = int(command.get("code", -1))
        int_args = command.get("intArgs") or []
        string_args = command.get("stringArgs") or []
        if code != 250 or len(int_args) < 5 or len(string_args) < 4:
            return None
        database_name = _DATABASE_JSON_BY_KIND.get((int(int_args[3]) >> 8) & 0x0F)
        schema = (schemas or {}).get(database_name, {})
        type_name = string_args[1].strip() if isinstance(string_args[1], str) else ""
        type_index = schema.get("types", {}).get(type_name.casefold(), int(int_args[0]))
        field_name = string_args[3].strip() if isinstance(string_args[3], str) else ""
        field_index = (
            schema.get("fields", {}).get(type_index, {}).get(field_name.casefold(), int(int_args[2]))
            if field_name
            else int(int_args[2])
        )
        destination = int(int_args[4])
    except (TypeError, ValueError, IndexError):
        return None
    if database_name is None or type_index < 0 or field_index < 0:
        return None
    if schema and (
        type_index not in schema.get("fields", {})
        or field_index not in schema["fields"][type_index].values()
    ):
        return None
    token = _string_variable_token(destination)
    if token is None:
        return None
    return (database_name, type_index, field_index), destination, token


def _command_overwrites_string(command, destination, token):
    try:
        code = int(command.get("code", -1))
        int_args = command.get("intArgs") or []
    except (TypeError, ValueError):
        return False
    if code not in (122, 250) or not int_args:
        return False
    try:
        target = int(int_args[4] if code == 250 and len(int_args) >= 5 else int_args[0])
    except (TypeError, ValueError, IndexError):
        return False
    if target != destination:
        return False
    return not any(
        isinstance(text, str) and token in text
        for text in (command.get("stringArgs") or [])
    )


def _command_uses_database_selector(command, variable_id):
    try:
        if int(command.get("code", -1)) != 250:
            return False
        selectors = command.get("intArgs") or []
        return any(int(value) == variable_id for value in selectors[:3])
    except (TypeError, ValueError):
        return False


def _analyze_json_usage(json_root):
    usage = {
        "display_database_fields": set(),
        "logic_database_fields": set(),
        "logic_database_literals": set(),
        "logic_database_record_literals": set(),
        "comparison_literals": set(),
        "display_command_literals": set(),
        "logic_command_literals": set(),
    }
    schemas = {}
    databases = {}
    for database_name in _DATABASE_JSON_BY_KIND.values():
        path = os.path.join(json_root, "databases", database_name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as source:
                database = json.load(source)
        except (OSError, ValueError) as error:
            raise WolfEngineError(f"无法分析 WOLF 数据库结构: {database_name}: {error}") from error
        databases[database_name] = database
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
    sequences = []
    for root, _, files in os.walk(json_root):
        parent = os.path.basename(root).lower()
        if parent not in ("common", "maps"):
            continue
        for filename in files:
            if not filename.lower().endswith(".json"):
                continue
            try:
                with open(os.path.join(root, filename), "r", encoding="utf-8") as source:
                    data = json.load(source)
            except (OSError, ValueError) as error:
                raise WolfEngineError(f"无法分析 WOLF JSON 用途: {filename}: {error}") from error
            if not isinstance(data, dict):
                continue
            if parent == "common":
                sequences.append(data.get("commands", []))
            else:
                for event in data.get("events", []):
                    for page in event.get("pages", []):
                        sequences.append(page.get("list", []))

    for commands in sequences:
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
                try:
                    strings = command.get("stringArgs") or []
                    datum_name = strings[2].strip() if isinstance(strings[2], str) else ""
                except IndexError:
                    continue
                if datum_name:
                    usage["logic_database_record_literals"].add(datum_name.casefold())

    for commands in sequences:
        for index, command in enumerate(commands):
            read = _database_read(command, schemas)
            if read is None:
                continue
            key, destination, token = read
            display_use = False
            logic_use = False
            # ponytail: Follow one local string variable until overwrite. Cross-event
            # aliases remain protected; a command CFG is the upgrade path.
            for following in commands[index + 1:]:
                strings = following.get("stringArgs") or []
                uses_token = any(isinstance(text, str) and token in text for text in strings)
                try:
                    following_code = int(following.get("code", -1))
                except (TypeError, ValueError):
                    following_code = -1
                if _command_uses_database_selector(following, destination):
                    logic_use = True
                elif uses_token:
                    if following_code in (112, 140, 213):
                        logic_use = True
                    elif following_code == 300:
                        if strings and token in str(strings[0]):
                            logic_use = True
                        elif strings and _common_event_displays_text(strings[0]):
                            display_use = True
                        else:
                            logic_use = True
                    elif following_code in (101, 102, 122, 150):
                        display_use = True
                if _command_overwrites_string(following, destination, token):
                    break
            if logic_use:
                usage["logic_database_fields"].add(key)
            elif display_use:
                usage["display_database_fields"].add(key)

    for commands in sequences:
        for index, command in enumerate(commands):
            try:
                code = int(command.get("code", -1))
                int_args = command.get("intArgs") or []
                strings = command.get("stringArgs") or []
                destination = int(int_args[0])
            except (TypeError, ValueError, IndexError):
                continue
            if code != 122 or not strings:
                continue
            token = _string_variable_token(destination)
            if token is None:
                continue
            display_use = False
            logic_use = False
            for following in commands[index + 1:]:
                following_strings = following.get("stringArgs") or []
                uses_token = any(
                    isinstance(text, str) and token in text
                    for text in following_strings
                )
                try:
                    following_code = int(following.get("code", -1))
                except (TypeError, ValueError):
                    following_code = -1
                if uses_token:
                    if following_code in (112, 140, 213):
                        logic_use = True
                    elif following_code in (101, 102, 150):
                        display_use = True
                    elif following_code == 300:
                        if following_strings and token in str(following_strings[0]):
                            logic_use = True
                        elif following_strings and _common_event_displays_text(following_strings[0]):
                            display_use = True
                        else:
                            logic_use = True
                if _command_overwrites_string(following, destination, token):
                    break
            if display_use:
                usage["display_command_literals"].update(
                    text for text in strings if isinstance(text, str) and text.strip()
                )
            if logic_use and not display_use:
                usage["logic_command_literals"].update(
                    text for text in strings if isinstance(text, str) and text.strip()
                )
    usage["logic_command_literals"].update(usage["comparison_literals"])
    usage["display_database_fields"] -= usage["logic_database_fields"]
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
    usage["display_database_fields"] -= usage["logic_database_fields"]
    for database_name, database in databases.items():
        for type_index, type_data in enumerate(database.get("types", [])):
            for datum in type_data.get("data", []):
                for field_index, field in enumerate(datum.get("data", [])):
                    value = field.get("value")
                    is_logic = (
                        _uses_first_string_database_id(datum, field_index)
                        or (database_name, type_index, field_index) in usage["logic_database_fields"]
                        or _is_explicit_internal_database_text(value)
                        or _database_field_marker(field.get("name")) == "WOLFLogic"
                    )
                    if is_logic and isinstance(value, str) and value.strip():
                        usage["logic_database_literals"].add(value.strip().casefold())
    return usage


def _database_value_marker(database_name, type_index, field_index, field, usage, type_name=""):
    value = field.get("value")
    normalized_value = value.strip().casefold() if isinstance(value, str) else ""
    explicit_internal = _is_explicit_internal_database_text(value)
    marker = (
        "WOLFLogic"
        if explicit_internal or _DEVELOPMENT_NAME_RE.search(str(type_name or ""))
        else _database_field_marker(field.get("name"))
    )
    if usage:
        key = (database_name, type_index, field_index)
        if (
            key in usage["logic_database_fields"]
            or value in usage["comparison_literals"]
            or (
                normalized_value
                and (
                    normalized_value in usage.get("logic_database_record_literals", set())
                    or normalized_value in usage.get("logic_database_literals", set())
                )
            )
        ):
            return "WOLFLogic"
        if marker == "WOLFLogic" and not explicit_internal and key in usage["display_database_fields"]:
            return "WOLFText"
    return marker


def _uses_first_string_database_id(datum, field_index):
    if field_index != 0 or str(datum.get("name") or "").strip():
        return False
    fields = datum.get("data") or []
    return bool(
        fields
        and isinstance(fields[0].get("value"), str)
        and fields[0]["value"].strip()
    )


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
            _command_entries(command, ["commands", command_index], entries, json_rel, usage)
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
            for data_index, datum in enumerate(type_data.get("data", [])):
                fields = datum.get("data", [])
                field_markers = []
                for field_index, field in enumerate(fields):
                    field_markers.append(
                        "WOLFLogic"
                        if _uses_first_string_database_id(datum, field_index)
                        else _database_value_marker(
                            database_name,
                            type_index,
                            field_index,
                            field,
                            usage,
                            type_data.get("name"),
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
                    _DEVELOPMENT_NAME_RE.search(str(type_data.get("name") or ""))
                    or _is_explicit_internal_database_text(datum_name)
                    or datum_name in logic_values
                    or (usage and datum_name in usage["comparison_literals"])
                    or (
                        usage
                        and normalized_datum_name
                        and (
                            normalized_datum_name in usage.get("logic_database_record_literals", set())
                            or normalized_datum_name in usage.get("logic_database_literals", set())
                        )
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
                        _add_entry(
                            entries,
                            {
                                "kind": "json",
                                "file": json_rel,
                                "path": path,
                                "marker": field_marker,
                                **({"logic_role": "identifier"} if field_marker == "WOLFLogic" else {}),
                            },
                            value,
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
    called_common_events = set()
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
                        if code != 300:
                            continue
                        string_args = command.get("stringArgs") or []
                        if string_args and isinstance(string_args[0], str) and string_args[0].strip():
                            called_common_events.add(string_args[0].strip().casefold())
                        scene_ids.update(
                            value.strip().casefold()
                            for value in string_args[1:]
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
    return strings, scene_ids, called_common_events


def _referenced_map_json_names(references):
    return {
        f"{match.group(1)}.json".casefold()
        for reference in references
        if isinstance(reference, str)
        for match in _MAP_REFERENCE_RE.finditer(reference.replace("\\", "/"))
    }


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

    for path in (snapshot_path, string_scripts_path, backup_path):
        if os.path.exists(path) and not file_system.safe_remove(path):
            raise WolfEngineError(f"无法清理旧目录: {path}")
    os.makedirs(snapshot_path, exist_ok=True)
    os.makedirs(string_scripts_path, exist_ok=True)

    if message_queue:
        message_queue.put(("log", ("normal", "使用 WolfRPGText 解析 WOLF 二进制数据...")))
    uberwolf.dump_text(data_path, snapshot_path)
    usage = _analyze_json_usage(snapshot_path)
    runtime_references, scene_references, called_common_events = _collect_runtime_references(snapshot_path)
    referenced_maps = _referenced_map_json_names(runtime_references)

    file_count = 0
    entry_count = 0
    for root, _, files in os.walk(snapshot_path):
        for filename in sorted(files):
            if not filename.lower().endswith(".json"):
                continue
            json_path = os.path.join(root, filename)
            common_event_name = re.sub(r"^\d+_", "", os.path.splitext(filename)[0])
            if (
                os.path.basename(root).lower() == "common"
                and _DEVELOPMENT_NAME_RE.search(common_event_name)
                and common_event_name.casefold() not in called_common_events
            ):
                continue
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
                    index += 1
                    continue
                metadata = _decode_metadata(lines[index][6:].strip())
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
    protected_logic_literals = {
        _split_text_format(full_text)[0]
        for _script_rel, metadata, full_text in released_entries
        if metadata.get("marker") == "WOLFLogic"
        and metadata.get("logic_role", "comparison") == "comparison"
    }
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

        translated = result["text"]
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
            elif marker != "WOLFLogic" and source_text not in protected_logic_literals and _CJK_RE.search(source_text):
                warnings.append(f"纯汉字原文未变化: {script_rel}: {source_text[:80]!r}")
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


def _verify_logic_json_unchanged(original_root, patched_root):
    usage = _analyze_json_usage(original_root)
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
    released_entries = list(_iter_released_entries(scripts_path))
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
            if metadata.get("marker") != "WOLFLogic":
                font_texts.append(target_text)
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
                txt_changes.setdefault(rel, []).append((metadata["start"], metadata["end"], restored_text))
            elif kind == "csv":
                csv_changes.setdefault(rel, []).append((metadata["row"], metadata["column"], target_text))
            else:
                raise WolfEngineError(f"未知的 WOLF 文本类型: {kind}")
            applied += 1

        missing_identities = set(origin_entries) - seen_identities
        if missing_identities:
            raise WolfEngineError(f"WOLF StringScripts 缺少 {len(missing_identities)} 个原始条目")

        for path, data in json_cache.items():
            _write_json_atomic(path, data)
        _verify_logic_json_unchanged(snapshot_path, patch_path)

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
