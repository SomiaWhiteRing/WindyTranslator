import csv
import json
import os
import shutil
import tempfile
import queue

from core.engines import wolf
from core.tasks import initialize, json_creation, json_release
from core.utils.engine_detection import detect_game_engine


def test_wolf_csv_sniffer_does_not_treat_cr_as_a_delimiter():
    content = "名称,x,y,背景画像ファイル,,\r\n1,2,3,Picture/base.png,\r\n"

    dialect = wolf._sniff_csv(content)
    rows = list(csv.reader(content.splitlines(keepends=True), dialect=dialect))

    assert dialect.delimiter == ","
    assert rows[0][3] == "背景画像ファイル"
    assert rows[1][3] == "Picture/base.png"


def test_wolf_detection_and_string_script_roundtrip():
    with tempfile.TemporaryDirectory() as root:
        open(os.path.join(root, "Game.exe"), "wb").close()
        open(os.path.join(root, "Data.wolf"), "wb").close()
        detected = detect_game_engine(root)
        assert detected is not None and detected.engine == "wolf"

        scripts = os.path.join(root, "StringScripts")
        source_text = "第一行\r\n第二行\r\n\r\n"
        entries = []
        wolf._add_entry(
            entries,
            {"kind": "json", "file": "game/Game.json", "path": ["Title"], "marker": "WOLFText"},
            source_text,
        )
        wolf._write_string_script(os.path.join(scripts, "Game.txt"), entries)

        restored = list(wolf._read_released_entries(scripts))
        assert len(restored) == 1
        metadata, text = restored[0]
        assert metadata["path"] == ["Title"]
        assert metadata["marker"] == "WOLFText"
        assert text == source_text

        try:
            wolf._safe_join(root, "../outside")
        except wolf.WolfEngineError:
            pass
        else:
            raise AssertionError("WOLF metadata traversal must be rejected")


def test_wolf_detection_accepts_split_basic_data_archive(tmp_path):
    (tmp_path / "Game.exe").write_bytes(b"")
    data_path = tmp_path / "Data"
    data_path.mkdir()
    (data_path / "BasicData.wolf").write_bytes(b"archive")

    detected = detect_game_engine(str(tmp_path))

    assert detected is not None and detected.engine == "wolf"
    assert "BasicData.wolf" in detected.reason


