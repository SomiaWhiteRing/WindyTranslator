import csv
import json as _json
import os
import shutil
import tempfile
import queue

from core.engines import wolf
from core.tasks import initialize, json_creation, json_release
from core.utils.engine_detection import detect_game_engine


def _with_control_metadata(value):
    if isinstance(value, list):
        result = [_with_control_metadata(item) for item in value]
        if result and all(isinstance(item, dict) and "code" in item for item in result):
            for index, item in enumerate(result):
                item.setdefault("index", index)
        return result
    if not isinstance(value, dict):
        return value
    result = {key: _with_control_metadata(item) for key, item in value.items()}
    if "code" in result:
        result.setdefault("indent", 0)
    if "commands" in result:
        result.setdefault("id", 0)
        result.setdefault("arguments", [""] * 10)
        result.setdefault("activation", {"raw": 0x1E848023, "extra": [0] * 7})
    return result


class _FixtureJson:
    load = staticmethod(_json.load)
    loads = staticmethod(_json.loads)

    @staticmethod
    def dump(value, output, *args, **kwargs):
        return _json.dump(_with_control_metadata(value), output, *args, **kwargs)

    @staticmethod
    def dumps(value, *args, **kwargs):
        return _json.dumps(_with_control_metadata(value), *args, **kwargs)


json = _FixtureJson()


def _translated_entry(entries, source_text, translated_text, status="success"):
    return {
        "text": translated_text,
        "status": status,
        "wolf_export_schema": wolf.WOLF_EXPORT_SCHEMA,
        "wolf_codes": [
            metadata["wolf_code"]
            for metadata, text in entries
            if text == source_text and metadata.get("marker") != "WOLFLogic"
        ],
    }


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
        lambda _path, _messages: {"archive_layout": "single"},
    )

    initialize.run_initialize(str(tmp_path), {}, messages)

    queued = list(messages.queue)
    assert ("success", "初始化完成（WOLF，原始封包布局 single）") in queued
    assert not any(message_type == "error" for message_type, _content in queued)
    assert ("wolf_initialized", str(tmp_path)) in queued
    assert queued.index(("wolf_initialized", str(tmp_path))) < queued.index(("done", None))


def test_wolf_command_roles_keep_logic_and_skip_identifier_parameters():
    usage = {
        "common_event_roles_by_name": {
            "internal_id": {5: {"display"}, 6: {"logic"}},
        }
    }
    entries = []
    wolf._command_entries(
        {"code": 112, "stringArgs": ["ウソツキ"]},
        ["commands", 0],
        entries,
        "common/Common.json",
        usage,
    )
    wolf._command_entries(
        {
            "code": 300,
            "intArgs": [0, 0x3020, 0, 0],
            "stringArgs": ["internal_id", "画面に表示します。", "short_id"],
        },
        ["commands", 1],
        entries,
        "common/Common.json",
        usage,
    )

    assert [(metadata["marker"], text) for metadata, text in entries] == [
        ("WOLFLogic", "ウソツキ"),
        ("WOLFText", "画面に表示します。"),
        ("WOLFLogic", "short_id"),
    ]


