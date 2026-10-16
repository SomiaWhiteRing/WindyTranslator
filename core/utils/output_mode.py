"""RM2000/2003 的全局输出默认值与翻译工程设置。"""
import copy
import json
import os
from pathlib import Path

from core.utils.file_system import write_json_atomic
from core.utils.rpg_rt_ini import SUPPORTED_ENCODINGS
from core.utils.task_control import exclusive_work
from core.utils.text_processing import sanitize_filename


DEFAULT_OUTPUT_SETTINGS = {
    "default_mode": "utf8",
    "legacy_encoding": "936",
    "onboarding_seen": False,
    "upgrade_notice_pending": False,
}

PROJECT_SETTINGS_FILENAME = "project.json"
SOURCE_ENCODINGS = ("auto", *SUPPORTED_ENCODINGS, "65001")


def migrate_settings(raw, existing_installation, old_encoding):
    settings = copy.deepcopy(DEFAULT_OUTPUT_SETTINGS)
    old_encoding = str(old_encoding)
    if old_encoding not in SUPPORTED_ENCODINGS:
        old_encoding = "936"
    if existing_installation:
        settings["legacy_encoding"] = old_encoding
    if not isinstance(raw, dict):
        if existing_installation:
            settings.update(
                onboarding_seen=True,
                upgrade_notice_pending=True,
            )
        return settings
    settings.update({key: raw[key] for key in settings if key in raw})
    if settings["default_mode"] not in ("utf8", "legacy"):
        settings["default_mode"] = "utf8"
    if settings["legacy_encoding"] not in SUPPORTED_ENCODINGS:
        settings["legacy_encoding"] = old_encoding if existing_installation else "936"
    for key in ("onboarding_seen", "upgrade_notice_pending"):
        if not isinstance(settings[key], bool):
            settings[key] = DEFAULT_OUTPUT_SETTINGS[key]
    return settings


def project_key(game_path):
    return os.path.normcase(os.path.realpath(game_path))


def project_settings_path(game_path, works_dir):
    folder = sanitize_filename(Path(game_path).name) or "UntitledGame"
    return Path(works_dir) / folder / PROJECT_SETTINGS_FILENAME


def _read_project_document(path):
    try:
        with path.open("r", encoding="utf-8-sig") as source:
            document = json.load(source)
    except FileNotFoundError:
        return {}
    if not isinstance(document, dict):
        raise ValueError(f"工程配置必须是 JSON 对象：{path}")
    record = document.get("output_settings")
    if record is not None and (not isinstance(record, dict) or record.get("mode") not in ("utf8", "legacy")):
        raise ValueError(f"工程输出设置无效：{path}")
    paths = document.get("game_paths", {})
    if not isinstance(paths, dict) or any(
        not isinstance(value, dict) or value.get("source_encoding") not in SOURCE_ENCODINGS
        for value in paths.values()
    ):
        raise ValueError(f"工程路径编码记录无效：{path}")
    return document


def _source_encoding(config, record):
    encoding = record.get("source_encoding")
    if encoding not in SOURCE_ENCODINGS:
        encoding = str(config.get("pro_mode_settings", {}).get("export_source_encoding", "auto"))
    return encoding if encoding in SOURCE_ENCODINGS else "auto"


def get_profile(config, game_path, works_dir):
    path = project_settings_path(game_path, works_dir)
    document = _read_project_document(path)
    return _profile_from_document(config, game_path, path, document)


def _profile_from_document(config, game_path, path, document):
    settings = config["output_settings"]
    record = document.get("output_settings")
    path_record = document.get("game_paths", {}).get(project_key(game_path))
    needs_source_encoding = path_record is None and (record is not None or path.parent.is_dir())
    existing = (
        bool(record.get("legacy_project", False)) if record is not None
        else path.parent.is_dir()
    )
    if record is not None:
        mode = record["mode"]
        legacy_encoding = str(record.get("legacy_encoding", settings["legacy_encoding"]))
    else:
        mode = "legacy" if existing else settings["default_mode"]
        legacy_encoding = settings["legacy_encoding"]
    if legacy_encoding not in SUPPORTED_ENCODINGS:
        legacy_encoding = settings["legacy_encoding"]
    return {
        "mode": mode,
        "legacy_encoding": legacy_encoding,
        "target_encoding": "65001" if mode == "utf8" else legacy_encoding,
        "existing_work": existing,
        # 旧版未绑定路径的 source_encoding 不能套用到未知副本。
        "source_encoding": "auto" if needs_source_encoding else _source_encoding(config, path_record or {}),
        "recorded": record is not None and path_record is not None,
        "needs_source_encoding": needs_source_encoding,
    }