def test_wolf_split_archive_inputs_keep_loose_content(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    data_path = source / "Data"
    (data_path / "BasicData").mkdir(parents=True)
    (data_path / "BasicData" / "Game.dat").write_bytes(b"unpacked")
    (data_path / "BasicData.wolf").write_bytes(b"archive")
    (data_path / "LooseArt").mkdir()
    (data_path / "LooseArt" / "name.txt").write_text("loose", encoding="utf-8")
    (data_path / "font.ttf").write_bytes(b"font")
    destination.mkdir()

    wolf._copy_archive_inputs(str(source), str(destination), "split")

    assert not (destination / "Data" / "BasicData").exists()
    assert (destination / "Data" / "BasicData.wolf").read_bytes() == b"archive"
    assert (destination / "Data" / "LooseArt" / "name.txt").read_text(encoding="utf-8") == "loose"
    assert (destination / "Data" / "font.ttf").read_bytes() == b"font"


def test_font_revision_context_is_unavailable_before_initialize(tmp_path):
    (tmp_path / "Game.exe").write_bytes(b"")
    (tmp_path / "Data.wolf").write_bytes(b"")

    assert wolf.try_get_font_revision_context(str(tmp_path)) is None


def test_wolf_initialize_requests_font_context_refresh(tmp_path, monkeypatch):
    messages = queue.Queue()
    monkeypatch.setattr(
        initialize,
        "detect_game_engine",
        lambda _path: type("Detected", (), {"engine": "wolf"})(),
    )
    monkeypatch.setattr(
        wolf,
        "initialize_game",
        lambda _path, _messages: {"pack_mode": "v3"},
    )

    initialize.run_initialize(str(tmp_path), {}, messages)

    queued = list(messages.queue)
    assert ("wolf_initialized", str(tmp_path)) in queued
    assert queued.index(("wolf_initialized", str(tmp_path))) < queued.index(("done", None))


def test_wolf_command_roles_keep_logic_and_skip_identifier_parameters():
    entries = []
    wolf._command_entries(
        {"code": 112, "stringArgs": ["ウソツキ"]},
        ["commands", 0],
        entries,
        "common/Common.json",
    )
    wolf._command_entries(
        {"code": 300, "stringArgs": ["internal_id", "画面に表示します。", "short_id"]},
        ["commands", 1],
        entries,
        "common/Common.json",
    )

    assert [(metadata["marker"], text) for metadata, text in entries] == [
        ("WOLFLogic", "ウソツキ"),
        ("WOLFText", "画面に表示します。"),
    ]


def test_wolf_display_common_event_includes_short_ui_parameters():
    entries = []
    wolf._command_entries(
        {"code": 300, "stringArgs": ["■Tipsメッセージ", "内部", "安全な空間"]},
        ["commands", 0],
        entries,
        "common/Common.json",
    )

    assert [(metadata["path"][-1], text) for metadata, text in entries] == [
        (1, "内部"),
        (2, "安全な空間"),
    ]

    entries = []
    wolf._command_entries(
        {"code": 300, "stringArgs": ["■エネミーを見る", "† おまけ †"]},
        ["commands", 1],
        entries,
        "common/Common.json",
    )
    assert [text for _metadata, text in entries] == ["† おまけ †"]


def test_wolf_database_separates_identifiers_from_display_text(tmp_path):
    json_root = tmp_path / "json"
    json_path = json_root / "databases" / "DataBase.json"
    json_path.parent.mkdir(parents=True)
    json_path.write_text(
        json.dumps({
            "types": [{
                "data": [
                    {
                        "name": "鉄の斧",
                        "data": [{"name": "説明文", "value": "重い斧です。"}],
                    },
                    {
                        "name": "ソフィア",
                        "data": [
                            {"name": "識別名", "value": "ソフィア"},
                            {"name": "表示名", "value": "ソフィア"},
                        ],
                    },
                ]
            }]
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    entries = wolf._json_entries(str(json_root), str(json_path))
    roles = [(metadata["path"], metadata["marker"], text) for metadata, text in entries]

    assert (["types", 0, "data", 0, "name"], "WOLFText", "鉄の斧") in roles
    assert (["types", 0, "data", 1, "name"], "WOLFLogic", "ソフィア") in roles
    assert (["types", 0, "data", 1, "data", 0, "value"], "WOLFLogic", "ソフィア") in roles
    assert (["types", 0, "data", 1, "data", 1, "value"], "WOLFText", "ソフィア") in roles

    scripts = tmp_path / "StringScripts"
    assert wolf._write_json_entry_groups(str(scripts), "databases/DataBase.json.txt", entries) == 2
    assert (scripts / "WOLF" / "Binary" / "databases" / "DataBase.json.txt").is_file()
    assert (scripts / "WOLF" / "Logic" / "databases" / "DataBase.json.txt").is_file()


def test_wolf_database_identifier_roles_follow_runtime_usage(tmp_path):
    json_root = tmp_path / "json"
    database_path = json_root / "databases" / "DataBase.json"
    common_path = json_root / "common" / "Common.json"
    database_path.parent.mkdir(parents=True)
    common_path.parent.mkdir(parents=True)
    database_path.write_text(
        json.dumps({
            "types": [
                {
                    "data": [
                        {
                            "name": "鉄の斧",
                            "data": [{"name": "識別名", "value": "鉄の斧"}],
                        },
                        {
                            "name": "ソフィア",
                            "data": [{"name": "識別名", "value": "ソフィア"}],
                        },
                    ]
                },
                {
                    "data": [{
                        "name": "内部キー",
                        "data": [{"name": "識別名", "value": "内部キー"}],
                    }]
                },
                {
                    "data": [{
                        "name": "未使用キー",
                        "data": [{"name": "識別名", "value": "未使用キー"}],
                    }]
                },
            ]
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    common_path.write_text(
        json.dumps({
            "commands": [
                {
                    "code": 250,
                    "codeStr": "Database",
                    "stringArgs": ["", "装備", "", "識別名"],
                    "intArgs": [0, 0, 0, 0x51200, 3000001],
                },
                {
                    "code": 122,
                    "codeStr": "SetString",
                    "stringArgs": [r"装備：\s[1]"],
                    "intArgs": [3000000, 0, 0],
                },
                {
                    "code": 250,
                    "codeStr": "Database",
                    "stringArgs": ["", "ルビ", "", "識別名"],
                    "intArgs": [1, 0, 0, 0x51200, 1600006],
                },
                {
                    "code": 112,
                    "codeStr": "StringCondition",
                    "stringArgs": [r"\cself[6]", "ソフィア"],
                    "intArgs": [1, 0],
                },
                {
                    "code": 122,
                    "codeStr": "SetString",
                    "stringArgs": ["ソフィア"],
                    "intArgs": [3000000, 0, 0],
                },
            ]
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    usage = wolf._analyze_json_usage(str(json_root))
    entries = wolf._json_entries(str(json_root), str(database_path), usage)
    roles = {
        (tuple(metadata["path"]), text): metadata["marker"]
        for metadata, text in entries
    }

    assert ("database.json", 0, 0) in usage["display_database_fields"]
    assert ("database.json", 1, 0) in usage["logic_database_fields"]
    assert roles[(("types", 0, "data", 0, "name"), "鉄の斧")] == "WOLFText"
    assert roles[(("types", 0, "data", 0, "data", 0, "value"), "鉄の斧")] == "WOLFText"
    assert roles[(("types", 0, "data", 1, "name"), "ソフィア")] == "WOLFLogic"
    assert roles[(("types", 1, "data", 0, "data", 0, "value"), "内部キー")] == "WOLFLogic"
    assert roles[(("types", 2, "data", 0, "data", 0, "value"), "未使用キー")] == "WOLFLogic"
    common_entries = wolf._json_entries(str(json_root), str(common_path), usage)
    assert any(
        metadata["marker"] == "WOLFLogic" and text == "ソフィア"
        for metadata, text in common_entries
        if metadata["path"] == ["commands", 4, "stringArgs", 0]
    )


def test_wolf_database_record_name_used_cross_database_by_code250_stays_logic(tmp_path):
    json_root = tmp_path / "json"
    database_path = json_root / "databases" / "DataBase.json"
    common_path = json_root / "common" / "Common.json"
    c_database_path = json_root / "databases" / "CDataBase.json"
    database_path.parent.mkdir(parents=True)
    common_path.parent.mkdir(parents=True)
    database_path.write_text(
        json.dumps({
            "types": [{
                "name": "EDリスト",
                "data": [
                    {"name": "Ayaは元気？", "data": []},
                    {
                        "name": "",
                        "data": [{"name": "名称", "value": "Ayaは元気？"}],
                    },
                ],
            }]
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    c_database_path.write_text(
        json.dumps({
            "types": [{
                "name": "クリア状況",
                "data": [{"name": "", "data": []}],
            }]
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    common_path.write_text(
        json.dumps({
            "commands": [{
                "code": 250,
                "stringArgs": ["", "クリア状況", "Ayaは元気？", "0=まだ/1=済"],
                "intArgs": [9, 0, 0, 0x51200, 3000000],
            }]
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    usage = wolf._analyze_json_usage(str(json_root))
    entries = wolf._json_entries(str(json_root), str(database_path), usage)

    assert "Ayaは元気？".casefold() in usage["logic_database_record_literals"]
    assert any(
        metadata["marker"] == "WOLFLogic" and text == "Ayaは元気？"
        for metadata, text in entries
    )
    assert any(
        metadata["path"] == ["types", 0, "data", 1, "data", 0, "value"]
        and metadata["marker"] == "WOLFLogic"
        and text == "Ayaは元気？"
        for metadata, text in entries
    )


def test_wolf_resource_detection_handles_multiline_parameters():
    assert wolf._looks_like_resource("1枚絵マップ/汽車.png\n0\n0\n0") is True
    assert wolf._looks_like_resource("Data/textfile/01_scenario.md") is True


def test_wolf_internal_memo_fields_stay_protected_unless_displayed():
    field = {"name": "【メモ】", "value": "内部調整値"}
    usage = {
        "display_database_fields": set(),
        "logic_database_fields": set(),
        "comparison_literals": set(),
    }

    assert wolf._database_value_marker("database.json", 2, 3, field, usage) == "WOLFLogic"
    usage["display_database_fields"].add(("database.json", 2, 3))
    assert wolf._database_value_marker("database.json", 2, 3, field, usage) == "WOLFText"
    assert wolf._database_field_marker("簡単な説明（作中には使わない）") == "WOLFLogic"
    assert wolf._database_field_marker("Runコモン") == "WOLFLogic"
    assert wolf._database_field_marker("名称") == "WOLFText"
    assert wolf._database_value_marker(
        "database.json", 2, 4, {"name": "説明", "value": "旧説明"}, usage, "武装一覧bk"
    ) == "WOLFLogic"
    assert wolf._database_value_marker(
        "database.json", 2, 5, {"name": "名前", "value": "# 未使用区切り"}, usage
    ) == "WOLFLogic"
    assert wolf._DEVELOPMENT_NAME_RE.search("bk└ 制御子")

    name_field = {"name": "名称", "value": "画面表示名"}
    display_usage = dict(usage)
    display_usage["display_database_fields"] = {("database.json", 4, 0)}
    assert wolf._database_value_marker(
        "database.json", 4, 0, name_field, display_usage
    ) == "WOLFText"


def test_wolf_string_assignment_is_exposed_only_when_locally_displayed(tmp_path):
    common = tmp_path / "common"
    common.mkdir()
    path = common / "Flow.json"
    path.write_text(
        json.dumps({
            "commands": [
                {"code": 122, "intArgs": [1600000], "stringArgs": [r"^.*?END$"]},
                {"code": 122, "intArgs": [1600001], "stringArgs": [r"\cself[0]"]},
                {"code": 122, "intArgs": [1600002], "stringArgs": ["画面に表示する"]},
                {"code": 150, "intArgs": [32], "stringArgs": [r"\cself[2]"]},
                {
                    "code": 250,
                    "intArgs": [0, 0, 0, 0, 1600003],
                    "stringArgs": ["", "◆武装", "サーマルガード", "名前"],
                },
            ]
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    usage = wolf._analyze_json_usage(str(tmp_path))
    entries = wolf._json_entries(str(tmp_path), str(path), usage)
    roles = {text: metadata["marker"] for metadata, text in entries}

    assert roles[r"^.*?END$"] == "WOLFLogic"
    assert roles["画面に表示する"] == "Message"
    assert wolf._looks_like_logic_assignment(r"Save\エンドリスト.sav") is True
    assert wolf._database_value_marker(
        "database.json",
        0,
        0,
        {"name": "名前", "value": "サーマルガード"},
        usage,
    ) == "WOLFLogic"


def test_wolf_string_assignment_keeps_runtime_identifiers_as_logic():
    entries = []
    literals = [
        "◆[rb]メンテナンスManager",
        "◆[rb]操作盤3",
        "変数5",
        r"\f[12]_dbgids={ブソウ\cself[74]}",
    ]
    for index, literal in enumerate(literals):
        wolf._command_entries(
            {"code": 122, "stringArgs": [literal]},
            ["commands", index],
            entries,
            "common/Common.json",
            {"logic_command_literals": set()},
        )

    assert [(metadata["marker"], text) for metadata, text in entries] == [
        ("WOLFLogic", literal) for literal in literals
    ]


def test_wolf_database_value_used_as_dynamic_common_target_stays_logic(tmp_path):
    databases = tmp_path / "databases"
    common = tmp_path / "common"
    databases.mkdir()
    common.mkdir()
    (databases / "DataBase.json").write_text(
        json.dumps({
            "types": [{
                "name": "Calls",
                "data": [{"name": "", "data": [{"name": "Runコモン", "value": "Target"}]}],
            }]
        }),
        encoding="utf-8",
    )
    (common / "Caller.json").write_text(
        json.dumps({
            "commands": [
                {
                    "code": 250,
                    "intArgs": [0, 0, 0, 0x51200, 1600000],
                    "stringArgs": ["", "Calls", "", "Runコモン"],
                },
                {"code": 300, "stringArgs": [r"\cself[0]"]},
            ]
        }),
        encoding="utf-8",
    )

    usage = wolf._analyze_json_usage(str(tmp_path))

    assert ("database.json", 0, 0) in usage["logic_database_fields"]


def test_wolf_transport_residue_scan_crosses_chunk_boundary(tmp_path):
    data_path = tmp_path / "Data"
    target = data_path / "MapData" / "Map001.mps"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"abcde" + wolf._WOLF_TRANSPORT_TAG_PREFIX.encode("ascii") + b"tail")

    assert wolf._transport_residue_files(str(data_path), chunk_size=8) == [
        os.path.join("MapData", "Map001.mps")
    ]


def test_wolf_archive_is_rolled_back_when_data_commit_fails(tmp_path, monkeypatch):
    destination = tmp_path / "Data.wolf"
    data_path = tmp_path / "Data"
    staging = tmp_path / "staging"
    session_backup = tmp_path / "before.wolf"
    destination.write_bytes(b"old archive")
    data_path.mkdir()
    staging.mkdir()
    monkeypatch.setattr(
        wolf,
        "_replace_data_directory",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("locked")),
    )

    try:
        wolf._replace_data_and_manifest(
            str(data_path),
            str(staging),
            root_archive=str(destination),
            archive_session_backup=str(session_backup),
        )
    except PermissionError:
        pass
    else:
        raise AssertionError("data commit failure must propagate")

    assert destination.read_bytes() == b"old archive"


def test_wolf_manifest_failure_rolls_back_archive_and_data(tmp_path, monkeypatch):
    destination = tmp_path / "Data.wolf"
    data_path = tmp_path / "Data"
    staging = tmp_path / "staging"
    session_backup = tmp_path / "before.wolf"
    manifest_path = tmp_path / "state" / "manifest.json"
    destination.write_bytes(b"old archive")
    data_path.mkdir()
    staging.mkdir()
    (data_path / "value.txt").write_text("old data", encoding="utf-8")
    (staging / "value.txt").write_text("new data", encoding="utf-8")
    wolf._write_json_atomic(str(manifest_path), {"version": "old"})
    real_replace = wolf.os.replace

    def fail_manifest_publish(source, destination_path):
        if source == f"{manifest_path}.tmp" and destination_path == str(manifest_path):
            raise PermissionError("manifest locked")
        return real_replace(source, destination_path)

    monkeypatch.setattr(wolf.os, "replace", fail_manifest_publish)

    try:
        wolf._replace_data_and_manifest(
            str(data_path),
            str(staging),
            str(manifest_path),
            {"version": "new"},
            str(destination),
            str(session_backup),
        )
    except PermissionError:
        pass
    else:
        raise AssertionError("manifest failure must propagate")

    assert destination.read_bytes() == b"old archive"
    assert (data_path / "value.txt").read_text(encoding="utf-8") == "old data"
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == {"version": "old"}


def test_wolf_loose_commit_replaces_data_and_manifest(tmp_path):
    data_path = tmp_path / "Data"
    staging = tmp_path / "staging"
    manifest_path = tmp_path / "state" / "manifest.json"
    data_path.mkdir()
    staging.mkdir()
    (data_path / "value.txt").write_text("old data", encoding="utf-8")
    (staging / "value.txt").write_text("new data", encoding="utf-8")
    wolf._write_json_atomic(str(manifest_path), {"version": "old"})

    wolf._replace_data_and_manifest(
        str(data_path),
        str(staging),
        str(manifest_path),
        {"version": "new"},
    )

    assert (data_path / "value.txt").read_text(encoding="utf-8") == "new data"
    assert not (tmp_path / "Data.wolf").exists()
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == {"version": "new"}


def test_wolf_control_transport_preserves_codes_and_exposes_ruby_text():
    metadata, encoded = wolf._encode_wolf_transport(
        {"kind": "json", "marker": "Message"},
        r"\c[12]\r[甘,あま]\r[酸,ず]っぱい思い出",
    )

    tags = [item[0] for item in metadata["wolf_transport"]["tokens"]]
    assert encoded == f"{tags[0]}甘{tags[1]}酸{tags[2]}っぱい思い出"
    translated = f"{tags[0]}酸甜的回忆"
    assert wolf._validate_wolf_transport(encoded, translated, metadata) == (True, "")
    assert wolf._decode_wolf_transport(translated, metadata) == r"\c[12]酸甜的回忆"
    assert wolf._decode_wolf_transport(encoded, metadata) == r"\c[12]\r[甘,あま]\r[酸,ず]っぱい思い出"

    valid, reason = wolf._validate_wolf_transport(
        encoded,
        f"{tags[0]}酸甜的回忆{tags[1][:-1]}",
        metadata,
    )
    assert valid is False
    assert "破损片段" in reason

    valid, reason = wolf._validate_wolf_transport(
        encoded,
        "酸甜的回忆",
        metadata,
    )
    assert valid is False
    assert "必需控制码标签缺失" in reason

    emoticon_metadata, emoticon_text = wolf._encode_wolf_transport(
        {"kind": "json", "marker": "Message"},
        "笑顔（ゝω・）",
    )
    assert "ゝ" not in emoticon_text
    assert wolf._decode_wolf_transport(emoticon_text, emoticon_metadata) == "笑顔（ゝω・）"


def test_wolf_optional_transport_tags_come_from_export_metadata(tmp_path):
    origin = tmp_path / wolf.STRING_SCRIPTS_ORIGIN_DIRNAME
    script = origin / "sample.txt"
    wolf._write_string_script(
        str(script),
        [({"kind": "json", "marker": "Message"}, r"\c[1]\r[風,かぜ]が吹く")],
    )
    _script_rel, metadata, _text = next(wolf._iter_released_entries(str(origin)))
    tokens = metadata["wolf_transport"]["tokens"]

    assert wolf.get_optional_transport_tags(str(tmp_path)) == (tokens[1][0],)
    assert tokens[0][0] not in wolf.get_optional_transport_tags(str(tmp_path))


def test_wolf_control_transport_protects_message_structure():
    source = "@43\n\n<C>一行目\n\u3000二行目"
    metadata, encoded = wolf._encode_wolf_transport(
        {"kind": "json", "marker": "Message"},
        source,
    )
    tokens = metadata["wolf_transport"]["tokens"]
    tags_by_kind = {}
    for item in tokens:
        tags_by_kind.setdefault(item[4], []).append(item[0])

    assert "\n" not in encoded
    assert "@43" not in encoded
    assert "<C>" not in encoded
    translated = (
        tags_by_kind["directive"][0]
        + tags_by_kind["newline"][0]
        + tags_by_kind["newline"][1]
        + tags_by_kind["alignment"][0]
        + "第一行"
        + tags_by_kind["newline"][2]
        + "\u3000第二行"
    )
    assert wolf._validate_wolf_transport(encoded, translated, metadata) == (True, "")
    assert wolf._decode_wolf_transport(translated, metadata) == "@43\n\n<C>第一行\n\u3000第二行"

    valid, reason = wolf._validate_wolf_transport(encoded, translated + "\n", metadata)
    assert valid is False
    assert "标签之外的换行" in reason

    assert wolf.validate_translation_transport(encoded, translated) == (True, "")
    valid, reason = wolf.validate_translation_transport(encoded, translated.replace(tags_by_kind["newline"][1], ""))
    assert valid is False
    assert "标签序列不一致" in reason


def test_wolf_export_discovers_runtime_resource_directory_without_dev_exports(tmp_path, monkeypatch):
    game_path = tmp_path / "Game"
    data_path = game_path / "Data"
    basic_data = data_path / "BasicData"
    contents = data_path / "contents"
    work_temp = data_path / "work_temp"
    basic_data.mkdir(parents=True)
    contents.mkdir()
    work_temp.mkdir()
    (basic_data / "Game.dat").write_bytes(b"game")
    (basic_data / "CommonEvent.dat").write_bytes(b"common")
    (contents / "page1.txt").write_text("一ページ目", encoding="utf-8")
    (contents / "page2.txt").write_text("二ページ目", encoding="utf-8")
    (work_temp / "database.txt").write_text("開発用の巨大な書き出し", encoding="utf-8")

    def fake_dump_text(_data_path, snapshot_path):
        common_path = os.path.join(snapshot_path, "common")
        os.makedirs(common_path)
        with open(os.path.join(common_path, "Reader.json"), "w", encoding="utf-8") as output:
            json.dump(
                {
                    "commands": [{
                        "code": 122,
                        "stringArgs": [r"Data/contents/page\cself[0].txt"],
                    }]
                },
                output,
                ensure_ascii=False,
            )

    monkeypatch.setattr(wolf.uberwolf, "dump_text", fake_dump_text)

    file_count, entry_count = wolf.export_to_string_scripts(str(game_path))

    resources = game_path / wolf.STRING_SCRIPTS_DIRNAME / "WOLF" / "Resources"
    assert file_count == 2
    assert entry_count == 2
    assert (resources / "contents" / "page1.txt.strings.txt").is_file()
    assert (resources / "contents" / "page2.txt.strings.txt").is_file()
    assert not (resources / "work_temp").exists()


def test_wolf_referenced_maps_exclude_unregistered_sample_maps():
    references = ["MapData/TitleMap.mps", "MapData\\Field01.mps\r\n100"]

    assert wolf._referenced_map_json_names(references) == {"titlemap.json", "field01.json"}


def test_wolf_logic_files_stay_out_of_translation_json(tmp_path):
    game_path = tmp_path / "Game"
    scripts = game_path / wolf.STRING_SCRIPTS_DIRNAME / "WOLF"
    display_entries = []
    logic_entries = []
    wolf._add_entry(display_entries, {"kind": "json", "file": "a.json", "path": [0]}, "表示文")
    wolf._add_entry(
        logic_entries,
        {"kind": "json", "file": "a.json", "path": [1], "marker": "WOLFLogic"},
        "内部識別子",
    )
    wolf._write_string_script(str(scripts / "Binary" / "a.txt"), display_entries)
    wolf._write_string_script(str(scripts / "Logic" / "a.txt"), logic_entries)

    json_creation.run_create_json(str(game_path), str(tmp_path / "Works"), queue.Queue())
    output = json.loads(
        (tmp_path / "Works" / "Game" / "untranslated" / "translation.json").read_text(encoding="utf-8")
    )

    assert list(output) == [os.path.join("WOLF", "Binary", "a.txt")]


def test_wolf_line_scenario_extracts_only_display_text_and_preserves_prefixes(tmp_path):
    data_path = tmp_path / "Data"
    scenario_path = data_path / "textfile" / "scenario.md"
    scenario_path.parent.mkdir(parents=True)
    scenario_path.write_text(
        "# 仕様書\n"
        "- この説明はゲーム中に表示しない\n"
        "---END_SCENE---\n"
        "---\n"
        "# scene_1\n"
        "# s1\n"
        "@l0101\n"
        ";「一行目\n"
        "　二行目」\n"
        "> 演出メモ\n"
        ":システム文\n"
        "%Common: 1,\n"
        "---END_SCENE---\n",
        encoding="utf-8",
        newline="",
    )

    entries = wolf._txt_entries(str(data_path), str(scenario_path))

    assert [text for _metadata, text in entries] == ["「一行目\n二行目」", "システム文"]
    first_metadata, first_text = entries[0]
    assert first_metadata["line_prefixes"] == [";", "　"]
    assert wolf._restore_line_prefixes(
        wolf._restore_text_format(first_text, first_metadata),
        first_metadata,
    ) == ";「一行目\n　二行目」\n"
    second_metadata, second_text = entries[1]
    assert second_metadata["line_prefixes"] == [":"]
    assert wolf._restore_line_prefixes("系统文本\n", second_metadata) == ":系统文本\n"


def test_wolf_line_scenario_follows_only_runtime_reachable_blocks(tmp_path):
    data_path = tmp_path / "Data"
    scenario_path = data_path / "textfile" / "scenario.md"
    scenario_path.parent.mkdir(parents=True)
    scenario_path.write_text(
        "# start_scene\n"
        ";正篇一\n"
        "%FlowController: next_scene\n"
        "---END_SCENE---\n"
        "# next_scene\n"
        ":正篇二\n"
        "---END_SCENE---\n"
        "# pressure_test\n"
        ";1234567890\n"
        "---END_SCENE---\n",
        encoding="utf-8",
        newline="",
    )

    entries = wolf._txt_entries(str(data_path), str(scenario_path), {"start_scene"})

    assert [text for _metadata, text in entries] == ["正篇一", "正篇二"]


def test_wolf_bullet_scenario_excludes_notes_and_commands(tmp_path):
    data_path = tmp_path / "Data"
    scenario_path = data_path / "ノベル" / "シーン1.txt"
    scenario_path.parent.mkdir(parents=True)
    scenario_path.write_text(
        "制作メモ\n"
        "●0\n"
        "@背景：0\n"
        "---\n"
        "表示する一行目\n"
        "表示する二行目\n"
        "●\n"
        "未使用の構想\n"
        "●1\n"
        "@文章：0\n"
        "次の場面\n",
        encoding="utf-8",
        newline="",
    )

    entries = wolf._txt_entries(str(data_path), str(scenario_path))

    assert [text for _metadata, text in entries] == [
        "表示する一行目\n表示する二行目",
        "次の場面",
    ]


def test_wolf_unknown_command_text_stays_protected(tmp_path):
    data_path = tmp_path / "Data"
    config_path = data_path / "戦闘グラ" / "設定.txt"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("[番号][内容]\n■1 通常攻撃\n1.2.3\n", encoding="utf-8")

    assert wolf._txt_entries(str(data_path), str(config_path)) == []


def test_wolf_line_scenario_roundtrips_through_translation_json(tmp_path):
    data_path = tmp_path / "Data"
    scenario_path = data_path / "scenario.md"
    scenario_path.parent.mkdir(parents=True)
    scenario_path.write_text(
        "# scene_1\n"
        "@l0101\n"
        ";「一行目\n"
        "　二行目」\n"
        "---END_SCENE---\n",
        encoding="utf-8",
        newline="",
    )
    entries = wolf._txt_entries(str(data_path), str(scenario_path))
    scripts = tmp_path / "StringScripts"
    script_path = scripts / "WOLF" / "Resources" / "scenario.md.strings.txt"
    wolf._write_string_script(str(script_path), entries)

    extracted = json_creation._extract_strings_from_file(str(script_path))
    source_text = "「一行目\n二行目」"
    extracted_text = next(iter(extracted))
    metadata, _released_text = next(wolf._read_released_entries(str(scripts)))
    assert wolf._decode_wolf_transport(extracted_text, metadata) == source_text
    translated_text = extracted_text.replace("一行目", "第一行").replace("二行目", "第二行")
    applied, skipped = json_release._apply_translations_to_file(
        str(script_path),
        {extracted_text: {"text": translated_text}},
    )

    assert (applied, skipped) == (1, 0)
    released = list(wolf._iter_released_entries(str(scripts)))
    assert len(released) == 1
    _script_rel, metadata, translated = released[0]
    translated = wolf._decode_wolf_transport(translated, metadata)
    assert wolf._restore_line_prefixes(translated, metadata) == ";「第一行\n　第二行」\n"


def test_wolf_import_font_overlay_preserves_applied_slots(tmp_path):
    patch_path = tmp_path / "patch"
    game_json = patch_path / "game" / "Game.json"
    game_json.parent.mkdir(parents=True)
    game_json.write_text(
        json.dumps({"MainFont": "MS UI Gothic", "SubFonts": ["", "", ""]}),
        encoding="utf-8",
    )

    manifest = {
        "font_revision": {
            "applied_slots": [
                {"family": "中文主字体"},
                {"family": "中文二号字体"},
                {"family": ""},
                {"family": ""},
            ]
        }
    }
    wolf._overlay_applied_font_slots(str(patch_path), manifest)
    updated = json.loads(game_json.read_text(encoding="utf-8"))

    assert updated["MainFont"] == "中文主字体"
    assert updated["SubFonts"] == ["中文二号字体", "", ""]


def test_wolf_font_copy_preserves_existing_different_file(tmp_path):
    destination = tmp_path / wolf.FUSION_FONT_FILENAME
    destination.write_bytes(b"user font")
    source = wolf._fusion_font_path()
    copies = {
        wolf.FUSION_FONT_FILENAME: {
            "path": source,
            "filename": wolf.FUSION_FONT_FILENAME,
            "sha256": wolf._sha256(source),
        }
    }

    rollback = wolf._commit_font_files(str(tmp_path), copies)
    wolf._finish_font_files(rollback)

    assert destination.read_bytes() != b"user font"
    assert destination.with_name(destination.name + ".windy-original.bak").read_bytes() == b"user font"


def test_wolf_font_copy_rollback_restores_existing_file(tmp_path):
    destination = tmp_path / wolf.FUSION_FONT_FILENAME
    destination.write_bytes(b"user font")
    source = wolf._fusion_font_path()
    copies = {
        wolf.FUSION_FONT_FILENAME: {
            "path": source,
            "filename": wolf.FUSION_FONT_FILENAME,
            "sha256": wolf._sha256(source),
        }
    }

    rollback = wolf._commit_font_files(str(tmp_path), copies)
    wolf._rollback_font_files(rollback)

    assert destination.read_bytes() == b"user font"


def test_wolf_font_revision_writes_loose_data_without_repacking(tmp_path, monkeypatch):
    game_path = tmp_path / "Game"
    data_path = game_path / "Data" / "BasicData"
    state_path = game_path / wolf.STATE_DIRNAME
    snapshot_game = state_path / wolf.JSON_SNAPSHOT_DIRNAME / "game" / "Game.json"
    data_path.mkdir(parents=True)
    snapshot_game.parent.mkdir(parents=True)
    (game_path / "Game.exe").write_bytes(b"exe")
    (game_path / "Data.wolf").write_bytes(b"archive")
    (data_path / "Game.dat").write_bytes(b"old game")
    (data_path / "CommonEvent.dat").write_bytes(b"common")
    original = {"MainFont": "MS UI Gothic", "SubFonts": ["M PLUS 1 Medium", "", ""]}
    snapshot_game.write_text(json.dumps(original), encoding="utf-8")
    wolf._write_json_atomic(
        str(state_path / wolf.MANIFEST_FILENAME),
        {"engine": "wolf", "pack_mode": 7, "archive_layout": "single", "archive_sha256": "original"},
    )
    def fake_dump(source_data, json_root):
        game_data = original
        source_game_dat = os.path.join(source_data, "BasicData", "Game.dat")
        try:
            with open(source_game_dat, encoding="utf-8") as source:
                slots = json.load(source)
        except (OSError, ValueError):
            slots = None
        if isinstance(slots, list) and len(slots) == 4:
            game_data = {"MainFont": slots[0], "SubFonts": slots[1:]}
        path = os.path.join(json_root, "game", "Game.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as output:
            json.dump(game_data, output)

    def fake_apply(_data, json_root, output_data):
        with open(os.path.join(json_root, "game", "Game.json"), encoding="utf-8") as source:
            slots = wolf._font_slots(json.load(source))
        with open(os.path.join(output_data, "BasicData", "Game.dat"), "w", encoding="utf-8") as output:
            json.dump(slots, output, ensure_ascii=False)

    monkeypatch.setattr(wolf.uberwolf, "dump_text", fake_dump)
    monkeypatch.setattr(wolf.uberwolf, "apply_text", fake_apply)
    monkeypatch.setattr(
        wolf.uberwolf,
        "unpack_game",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not unpack verification archive")),
    )

    font_path = wolf._fusion_font_path()
    revision = {
        "slots": [
            {
                "family": wolf.FUSION_FONT_FAMILY,
                "source": "module",
                "files": [{"path": font_path, "relative": "FusionPixel/font.ttf"}],
            },
            {
                "family": wolf.FUSION_FONT_FAMILY,
                "source": "module",
                "files": [{"path": font_path, "relative": "FusionPixel/font.ttf"}],
            },
            {"family": "", "source": "empty", "files": []},
            {"family": "", "source": "empty", "files": []},
        ],
        "system_font_copy_ack": [],
    }
    archive_sha256 = wolf._sha256(str(game_path / "Data.wolf"))

    assert wolf.apply_font_revision(str(game_path), revision) is True
    assert json.loads((data_path / "Game.dat").read_text(encoding="utf-8")) == [
        wolf.FUSION_FONT_FAMILY,
        wolf.FUSION_FONT_FAMILY,
        "",
        "",
    ]
    assert (game_path / wolf.FUSION_FONT_FILENAME).is_file()
    assert not (game_path / "Data.wolf").exists()
    manifest = json.loads((state_path / wolf.MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert manifest["deployment_layout"] == "loose"
    assert manifest["retained_archive_sha256"] == archive_sha256
    assert len(manifest["disabled_archives"]) == 1
    disabled_archive = manifest["disabled_archives"][0]
    assert disabled_archive["source_path"] == "Data.wolf"
    assert disabled_archive["sha256"] == archive_sha256
    assert wolf._sha256(wolf._safe_join(str(game_path), disabled_archive["stored_path"])) == archive_sha256
    assert manifest["font_revision"]["original_slots"] == ["MS UI Gothic", "M PLUS 1 Medium", "", ""]
    assert [item["family"] for item in manifest["font_revision"]["applied_slots"]] == [
        wolf.FUSION_FONT_FAMILY,
        wolf.FUSION_FONT_FAMILY,
        "",
        "",
    ]
    assert manifest["font_revision"]["applied_slots"][0]["files"][0]["relative"] == "FusionPixel/font.ttf"
    assert wolf.initialize_game(str(game_path))["font_revision"] == manifest["font_revision"]


def test_wolf_import_writes_loose_data_without_repacking(tmp_path, monkeypatch):
    game_path = tmp_path / "Game"
    data_path = game_path / "Data"
    basic_data = data_path / "BasicData"
    state_path = game_path / wolf.STATE_DIRNAME
    snapshot_path = state_path / wolf.JSON_SNAPSHOT_DIRNAME
    origin_path = game_path / wolf.STRING_SCRIPTS_ORIGIN_DIRNAME
    scripts_path = game_path / wolf.STRING_SCRIPTS_DIRNAME
    basic_data.mkdir(parents=True)
    (snapshot_path / "game").mkdir(parents=True)
    (snapshot_path / "maps").mkdir()
    origin_path.mkdir()
    (game_path / "Game.exe").write_bytes(b"exe")
    (basic_data / "Game.dat").write_bytes(b"old game")
    (basic_data / "CommonEvent.dat").write_bytes(b"common")
    (data_path / "BasicData.wolf").write_bytes(b"archive")
    (snapshot_path / "game" / "Game.json").write_text(
        json.dumps({"MainFont": "Test Font", "SubFonts": ["", "", ""]}),
        encoding="utf-8",
    )
    (snapshot_path / "maps" / "a.json").write_text(
        json.dumps([{"text": "原文です"}], ensure_ascii=False),
        encoding="utf-8",
    )
    script = origin_path / "sample.txt"
    wolf._write_string_script(
        str(script),
        [({"kind": "json", "file": "maps/a.json", "path": [0, "text"], "marker": "Message"}, "原文です")],
    )
    shutil.copytree(origin_path, scripts_path)
    translated_script = scripts_path / "sample.txt"
    translated_script.write_text(
        translated_script.read_text(encoding="utf-8").replace("原文です", "中文"),
        encoding="utf-8",
    )
    wolf._write_json_atomic(
        str(state_path / wolf.MANIFEST_FILENAME),
        {
            "engine": "wolf",
            "archive_layout": "split",
            "archive_sha256": wolf._archive_digest(str(game_path), "split"),
            "data_sha256": wolf._data_digest(str(data_path)),
        },
    )

    def fake_apply(_data, json_root, output_data):
        with open(os.path.join(json_root, "maps", "a.json"), encoding="utf-8") as source:
            translated = json.load(source)[0]["text"]
        with open(os.path.join(output_data, "BasicData", "Game.dat"), "w", encoding="utf-8") as output:
            output.write(translated)

    def fake_dump(_data, json_root):
        os.makedirs(os.path.join(json_root, "game"), exist_ok=True)
        with open(os.path.join(json_root, "game", "Game.json"), "w", encoding="utf-8") as output:
            json.dump({"MainFont": "Test Font", "SubFonts": ["", "", ""]}, output)

    monkeypatch.setattr(wolf.uberwolf, "apply_text", fake_apply)
    monkeypatch.setattr(wolf.uberwolf, "dump_text", fake_dump)
    monkeypatch.setattr(
        wolf.uberwolf,
        "unpack_game",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not unpack verification archive")),
    )

    assert wolf.import_from_string_scripts(str(game_path)) == 1
    assert (basic_data / "Game.dat").read_text(encoding="utf-8") == "中文"
    assert not (game_path / "Data.wolf").exists()
    assert not (data_path / "BasicData.wolf").exists()
    manifest = json.loads((state_path / wolf.MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert manifest["archive_layout"] == "split"
    assert manifest["deployment_layout"] == "loose"
    assert manifest["last_import_data_sha256"] == manifest["data_sha256"]
    assert [item["source_path"] for item in manifest["disabled_archives"]] == ["Data/BasicData.wolf"]
    wolf.initialize_game(str(game_path))


def test_wolf_release_validation_rejects_missing_fallback_and_broken_structure(tmp_path):
    game_path = tmp_path / "Game"
    origin_path = game_path / wolf.STRING_SCRIPTS_ORIGIN_DIRNAME
    script_path = origin_path / "WOLF" / "sample.txt"
    entries = []
    wolf._add_entry(entries, {"kind": "json", "file": "a.json", "path": [0], "marker": "WOLFLogic"}, "タスケテ")
    wolf._add_entry(entries, {"kind": "json", "file": "a.json", "path": [1], "marker": "Message"}, "合言葉は「タスケテ」です。")
    wolf._add_entry(entries, {"kind": "txt", "file": "a.txt", "start": 0, "end": 2, "marker": "WOLFText"}, "一行目\n二行目")
    wolf._write_string_script(str(script_path), entries)
    script_rel = os.path.relpath(script_path, origin_path)
    sources = {
        (metadata["kind"], tuple(metadata.get("path", ()))): wolf._split_text_format(text)[0]
        for _rel, metadata, text in wolf._iter_released_entries(str(origin_path))
    }
    translations = {
        script_rel: {
            sources[("json", (0,))]: {"text": "タスケテ", "status": "success"},
            sources[("json", (1,))]: {"text": "口令是救救我。", "status": "fallback"},
            sources[("txt", ())]: {"text": "第一行和第二行", "status": "success"},
        }
    }

    errors, _warnings, stats = wolf.validate_translation_release(str(game_path), translations)

    assert stats["fallback"] == 1
    assert any("仍为 fallback" in error for error in errors)
    assert any("逻辑提示与判断值不一致" in error for error in errors)
    assert any("WOLF 控制码损坏" in error for error in errors)

    translations[script_rel][sources[("json", (0,))]]["text"] = "救救我"
    errors, _warnings, _stats = wolf.validate_translation_release(str(game_path), translations)
    assert any("逻辑字面量不得翻译" in error for error in errors)


def test_wolf_logic_literal_can_also_be_displayed_unchanged(tmp_path):
    game_path = tmp_path / "Game"
    origin_path = game_path / wolf.STRING_SCRIPTS_ORIGIN_DIRNAME
    script_path = origin_path / "sample.txt"
    entries = []
    wolf._add_entry(entries, {"kind": "json", "file": "a.json", "path": [0], "marker": "WOLFLogic"}, "タスケテ")
    wolf._add_entry(entries, {"kind": "json", "file": "a.json", "path": [1], "marker": "Message"}, "タスケテ")
    wolf._write_string_script(str(script_path), entries)
    script_rel = os.path.relpath(script_path, origin_path)

    errors, _warnings, _stats = wolf.validate_translation_release(
        str(game_path),
        {script_rel: {"タスケテ": {"text": "タスケテ", "status": "success"}}},
    )

    assert errors == []
    assert wolf.get_logic_literals(str(game_path)) == ("タスケテ",)


def test_wolf_release_validation_allows_omitted_logic_files(tmp_path):
    game_path = tmp_path / "Game"
    origin_path = game_path / wolf.STRING_SCRIPTS_ORIGIN_DIRNAME
    logic_entries = []
    display_entries = []
    wolf._add_entry(
        logic_entries,
        {"kind": "json", "file": "a.json", "path": [0], "marker": "WOLFLogic"},
        "内部識別子",
    )
    wolf._add_entry(
        display_entries,
        {"kind": "json", "file": "a.json", "path": [1], "marker": "Message"},
        "表示文",
    )
    wolf._write_string_script(str(origin_path / "WOLF" / "Logic" / "a.txt"), logic_entries)
    display_path = origin_path / "WOLF" / "Binary" / "a.txt"
    wolf._write_string_script(str(display_path), display_entries)
    display_rel = os.path.relpath(display_path, origin_path)

    errors, _warnings, stats = wolf.validate_translation_release(
        str(game_path),
        {display_rel: {"表示文": {"text": "显示文本", "status": "success"}}},
    )

    assert errors == []
    assert stats == {"locations": 2, "changed": 1, "unchanged": 1, "fallback": 0, "missing": 0}


def test_wolf_identifier_logic_does_not_protect_display_names(tmp_path):
    game_path = tmp_path / "Game"
    origin_path = game_path / wolf.STRING_SCRIPTS_ORIGIN_DIRNAME
    script_path = origin_path / "sample.txt"
    entries = []
    wolf._add_entry(
        entries,
        {
            "kind": "json",
            "file": "a.json",
            "path": [0],
            "marker": "WOLFLogic",
            "logic_role": "identifier",
        },
        "ソフィア",
    )
    wolf._add_entry(
        entries,
        {"kind": "json", "file": "a.json", "path": [1], "marker": "Message"},
        "彼女は「ソフィア」です。",
    )
    wolf._write_string_script(str(script_path), entries)
    script_rel = os.path.relpath(script_path, origin_path)

    errors, _warnings, _stats = wolf.validate_translation_release(
        str(game_path),
        {
            script_rel: {
                "ソフィア": {"text": "ソフィア", "status": "success"},
                "彼女は「ソフィア」です。": {"text": "她是索菲娅。", "status": "success"},
            }
        },
    )

    assert errors == []
    assert wolf.get_logic_literals(str(game_path)) == ("ソフィア",)
    assert wolf.get_protected_logic_literals(str(game_path)) == ()


def test_wolf_release_validation_rejects_partial_kana(tmp_path):
    game_path = tmp_path / "Game"
    origin_path = game_path / wolf.STRING_SCRIPTS_ORIGIN_DIRNAME
    script_path = origin_path / "sample.txt"
    entries = []
    wolf._add_entry(entries, {"kind": "json", "file": "a.json", "path": [0]}, "日本語です")
    wolf._write_string_script(str(script_path), entries)
    script_rel = os.path.relpath(script_path, origin_path)

    errors, _warnings, _stats = wolf.validate_translation_release(
        str(game_path),
        {script_rel: {"日本語です": {"text": "中文です", "status": "success"}}},
    )

    assert any("译文残留日语假名" in error for error in errors)


def test_wolf_release_validation_accepts_kana_block_punctuation(tmp_path):
    game_path = tmp_path / "Game"
    origin_path = game_path / wolf.STRING_SCRIPTS_ORIGIN_DIRNAME
    script_path = origin_path / "sample.txt"
    entries = []
    wolf._add_entry(entries, {"kind": "json", "file": "a.json", "path": [0]}, "・マラソン大会")
    wolf._write_string_script(str(script_path), entries)
    script_rel = os.path.relpath(script_path, origin_path)

    errors, _warnings, _stats = wolf.validate_translation_release(
        str(game_path),
        {script_rel: {"・マラソン大会": {"text": "・马拉松大会", "status": "success"}}},
    )

    assert errors == []


def test_replace_data_directory_preserves_first_backup(tmp_path):
    data_path = tmp_path / "Data"
    source_path = tmp_path / "staging"
    data_path.mkdir()
    source_path.mkdir()
    (data_path / "value.txt").write_text("old", encoding="utf-8")
    (source_path / "value.txt").write_text("new", encoding="utf-8")

    wolf._replace_data_directory(str(data_path), str(source_path))

    assert (data_path / "value.txt").read_text(encoding="utf-8") == "new"
    assert (tmp_path / "Data.windy-original.bak" / "value.txt").read_text(encoding="utf-8") == "old"


def test_replace_data_directory_retries_transient_windows_lock(tmp_path, monkeypatch):
    data_path = tmp_path / "Data"
    source_path = tmp_path / "staging"
    data_path.mkdir()
    source_path.mkdir()
    (data_path / "value.txt").write_text("old", encoding="utf-8")
    (source_path / "value.txt").write_text("new", encoding="utf-8")
    real_replace = os.replace
    blocked = 0

    def transient_replace(source, destination):
        nonlocal blocked
        if os.path.basename(source) == "Data.windy.tmp" and blocked < 2:
            blocked += 1
            raise PermissionError(5, "transient scanner lock")
        return real_replace(source, destination)

    monkeypatch.setattr(wolf.os, "replace", transient_replace)
    monkeypatch.setattr(wolf.time, "sleep", lambda _seconds: None)

    wolf._replace_data_directory(str(data_path), str(source_path))

    assert blocked == 2
    assert (data_path / "value.txt").read_text(encoding="utf-8") == "new"


def test_initialize_migrates_stale_data_from_verified_last_import(tmp_path, monkeypatch):
    game_path = tmp_path / "Game"
    data_path = game_path / "Data" / "BasicData"
    state_path = game_path / wolf.STATE_DIRNAME
    data_path.mkdir(parents=True)
    state_path.mkdir()
    (game_path / "Game.exe").write_bytes(b"exe")
    archive_path = game_path / "Data.wolf"
    archive_path.write_bytes(b"verified archive")
    (data_path / "Game.dat").write_bytes(b"old")
    (data_path / "CommonEvent.dat").write_bytes(b"old")
    wolf._write_json_atomic(
        str(state_path / wolf.MANIFEST_FILENAME),
        {
            "engine": "wolf",
            "pack_mode": 7,
            "archive_sha256": "original",
            "last_import_sha256": wolf._sha256(str(archive_path)),
        },
    )

    def fake_unpack(root):
        basic_data = os.path.join(root, "Data", "BasicData")
        os.makedirs(basic_data)
        with open(os.path.join(basic_data, "Game.dat"), "wb") as output:
            output.write(b"new")
        with open(os.path.join(basic_data, "CommonEvent.dat"), "wb") as output:
            output.write(b"new")
        return os.path.join(root, "Data")

    monkeypatch.setattr(wolf.uberwolf, "unpack_game", fake_unpack)
    manifest = wolf.initialize_game(str(game_path))

    assert (data_path / "Game.dat").read_bytes() == b"new"
    assert manifest["data_sha256"] == wolf._data_digest(str(game_path / "Data"))
    assert manifest["last_import_data_sha256"] == manifest["data_sha256"]


def test_initialize_rejects_restored_archive_with_imported_data(tmp_path, monkeypatch):
    game_path = tmp_path / "Game"
    data_path = game_path / "Data" / "BasicData"
    state_path = game_path / wolf.STATE_DIRNAME
    data_path.mkdir(parents=True)
    state_path.mkdir()
    (game_path / "Game.exe").write_bytes(b"exe")
    archive_path = game_path / "Data.wolf"
    archive_path.write_bytes(b"original archive")
    (data_path / "Game.dat").write_bytes(b"translated")
    (data_path / "CommonEvent.dat").write_bytes(b"translated")
    wolf._write_json_atomic(
        str(state_path / wolf.MANIFEST_FILENAME),
        {
            "engine": "wolf",
            "pack_mode": 7,
            "archive_sha256": wolf._sha256(str(archive_path)),
            "last_import_sha256": "different imported archive",
            "data_sha256": wolf._data_digest(str(game_path / "Data")),
        },
    )

    def fake_unpack(root):
        basic_data = os.path.join(root, "Data", "BasicData")
        os.makedirs(basic_data)
        with open(os.path.join(basic_data, "Game.dat"), "wb") as output:
            output.write(b"original")
        with open(os.path.join(basic_data, "CommonEvent.dat"), "wb") as output:
            output.write(b"original")
        return os.path.join(root, "Data")

    monkeypatch.setattr(wolf.uberwolf, "unpack_game", fake_unpack)
    try:
        wolf.initialize_game(str(game_path))
    except wolf.WolfEngineError as error:
        assert "不一致" in str(error)
    else:
        raise AssertionError("mismatched Data and Data.wolf must be rejected")


def test_initialize_unpacks_and_records_split_archive_layout(tmp_path, monkeypatch):
    data_path = tmp_path / "Data"
    data_path.mkdir()
    (tmp_path / "Game.exe").write_bytes(b"exe")
    (data_path / "BasicData.wolf").write_bytes(b"archive")
    unpack_calls = 0

    def fake_unpack(root):
        nonlocal unpack_calls
        unpack_calls += 1
        basic_data = os.path.join(root, "Data", "BasicData")
        os.makedirs(basic_data)
        with open(os.path.join(basic_data, "Game.dat"), "wb") as output:
            output.write(b"game")
        with open(os.path.join(basic_data, "CommonEvent.dat"), "wb") as output:
            output.write(b"common")
        return os.path.join(root, "Data")

    monkeypatch.setattr(wolf.uberwolf, "unpack_game", fake_unpack)
    manifest = wolf.initialize_game(str(tmp_path))

    assert unpack_calls == 1
    assert manifest["archive_layout"] == "split"
    assert manifest["archive_sha256"] == wolf._archive_digest(str(tmp_path), "split")
    assert manifest["archive_data_sha256"] == manifest["data_sha256"]


def test_initialize_accepts_game_that_started_with_loose_data(tmp_path, monkeypatch):
    data_path = tmp_path / "Data" / "BasicData"
    data_path.mkdir(parents=True)
    (tmp_path / "Game.exe").write_bytes(b"exe")
    (data_path / "Game.dat").write_bytes(b"game")
    (data_path / "CommonEvent.dat").write_bytes(b"common")
    monkeypatch.setattr(
        wolf.uberwolf,
        "unpack_game",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("loose game must not unpack")),
    )

    manifest = wolf.initialize_game(str(tmp_path))

    assert manifest["archive_layout"] == "loose"
    assert manifest["deployment_layout"] == "loose"
    assert manifest["archive_sha256"] is None
    assert manifest["data_sha256"] == wolf._data_digest(str(tmp_path / "Data"))


def test_initialize_accepts_verified_loose_deployment_beside_retained_archive(tmp_path, monkeypatch):
    data_path = tmp_path / "Data" / "BasicData"
    state_path = tmp_path / wolf.STATE_DIRNAME
    data_path.mkdir(parents=True)
    state_path.mkdir()
    (tmp_path / "Game.exe").write_bytes(b"exe")
    archive_path = tmp_path / "Data.wolf"
    archive_path.write_bytes(b"retained archive")
    (data_path / "Game.dat").write_bytes(b"translated")
    (data_path / "CommonEvent.dat").write_bytes(b"translated")
    archive_sha256 = wolf._sha256(str(archive_path))
    data_sha256 = wolf._data_digest(str(tmp_path / "Data"))
    wolf._write_json_atomic(
        str(state_path / wolf.MANIFEST_FILENAME),
        {
            "engine": "wolf",
            "pack_mode": 7,
            "archive_layout": "single",
            "archive_sha256": archive_sha256,
            "archive_data_sha256": "original data",
            "retained_archive_sha256": archive_sha256,
            "deployment_layout": "loose",
            "last_import_data_sha256": data_sha256,
            "data_sha256": data_sha256,
            "font_revision": {"original_slots": ["A", "B", "", ""]},
        },
    )
    monkeypatch.setattr(
        wolf.uberwolf,
        "unpack_game",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("verified loose Data must not be replaced")),
    )

    manifest = wolf.initialize_game(str(tmp_path))

    assert manifest["deployment_layout"] == "loose"
    assert manifest["data_sha256"] == data_sha256
    assert manifest["font_revision"]["original_slots"] == ["A", "B", "", ""]
    assert (data_path / "Game.dat").read_bytes() == b"translated"