def test_wolf_call_parameter_roles_include_short_ui_text():
    usage = {
        "common_event_roles_by_name": {
            "■tipsメッセージ": {5: {"display"}, 6: {"display"}},
            "■エネミーを見る": {5: {"display"}},
        }
    }
    entries = []
    wolf._command_entries(
        {
            "code": 300,
            "intArgs": [0, 0x3020, 0, 0],
            "stringArgs": ["■Tipsメッセージ", "内部", "安全な空間"],
        },
        ["commands", 0],
        entries,
        "common/Common.json",
        usage,
    )

    assert [(metadata["path"][-1], text) for metadata, text in entries] == [
        (1, "内部"),
        (2, "安全な空間"),
    ]

    entries = []
    wolf._command_entries(
        {
            "code": 300,
            "intArgs": [0, 0x1010, 0],
            "stringArgs": ["■エネミーを見る", "† おまけ †"],
        },
        ["commands", 1],
        entries,
        "common/Common.json",
        usage,
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
    maps = json_root / "maps"
    maps.mkdir()
    (maps / "Map001.json").write_text(
        json.dumps({"events": [{"pages": [{"list": [
            {
                "code": 250,
                    "intArgs": [0, 1, 1, 0x1200, 1600000],
                "stringArgs": ["", "", "", "表示名"],
            },
            {"code": 101, "stringArgs": [r"\cself[0]"]},
        ]}]}]}),
        encoding="utf-8",
    )

    usage = wolf._analyze_json_usage(str(json_root))
    entries = wolf._json_entries(str(json_root), str(json_path), usage)
    roles = [(metadata["path"], metadata["marker"], text) for metadata, text in entries]

    assert not any(path == ["types", 0, "data", 0, "name"] for path, _marker, _text in roles)
    assert (["types", 0, "data", 1, "name"], "WOLFLogic", "ソフィア") in roles
    assert (["types", 0, "data", 1, "data", 0, "value"], "WOLFLogic", "ソフィア") in roles
    assert (["types", 0, "data", 1, "data", 1, "value"], "WOLFText", "ソフィア") in roles

    scripts = tmp_path / "StringScripts"
    assert wolf._write_json_entry_groups(str(scripts), "databases/DataBase.json.txt", entries) == 2
    assert (scripts / "WOLF" / "Binary" / "databases" / "DataBase.json.txt").is_file()
    assert (scripts / "WOLF" / "Logic" / "databases" / "DataBase.json.txt").is_file()


def test_wolf_database_first_string_data_id_stays_logic(tmp_path):
    json_root = tmp_path / "json"
    databases = json_root / "databases"
    databases.mkdir(parents=True)
    database_path = databases / "DataBase.json"
    database_path.write_text(
        json.dumps({
            "types": [{
                "name": "武装一覧",
                "data": [
                    {
                        "name": "",
                        "data": [
                            {"name": "名称", "value": "ハンドル"},
                            {"name": "説明", "value": "回転させる装置"},
                        ],
                    },
                    {
                        "name": "handle_id",
                        "data": [{"name": "名称", "value": "予備ハンドル"}],
                    },
                ],
            }],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    maps = json_root / "maps"
    maps.mkdir()
    (maps / "Map001.json").write_text(
        json.dumps({"events": [{"pages": [{"list": [
            {
                "code": 250,
                    "intArgs": [0, 0, 1, 0x1200, 1600000],
                "stringArgs": ["", "", "", "説明"],
            },
            {"code": 101, "stringArgs": [r"\cself[0]"]},
        ]}]}]}),
        encoding="utf-8",
    )

    usage = wolf._analyze_json_usage(str(json_root))
    roles = {
        (tuple(metadata["path"]), text): metadata["marker"]
        for metadata, text in wolf._json_entries(
            str(json_root), str(database_path), usage
        )
    }

    assert roles[(("types", 0, "data", 0, "data", 0, "value"), "ハンドル")] == "WOLFLogic"
    assert roles[(("types", 0, "data", 0, "data", 1, "value"), "回転させる装置")] == "WOLFText"
    assert roles[(("types", 0, "data", 1, "data", 0, "value"), "予備ハンドル")] == "WOLFLogic"


def test_wolf_visible_first_string_id_is_translatable_with_namespace(tmp_path):
    json_root = tmp_path / "json"
    databases = json_root / "databases"
    common = json_root / "common"
    databases.mkdir(parents=True)
    common.mkdir()
    database_path = databases / "DataBase.json"
    database_path.write_text(
        json.dumps({
            "types": [{
                "name": "武装 test",
                "fields": [{"name": "名称"}],
                "data": [{
                    "name": "",
                    "data": [{"name": "名称", "value": "ライフル"}],
                }],
            }],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    (common / "Display.json").write_text(
        json.dumps({
            "commands": [
                {
                    "code": 250,
                    "intArgs": [0, 0, 0, 0x51200, 1600000],
                    "stringArgs": ["", "武装 test", "", "名称"],
                },
                {"code": 150, "intArgs": [32], "stringArgs": [r"\cself[0]"]},
                {
                    "code": 250,
                    "intArgs": [0, 0, 0, 0x71200, 1600001],
                    "stringArgs": ["", "武装 test", "ライフル", "名称"],
                },
            ]
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    usage = wolf._analyze_json_usage(str(json_root))
    entries = wolf._json_entries(str(json_root), str(database_path), usage)
    metadata = next(metadata for metadata, text in entries if text == "ライフル")

    assert ("database.json", 0, 0, 0) in usage["visible_database_records"]
    assert metadata["marker"] == "WOLFText"
    assert metadata["identifier_namespace"] == ["database.json", 0]
    assert metadata["identifier_record"] == ["database.json", 0, 0]
    assert "identifier_targets" not in metadata
    assert metadata["identifier_references"] == [{
        "file": "common/Display.json",
        "path": ["commands", 2, "stringArgs", 2],
        "target_data_indexes": [0],
        "expected_original": "ライフル",
        "reference_kind": "database_selector",
    }]
    assert wolf._first_string_database_marker(
        "database.json",
        0,
        {"name": "名称", "value": "ライフル"},
        {
            "visible_database_fields": {("database.json", 0, 0)},
            "nonselector_logic_database_fields": {("database.json", 0, 0)},
        },
    ) == "WOLFLogic"
    assert wolf._first_string_database_marker(
        "database.json",
        0,
        {"name": "名称", "value": "ライフル"},
        {
            "visible_database_fields": {("database.json", 0, 0)},
            "nonselector_logic_database_fields": set(),
            "comparison_literals": {"ライフル"},
        },
    ) == "WOLFLogic"


def test_wolf_database_read_compared_by_code112_int_arg_stays_logic(tmp_path):
    json_root = tmp_path / "json"
    database_path = json_root / "databases" / "DataBase.json"
    common_path = json_root / "common" / "Compare.json"
    database_path.parent.mkdir(parents=True)
    common_path.parent.mkdir(parents=True)
    database_path.write_text(
        json.dumps({
            "types": [{
                "name": "Actions",
                "fields": [{"name": "Action"}],
                "data": [{
                    "name": "entry",
                    "data": [{"name": "Action", "value": "OpenPanel"}],
                }],
            }],
        }),
        encoding="utf-8",
    )
    common_path.write_text(
        json.dumps({
            "commands": [
                {
                    "code": 250,
                    "intArgs": [0, 0, 0, 0x51200, 1600009],
                    "stringArgs": ["", "Actions", "", "Action"],
                },
                {
                    "code": 112,
                    "intArgs": [17, 1600009],
                    "stringArgs": ["OpenPanel", "", "", ""],
                },
            ],
        }),
        encoding="utf-8",
    )

    usage = wolf._analyze_json_usage(str(json_root))
    entries = wolf._json_entries(str(json_root), str(database_path), usage)
    metadata = next(metadata for metadata, text in entries if text == "OpenPanel")

    assert ("database.json", 0, 0, 0) in usage["logic_database_records"]
    assert ("database.json", 0, 0, 0) in usage["nonselector_logic_database_records"]
    assert metadata["marker"] == "WOLFLogic"


def test_wolf_code300_output_stops_earlier_database_value_from_becoming_display(tmp_path):
    json_root = tmp_path / "json"
    database_path = json_root / "databases" / "DataBase.json"
    common_path = json_root / "common" / "Overwrite.json"
    database_path.parent.mkdir(parents=True)
    common_path.parent.mkdir(parents=True)
    database_path.write_text(
        json.dumps({
            "types": [{
                "name": "InternalActions",
                "fields": [{"name": "Reference"}],
                "data": [{
                    "name": "entry",
                    "data": [{"name": "Reference", "value": "InternalTarget"}],
                }],
            }],
        }),
        encoding="utf-8",
    )
    common_path.write_text(
        json.dumps({
            "commands": [
                {
                    "code": 250,
                    "intArgs": [0, 0, 0, 0x51200, 1600009],
                    "stringArgs": ["", "InternalActions", "", "Reference"],
                },
                {
                    "code": 300,
                    "intArgs": [0, 16789540, 151, 3, 0, 0, 0, 0, 1600009],
                    "stringArgs": ["SystemFormatter", "Heading", "H2"],
                },
                {
                    "code": 150,
                    "intArgs": [32],
                    "stringArgs": [r"\cself[9]"],
                },
            ],
        }),
        encoding="utf-8",
    )

    usage = wolf._analyze_json_usage(str(json_root))

    assert ("database.json", 0, 0) not in usage["visible_database_fields"]
    assert ("database.json", 0, 0) not in usage["display_database_fields"]
    assert ("database.json", 0, 0, 0) not in usage["visible_database_records"]
    assert ("database.json", 0, 0, 0) not in usage["display_database_records"]


def test_wolf_control_flow_entry_stops_stale_database_value(tmp_path):
    json_root = tmp_path / "json"
    database_path = json_root / "databases" / "DataBase.json"
    common_path = json_root / "common" / "Flow.json"
    database_path.parent.mkdir(parents=True)
    common_path.parent.mkdir(parents=True)
    database_path.write_text(
        json.dumps({
            "types": [{
                "name": "InternalActions",
                "fields": [{"name": "Reference"}],
                "data": [{
                    "name": "entry",
                    "data": [{"name": "Reference", "value": "InternalTarget"}],
                }],
            }],
        }),
        encoding="utf-8",
    )
    common_path.write_text(
        json.dumps({
            "commands": [
                {
                    "code": 250,
                    "intArgs": [0, 0, 0, 0x51200, 1600009],
                    "stringArgs": ["", "InternalActions", "", "Reference"],
                },
                {"code": 213, "stringArgs": ["END"]},
                {"code": 99, "intArgs": [0]},
                {"code": 212, "stringArgs": ["cmd:601"]},
                {
                    "code": 300,
                    "intArgs": [0, 16785444, 151, 2, 0, 0, 1600009, 0, 1600009],
                    "stringArgs": ["◆[rb]システム管理", "", "H5"],
                },
                {"code": 150, "intArgs": [32], "stringArgs": [r"\cself[9]"]},
            ],
        }),
        encoding="utf-8",
    )

    usage = wolf._analyze_json_usage(str(json_root))

    assert ("database.json", 0, 0) not in usage["visible_database_fields"]
    assert ("database.json", 0, 0, 0) not in usage["visible_database_records"]


def test_wolf_usage_rejects_commands_without_control_metadata(tmp_path):
    common_path = tmp_path / "common" / "Legacy.json"
    common_path.parent.mkdir()
    common_path.write_text(
        _json.dumps({
            "id": 0,
            "arguments": [""] * 10,
            "activation": {"raw": 0x1E848023, "extra": [0] * 7},
            "commands": [{"code": 101, "stringArgs": ["legacy"]}],
        }),
        encoding="utf-8",
    )

    try:
        wolf._analyze_json_usage(str(tmp_path))
    except wolf.WolfEngineError as error:
        assert "控制流元数据" in str(error)
    else:
        raise AssertionError("legacy WOLF command dumps must be rejected")


def test_wolf_usage_rejects_common_event_without_id(tmp_path):
    common_path = tmp_path / "common" / "MissingId.json"
    common_path.parent.mkdir()
    common_path.write_text(
        _json.dumps({
            "arguments": [""] * 10,
            "activation": {"raw": 0x1E848023, "extra": [0] * 7},
            "commands": [{"index": 0, "indent": 0, "code": 0}],
        }),
        encoding="utf-8",
    )

    try:
        wolf._analyze_json_usage(str(tmp_path))
    except wolf.WolfEngineError as error:
        assert "编号无效" in str(error)
    else:
        raise AssertionError("missing CommonEvent id must be rejected")


def test_wolf_automatic_root_reaches_call_only_child(tmp_path):
    json_root = tmp_path / "json"
    common = json_root / "common"
    databases = json_root / "databases"
    common.mkdir(parents=True)
    databases.mkdir(parents=True)
    (databases / "DataBase.json").write_text(
        json.dumps({
            "types": [{
                "name": "Labels",
                "fields": [{"name": "Text"}],
                "data": [{"data": [{"name": "Text", "value": "Visible"}]}],
            }],
        }),
        encoding="utf-8",
    )
    (common / "Auto.json").write_text(
        json.dumps({
            "id": 1,
            "activation": {"raw": 0x1E848023, "extra": [0] * 7},
            "commands": [{
                "code": 210,
                "intArgs": [500002, 0],
                "stringArgs": [""],
            }],
        }),
        encoding="utf-8",
    )
    child_path = common / "Child.json"
    child_path.write_text(
        json.dumps({
            "id": 2,
            "activation": {"raw": 0x1E848020, "extra": [0] * 7},
            "commands": [
                {
                    "code": 250,
                    "intArgs": [0, 0, 0, 0x51200, 1600000],
                    "stringArgs": ["", "Labels", "", "Text"],
                },
                {"code": 101, "stringArgs": [r"\cself[0]"]},
            ],
        }),
        encoding="utf-8",
    )

    usage = wolf._analyze_json_usage(str(json_root))

    assert "common/Auto.json" in usage["active_common_event_files"]
    assert "common/Child.json" in usage["active_common_event_files"]
    assert ("database.json", 0, 0, 0) in usage["display_database_records"]
    child_entries = wolf._json_entries(str(json_root), str(child_path), usage)
    assert next(
        metadata["marker"]
        for metadata, text in child_entries
        if text == r"\cself[0]"
    ) == "Message"


def test_wolf_unknown_activation_protects_event_without_polluting_database(tmp_path):
    json_root = tmp_path / "json"
    common = json_root / "common"
    databases = json_root / "databases"
    common.mkdir(parents=True)
    databases.mkdir()
    (databases / "DataBase.json").write_text(
        json.dumps({
            "types": [{
                "name": "Labels",
                "fields": [{"name": "Text"}],
                "data": [{"data": [{"name": "Text", "value": "Maybe"}]}],
            }],
        }),
        encoding="utf-8",
    )
    unknown_path = common / "Unknown.json"
    unknown_path.write_text(
        json.dumps({
            "id": 7,
            "activation": {"raw": 0x1E848022, "extra": [0] * 7},
            "commands": [
                {
                    "code": 250,
                    "intArgs": [0, 0, 0, 0x51200, 1600000],
                    "stringArgs": ["", "Labels", "", "Text"],
                },
                {"code": 101, "stringArgs": [r"\cself[0]"]},
            ],
        }),
        encoding="utf-8",
    )

    usage = wolf._analyze_json_usage(str(json_root))
    entries = wolf._json_entries(str(json_root), str(unknown_path), usage)

    assert "common/Unknown.json" in usage["protected_unknown_common_files"]
    assert ("database.json", 0, 0) not in usage["unknown_database_fields"]
    assert all(metadata["marker"] == "WOLFLogic" for metadata, _text in entries)
    assert usage["analysis_diagnostics"] == [{
        "reason": "unknown_common_event_activation",
        "file": "common/Unknown.json",
        "path": ["activation"],
        "effect": "protected",
        "details": {"raw": 0x1E848022, "mode": 0x22, "extra": [0] * 7},
    }]

    payload = wolf._write_analysis_diagnostics(str(tmp_path), usage)
    saved = _json.loads(
        (tmp_path / wolf.STATE_DIRNAME / wolf.ANALYSIS_DIAGNOSTICS_FILENAME).read_text(
            encoding="utf-8"
        )
    )
    assert saved == payload
    assert saved["summary"] == {
        "protected": 1,
        "reasons": {"unknown_common_event_activation": 1},
    }


def test_wolf_common_call_flags_distinguish_inputs_outputs_and_self_calls():
    event = {"id": 7, "name": "Display"}
    by_id = {7: event}
    by_name = {"display": event}
    literal = wolf._decode_common_call(
        {
            "code": 300,
            "intArgs": [0, 0x1010, 0],
            "stringArgs": ["Display", "label"],
        },
        by_id,
        by_name,
    )
    assert literal["event"] is event
    assert literal["string_inputs"] == ((5, "literal", "label"),)
    assert literal["output"] is None

    with_output = wolf._decode_common_call(
        {
            "code": 210,
            "intArgs": [500007, 0x01000010, 1600005, 1600009],
            "stringArgs": [""],
        },
        by_id,
        by_name,
    )
    assert with_output["event"] is event
    assert with_output["string_inputs"] == ((5, "variable", 1600005),)
    assert with_output["output"] == 1600009

    self_call = wolf._decode_common_call(
        {"code": 210, "intArgs": [600100, 0], "stringArgs": [""]},
        by_id,
        by_name,
        current_event_id=7,
    )
    assert self_call["event"] is event
    assert wolf._decode_common_call(
        {"code": 210, "intArgs": [500007, 0x02000000], "stringArgs": [""]},
        by_id,
        by_name,
    ) is None


def test_wolf_cfg_keeps_nested_loop_break_and_continue_targets():
    commands = [
        {"index": 0, "indent": 0, "code": 179},
        {"index": 1, "indent": 1, "code": 179},
        {"index": 2, "indent": 2, "code": 176},
        {"index": 3, "indent": 2, "code": 171},
        {"index": 4, "indent": 1, "code": 498},
        {"index": 5, "indent": 1, "code": 171},
        {"index": 6, "indent": 0, "code": 498},
        {"index": 7, "indent": 0, "code": 101, "stringArgs": ["done"]},
    ]

    successors = wolf._command_successors(commands)

    assert successors[0] == (1, 7)
    assert successors[1] == (2, 5)
    assert successors[2] == (4,)
    assert successors[3] == (5,)
    assert successors[5] == (7,)
    assert successors[6] == (1, 7)


def test_wolf_cfg_specializes_cmd_argument_dynamic_jump():
    commands = [
        {
            "index": 0,
            "indent": 0,
            "code": 122,
            "intArgs": [3000001, 0],
            "stringArgs": [r"cmd:\cself[0]"],
        },
        {"index": 1, "indent": 0, "code": 213, "stringArgs": [r"\s[1]"]},
        {"index": 2, "indent": 0, "code": 212, "stringArgs": ["cmd:1"]},
        {"index": 3, "indent": 0, "code": 101, "stringArgs": [r"\cself[5]"]},
        {"index": 4, "indent": 0, "code": 213, "stringArgs": ["END"]},
        {"index": 5, "indent": 0, "code": 212, "stringArgs": ["cmd:2"]},
        {"index": 6, "indent": 0, "code": 112, "stringArgs": [r"\cself[5]"]},
        {"index": 7, "indent": 0, "code": 213, "stringArgs": ["END"]},
    ]

    aggregate = wolf._command_successors(commands)
    display = wolf._command_successors(commands, {0: 1})
    logic = wolf._command_successors(commands, {0: 2})

    assert aggregate[1] == (2, 5)
    assert display[1] == (2,)
    assert logic[1] == (5,)
    display_role = wolf._trace_string_variable_usage(
        commands, None, 1600005, {}, {}, {}, {}, display
    )
    logic_role = wolf._trace_string_variable_usage(
        commands, None, 1600005, {}, {}, {}, {}, logic
    )
    assert display_role["display"] is True and display_role["logic"] is False
    assert logic_role["display"] is False and logic_role["logic"] is True


def test_wolf_cfg_missing_jump_label_falls_through_and_real_end_label_wins():
    missing = [
        {"index": 0, "indent": 0, "code": 213, "stringArgs": ["missing"]},
        {"index": 1, "indent": 0, "code": 101, "stringArgs": ["visible"]},
    ]
    named_end = [
        {"index": 0, "indent": 0, "code": 213, "stringArgs": ["END"]},
        {"index": 1, "indent": 0, "code": 101, "stringArgs": ["skipped"]},
        {"index": 2, "indent": 0, "code": 212, "stringArgs": ["END"]},
    ]

    assert wolf._command_successors(missing)[0] == (1,)
    assert wolf._command_successors(named_end)[0] == (2,)


def test_wolf_cfg_does_not_specialize_branch_local_dispatch_assignment():
    commands = [
        {"index": 0, "indent": 0, "code": 111},
        {"index": 1, "indent": 0, "code": 401},
        {
            "index": 2,
            "indent": 1,
            "code": 122,
            "intArgs": [3000001, 0],
            "stringArgs": [r"cmd:\cself[0]"],
        },
        {"index": 3, "indent": 0, "code": 420},
        {
            "index": 4,
            "indent": 1,
            "code": 122,
            "intArgs": [3000001, 0],
            "stringArgs": [r"alt:\cself[0]"],
        },
        {"index": 5, "indent": 0, "code": 499},
        {"index": 6, "indent": 0, "code": 213, "stringArgs": [r"\s[1]"]},
        {"index": 7, "indent": 0, "code": 212, "stringArgs": ["cmd:1"]},
        {"index": 8, "indent": 0, "code": 213, "stringArgs": ["END"]},
        {"index": 9, "indent": 0, "code": 212, "stringArgs": ["alt:1"]},
        {"index": 10, "indent": 0, "code": 213, "stringArgs": ["END"]},
    ]

    successors = wolf._command_successors(commands, {0: 1})

    assert successors[6] == (7, 9)


def test_wolf_resource_template_assignment_is_logic_use():
    commands = [
        {
            "index": 0,
            "indent": 0,
            "code": 250,
            "intArgs": [0, 0, 0, 0x51200, 1600005],
            "stringArgs": ["", "Names", "", "Name"],
        },
        {
            "index": 1,
            "indent": 0,
            "code": 122,
            "intArgs": [1600006, 0],
            "stringArgs": [r"Picture_UI/\cself[5].png"],
        },
    ]
    successors = wolf._command_successors(commands)

    role = wolf._trace_string_variable_usage(
        commands, 0, 1600005, {}, {}, {}, {}, successors
    )

    assert role["logic"] is True


def test_wolf_cmd_context_does_not_mix_dead_return_branch_into_database_use(tmp_path):
    json_root = tmp_path / "json"
    database_path = json_root / "databases" / "DataBase.json"
    common = json_root / "common"
    database_path.parent.mkdir(parents=True)
    common.mkdir()
    database_path.write_text(
        json.dumps({
            "types": [{
                "name": "Weapons",
                "fields": [{"name": "Name"}],
                "data": [{
                    "name": "weapon",
                    "data": [{"name": "Name", "value": "Rifle"}],
                }],
            }],
        }),
        encoding="utf-8",
    )
    (common / "Dispatcher.json").write_text(
        json.dumps({
            "id": 13,
            "name": "Dispatcher",
            "activation": {"raw": 0x1E848020, "extra": [0] * 7},
            "commands": [
                {
                    "code": 122,
                    "intArgs": [3000001, 0],
                    "stringArgs": [r"cmd:\cself[0]"],
                },
                {"code": 213, "stringArgs": [r"\s[1]"]},
                {"code": 212, "stringArgs": ["cmd:101"]},
                {"code": 122, "intArgs": [1600005, 0], "stringArgs": ["Internal"]},
                {"code": 213, "stringArgs": ["END"]},
                {"code": 212, "stringArgs": ["cmd:801"]},
                {
                    "code": 250,
                    "intArgs": [0, 0, 0, 0x51200, 1600005],
                    "stringArgs": ["", "Weapons", "", "Name"],
                },
                {"code": 213, "stringArgs": ["END"]},
            ],
        }),
        encoding="utf-8",
    )
    (common / "Caller.json").write_text(
        json.dumps({
            "id": 20,
            "name": "Caller",
            "commands": [
                {
                    "code": 210,
                    "intArgs": [500013, 0x01000001, 101, 3000001],
                    "stringArgs": [""],
                },
                {"code": 112, "intArgs": [0, 3000001], "stringArgs": ["used"]},
                {
                    "code": 250,
                    "intArgs": [0, 1600000, 0, 0x51200, 3000002],
                    "stringArgs": ["", "Weapons", "", "Name"],
                },
                {"code": 101, "stringArgs": [r"\s[2]"]},
            ],
        }),
        encoding="utf-8",
    )

    usage = wolf._analyze_json_usage(str(json_root))
    key = ("database.json", 0, 0)
    contexts = usage["common_event_contexts_by_id"][13]

    assert contexts["return_roles"][101] == {"logic"}
    assert contexts["return_roles"][801] == set()
    assert key in usage["display_database_fields"]
    assert key not in usage["logic_database_fields"]
    entries = wolf._json_entries(str(json_root), str(database_path), usage)
    rifle = next(metadata for metadata, text in entries if text == "Rifle")
    assert rifle["marker"] == "WOLFText"


def test_wolf_database_callback_seeds_exact_cmd_context(tmp_path):
    json_root = tmp_path / "json"
    databases = json_root / "databases"
    common = json_root / "common"
    databases.mkdir(parents=True)
    common.mkdir()
    target_path = databases / "DataBase.json"
    target_path.write_text(
        json.dumps({
            "types": [{
                "name": "Names",
                "fields": [{"name": "Name"}],
                "data": [{
                    "name": "entry",
                    "data": [{"name": "Name", "value": "Callback text"}],
                }],
            }],
        }),
        encoding="utf-8",
    )
    (databases / "CDataBase.json").write_text(
        json.dumps({
            "types": [{
                "name": "Buttons bk",
                "fields": [
                    {"name": "実行コモン"},
                    {"name": "└ 引数1_1_Open"},
                    {"name": "└ 引数1_2"},
                    {"name": "└ 引数1_3"},
                ],
                "data": [{
                    "name": "button",
                    "data": [
                        {"name": "実行コモン", "value": "Dispatcher"},
                        {"name": "└ 引数1_1_Open", "value": 2},
                        {"name": "└ 引数1_2", "value": 0},
                        {"name": "└ 引数1_3", "value": 0},
                    ],
                }],
            }],
        }),
        encoding="utf-8",
    )
    (common / "Dispatcher.json").write_text(
        json.dumps({
            "id": 13,
            "name": "Dispatcher",
            "activation": {"raw": 0x1E848020, "extra": [0] * 7},
            "commands": [
                {
                    "code": 122,
                    "intArgs": [3000001, 0],
                    "stringArgs": [r"cmd:\cself[0]"],
                },
                {"code": 213, "stringArgs": [r"\s[1]"]},
                {"code": 212, "stringArgs": ["cmd:1"]},
                {"code": 112, "stringArgs": ["internal"]},
                {"code": 213, "stringArgs": ["END"]},
                {"code": 212, "stringArgs": ["cmd:2"]},
                {
                    "code": 250,
                    "intArgs": [0, 1600000, 0, 0x51200, 1600005],
                    "stringArgs": ["", "Names", "", "Name"],
                },
                {"code": 101, "stringArgs": [r"\cself[5]"]},
                {"code": 213, "stringArgs": ["END"]},
            ],
        }),
        encoding="utf-8",
    )
    (common / "CallbackReader.json").write_text(
        json.dumps({
            "id": 1,
            "name": "CallbackReader",
            "activation": {"raw": 0x1E848023, "extra": [0] * 7},
            "commands": [
                {
                    "code": 250,
                    "intArgs": [0, 0, 0, 0x51000, 1600000],
                        "stringArgs": ["", "Buttons bk", "", "実行コモン"],
                },
                {
                    "code": 300,
                    "intArgs": [0, 0x100],
                    "stringArgs": [r"\cself[0]"],
                },
            ],
        }),
        encoding="utf-8",
    )

    usage = wolf._analyze_json_usage(str(json_root))

    assert (
        "common/Dispatcher.json", ("commands", 6)
    ) in usage["reachable_command_paths"]
    assert (
        "common/Dispatcher.json", ("commands", 3)
    ) not in usage["reachable_command_paths"]
    assert ("database.json", 0, 0) in usage["display_database_fields"]


def test_wolf_forwarded_dispatch_argument_keeps_nested_context(tmp_path):
    json_root = tmp_path / "json"
    common = json_root / "common"
    maps = json_root / "maps"
    common.mkdir(parents=True)
    maps.mkdir()
    (common / "Callee.json").write_text(
        json.dumps({
            "id": 2,
            "name": "Callee",
            "activation": {"raw": 0x1E848020, "extra": [0] * 7},
            "commands": [
                {"code": 122, "intArgs": [3000001, 0], "stringArgs": [r"cmd:\cself[0]"]},
                {"code": 213, "stringArgs": [r"\s[1]"]},
                {"code": 212, "stringArgs": ["cmd:1"]},
                {"code": 101, "stringArgs": [r"\cself[5]"]},
                {"code": 213, "stringArgs": ["END"]},
                {"code": 212, "stringArgs": ["cmd:2"]},
                {"code": 112, "stringArgs": [r"\cself[5]"]},
                {"code": 213, "stringArgs": ["END"]},
            ],
        }),
        encoding="utf-8",
    )
    caller_path = common / "Caller.json"
    caller_path.write_text(
        json.dumps({
            "id": 1,
            "name": "Caller",
            "activation": {"raw": 0x1E848020, "extra": [0] * 7},
            "commands": [
                {"code": 122, "intArgs": [3000001, 0], "stringArgs": [r"cmd:\cself[0]"]},
                {"code": 213, "stringArgs": [r"\s[1]"]},
                {"code": 212, "stringArgs": ["cmd:1"]},
                {
                    "code": 210,
                    "intArgs": [500002, 0x1011, 1600000, 0],
                    "stringArgs": ["", "Visible"],
                },
                {"code": 213, "stringArgs": ["END"]},
                {"code": 212, "stringArgs": ["cmd:2"]},
                {"code": 172},
                {"code": 213, "stringArgs": ["END"]},
            ],
        }),
        encoding="utf-8",
    )
    (maps / "Map001.json").write_text(
        json.dumps({
            "events": [{"pages": [{"list": [{
                "code": 210,
                "intArgs": [500001, 0x0001, 1],
                "stringArgs": [""],
            }]}]}],
        }),
        encoding="utf-8",
    )

    usage = wolf._analyze_json_usage(str(json_root), {"Map001.json"})
    entries = wolf._json_entries(str(json_root), str(caller_path), usage)
    visible = next(metadata for metadata, text in entries if text == "Visible")

    assert ("common/Callee.json", ("commands", 3)) in usage["reachable_command_paths"]
    assert ("common/Callee.json", ("commands", 6)) not in usage["reachable_command_paths"]
    assert visible["marker"] == "WOLFText"


def test_wolf_forwarded_dispatch_uses_reassigned_parent_argument():
    target = {
        "id": 2,
        "dispatch_slot": 0,
        "dispatch_contexts": {1: [], 2: []},
    }
    commands = [
        {"code": 121, "intArgs": [1600000, 2, 0, 0]},
        {"code": 210, "intArgs": [500002, 0x0001, 1600000], "stringArgs": [""]},
    ]
    numeric_values = wolf._numeric_values_by_command(
        commands, [(1,), ()], {1600000: 1}
    )
    call = wolf._decode_common_call(commands[1], {2: target}, {})

    assert numeric_values[1] == {1600000: 2}
    assert wolf._common_call_role_key(call, numeric_values[1]) == (2, 2)

    database_write = [
        {"code": 250, "intArgs": [0, 0, 0, 0x51200, 1600000]},
        commands[1],
    ]
    killed_values = wolf._numeric_values_by_command(
        database_write, [(1,), ()], {1600000: 1}
    )
    assert killed_values[1] == {}
    assert wolf._common_call_role_key(call, killed_values[1]) == (2, None)


def test_wolf_numeric_dataflow_tracks_fresh_direct_assignment():
    commands = [
        {"code": 121, "intArgs": [1600001, 2, 0, 0]},
        {"code": 210, "intArgs": [500002, 0x0001, 1600001], "stringArgs": [""]},
    ]

    values = wolf._numeric_values_by_command(commands, [(1,), ()])

    assert values[1] == {1600001: 2}


def test_wolf_empty_string_trace_is_safe():
    assert wolf._trace_string_variable_usage(
        [], None, 1600000, {}, {}, {}, {}, []
    ) == {
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


def test_wolf_database_display_flow_crosses_exact_database_write(tmp_path):
    databases = tmp_path / "databases"
    common = tmp_path / "common"
    databases.mkdir()
    common.mkdir()
    source_path = databases / "DataBase.json"
    source_path.write_text(
        json.dumps({"types": [{
            "name": "Characters",
            "fields": [{"name": "Name"}],
            "data": [{"data": [{"name": "Name", "value": "Will"}]}],
        }]}),
        encoding="utf-8",
    )
    (databases / "CDataBase.json").write_text(
        json.dumps({"types": [{
            "name": "Log",
            "fields": [{"name": "Name"}],
            "data": [{"data": [{"name": "Name", "value": ""}]}],
        }]}),
        encoding="utf-8",
    )
    (common / "Read.json").write_text(
        json.dumps({
            "id": 1,
            "activation": {"raw": 0x1E848023, "extra": [0] * 7},
            "commands": [
                {
                    "code": 250,
                    "intArgs": [0, 0, 0, 0x51200, 1600000],
                    "stringArgs": ["", "Characters", "", "Name"],
                },
                {
                    "code": 210,
                    "intArgs": [500002, 0x10, 1600000],
                    "stringArgs": [""],
                },
            ],
        }),
        encoding="utf-8",
    )
    (common / "Write.json").write_text(
        json.dumps({
            "id": 2,
            "activation": {"raw": 0x1E848020, "extra": [0] * 7},
            "commands": [{
                "code": 250,
                "intArgs": [0, 0, 0, 0x50000, 1600005],
                "stringArgs": ["", "Log", "", "Name"],
            }],
        }),
        encoding="utf-8",
    )
    (common / "Display.json").write_text(
        json.dumps({
            "id": 3,
            "activation": {"raw": 0x1E848023, "extra": [0] * 7},
            "commands": [
                {
                    "code": 250,
                    "intArgs": [0, 0, 0, 0x51000, 1600000],
                    "stringArgs": ["", "Log", "", "Name"],
                },
                {"code": 101, "stringArgs": [r"\cself[0]"]},
            ],
        }),
        encoding="utf-8",
    )

    usage = wolf._analyze_json_usage(str(tmp_path))
    entries = wolf._json_entries(str(tmp_path), str(source_path), usage)

    assert ("database.json", 0, 0, 0) in usage["display_database_records"]
    assert next(metadata["marker"] for metadata, text in entries if text == "Will") == "WOLFText"


def test_wolf_cfg_stops_specializing_after_dispatch_argument_write():
    commands = [
        {
            "index": 0,
            "indent": 0,
            "code": 122,
            "intArgs": [3000001, 0],
            "stringArgs": [r"cmd:\cself[0]"],
        },
        {"index": 1, "indent": 0, "code": 213, "stringArgs": [r"\s[1]"]},
        {"index": 2, "indent": 0, "code": 212, "stringArgs": ["cmd:500"]},
        {"index": 3, "indent": 0, "code": 121, "intArgs": [1600000, 1600001, 1, 0]},
        {
            "index": 4,
            "indent": 0,
            "code": 122,
            "intArgs": [3000001, 0],
            "stringArgs": [r"cmd:\cself[0]"],
        },
        {"index": 5, "indent": 0, "code": 213, "stringArgs": [r"\s[1]"]},
        {"index": 6, "indent": 0, "code": 212, "stringArgs": ["cmd:501"]},
        {"index": 7, "indent": 0, "code": 213, "stringArgs": ["END"]},
    ]

    successors = wolf._command_successors(commands, {0: 500})

    assert successors[1] == (2,)
    assert successors[5] == (2, 6)


def test_wolf_cfg_specializes_dispatch_after_known_numeric_branch():
    commands = [
        {
            "index": 0,
            "indent": 0,
            "code": 111,
            "intArgs": [1, 1600000, 0, 2],
        },
        {"index": 1, "indent": 0, "code": 401, "intArgs": [1]},
        {
            "index": 2,
            "indent": 1,
            "code": 121,
            "intArgs": [1600000, 3, 0, 0],
        },
        {"index": 3, "indent": 1, "code": 0},
        {"index": 4, "indent": 0, "code": 499},
        {
            "index": 5,
            "indent": 0,
            "code": 122,
            "intArgs": [3000001, 0],
            "stringArgs": [r"cmd:\cself[0]"],
        },
        {"index": 6, "indent": 0, "code": 213, "stringArgs": [r"\s[1]"]},
        {"index": 7, "indent": 0, "code": 212, "stringArgs": ["cmd:3"]},
        {"index": 8, "indent": 0, "code": 213, "stringArgs": ["END"]},
        {"index": 9, "indent": 0, "code": 212, "stringArgs": ["cmd:52"]},
        {"index": 10, "indent": 0, "code": 213, "stringArgs": ["END"]},
    ]

    unchanged = wolf._command_successors(commands, {0: 52})
    normalized = wolf._command_successors(commands, {0: 2})

    assert unchanged[0] == (5,)
    assert unchanged[6] == (9,)
    assert normalized[0] == (1,)
    assert normalized[6] == (7,)


def test_wolf_database_type_reference_does_not_widen_exact_target_callbacks(tmp_path):
    json_root = tmp_path / "json"
    databases = json_root / "databases"
    common = json_root / "common"
    databases.mkdir(parents=True)
    common.mkdir()
    (databases / "DataBase.json").write_text(
        json.dumps({
            "types": [{
                "name": "◆[rb]TargetDefs",
                "fields": [{"name": "実行コモン"}, {"name": "引数1"}],
                "data": [{
                    "data": [
                        {"name": "実行コモン", "value": "Dispatcher"},
                        {"name": "引数1", "value": 2},
                    ],
                }],
            }],
        }),
        encoding="utf-8",
    )
    (databases / "CDataBase.json").write_text(
        json.dumps({
            "types": [{
                "name": "Links",
                "fields": [{"name": "実行コモン"}],
                "data": [{
                    "data": [{
                        "name": "実行コモン",
                        "value": "（UDB TargetDefsにて定義）",
                    }],
                }],
            }],
        }),
        encoding="utf-8",
    )
    (common / "Dispatcher.json").write_text(
        json.dumps({
            "id": 13,
            "name": "Dispatcher",
            "activation": {"raw": 0x1E848020, "extra": [0] * 7},
            "commands": [
                {"code": 122, "intArgs": [3000001, 0], "stringArgs": [r"cmd:\cself[0]"]},
                {"code": 213, "stringArgs": [r"\s[1]"]},
                {"code": 212, "stringArgs": ["cmd:1"]},
                {"code": 112, "stringArgs": ["logic"]},
                {"code": 213, "stringArgs": ["END"]},
                {"code": 212, "stringArgs": ["cmd:2"]},
                {"code": 101, "stringArgs": ["visible"]},
                {"code": 213, "stringArgs": ["END"]},
            ],
        }),
        encoding="utf-8",
    )

    usage = wolf._analyze_json_usage(str(json_root))

    assert not usage["reachable_command_paths"]
    assert not usage["unresolved_database_callbacks"]


def test_wolf_unreferenced_common_does_not_pollute_database_role(tmp_path):
    json_root = tmp_path / "json"
    databases = json_root / "databases"
    common = json_root / "common"
    databases.mkdir(parents=True)
    common.mkdir()
    (databases / "DataBase.json").write_text(
        json.dumps({
            "types": [{
                "name": "Labels",
                "fields": [{"name": "Text"}],
                "data": [{"data": [{"name": "Text", "value": "Visible"}]}],
            }],
        }),
        encoding="utf-8",
    )
    read = {
        "code": 250,
        "intArgs": [0, 0, 0, 0x51200, 1600000],
        "stringArgs": ["", "Labels", "", "Text"],
    }
    (common / "Production.json").write_text(
        json.dumps({
            "id": 1,
            "name": "Production",
            "activation": {"raw": 0x1E848023, "extra": [0] * 7},
            "commands": [read, {"code": 101, "stringArgs": [r"\cself[0]"]}],
        }),
        encoding="utf-8",
    )
    (common / "Uncalled.json").write_text(
        json.dumps({
            "id": 2,
            "name": "Uncalled",
            "activation": {"raw": 0x1E848020, "extra": [0] * 7},
            "commands": [read, {"code": 112, "stringArgs": [r"\cself[0]"]}],
        }),
        encoding="utf-8",
    )

    usage = wolf._analyze_json_usage(str(json_root))
    key = ("database.json", 0, 0)
    record_key = ("database.json", 0, 0, 0)

    assert record_key in usage["display_database_records"]
    assert key not in usage["logic_database_fields"]
    assert record_key not in usage["logic_database_records"]
    assert "common/Uncalled.json" not in usage["active_common_event_files"]


def test_wolf_analysis_ignores_calls_from_unreferenced_maps(tmp_path):
    json_root = tmp_path / "json"
    common = json_root / "common"
    maps = json_root / "maps"
    common.mkdir(parents=True)
    maps.mkdir()
    (common / "Dispatcher.json").write_text(
        json.dumps({
            "id": 13,
            "name": "Dispatcher",
            "activation": {"raw": 0x1E848020, "extra": [0] * 7},
            "commands": [
                {"code": 122, "intArgs": [3000001, 0], "stringArgs": [r"cmd:\cself[0]"]},
                {"code": 213, "stringArgs": [r"\s[1]"]},
                {"code": 212, "stringArgs": ["cmd:1"]},
                {"code": 112, "stringArgs": ["logic"]},
                {"code": 213, "stringArgs": ["END"]},
                {"code": 212, "stringArgs": ["cmd:2"]},
                {"code": 101, "stringArgs": ["visible"]},
                {"code": 213, "stringArgs": ["END"]},
            ],
        }),
        encoding="utf-8",
    )
    for filename, context in (("Live.json", 2), ("Sample.json", 1)):
        (maps / filename).write_text(
            json.dumps({
                "events": [{"pages": [{"list": [{
                    "code": 210,
                    "intArgs": [500013, 0x0001, context],
                    "stringArgs": [""],
                }]}]}],
            }),
            encoding="utf-8",
        )

    usage = wolf._analyze_json_usage(str(json_root), {"Live.json"})

    assert ("common/Dispatcher.json", ("commands", 6)) in usage["reachable_command_paths"]
    assert ("common/Dispatcher.json", ("commands", 3)) not in usage["reachable_command_paths"]


def test_wolf_literal_call_argument_follows_return_output_logic(tmp_path):
    json_root = tmp_path / "json"
    common = json_root / "common"
    common.mkdir(parents=True)
    callee_path = common / "Callee.json"
    caller_path = common / "Caller.json"
    callee_path.write_text(
        json.dumps({
            "id": 30,
            "name": "Callee",
            "commands": [
                {"code": 101, "stringArgs": [r"\cself[5]"]},
                {"code": 213, "stringArgs": ["END"]},
            ],
        }),
        encoding="utf-8",
    )
    caller_path.write_text(
        json.dumps({
            "id": 40,
            "name": "Caller",
            "commands": [
                {
                    "code": 210,
                    "intArgs": [500030, 0x1010, 0],
                    "stringArgs": ["", "Visible"],
                },
                {
                    "code": 210,
                    "intArgs": [500030, 0x01001010, 0, 3000001],
                    "stringArgs": ["", "Label"],
                },
                {"code": 112, "intArgs": [0, 3000001], "stringArgs": ["used"]},
            ],
        }),
        encoding="utf-8",
    )

    usage = wolf._analyze_json_usage(str(json_root))
    entries = wolf._json_entries(str(json_root), str(caller_path), usage)
    label = next(metadata for metadata, text in entries if text == "Label")
    visible = next(metadata for metadata, text in entries if text == "Visible")

    assert usage["common_event_roles_by_id"][30][5] >= {"display", "return"}
    assert usage["common_event_return_roles_by_id"][30] == {"logic"}
    assert label["marker"] == "WOLFLogic"
    assert visible["marker"] == "WOLFText"


def test_wolf_cfg_propagates_alias_through_conditional_exit_and_common_call(tmp_path):
    json_root = tmp_path / "json"
    database_path = json_root / "databases" / "DataBase.json"
    common_path = json_root / "common" / "Display.json"
    database_path.parent.mkdir(parents=True)
    common_path.parent.mkdir()
    database_path.write_text(
        json.dumps({
            "types": [{
                "name": "Characters",
                "fields": [{"name": "Name"}],
                "data": [{
                    "name": "entry",
                    "data": [{"name": "Name", "value": "Visible name"}],
                }],
            }],
        }),
        encoding="utf-8",
    )
    common_path.write_text(
        json.dumps({
            "id": 1,
            "name": "Display",
            "commands": [
                {
                    "code": 250,
                    "indent": 0,
                    "intArgs": [0, 0, 0, 0x51200, 1600007],
                    "stringArgs": ["", "Characters", "", "Name"],
                },
                {"code": 112, "indent": 0, "intArgs": [1, 1600005], "stringArgs": ["", "", "", ""]},
                {"code": 401, "indent": 0, "intArgs": [1]},
                {"code": 213, "indent": 1, "stringArgs": ["END"]},
                {"code": 0, "indent": 1},
                {"code": 499, "indent": 0},
                {"code": 122, "indent": 0, "intArgs": [1600008, 2561, 1600007]},
                {
                    "code": 300,
                    "indent": 0,
                    "intArgs": [0, 0, 0, 0, 0, 0, 1600008, 0, 1600008],
                    "stringArgs": ["Formatter"],
                },
                {"code": 150, "indent": 0, "intArgs": [32], "stringArgs": [r"\cself[8]"]},
            ],
        }),
        encoding="utf-8",
    )

    usage = wolf._analyze_json_usage(str(json_root))

    assert ("database.json", 0, 0, 0) in usage["display_database_records"]
    assert ("database.json", 0, 0, 0) not in usage["logic_database_records"]


def test_wolf_display_schema_does_not_override_opaque_callback(tmp_path):
    json_root = tmp_path / "json"
    database_path = json_root / "databases" / "DataBase.json"
    common_path = json_root / "common" / "Glossary.json"
    database_path.parent.mkdir(parents=True)
    common_path.parent.mkdir()
    database_path.write_text(
        json.dumps({
            "types": [{
                "name": "Glossary",
                "fields": [{"name": "Name"}, {"name": "Description"}],
                "data": [{
                    "name": "",
                    "data": [
                        {"name": "Name", "value": "Term"},
                        {"name": "Description", "value": "Visible explanation."},
                    ],
                }],
            }],
        }),
        encoding="utf-8",
    )
    common_path.write_text(
        json.dumps({
            "id": 1,
            "name": "Glossary",
            "commands": [
                {
                    "code": 250,
                    "intArgs": [0, 0, 0, 0x51200, 1600007],
                    "stringArgs": ["", "Glossary", "", "Name"],
                },
                {"code": 210, "intArgs": [600099, 0x10, 1600007]},
                {
                    "code": 250,
                    "intArgs": [0, 0, 1, 0x51200, 1600008],
                    "stringArgs": ["", "Glossary", "", "Description"],
                },
                {"code": 210, "intArgs": [600099, 0x10, 1600008]},
            ],
        }),
        encoding="utf-8",
    )

    usage = wolf._analyze_json_usage(str(json_root))
    entries = wolf._json_entries(str(json_root), str(database_path), usage)
    roles = {text: metadata["marker"] for metadata, text in entries}

    assert roles["Term"] == "WOLFLogic"
    assert roles["Visible explanation."] == "WOLFLogic"


def test_wolf_database_count_query_is_not_a_string_field_read(tmp_path):
    json_root = tmp_path / "json"
    database_path = json_root / "databases" / "DataBase.json"
    common_path = json_root / "common" / "Count.json"
    database_path.parent.mkdir(parents=True)
    common_path.parent.mkdir()
    database_path.write_text(
        json.dumps({
            "types": [{
                "name": "Credits",
                "fields": [{"name": "Text"}],
                "data": [{"name": "row", "data": [{"name": "Text", "value": "Credit"}]}],
            }],
        }),
        encoding="utf-8",
    )
    common_path.write_text(
        json.dumps({
            "id": 1,
            "name": "Count",
            "commands": [
                {
                    "code": 250,
                    "intArgs": [0, 0xFFFFFFFF, 0, 0x11200, 1600081],
                    "stringArgs": ["", "Credits", "", ""],
                },
                {"code": 150, "intArgs": [32], "stringArgs": [r"\cself[81]"]},
            ],
        }),
        encoding="utf-8",
    )

    usage = wolf._analyze_json_usage(str(json_root))

    assert ("database.json", 0, 0) not in usage["visible_database_fields"]
    assert ("database.json", 0, 0, 0) not in usage["visible_database_records"]


def test_wolf_same_path_code300_inout_keeps_database_value_visible(tmp_path):
    json_root = tmp_path / "json"
    database_path = json_root / "databases" / "DataBase.json"
    common_path = json_root / "common" / "Display.json"
    database_path.parent.mkdir(parents=True)
    common_path.parent.mkdir(parents=True)
    database_path.write_text(
        json.dumps({
            "types": [{
                "name": "Labels",
                "fields": [{"name": "Text"}],
                "data": [{
                    "name": "entry",
                    "data": [{"name": "Text", "value": "Visible label"}],
                }],
            }],
        }),
        encoding="utf-8",
    )
    common_path.write_text(
        json.dumps({
            "id": 1,
            "name": "Caller",
            "commands": [
                {
                    "code": 250,
                    "intArgs": [0, 0, 0, 0x51200, 1600009],
                    "stringArgs": ["", "Labels", "", "Text"],
                },
                {
                    "code": 300,
                    "intArgs": [0, 16785444, 151, 2, 0, 0, 1600009, 0, 1600009],
                    "stringArgs": ["◆[rb]システム管理", "", "H5"],
                },
                {"code": 150, "intArgs": [32], "stringArgs": [r"\cself[9]"]},
            ],
        }),
        encoding="utf-8",
    )
    (common_path.parent / "Formatter.json").write_text(
        json.dumps({
            "id": 8,
            "name": "◆[rb]システム管理",
            "arguments": ["", "", "", "", "", "source", "style", "", "", ""],
            "commands": [{"code": 172}],
        }),
        encoding="utf-8",
    )

    usage = wolf._analyze_json_usage(str(json_root))

    assert ("database.json", 0, 0, 0) in usage["visible_database_records"]
    assert ("database.json", 0, 0, 0) in usage["display_database_records"]


def test_wolf_identifier_completeness_is_not_inherited_from_another_record(tmp_path):
    json_root = tmp_path / "json"
    database_path = json_root / "databases" / "DataBase.json"
    common_path = json_root / "common" / "Lookup.json"
    database_path.parent.mkdir(parents=True)
    common_path.parent.mkdir(parents=True)
    database_path.write_text(
        json.dumps({
            "types": [{
                "name": "Labels",
                "fields": [{"name": "Name"}],
                "data": [
                    {"name": "", "data": [{"name": "Name", "value": "Alpha"}]},
                    {"name": "", "data": [{"name": "Name", "value": "Beta"}]},
                ],
            }],
        }),
        encoding="utf-8",
    )
    common_path.write_text(
        json.dumps({
            "commands": [
                {
                    "code": 250,
                    "intArgs": [0, 0, 0, 0x51200, 1600000],
                    "stringArgs": ["", "Labels", "", "Name"],
                },
                {"code": 150, "intArgs": [32], "stringArgs": [r"\cself[0]"]},
                {
                    "code": 250,
                    "intArgs": [0, 0, 0, 0x71200, 1600001],
                    "stringArgs": ["", "Labels", "Alpha", "Name"],
                },
            ],
        }),
        encoding="utf-8",
    )

    usage = wolf._analyze_json_usage(str(json_root))
    entries = wolf._json_entries(str(json_root), str(database_path), usage)
    beta = next(metadata for metadata, text in entries if text == "Beta")

    assert usage["identifier_reference_paths"].get(("database.json", 0, "Beta"), set()) == set()
    assert beta["marker"] == "WOLFLogic"
    assert beta["identifier_reference_complete"] is False


def test_wolf_symbol_references_are_synchronized_without_touching_display_aliases(tmp_path):
    original = tmp_path / "original"
    databases = original / "databases"
    common = original / "common"
    databases.mkdir(parents=True)
    common.mkdir()
    database_path = databases / "DataBase.json"
    database_path.write_text(
        json.dumps({
            "types": [
                {
                    "name": "Symbols",
                    "fields": [{"name": "Name"}],
                    "data": [
                        {"name": "", "data": [{"name": "Name", "value": "Alpha"}]},
                        {"name": "", "data": [{"name": "Name", "value": "Beta"}]},
                    ],
                },
                {
                    "name": "Links",
                    "fields": [{"name": "NextSymbol"}],
                    "data": [{
                        "name": "link",
                        "data": [{"name": "NextSymbol", "value": "<NEXT>|Alpha"}],
                    }],
                },
                {
                    "name": "Labels",
                    "fields": [{"name": "Text"}],
                    "data": [{
                        "name": "label",
                        "data": [{"name": "Text", "value": "Alpha"}],
                    }],
                },
            ],
        }),
        encoding="utf-8",
    )
    (common / "Use.json").write_text(
        json.dumps({
            "commands": [
                {
                    "code": 250,
                    "intArgs": [0, 1600073, 0, 0x51200, 1600000],
                    "stringArgs": ["", "Symbols", "", "Name"],
                },
                {"code": 150, "intArgs": [32], "stringArgs": [r"\cself[0]"]},
                {
                    "code": 250,
                    "intArgs": [0, 0, 0, 0x71200, 1600001],
                    "stringArgs": ["", "Symbols", "Alpha", "Name"],
                },
                {
                    "code": 250,
                    "intArgs": [1, 0, 0, 0x51200, 1600003],
                    "stringArgs": ["", "Links", "", "NextSymbol"],
                },
                {
                    "code": 250,
                    "intArgs": [0, 1600003, 0, 0x51200, 1600004],
                    "stringArgs": ["", "Symbols", "", "Name"],
                },
                {
                    "code": 250,
                    "intArgs": [2, 0, 0, 0x51200, 1600002],
                    "stringArgs": ["", "Labels", "", "Text"],
                },
                {"code": 150, "intArgs": [32], "stringArgs": [r"\cself[2]"]},
            ],
        }),
        encoding="utf-8",
    )

    usage = wolf._analyze_json_usage(str(original))
    entries = wolf._json_entries(str(original), str(database_path), usage)
    alpha = next(
        metadata
        for metadata, text in entries
        if text == "Alpha" and metadata.get("identifier_record") == ["database.json", 0, 0]
    )
    roles = {
        tuple(metadata["path"]): metadata["marker"]
        for metadata, _text in entries
    }
    link_path = ("types", 1, "data", 0, "data", 0, "value")
    label_path = ("types", 2, "data", 0, "data", 0, "value")

    assert roles[link_path] == "WOLFLogic"
    assert roles[label_path] == "WOLFText"
    assert any(
        reference["file"].casefold() == "databases/database.json"
        and tuple(reference["path"]) == link_path
        for reference in alpha["identifier_references"]
    )

    patched = tmp_path / "patched"
    shutil.copytree(original, patched)
    patched_database = json.loads(
        (patched / "databases" / "DataBase.json").read_text(encoding="utf-8")
    )
    patched_database["types"][0]["data"][0]["data"][0]["value"] = "甲"
    (patched / "databases" / "DataBase.json").write_text(
        json.dumps(patched_database), encoding="utf-8"
    )

    wolf._synchronize_database_identifiers(
        str(original),
        str(patched),
        [
            (
                ["database.json", 0],
                "Alpha",
                "甲",
                "name_referenced",
                alpha["identifier_references"],
                "name_closed",
                alpha["identifier_missing_names"],
            )
        ],
    )

    synchronized = json.loads(
        (patched / "databases" / "DataBase.json").read_text(encoding="utf-8")
    )
    synchronized_common = json.loads(
        (patched / "common" / "Use.json").read_text(encoding="utf-8")
    )
    assert synchronized["types"][1]["data"][0]["data"][0]["value"] == "<NEXT>|甲"
    assert synchronized["types"][2]["data"][0]["data"][0]["value"] == "Alpha"
    assert synchronized_common["commands"][2]["stringArgs"][2] == "甲"


def test_wolf_reference_like_field_name_cannot_authorize_identifier_sync(tmp_path):
    databases = tmp_path / "databases"
    common = tmp_path / "common"
    databases.mkdir()
    common.mkdir()
    database_path = databases / "DataBase.json"
    database_path.write_text(
        json.dumps({"types": [
            {
                "name": "Symbols",
                "fields": [{"name": "Name"}],
                "data": [{
                    "name": "",
                    "data": [{"name": "Name", "value": "Alpha"}],
                }],
            },
            {
                "name": "Links",
                "fields": [{"name": "NextSymbol"}],
                "data": [{
                    "name": "link",
                    "data": [{"name": "NextSymbol", "value": "Alpha"}],
                }],
            },
        ]}),
        encoding="utf-8",
    )
    (common / "Display.json").write_text(
        json.dumps({
            "id": 1,
            "activation": {"raw": 0x1E848023, "extra": [0] * 7},
            "commands": [
                {
                    "code": 250,
                    "intArgs": [0, 0, 0, 0x51200, 1600000],
                    "stringArgs": ["", "Symbols", "", "Name"],
                },
                {"code": 101, "stringArgs": [r"\cself[0]"]},
            ],
        }),
        encoding="utf-8",
    )

    usage = wolf._analyze_json_usage(str(tmp_path))
    entries = wolf._json_entries(str(tmp_path), str(database_path), usage)
    alpha = next(
        metadata
        for metadata, text in entries
        if text == "Alpha"
        and metadata.get("identifier_namespace") == ["database.json", 0]
    )

    assert alpha["marker"] == "WOLFText"
    assert alpha["identifier_translation_policy"] == "numeric_only"
    assert alpha["identifier_references"] == []
    assert ("database.json", 1, 0) not in usage["symbol_reference_database_fields"]


def test_wolf_cross_namespace_first_string_alias_fails_closed(tmp_path):
    original = tmp_path / "original"
    databases = original / "databases"
    common = original / "common"
    databases.mkdir(parents=True)
    common.mkdir()
    database_path = databases / "DataBase.json"
    database_path.write_text(
        json.dumps({
            "types": [
                {
                    "name": "Weapons",
                    "fields": [{"name": "Name"}],
                    "data": [{
                        "name": "",
                        "data": [{"name": "Name", "value": "ライフル"}],
                    }],
                },
                {
                    "name": "RuntimeAliases",
                    "fields": [{"name": "Name"}],
                    "data": [{
                        "name": "",
                        "data": [{"name": "Name", "value": "ライフル"}],
                    }],
                },
            ],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    (common / "Use.json").write_text(
        json.dumps({
            "commands": [
                {
                    "code": 250,
                    "intArgs": [0, 0, 0, 0x71200, 1600000],
                    "stringArgs": ["", "Weapons", "ライフル", "Name"],
                },
                {"code": 101, "stringArgs": [r"\cself[0]"]},
            ],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    usage = wolf._analyze_json_usage(str(original))
    entries = wolf._json_entries(str(original), str(database_path), usage)
    metadata = next(
        metadata
        for metadata, text in entries
        if text == "ライフル"
        and metadata.get("identifier_namespace") == ["database.json", 0]
    )

    assert ("database.json", 0, "ライフル") in usage["incomplete_identifier_symbols"]
    assert metadata["marker"] == "WOLFLogic"
    assert metadata["identifier_reference_complete"] is False
    assert any(
        item["reason"] == "ambiguous_database_identifier_alias"
        and item["effect"] == "protected"
        and item["candidate_namespaces"] == [
            ["database.json", 0],
            ["database.json", 1],
        ]
        for item in usage["analysis_diagnostics"]
    )

    patched = tmp_path / "patched"
    shutil.copytree(original, patched)
    patched_database = json.loads(
        (patched / "databases" / "DataBase.json").read_text(encoding="utf-8")
    )
    patched_database["types"][0]["data"][0]["data"][0]["value"] = "步枪"
    (patched / "databases" / "DataBase.json").write_text(
        json.dumps(patched_database, ensure_ascii=False), encoding="utf-8"
    )
    try:
        wolf._synchronize_database_identifiers(
            str(original),
            str(patched),
            [
                (
                    ["database.json", 0],
                    "ライフル",
                    "步枪",
                    "name_referenced",
                    metadata["identifier_references"],
                    "name_closed",
                    [],
                )
            ],
        )
    except wolf.WolfEngineError as error:
        assert "标识引用未闭合" in str(error)
    else:
        raise AssertionError("an unresolved cross-namespace alias must fail closed")


def test_wolf_unique_same_type_runtime_database_alias_is_synchronized(tmp_path):
    original = tmp_path / "original"
    databases = original / "databases"
    common = original / "common"
    databases.mkdir(parents=True)
    common.mkdir()
    database_path = databases / "DataBase.json"
    database_path.write_text(
        json.dumps({
            "types": [{
                "name": "Weapons",
                "fields": [{"name": "Name"}],
                "data": [{
                    "name": "",
                    "data": [{"name": "Name", "value": "装填レバー"}],
                }],
            }],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    (databases / "CDataBase.json").write_text(
        json.dumps({
            "types": [{
                "name": "RuntimeWeapons",
                "fields": [{"name": "Name"}],
                "data": [{
                    "name": "",
                    "data": [{"name": "Name", "value": ""}],
                }],
            }],
        }),
        encoding="utf-8",
    )
    (common / "Use.json").write_text(
        json.dumps({
            "commands": [
                {
                    "code": 250,
                    "intArgs": [0, 0, 0, 0x71200, 1600000],
                    "stringArgs": ["", "Weapons", "装填レバー", "Name"],
                },
                {"code": 101, "stringArgs": [r"\cself[0]"]},
                {
                    "code": 250,
                    "intArgs": [0, 0, 0, 0x71000, 1600001],
                    "stringArgs": ["", "RuntimeWeapons", "装填レバー", "Name"],
                },
            ],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    usage = wolf._analyze_json_usage(str(original))
    entries = wolf._json_entries(str(original), str(database_path), usage)
    metadata = next(metadata for metadata, text in entries if text == "装填レバー")

    assert ("database.json", 0, "装填レバー") not in usage["incomplete_identifier_symbols"]
    assert metadata["marker"] == "WOLFText"
    assert metadata["identifier_reference_complete"] is True
    assert [reference["reference_kind"] for reference in metadata["identifier_references"]] == [
        "database_selector",
        "runtime_database_alias",
    ]

    patched = tmp_path / "patched"
    shutil.copytree(original, patched)
    patched_database = json.loads(
        (patched / "databases" / "DataBase.json").read_text(encoding="utf-8")
    )
    patched_database["types"][0]["data"][0]["data"][0]["value"] = "装填杆"
    (patched / "databases" / "DataBase.json").write_text(
        json.dumps(patched_database, ensure_ascii=False), encoding="utf-8"
    )
    wolf._synchronize_database_identifiers(
        str(original),
        str(patched),
        [
            (
                ["database.json", 0],
                "装填レバー",
                "装填杆",
                "name_referenced",
                metadata["identifier_references"],
                "name_closed",
                metadata["identifier_missing_names"],
            )
        ],
    )
    patched_common = json.loads(
        (patched / "common" / "Use.json").read_text(encoding="utf-8")
    )
    assert patched_common["commands"][0]["stringArgs"][2] == "装填杆"
    assert patched_common["commands"][2]["stringArgs"][2] == "装填杆"


def test_wolf_runtime_database_alias_with_multiple_aligned_sources_stays_protected(tmp_path):
    databases = tmp_path / "databases"
    common = tmp_path / "common"
    databases.mkdir()
    common.mkdir()
    source_type = {
        "name": "Names",
        "fields": [{"name": "Name"}],
        "data": [{
            "name": "",
            "data": [{"name": "Name", "value": "Shared"}],
        }],
    }
    for filename in ("DataBase.json", "SysDatabase.json"):
        (databases / filename).write_text(
            json.dumps({"types": [source_type]}), encoding="utf-8"
        )
    (databases / "CDataBase.json").write_text(
        json.dumps({
            "types": [{
                "name": "RuntimeNames",
                "fields": [{"name": "Name"}],
                "data": [{
                    "name": "",
                    "data": [{"name": "Name", "value": ""}],
                }],
            }],
        }),
        encoding="utf-8",
    )
    (common / "Use.json").write_text(
        json.dumps({
            "commands": [
                {
                    "code": 250,
                    "intArgs": [0, 0, 0, 0x71200, 1600000],
                    "stringArgs": ["", "Names", "Shared", "Name"],
                },
                {"code": 101, "stringArgs": [r"\cself[0]"]},
                {
                    "code": 250,
                    "intArgs": [0, 0, 0, 0x71000, 1600001],
                    "stringArgs": ["", "RuntimeNames", "Shared", "Name"],
                },
            ],
        }),
        encoding="utf-8",
    )

    usage = wolf._analyze_json_usage(str(tmp_path))

    assert ("database.json", 0, "shared") in usage["incomplete_identifier_symbols"]
    assert ("sysdatabase.json", 0, "shared") in usage["incomplete_identifier_symbols"]
    assert any(
        item["reason"] == "unresolved_runtime_database_alias"
        for item in usage["analysis_diagnostics"]
    )


def test_wolf_closed_identifier_graph_synchronizes_database_references(tmp_path):
    original = tmp_path / "original"
    databases = original / "databases"
    common = original / "common"
    databases.mkdir(parents=True)
    common.mkdir()
    (databases / "DataBase.json").write_text(
        json.dumps({
            "types": [
                {
                    "name": "武装",
                    "fields": [{"name": "名称"}],
                    "data": [
                        {"name": "", "data": [{"name": "名称", "value": "A"}]},
                        {"name": "", "data": [{"name": "名称", "value": "B"}]},
                        {"name": "", "data": [{"name": "名称", "value": "A"}]},
                    ],
                },
                {
                    "name": "装备",
                    "fields": [{"name": "武装名"}],
                    "data": [{"name": "slot", "data": [{"name": "武装名", "value": "A"}]}],
                },
            ]
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    (common / "Lookup.json").write_text(
        json.dumps({
            "commands": [
                {
                    "code": 250,
                    "intArgs": [0, 0, 0, 0x51200, 1600000],
                    "stringArgs": ["", "武装", "", "名称"],
                },
                {"code": 150, "intArgs": [32], "stringArgs": [r"\cself[0]"]},
                {
                    "code": 250,
                    "intArgs": [0, 0, 0, 0x71200, 1600001],
                    "stringArgs": ["", "武装", "a", "名称"],
                },
                {"code": 101, "stringArgs": ["A"]},
            ]
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    usage = wolf._analyze_json_usage(str(original))
    entries = wolf._json_entries(
        str(original), str(databases / "DataBase.json"), usage
    )
    metadata = next(metadata for metadata, text in entries if text == "A")

    assert usage["database_identifier_translation_policy"][("database.json", 0)] == "name_closed"
    assert metadata["marker"] == "WOLFText"
    assert metadata["identifier_reference_complete"] is True
    assert metadata["identifier_references"] == [{
        "file": "common/Lookup.json",
        "path": ["commands", 2, "stringArgs", 2],
        "target_data_indexes": [0, 2],
        "expected_original": "a",
        "reference_kind": "database_selector",
    }]

    patched = tmp_path / "patched"
    shutil.copytree(original, patched)
    patched_database = json.loads(
        (patched / "databases" / "DataBase.json").read_text(encoding="utf-8")
    )
    patched_database["types"][0]["data"][0]["data"][0]["value"] = "甲"
    patched_database["types"][0]["data"][2]["data"][0]["value"] = "甲"
    (patched / "databases" / "DataBase.json").write_text(
        json.dumps(patched_database, ensure_ascii=False), encoding="utf-8"
    )

    references = metadata["identifier_references"]
    allowed = wolf._synchronize_database_identifiers(
        str(original),
        str(patched),
        [(["database.json", 0], "A", "甲", "name_referenced", references, "name_closed", [])],
    )

    synchronized = json.loads(
        (patched / "databases" / "DataBase.json").read_text(encoding="utf-8")
    )
    synchronized_common = json.loads(
        (patched / "common" / "Lookup.json").read_text(encoding="utf-8")
    )
    assert synchronized["types"][1]["data"][0]["data"][0]["value"] == "A"
    assert synchronized_common["commands"][2]["stringArgs"][2] == "甲"
    assert synchronized_common["commands"][3]["stringArgs"][0] == "A"
    assert (
        "common/Lookup.json",
        ("commands", 2, "stringArgs", 2),
    ) in allowed
    wolf._verify_logic_json_unchanged(str(original), str(patched), allowed)

    incomplete = tmp_path / "incomplete"
    shutil.copytree(original, incomplete)
    incomplete_database = json.loads(
        (incomplete / "databases" / "DataBase.json").read_text(encoding="utf-8")
    )
    incomplete_database["types"][0]["data"][0]["data"][0]["value"] = "乙"
    (incomplete / "databases" / "DataBase.json").write_text(
        json.dumps(incomplete_database, ensure_ascii=False), encoding="utf-8"
    )
    try:
        wolf._synchronize_database_identifiers(
            str(original),
            str(incomplete),
            [(["database.json", 0], "A", "甲", "name_referenced", [], "name_closed", [])],
        )
    except wolf.WolfEngineError as error:
        assert "名称解析目标改变" in str(error)
    else:
        raise AssertionError("a changed identifier target must fail closed")


def test_wolf_identifier_collision_policy_follows_record_selector_sources(tmp_path):
    json_root = tmp_path / "json"
    databases = json_root / "databases"
    common = json_root / "common"
    databases.mkdir(parents=True)
    common.mkdir()
    (databases / "DataBase.json").write_text(
        json.dumps({
            "types": [
                {"name": "数字", "data": [{"name": "", "data": [{"name": "名称", "value": "A"}]}]},
                {"name": "名称", "data": [{"name": "", "data": [{"name": "名称", "value": "B"}]}]},
                {"name": "不明", "data": [{"name": "", "data": [{"name": "名称", "value": "C"}]}]},
            ]
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    (common / "Selectors.json").write_text(
        json.dumps({
            "commands": [
                {"code": 121, "intArgs": [1600001, 2, 0, 0]},
                {
                    "code": 250,
                    "intArgs": [0, 1600001, 0, 0x51200, 1600002],
                    "stringArgs": ["", "数字", "", "名称"],
                },
                {
                    "code": 250,
                    "intArgs": [1, 0, 0, 0x71200, 1600003],
                    "stringArgs": ["", "名称", "B", "名称"],
                },
            ]
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    usage = wolf._analyze_json_usage(str(json_root))

    assert wolf._database_identifier_collision_policy(
        usage, ("database.json", 0)
    ) == "numeric_only"
    assert wolf._database_identifier_collision_policy(
        usage, ("database.json", 1)
    ) == "name_referenced"
    assert wolf._database_identifier_collision_policy(
        usage, ("database.json", 2)
    ) == "unknown"
    assert usage["database_identifier_translation_policy"] == {
        ("database.json", 0): "numeric_only",
        ("database.json", 1): "name_closed",
        ("database.json", 2): "unsafe",
    }


def test_wolf_identifier_references_include_cfg_unreachable_commands(tmp_path):
    databases = tmp_path / "databases"
    common = tmp_path / "common"
    databases.mkdir()
    common.mkdir()
    (databases / "DataBase.json").write_text(
        json.dumps({
            "types": [{
                "name": "Names",
                "data": [{
                    "name": "",
                    "data": [{"name": "Name", "value": "Alpha"}],
                }],
            }],
        }),
        encoding="utf-8",
    )
    (common / "Selector.json").write_text(
        json.dumps({
            "id": 1,
            "name": "Selector",
            "activation": {"raw": 0x1E848023, "extra": [0] * 7},
            "commands": [
                {"code": 213, "stringArgs": ["END"]},
                {
                    "code": 250,
                    "intArgs": [1, 0, 0, 0x71200, 1600000],
                    "stringArgs": ["", "Names", "Alpha", "Name"],
                },
            ],
        }),
        encoding="utf-8",
    )

    usage = wolf._analyze_json_usage(str(tmp_path))
    key = ("database.json", 0, "alpha")

    assert ("common/Selector.json", ("commands", 1)) not in usage["reachable_command_paths"]
    assert usage["identifier_reference_paths"][key] == {
        (
            "common/Selector.json",
            ("commands", 1, "stringArgs", 2),
            (0,),
            "Alpha",
            "database_selector",
        )
    }


def test_wolf_missing_name_lookup_guards_only_that_future_name(tmp_path):
    original = tmp_path / "original"
    patched = tmp_path / "patched"
    databases = original / "databases"
    databases.mkdir(parents=True)
    (databases / "DataBase.json").write_text(
        json.dumps({"types": [{
            "name": "Names",
            "fields": [{"name": "Name"}],
            "data": [{"name": "", "data": [{"name": "Name", "value": "A"}]}],
        }]}),
        encoding="utf-8",
    )
    shutil.copytree(original, patched)
    patched_data = json.loads(
        (patched / "databases" / "DataBase.json").read_text(encoding="utf-8")
    )
    patched_data["types"][0]["data"][0]["data"][0]["value"] = "Ghost"
    (patched / "databases" / "DataBase.json").write_text(
        json.dumps(patched_data), encoding="utf-8"
    )

    try:
        wolf._synchronize_database_identifiers(
            str(original),
            str(patched),
            [(["database.json", 0], "A", "Ghost", "numeric_only", [], "numeric_only", ["ghost"])],
        )
    except wolf.WolfEngineError as error:
        assert "激活原本不存在的名称" in str(error)
    else:
        raise AssertionError("a translated identifier must not capture a missing lookup")


def test_wolf_database_instruction_flags_define_selector_and_direction():
    schemas = {
        "database.json": {
            "types": {"武装": 0},
            "fields": {0: {"名称": 0}},
        }
    }
    identifiers = {("database.json", 0): {"a": {0}, r"\cself[5]": {1}}}
    numeric_read = {
        "code": 250,
        "intArgs": [0, 1600001, 0, 0x51200, 1600002],
        "stringArgs": ["", "武装", "", "名称"],
    }
    name_read = {
        "code": 250,
        "intArgs": [0, 0, 0, 0x71200, 1600002],
        "stringArgs": ["", "武装", "A", "名称"],
    }
    literal_control_name = {
        **name_read,
        "stringArgs": ["", "武装", r"\cself[5]", "名称"],
    }
    database_write = {
        "code": 250,
        "intArgs": [0, 0, 0, 0x50200, 1600002],
        "stringArgs": ["", "武装", "", "名称"],
    }

    assert wolf._database_read_descriptor(numeric_read, schemas) == (
        ("database.json", 0, 0),
        1600002,
    )
    assert wolf._record_selector_access(numeric_read, schemas, identifiers) == (
        ("database.json", 0),
        "numeric",
        None,
    )
    assert wolf._record_selector_access(name_read, schemas, identifiers) == (
        ("database.json", 0),
        "name",
        (0,),
    )
    assert wolf._record_selector_access(literal_control_name, schemas, identifiers) == (
        ("database.json", 0),
        "name",
        (1,),
    )
    assert wolf._database_read_descriptor(database_write, schemas) is None
    assert wolf._command_database_selector_role(numeric_read, 1600001) == "data"
    assert wolf._command_database_selector_role(
        {**numeric_read, "intArgs": [1600001, 0, 0, 0x41200, 1600002]},
        1600001,
    ) == "type"
    assert wolf._record_selector_access(
        {**numeric_read, "intArgs": [1600001, 0, 0, 0x41200, 1600002]},
        schemas,
        identifiers,
    ) == (("database.json", None), "numeric", None)
    assert wolf._command_database_selector_role(
        {**numeric_read, "intArgs": [0, 0, 1600001, 0x11200, 1600002]},
        1600001,
    ) == "field"


def test_wolf_identifier_sync_is_type_scoped_and_rejects_collisions(tmp_path):
    original = tmp_path / "original"
    patched = tmp_path / "patched"
    databases = original / "databases"
    common = original / "common"
    databases.mkdir(parents=True)
    common.mkdir()
    (databases / "DataBase.json").write_text(
        json.dumps({
            "types": [{
                "name": "武装",
                "data": [
                    {"name": "", "data": [{"name": "名称", "value": "ライフル"}]},
                    {"name": "", "data": [{"name": "名称", "value": "シールド"}]},
                ],
            }],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    (databases / "CDataBase.json").write_text(
        json.dumps({
            "types": [{
                "name": "参照先",
                "data": [{
                    "name": "ライフル",
                    "data": [{"name": "名称", "value": "ライフル"}],
                }],
            }],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    (common / "Lookup.json").write_text(
        json.dumps({
            "commands": [{
                "code": 250,
                "intArgs": [0, 0, 0, 0x71200, 1600000],
                "stringArgs": ["", "武装", "ライフル", "名称"],
            }]
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    shutil.copytree(original, patched)
    patched_database = json.loads((patched / "databases" / "DataBase.json").read_text(encoding="utf-8"))
    patched_database["types"][0]["data"][0]["data"][0]["value"] = "步枪"
    (patched / "databases" / "DataBase.json").write_text(
        json.dumps(patched_database, ensure_ascii=False), encoding="utf-8"
    )

    reference = {
        "file": "common/Lookup.json",
        "path": ["commands", 0, "stringArgs", 2],
        "target_data_indexes": [0],
        "expected_original": "ライフル",
        "reference_kind": "database_selector",
    }
    allowed = wolf._synchronize_database_identifiers(
        str(original),
        str(patched),
        [
            (
                ["database.json", 0],
                "ライフル",
                "步枪",
                "name_referenced",
                [reference],
                "name_closed",
                [],
            )
        ],
    )

    patched_cdb = json.loads((patched / "databases" / "CDataBase.json").read_text(encoding="utf-8"))
    patched_common = json.loads((patched / "common" / "Lookup.json").read_text(encoding="utf-8"))
    assert patched_cdb["types"][0]["data"][0]["name"] == "ライフル"
    assert patched_cdb["types"][0]["data"][0]["data"][0]["value"] == "ライフル"
    assert patched_common["commands"][0]["stringArgs"][2] == "步枪"
    assert ("common/Lookup.json", ("commands", 0, "stringArgs", 2)) in allowed
    assert {path.name for path in (patched / "databases").iterdir()} == {
        "CDataBase.json",
        "DataBase.json",
    }

    try:
        wolf._synchronize_database_identifiers(
            str(original),
            str(patched),
            [
                (
                    ["database.json", 0],
                    "ライフル",
                    "シールド",
                    "name_referenced",
                    [],
                    "name_closed",
                    [],
                )
            ],
        )
    except wolf.WolfEngineError as error:
        assert "译名碰撞" in str(error)
    else:
        raise AssertionError("same-type identifier collision must be rejected")

    numeric_original = tmp_path / "numeric_original"
    numeric_patched = tmp_path / "numeric_patched"
    (numeric_original / "databases").mkdir(parents=True)
    (numeric_original / "databases" / "DataBase.json").write_text(
        json.dumps({
            "types": [{
                "name": "数字读取",
                "data": [
                    {"name": "", "data": [{"name": "名称", "value": "A"}]},
                    {"name": "", "data": [{"name": "名称", "value": "B"}]},
                ],
            }],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    shutil.copytree(numeric_original, numeric_patched)
    numeric_data = json.loads(
        (numeric_patched / "databases" / "DataBase.json").read_text(encoding="utf-8")
    )
    numeric_data["types"][0]["data"][0]["data"][0]["value"] = "B"
    (numeric_patched / "databases" / "DataBase.json").write_text(
        json.dumps(numeric_data, ensure_ascii=False), encoding="utf-8"
    )

    wolf._synchronize_database_identifiers(
        str(numeric_original),
        str(numeric_patched),
        [(["database.json", 0], "A", "B", "numeric_only", [], "numeric_only", [])],
    )


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
                    "name": "装備",
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
                    "name": "ルビ",
                    "data": [{
                        "name": "内部キー",
                        "data": [{"name": "識別名", "value": "内部キー"}],
                    }]
                },
                {
                    "name": "未使用",
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
                    "code": 101,
                    "codeStr": "Message",
                    "stringArgs": [r"\s[0]"],
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

    assert ("database.json", 0, 0, 0) in usage["display_database_records"]
    assert ("database.json", 1, 0, 0) in usage["logic_database_records"]
    assert (("types", 0, "data", 0, "name"), "鉄の斧") not in roles
    assert roles[(("types", 0, "data", 0, "data", 0, "value"), "鉄の斧")] == "WOLFText"
    assert roles[(("types", 0, "data", 1, "name"), "ソフィア")] == "WOLFLogic"
    assert roles[(("types", 1, "data", 0, "data", 0, "value"), "内部キー")] == "WOLFLogic"
    assert roles[(("types", 2, "data", 0, "data", 0, "value"), "未使用キー")] == "WOLFLogic"
    common_entries = wolf._json_entries(str(json_root), str(common_path), usage)
    command_logic = next(
        metadata for metadata, text in common_entries
        if metadata["path"] == ["commands", 5, "stringArgs", 0] and text == "ソフィア"
    )
    assert command_logic["marker"] == "WOLFLogic"


def test_wolf_database_record_name_call_is_scoped_to_its_database_type(tmp_path):
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
                "data": [{"name": "Ayaは元気？", "data": []}],
            }]
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    common_path.write_text(
        json.dumps({
                "commands": [{
                    "code": 250,
                    "stringArgs": ["", "クリア状況", "Ayaは元気？", "0=まだ/1=済"],
                    "intArgs": [0, 0, 0, 0x71000, 3000000],
            }]
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    usage = wolf._analyze_json_usage(str(json_root))
    entries = wolf._json_entries(str(json_root), str(database_path), usage)

    assert (
        "cdatabase.json",
        0,
        "Ayaは元気？".casefold(),
    ) in usage["named_database_record_references"]
    assert not any(
        metadata["path"] == ["types", 0, "data", 0, "name"]
        for metadata, _text in entries
    )
    assert any(
        metadata["path"] == ["types", 0, "data", 1, "data", 0, "value"]
        and metadata["marker"] == "WOLFLogic"
        and text == "Ayaは元気？"
        for metadata, text in entries
    )


def test_wolf_database_value_used_as_jump_label_stays_logic(tmp_path):
    json_root = tmp_path / "json"
    database_path = json_root / "databases" / "DataBase.json"
    common_path = json_root / "common" / "Particle.json"
    database_path.parent.mkdir(parents=True)
    common_path.parent.mkdir(parents=True)
    database_path.write_text(
        json.dumps({
            "types": [{
                "name": "パーティクル",
                "data": [{
                    "name": "arm_pose",
                    "data": [{"name": "名前", "value": "[rb]ウィル腕 - 構え"}],
                }],
            }],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    common_path.write_text(
        json.dumps({
            "commands": [
                {
                    "code": 250,
                    "stringArgs": ["", "パーティクル", "", "名前"],
                    "intArgs": [0, 1600000, 0, 0x51200, 3000001],
                },
                {"code": 213, "stringArgs": [r"\s[1]"]},
            ],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    usage = wolf._analyze_json_usage(str(json_root))
    roles = {
        (tuple(metadata["path"]), text): metadata["marker"]
        for metadata, text in wolf._json_entries(str(json_root), str(database_path), usage)
    }

    assert ("database.json", 0, 0) in usage["logic_database_fields"]
    assert roles[
        (("types", 0, "data", 0, "data", 0, "value"), "[rb]ウィル腕 - 構え")
    ] == "WOLFLogic"


def test_wolf_database_value_used_as_sound_resource_stays_logic(tmp_path):
    json_root = tmp_path / "json"
    database_path = json_root / "databases" / "DataBase.json"
    common_path = json_root / "common" / "Sound.json"
    database_path.parent.mkdir(parents=True)
    common_path.parent.mkdir(parents=True)
    database_path.write_text(
        json.dumps({
            "types": [{
                "name": "Scene",
                "data": [{
                    "name": "battle",
                    "data": [{"name": "Value", "value": "BGM/battle.ogg"}],
                }],
            }],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    common_path.write_text(
        json.dumps({
            "commands": [
                {
                    "code": 250,
                    "stringArgs": ["", "Scene", "", "Value"],
                    "intArgs": [0, 0, 0, 0x51200, 1600006],
                },
                {"code": 140, "stringArgs": [r"\cself[6]"]},
            ],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    usage = wolf._analyze_json_usage(str(json_root))

    assert ("database.json", 0, 0, 0) in usage["logic_database_records"]


def test_wolf_closed_jump_label_group_stays_logic(tmp_path):
    json_root = tmp_path / "json"
    database_path = json_root / "databases" / "DataBase.json"
    common_path = json_root / "common" / "Particle.json"
    database_path.parent.mkdir(parents=True)
    common_path.parent.mkdir(parents=True)
    database_path.write_text(
        json.dumps({
            "types": [{
                "name": "パーティクル",
                "data": [{
                    "name": "[rb]ウィル腕 - 構え",
                    "data": [{"name": "名前", "value": "[rb]ウィル腕 - 構え"}],
                }],
            }],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    common_path.write_text(
        json.dumps({
            "commands": [
                {
                    "code": 250,
                    "stringArgs": ["", "パーティクル", "", "名前"],
                    "intArgs": [0, 1600000, 0, 0x51200, 3000001],
                },
                {"code": 213, "stringArgs": [r"\s[1]"]},
                {"code": 212, "stringArgs": ["[rb]ウィル腕 - 構え"]},
            ],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    usage = wolf._analyze_json_usage(str(json_root))
    entries = [
        *wolf._json_entries(str(json_root), str(database_path), usage),
        *wolf._json_entries(str(json_root), str(common_path), usage),
    ]
    grouped = [
        metadata for metadata, text in entries
        if text == "[rb]ウィル腕 - 構え"
    ]

    assert len(grouped) == 2
    assert {metadata["marker"] for metadata in grouped} == {"WOLFLogic"}


def test_wolf_dynamic_database_selector_protects_reference_pair(tmp_path):
    json_root = tmp_path / "json"
    databases = json_root / "databases"
    common = json_root / "common"
    databases.mkdir(parents=True)
    common.mkdir()
    database_path = databases / "DataBase.json"
    c_database_path = databases / "CDataBase.json"
    database_path.write_text(
        json.dumps({
            "types": [{
                "name": "操作盤名",
                "data": [{
                    "name": "通常戦闘",
                    "data": [
                        {"name": "リソース名1", "value": "戦_バリア"},
                        {"name": "リソース名2", "value": "戦_フォーカス"},
                    ],
                }],
            }],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    c_database_path.write_text(
        json.dumps({
            "types": [{
                "name": "汎用リソース管理",
                "data": [
                    {
                        "name": "戦_バリア",
                        "data": [{"name": "name", "value": "戦_バリア"}],
                    },
                    {
                        "name": "表示用",
                        "data": [{"name": "説明", "value": "戦_バリア"}],
                    },
                ],
            }],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    (common / "OperationPanel.json").write_text(
        json.dumps({
            "commands": [
                {
                    "code": 250,
                    "intArgs": [0, 0, 0, 0x51200, 1600096],
                    "stringArgs": ["", "操作盤名", "", "リソース名1"],
                },
                    {
                        "code": 250,
                        "intArgs": [0, 0, 1600096, 0x11000, 1600009],
                        "stringArgs": ["", "汎用リソース管理", "", ""],
                },
            ],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    usage = wolf._analyze_json_usage(str(json_root))
    database_entries = wolf._json_entries(str(json_root), str(database_path), usage)
    c_database_entries = wolf._json_entries(str(json_root), str(c_database_path), usage)
    database_roles = {
        (tuple(metadata["path"]), text): metadata["marker"]
        for metadata, text in database_entries
    }
    c_database_roles = {
        (tuple(metadata["path"]), text): metadata["marker"]
        for metadata, text in c_database_entries
    }

    assert ("database.json", 0, 0, 0) in usage["logic_database_records"]
    assert ("database.json", 0, 0, 1) in usage["logic_database_records"]
    assert database_roles[(("types", 0, "data", 0, "data", 0, "value"), "戦_バリア")] == "WOLFLogic"
    assert c_database_roles[
        (("types", 0, "data", 0, "name"), "戦_バリア")
    ] == "WOLFLogic"
    assert c_database_roles[
        (("types", 0, "data", 0, "data", 0, "value"), "戦_バリア")
    ] == "WOLFLogic"
    assert c_database_roles[
        (("types", 0, "data", 1, "data", 0, "value"), "戦_バリア")
    ] == "WOLFLogic"

    patched_root = tmp_path / "patched"
    shutil.copytree(json_root, patched_root)
    patched_database = json.loads((patched_root / "databases" / "DataBase.json").read_text(encoding="utf-8"))
    patched_database["types"][0]["data"][0]["data"][0]["value"] = "战_屏障"
    (patched_root / "databases" / "DataBase.json").write_text(
        json.dumps(patched_database, ensure_ascii=False),
        encoding="utf-8",
    )

    try:
        wolf._verify_logic_json_unchanged(str(json_root), str(patched_root))
    except wolf.WolfEngineError as error:
        assert "逻辑字段被翻译" in str(error)
    else:
        raise AssertionError("dynamic database selector values must remain unchanged")


def test_wolf_resource_detection_handles_multiline_parameters():
    assert wolf._looks_like_resource("1枚絵マップ/汽車.png\n0\n0\n0") is True
    assert wolf._looks_like_resource("Data/textfile/01_scenario.md") is True


def test_wolf_database_resource_schema_protects_extensionless_file_reference(tmp_path):
    json_root = tmp_path / "json"
    database_path = json_root / "databases" / "DataBase.json"
    database_path.parent.mkdir(parents=True)
    database_path.write_text(
        json.dumps({
            "types": [{
                "name": "画像一覧",
                "fields": [
                    {"name": "画像", "stringArgs": ["Picture_UI"]},
                    {"name": "表示名"},
                ],
                "data": [{
                    "name": "entry",
                    "data": [
                        {"name": "画像", "value": "button_attack"},
                        {"name": "表示名", "value": "攻撃"},
                    ],
                }],
            }],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    maps = json_root / "maps"
    maps.mkdir()
    (maps / "Map001.json").write_text(
        json.dumps({"events": [{"pages": [{"list": [
            {
                "code": 250,
                "intArgs": [0, 0, 1, 0x51200, 1600000],
                "stringArgs": ["", "画像一覧", "", "表示名"],
            },
            {"code": 101, "stringArgs": [r"\cself[0]"]},
        ]}]}]}),
        encoding="utf-8",
    )

    usage = wolf._analyze_json_usage(str(json_root))
    roles = {
        (tuple(metadata["path"]), text): (metadata["marker"], metadata.get("logic_role"))
        for metadata, text in wolf._json_entries(
            str(json_root), str(database_path), usage
        )
    }

    assert roles[(("types", 0, "data", 0, "data", 0, "value"), "button_attack")] == (
        "WOLFLogic",
        "resource",
    )
    assert roles[(("types", 0, "data", 0, "data", 1, "value"), "攻撃")][0] == "WOLFText"


def test_wolf_database_fields_require_display_evidence_and_hash_values_stay_protected():
    field = {"name": "【メモ】", "value": "内部調整値"}
    usage = {
        "display_database_fields": set(),
        "logic_database_fields": set(),
        "comparison_literals": set(),
    }

    assert wolf._database_value_marker("database.json", 2, 3, field, usage) == "WOLFLogic"
    usage["display_database_fields"].add(("database.json", 2, 3))
    assert wolf._database_value_marker("database.json", 2, 3, field, usage) == "WOLFText"
    assert wolf._database_value_marker(
        "database.json", 2, 5, {"name": "名前", "value": "# 未使用区切り"}, usage
    ) == "WOLFLogic"

    name_field = {"name": "名称", "value": "test"}
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
    usage["display_database_fields"].update({
        ("database.json", 0, 0),
        ("cdatabase.json", 0, 0),
    })
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
    ) == "WOLFText"
    assert wolf._database_value_marker(
        "cdatabase.json",
        0,
        0,
        {"name": "名前", "value": "サーマルガード"},
        usage,
        datum_name="サーマルガード",
    ) == "WOLFText"


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
                "fields": [{"name": "Runコモン"}, {"name": "引数1"}],
                "data": [{
                    "name": "",
                    "data": [
                        {"name": "Runコモン", "value": "Target"},
                        {"name": "引数1", "value": "Runtime key"},
                    ],
                }],
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
                {"code": 300, "intArgs": [0, 0x100], "stringArgs": [r"\cself[0]"]},
            ]
        }),
        encoding="utf-8",
    )

    usage = wolf._analyze_json_usage(str(tmp_path))

    assert ("database.json", 0, 0, 0) in usage["logic_database_records"]
    assert ("database.json", 0, 0, 1) in usage["opaque_database_records"]
    assert usage["unresolved_database_callbacks"][0]["candidate_contexts"] == ()


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


def test_wolf_original_data_backup_is_published_atomically(tmp_path, monkeypatch):
    data_path = tmp_path / "Data"
    staging = tmp_path / "staging"
    data_path.mkdir()
    staging.mkdir()
    (data_path / "a.txt").write_text("original a", encoding="utf-8")
    (data_path / "b.txt").write_text("original b", encoding="utf-8")
    (staging / "a.txt").write_text("translated a", encoding="utf-8")
    (staging / "b.txt").write_text("translated b", encoding="utf-8")
    real_copytree = wolf.shutil.copytree

    def interrupt_backup(source, destination, *args, **kwargs):
        if os.path.basename(destination) == "Data.windy-original.bak.tmp":
            os.makedirs(destination)
            shutil.copy2(os.path.join(source, "a.txt"), os.path.join(destination, "a.txt"))
            raise OSError("interrupted backup")
        return real_copytree(source, destination, *args, **kwargs)

    monkeypatch.setattr(wolf.shutil, "copytree", interrupt_backup)
    try:
        wolf._replace_data_directory(str(data_path), str(staging))
    except OSError:
        pass
    else:
        raise AssertionError("interrupted backup must fail the replacement")

    backup = tmp_path / "Data.windy-original.bak"
    assert not backup.exists()
    assert not (tmp_path / "Data.windy-original.bak.tmp").exists()
    assert (data_path / "b.txt").read_text(encoding="utf-8") == "original b"

    monkeypatch.setattr(wolf.shutil, "copytree", real_copytree)
    wolf._replace_data_directory(str(data_path), str(staging))

    assert (backup / "a.txt").read_text(encoding="utf-8") == "original a"
    assert (backup / "b.txt").read_text(encoding="utf-8") == "original b"
    assert (data_path / "b.txt").read_text(encoding="utf-8") == "translated b"


def test_wolf_json_reread_rejects_ignored_patch(tmp_path):
    expected = tmp_path / "expected" / "maps"
    actual = tmp_path / "actual" / "maps"
    expected.mkdir(parents=True)
    actual.mkdir(parents=True)
    (expected / "Map001.json").write_text('{"text":"中文"}', encoding="utf-8")
    (actual / "Map001.json").write_text('{"text":"原文"}', encoding="utf-8")

    try:
        wolf._verify_json_dump_matches(str(expected.parent), str(actual.parent))
    except wolf.WolfEngineError as error:
        assert "重读内容不一致" in str(error)
    else:
        raise AssertionError("ignored JSON patch must fail reread verification")


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

    assert metadata["wolf_export_schema"] == wolf.WOLF_EXPORT_SCHEMA
    assert wolf.get_optional_transport_tags(str(tmp_path)) == (tokens[1][0],)
    assert tokens[0][0] not in wolf.get_optional_transport_tags(str(tmp_path))


def test_wolf_rejects_string_scripts_without_current_export_schema(tmp_path):
    scripts = tmp_path / wolf.STRING_SCRIPTS_ORIGIN_DIRNAME
    scripts.mkdir()
    metadata = {
        "kind": "json",
        "file": "common/Old.json",
        "path": ["commands", 0, "stringArgs", 0],
        "marker": "Message",
        "wolf_code": "JSON:common/Old.json:[commands,0,stringArgs,0]",
    }
    (scripts / "old.txt").write_text(
        f"@WOLF {wolf._encode_metadata(metadata)}\n#Message#\n旧数据\n##\n",
        encoding="utf-8",
    )

    try:
        list(wolf._iter_released_entries(str(scripts)))
    except wolf.WolfEngineError as error:
        assert "版本已过期" in str(error)
    else:
        raise AssertionError("old WOLF StringScripts must be rejected")


def test_wolf_rejects_string_scripts_without_metadata_header(tmp_path):
    scripts = tmp_path / wolf.STRING_SCRIPTS_ORIGIN_DIRNAME
    scripts.mkdir()
    (scripts / "old.txt").write_text(
        "#Message#\n旧数据\n##\n", encoding="utf-8"
    )

    try:
        list(wolf._iter_released_entries(str(scripts)))
    except wolf.WolfEngineError as error:
        assert "缺少当前版本元数据" in str(error)
    else:
        raise AssertionError("metadata-free WOLF StringScripts must be rejected")


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
    assert file_count == 3
    assert entry_count == 3
    assert (resources / "contents" / "page1.txt.strings.txt").is_file()
    assert (resources / "contents" / "page2.txt.strings.txt").is_file()
    assert (game_path / wolf.STRING_SCRIPTS_DIRNAME / "WOLF" / "Logic" / "common" / "Reader.json.txt").is_file()
    assert not (resources / "work_temp").exists()


def test_wolf_referenced_maps_exclude_unregistered_sample_maps():
    references = ["MapData/TitleMap.mps", "MapData\\Field01.mps\r\n100"]

    assert wolf._referenced_map_json_names(references) == {"titlemap.json", "field01.json"}


def test_wolf_runtime_maps_exclude_blank_sysdb_test_play_slot(tmp_path):
    databases = tmp_path / "databases"
    maps = tmp_path / "maps"
    databases.mkdir()
    maps.mkdir()
    for filename in ("TitleMap.json", "Dungeon.json", "SampleMapA.json", "TestMap.json"):
        (maps / filename).write_text("{}", encoding="utf-8")
    (databases / "SysDatabase.json").write_text(
        json.dumps({
            "types": [{
                "name": "マップ設定",
                "fields": [{"name": "マップファイル名"}],
                "data": [
                    {
                        "name": "タイトル",
                        "data": [{"name": "マップファイル名", "value": "MapData/TitleMap.mps"}],
                    },
                    {
                        "name": "",
                        "data": [{"name": "マップファイル名", "value": "MapData/TestMap.mps"}],
                    },
                ],
            }],
        }),
        encoding="utf-8",
    )

    assert wolf._runtime_map_json_names(
        str(tmp_path), ["MapData/TitleMap.mps", "MapData/TestMap.mps"]
    ) == {"titlemap.json"}
    assert wolf._runtime_map_json_names(
        str(tmp_path), ["MapData/TitleMap.mps", "MapData/TestMap.mps", r"MapData/\s[1].mps"]
    ) == {"titlemap.json", "dungeon.json", "samplemapa.json"}


def test_wolf_logic_files_stay_out_of_translation_json(tmp_path):
    game_path = tmp_path / "Game"
    scripts = game_path / wolf.STRING_SCRIPTS_DIRNAME / "WOLF"
    display_entries = []
    logic_entries = []
    wolf._add_entry(display_entries, {"kind": "json", "file": "a.json", "path": [0]}, "表示文")
    wolf._add_entry(display_entries, {"kind": "json", "file": "a.json", "path": [2]}, "表示文")
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

    file_key = os.path.join("WOLF", "Binary", "a.txt")
    assert list(output) == [file_key]
    assert output[file_key]["表示文"]["wolf_codes"] == [
        display_entries[0][0]["wolf_code"],
        display_entries[1][0]["wolf_code"],
    ]
    assert output[file_key]["表示文"]["wolf_export_schema"] == wolf.WOLF_EXPORT_SCHEMA


def test_wolf_json_creation_rejects_old_string_scripts_without_output(tmp_path):
    game_path = tmp_path / "Game"
    scripts = game_path / wolf.STRING_SCRIPTS_DIRNAME / "WOLF"
    scripts.mkdir(parents=True)
    metadata = {
        "kind": "json",
        "file": "common/Old.json",
        "path": ["commands", 0, "stringArgs", 0],
        "marker": "Message",
        "wolf_code": "JSON:common/Old.json:[commands,0,stringArgs,0]",
    }
    (scripts / "old.txt").write_text(
        f"@WOLF {wolf._encode_metadata(metadata)}\n#Message#\n旧数据\n##\n",
        encoding="utf-8",
    )
    works = tmp_path / "Works"

    json_creation.run_create_json(str(game_path), str(works), queue.Queue())

    assert not (works / "Game" / "untranslated" / "translation.json").exists()


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
        json.dumps([
            {"text": "原文です"},
            {"text": "一\r\n二"},
        ], ensure_ascii=False),
        encoding="utf-8",
    )
    script = origin_path / "sample.txt"
    entries = []
    wolf._add_entry(
        entries,
        {"kind": "json", "file": "maps/a.json", "path": [0, "text"], "marker": "Message"},
        "原文です",
    )
    wolf._add_entry(
        entries,
        {"kind": "json", "file": "maps/a.json", "path": [1, "text"], "marker": "Message"},
        "一\r\n二",
    )
    wolf._write_string_script(str(script), entries)
    shutil.copytree(origin_path, scripts_path)
    translated_script = scripts_path / "sample.txt"
    translated_script.write_bytes(
        translated_script.read_bytes().replace(
            "原文です".encode(), "中文".encode()
        )
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
            patched = json.load(source)
        translated = patched[0]["text"]
        assert patched[1]["text"] == "一\r\n二"
        with open(os.path.join(output_data, "BasicData", "Game.dat"), "w", encoding="utf-8") as output:
            output.write(translated)

    def fake_dump(_data, json_root):
        os.makedirs(os.path.join(json_root, "game"), exist_ok=True)
        os.makedirs(os.path.join(json_root, "maps"), exist_ok=True)
        with open(os.path.join(json_root, "game", "Game.json"), "w", encoding="utf-8") as output:
            json.dump({"MainFont": "Test Font", "SubFonts": ["", "", ""]}, output)
        with open(os.path.join(_data, "BasicData", "Game.dat"), encoding="utf-8") as source:
            translated = source.read()
        with open(os.path.join(json_root, "maps", "a.json"), "w", encoding="utf-8") as output:
            json.dump([
                {"text": translated},
                {"text": "一\r\n二"},
            ], output, ensure_ascii=False)

    monkeypatch.setattr(wolf.uberwolf, "apply_text", fake_apply)
    monkeypatch.setattr(wolf.uberwolf, "dump_text", fake_dump)
    monkeypatch.setattr(
        wolf.uberwolf,
        "unpack_game",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not unpack verification archive")),
    )

    messages = queue.Queue()
    assert wolf.import_from_string_scripts(str(game_path), messages) == 1
    assert ("wolf_translation_imported", str(game_path)) in list(messages.queue)
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
        {script_rel: {"タスケテ": _translated_entry(entries, "タスケテ", "タスケテ")}},
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
        {display_rel: {"表示文": _translated_entry(display_entries, "表示文", "显示文本")}},
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
                "彼女は「ソフィア」です。": _translated_entry(
                    entries, "彼女は「ソフィア」です。", "她是索菲娅。"
                ),
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
        {script_rel: {"・マラソン大会": _translated_entry(entries, "・マラソン大会", "・马拉松大会")}},
    )

    assert errors == []


def test_wolf_release_validation_rejects_stale_structure_code(tmp_path):
    game_path = tmp_path / "Game"
    origin_path = game_path / wolf.STRING_SCRIPTS_ORIGIN_DIRNAME
    entries = []
    wolf._add_entry(entries, {"kind": "json", "file": "a.json", "path": [0]}, "表示文")
    script_path = origin_path / "WOLF" / "Binary" / "a.txt"
    wolf._write_string_script(str(script_path), entries)
    script_rel = os.path.relpath(script_path, origin_path)

    errors, _warnings, _stats = wolf.validate_translation_release(
        str(game_path),
        {
            script_rel: {
                "表示文": {
                    "text": "显示文本",
                    "status": "success",
                    "wolf_export_schema": wolf.WOLF_EXPORT_SCHEMA,
                    "wolf_codes": ["JSON:a.json:[99]"],
                }
            }
        },
    )

    assert any("结构 Code 不匹配" in error for error in errors)


def test_wolf_release_validation_rejects_stale_export_schema(tmp_path):
    game_path = tmp_path / "Game"
    origin_path = game_path / wolf.STRING_SCRIPTS_ORIGIN_DIRNAME
    entries = []
    wolf._add_entry(entries, {"kind": "json", "file": "a.json", "path": [0]}, "表示文")
    script_path = origin_path / "WOLF" / "Binary" / "a.txt"
    wolf._write_string_script(str(script_path), entries)
    script_rel = os.path.relpath(script_path, origin_path)
    translated = _translated_entry(entries, "表示文", "显示文本")
    translated.pop("wolf_export_schema")

    errors, _warnings, _stats = wolf.validate_translation_release(
        str(game_path), {script_rel: {"表示文": translated}}
    )

    assert any("导出版本不匹配" in error for error in errors)


def test_wolf_release_validation_rejects_same_type_identifier_collision(tmp_path):
    game_path = tmp_path / "Game"
    origin_path = game_path / wolf.STRING_SCRIPTS_ORIGIN_DIRNAME
    entries = []
    wolf._add_entry(
        entries,
        {
            "kind": "json",
            "file": "databases/DataBase.json",
            "path": ["types", 0, "data", 0, "data", 0, "value"],
                "marker": "WOLFText",
                "identifier_namespace": ["database.json", 0],
                "identifier_collision_policy": "name_referenced",
                "identifier_translation_policy": "name_closed",
                "identifier_reference_complete": True,
                "identifier_references": [],
                "identifier_missing_names": [],
        },
        "ライフル",
    )
    wolf._add_entry(
        entries,
        {
            "kind": "json",
            "file": "databases/DataBase.json",
            "path": ["types", 0, "data", 1, "data", 0, "value"],
                "marker": "WOLFText",
                "identifier_namespace": ["database.json", 0],
                "identifier_collision_policy": "name_referenced",
                "identifier_translation_policy": "name_closed",
                "identifier_reference_complete": True,
                "identifier_references": [],
                "identifier_missing_names": [],
        },
        "マスケット",
    )
    script_path = origin_path / "WOLF" / "Binary" / "database.txt"
    wolf._write_string_script(str(script_path), entries)
    script_rel = os.path.relpath(script_path, origin_path)

    errors, _warnings, _stats = wolf.validate_translation_release(
        str(game_path),
        {
            script_rel: {
                "ライフル": _translated_entry(entries, "ライフル", "步枪"),
                "マスケット": _translated_entry(entries, "マスケット", "步枪"),
            }
        },
    )

    assert any("数据库译名碰撞" in error for error in errors)

    for metadata, _text in entries:
        metadata["identifier_collision_policy"] = "numeric_only"
        metadata["identifier_translation_policy"] = "numeric_only"
    wolf._write_string_script(str(script_path), entries)
    errors, warnings, _stats = wolf.validate_translation_release(
        str(game_path),
        {
            script_rel: {
                "ライフル": _translated_entry(entries, "ライフル", "步枪"),
                "マスケット": _translated_entry(entries, "マスケット", "步枪"),
            }
        },
    )
    assert not any("数据库译名碰撞" in error for error in errors)
    assert any("仅按数字 ID 读取，已允许" in warning for warning in warnings)


def test_wolf_identifier_collision_checks_only_reject_new_casefold_groups(tmp_path):
    game_path = tmp_path / "Game"
    origin_path = game_path / wolf.STRING_SCRIPTS_ORIGIN_DIRNAME
    entries = []
    for index, text in enumerate(("▲END", "▲end")):
        wolf._add_entry(
            entries,
            {
                "kind": "json",
                "file": "databases/DataBase.json",
                "path": ["types", 18, "data", index, "data", 0, "value"],
                "marker": "WOLFLogic",
                "identifier_namespace": ["database.json", 18],
                "identifier_collision_policy": "name_referenced",
                "identifier_translation_policy": "name_closed",
                "identifier_reference_complete": False,
                "identifier_references": [],
                "identifier_missing_names": [],
            },
            text,
        )
    script_path = origin_path / "WOLF" / "Logic" / "database.txt"
    wolf._write_string_script(str(script_path), entries)

    errors, _warnings, _stats = wolf.validate_translation_release(str(game_path), {})

    assert not any("数据库译名碰撞" in error for error in errors)

    original = tmp_path / "original"
    patched = tmp_path / "patched"
    (original / "databases").mkdir(parents=True)
    database = {
        "types": [{
            "name": "名称读取",
            "data": [
                {"name": "", "data": [{"name": "名称", "value": "▲END"}]},
                {"name": "", "data": [{"name": "名称", "value": "▲end"}]},
                {"name": "", "data": [{"name": "名称", "value": "変更前"}]},
            ],
        }],
    }
    (original / "databases" / "DataBase.json").write_text(
        json.dumps(database, ensure_ascii=False), encoding="utf-8"
    )
    shutil.copytree(original, patched)
    database["types"][0]["data"][2]["data"][0]["value"] = "変更後"
    (patched / "databases" / "DataBase.json").write_text(
        json.dumps(database, ensure_ascii=False), encoding="utf-8"
    )

    wolf._synchronize_database_identifiers(
        str(original),
        str(patched),
        [
            (
                ["database.json", 0],
                "変更前",
                "変更後",
                "name_referenced",
                [],
                "name_closed",
                [],
            )
        ],
    )


def test_font_required_characters_waits_for_released_translation(tmp_path):
    origin_path = tmp_path / wolf.STRING_SCRIPTS_ORIGIN_DIRNAME
    scripts_path = tmp_path / wolf.STRING_SCRIPTS_DIRNAME
    metadata = {"kind": "json", "file": "maps/a.json", "path": [0], "marker": "Message"}
    original = [(metadata, "原文です")]
    wolf._write_string_script(str(origin_path / "sample.txt"), original)
    wolf._write_string_script(str(scripts_path / "sample.txt"), original)

    required, from_scripts = wolf.font_revision_required_characters(str(tmp_path), "示例文字")

    assert required == set("示例文字")
    assert from_scripts is False

    wolf._write_string_script(str(scripts_path / "sample.txt"), [(metadata, "中文译文")])
    required, from_scripts = wolf.font_revision_required_characters(str(tmp_path), "示例文字")

    assert required == set("中文译文")
    assert from_scripts is True


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


def test_replace_data_directory_retries_initial_displacement_lock(tmp_path, monkeypatch):
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
        if source == str(data_path) and blocked < 2:
            blocked += 1
            raise PermissionError(5, "transient data lock")
        return real_replace(source, destination)

    monkeypatch.setattr(wolf.os, "replace", transient_replace)
    monkeypatch.setattr(wolf.time, "sleep", lambda _seconds: None)

    wolf._replace_data_directory(str(data_path), str(source_path))

    assert blocked == 2
    assert (data_path / "value.txt").read_text(encoding="utf-8") == "new"


def test_replace_data_directory_cleans_prepared_data_after_persistent_lock(tmp_path, monkeypatch):
    data_path = tmp_path / "Data"
    source_path = tmp_path / "staging"
    data_path.mkdir()
    source_path.mkdir()
    (data_path / "value.txt").write_text("old", encoding="utf-8")
    (source_path / "value.txt").write_text("new", encoding="utf-8")
    real_replace = os.replace

    def locked_replace(source, destination):
        if source == str(data_path):
            raise PermissionError(5, "persistent data lock")
        return real_replace(source, destination)

    monkeypatch.setattr(wolf.os, "replace", locked_replace)
    monkeypatch.setattr(wolf.time, "sleep", lambda _seconds: None)

    try:
        wolf._replace_data_directory(str(data_path), str(source_path))
    except wolf.WolfEngineError as error:
        assert "目录仍被占用" in str(error)
    else:
        raise AssertionError("persistent Data lock must fail")

    assert (data_path / "value.txt").read_text(encoding="utf-8") == "old"
    assert not (tmp_path / "Data.windy.tmp").exists()
    assert not (tmp_path / "Data.windy.previous.tmp").exists()


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
    archive_sha256 = wolf._archive_digest(str(tmp_path), "split")
    split_file_sha256 = wolf._sha256(str(data_path / "BasicData.wolf"))
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
    assert manifest["archive_sha256"] == archive_sha256
    assert manifest["archive_data_sha256"] == manifest["data_sha256"]
    assert manifest["deployment_layout"] == "loose"
    assert not (data_path / "BasicData.wolf").exists()
    disabled = manifest["disabled_archives"]
    assert [item["source_path"] for item in disabled] == ["Data/BasicData.wolf"]
    assert wolf._sha256(wolf._safe_join(str(tmp_path), disabled[0]["stored_path"])) == split_file_sha256


def test_initialize_unpacks_and_disables_single_archive(tmp_path, monkeypatch):
    (tmp_path / "Game.exe").write_bytes(b"exe")
    archive_path = tmp_path / "Data.wolf"
    archive_path.write_bytes(b"archive")
    archive_sha256 = wolf._sha256(str(archive_path))

    def fake_unpack(root):
        basic_data = os.path.join(root, "Data", "BasicData")
        os.makedirs(basic_data)
        with open(os.path.join(basic_data, "Game.dat"), "wb") as output:
            output.write(b"game")
        with open(os.path.join(basic_data, "CommonEvent.dat"), "wb") as output:
            output.write(b"common")
        return os.path.join(root, "Data")

    monkeypatch.setattr(wolf.uberwolf, "unpack_game", fake_unpack)
    manifest = wolf.initialize_game(str(tmp_path))

    assert not archive_path.exists()
    assert manifest["archive_layout"] == "single"
    assert manifest["archive_sha256"] == archive_sha256
    assert manifest["archive_data_sha256"] == manifest["data_sha256"]
    assert manifest["deployment_layout"] == "loose"
    disabled = manifest["disabled_archives"]
    assert [item["source_path"] for item in disabled] == ["Data.wolf"]
    assert wolf._sha256(wolf._safe_join(str(tmp_path), disabled[0]["stored_path"])) == archive_sha256
    assert not (tmp_path / "Data.windy-original.bak").exists()
    assert wolf.initialize_game(str(tmp_path))["data_sha256"] == manifest["data_sha256"]


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


def test_initialize_disables_restored_archive_beside_verified_loose_deployment(tmp_path, monkeypatch):
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
    assert not archive_path.exists()
    assert [item["source_path"] for item in manifest["disabled_archives"]] == ["Data.wolf"]