def ensure_project(config, game_path, works_dir, resolve_source_encoding):
    """只为未登记路径补记录；检测/询问仅用于已经存在的工程。"""
    profile = get_profile(config, game_path, works_dir)
    if profile["recorded"]:
        return profile
    with exclusive_work(game_path, works_dir):
        path = project_settings_path(game_path, works_dir)
        document = _read_project_document(path)
        profile = _profile_from_document(config, game_path, path, document)
        if not profile["recorded"]:
            source_encoding = profile["source_encoding"]
            if profile["needs_source_encoding"]:
                source_encoding = resolve_source_encoding(game_path)
                if source_encoding is None:
                    return None
            _save_project_document(config, game_path, path, document, source_encoding=source_encoding)
            profile = _profile_from_document(config, game_path, path, document)
        return profile


def detect_unrecorded_encoding(game_path):
    """未知路径结合 INI 声明和现有检测；冲突交由用户指定。"""
    from core.external import easyrpg
    from core.utils import rpg_rt_ini

    ini_path = Path(game_path) / "RPG_RT.ini"
    hint = rpg_rt_ini.read_game_encoding_hint(ini_path) if ini_path.is_file() else None
    try:
        detected = easyrpg.detect_game_encoding(game_path)
    except (easyrpg.EncodingDetectionError, OSError, UnicodeError):
        if hint is not None:
            return hint
        raise
    if hint is not None and hint != detected:
        raise easyrpg.EncodingDetectionError(
            f"RPG_RT.ini 标记为 {hint}，游戏数据检测结果为 {detected}，请指定当前数据编码。"
        )
    return detected


def save_project_locked(config, game_path, works_dir, mode=None, legacy_encoding=None, *,
                        make_default=False, source_encoding=None):
    """调用方须持有 exclusive_work；只修改传入字段，返回原文档用于锁内回滚。"""
    path = project_settings_path(game_path, works_dir)
    document = _read_project_document(path)
    return _save_project_document(
        config, game_path, path, document, mode, legacy_encoding,
        make_default=make_default, source_encoding=source_encoding,
    )


def _save_project_document(config, game_path, path, document, mode=None, legacy_encoding=None, *,
                           make_default=False, source_encoding=None):
    """在调用方持有的作品锁内更新已读取的文档，保留原文档用于回滚。"""
    profile = _profile_from_document(config, game_path, path, document)
    mode = profile["mode"] if mode is None else mode
    legacy_encoding = profile["legacy_encoding"] if legacy_encoding is None else legacy_encoding
    if mode not in ("utf8", "legacy") or legacy_encoding not in SUPPORTED_ENCODINGS:
        raise ValueError("无效的输出方式或传统目标编码")
    settings = config["output_settings"]
    previous_document = document.copy()
    previous = document.get("output_settings")
    record = dict(previous) if previous is not None else {}
    if previous is None:
        record["legacy_project"] = path.parent.is_dir()
    record.update({
        "mode": mode,
        "legacy_encoding": legacy_encoding,
    })
    paths = dict(document.get("game_paths", {}))
    key = project_key(game_path)
    path_record = dict(paths.get(key, {}))
    if source_encoding is None and not path_record:
        if profile["needs_source_encoding"]:
            raise ValueError("当前路径尚未确认数据编码")
        source_encoding = profile["source_encoding"]
    if source_encoding is not None and source_encoding not in SOURCE_ENCODINGS:
        raise ValueError("无效的源数据编码")
    if source_encoding is not None:
        path_record["source_encoding"] = source_encoding
    paths[key] = path_record
    record.pop("source_encoding", None)
    document["output_settings"] = record
    document["game_paths"] = paths
    write_json_atomic(path, document)
    if make_default:
        settings["default_mode"] = mode
        settings["legacy_encoding"] = legacy_encoding
    return previous_document


def record_import_encoding_locked(game_path, works_dir, target_encoding):
    """在作品锁内记录本次已成功写入的编码，保留输出选项和其他工程字段。"""
    if target_encoding not in (*SUPPORTED_ENCODINGS, "65001"):
        raise ValueError("无效的实际写入编码")
    path = project_settings_path(game_path, works_dir)
    document = _read_project_document(path)
    record = document.get("game_paths", {}).get(project_key(game_path))
    if record is None:
        raise ValueError(f"工程输出设置尚未初始化：{path}")
    record["source_encoding"] = target_encoding
    write_json_atomic(path, document)
