import queue

import core.tasks.json_release as json_release
from core.tasks.json_release import (
    _apply_translations_to_file,
    _format_schema_errors,
    _validate_translation_json_schema,
    run_release_json,
)


def test_validate_translation_json_schema_reports_file_mapping_type_error():
    json_text = '{\n  "Map001.txt": "bad schema"\n}'
    errors = _validate_translation_json_schema({"Map001.txt": "bad schema"}, json_text)

    assert len(errors) == 1
    assert errors[0]["path"] == "$['Map001.txt']"
    assert errors[0]["line"] == 2
    assert "必须是原文到翻译对象的映射" in errors[0]["message"]
    assert "字符串" in errors[0]["message"]

    message = _format_schema_errors("translation_translated.json", errors)
    assert "translation_translated.json" in message
    assert "行 2" in message
    assert '"Map001.txt"' in message
    assert '"原文": { "text": "译文"' in message


def test_validate_translation_json_schema_rejects_path_traversal():
    data = {"../outside.txt": {"原文": {"text": "译文"}}}

    errors = _validate_translation_json_schema(data, '{"../outside.txt": {}}')

    assert len(errors) == 1
    assert "StringScripts 内相对路径" in errors[0]["message"]


def test_validate_translation_json_schema_skips_line_lookup_for_valid_data(monkeypatch):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("valid schema should not search json text for line numbers")

    monkeypatch.setattr(json_release, "_find_json_key_line", fail_if_called)

    errors = _validate_translation_json_schema(
        {"Map001.txt": {"原文": {"text": "译文", "status": "success"}}},
        '{"Map001.txt": {"原文": {"text": "译文", "status": "success"}}}',
    )

    assert errors == []


def test_validate_translation_json_schema_stops_after_error_report_limit(monkeypatch):
    lookup_count = 0

    def count_lookup(*args, **kwargs):
        nonlocal lookup_count
        lookup_count += 1
        return 1, 0

    monkeypatch.setattr(json_release, "_find_json_key_line", count_lookup)

    data = {"Map001.txt": {f"原文{i}": {"status": "missing text"} for i in range(20)}}
    errors = _validate_translation_json_schema(data, "{}")

    assert len(errors) == json_release.MAX_SCHEMA_ERRORS_TO_REPORT
    assert lookup_count == json_release.MAX_SCHEMA_ERRORS_TO_REPORT + 1


def test_run_release_json_reports_json_syntax_line_and_does_not_restore(tmp_path):
    game_path = tmp_path / "Game"
    backup_path = game_path / "StringScripts_Origin"
    string_scripts_path = game_path / "StringScripts"
    translated_json_path = tmp_path / "translation_translated.json"

    backup_path.mkdir(parents=True)
    (backup_path / "Map001.txt").write_text("#Name#\n原文\n", encoding="utf-8")
    string_scripts_path.mkdir()
    (string_scripts_path / "Map001.txt").write_text("keep current file\n", encoding="utf-8")
    translated_json_path.write_text('{\n  "Map001.txt": \n}', encoding="utf-8")

    messages = queue.Queue()

    result = run_release_json(str(game_path), str(tmp_path), str(translated_json_path), messages)

    emitted = []
    while not messages.empty():
        emitted.append(messages.get())

    errors = [payload for kind, payload in emitted if kind == "error"]
    statuses = [payload for kind, payload in emitted if kind == "status"]

    assert result is False
    assert any("不是合法 JSON" in error for error in errors)
    assert any("第 3 行" in error for error in errors)
    assert "释放 JSON 失败 (JSON语法错误)" in statuses
    assert (string_scripts_path / "Map001.txt").read_text(encoding="utf-8") == "keep current file\n"


def test_run_release_json_returns_true_on_success(tmp_path):
    game_path = tmp_path / "Game"
    backup_path = game_path / "StringScripts_Origin"
    string_scripts_path = game_path / "StringScripts"
    translated_json_path = tmp_path / "translation_translated.json"

    backup_path.mkdir(parents=True)
    (backup_path / "Map001.txt").write_text("#Name#\n原文\n", encoding="utf-8")
    string_scripts_path.mkdir()
    (string_scripts_path / "Map001.txt").write_text("old current file\n", encoding="utf-8")
    translated_json_path.write_text(
        '{"Map001.txt": {"原文": {"text": "译文", "status": "success"}}}',
        encoding="utf-8",
    )

    messages = queue.Queue()

    result = run_release_json(str(game_path), str(tmp_path), str(translated_json_path), messages)

    assert result is True
    assert (string_scripts_path / "Map001.txt").read_text(encoding="utf-8") == "#Name#\n译文\n"


def test_run_release_json_rejects_empty_backup_without_replacing_current(tmp_path):
    game_path = tmp_path / "Game"
    backup_path = game_path / "StringScripts_Origin"
    current_path = game_path / "StringScripts"
    translated_json_path = tmp_path / "translation_translated.json"
    backup_path.mkdir(parents=True)
    current_path.mkdir()
    (current_path / "keep.txt").write_text("keep", encoding="utf-8")
    translated_json_path.write_text("{}", encoding="utf-8")

    result = run_release_json(
        str(game_path), str(tmp_path), str(translated_json_path), queue.Queue()
    )

    assert result is False
    assert (current_path / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert not (game_path / "StringScripts.release-staging").exists()


def test_run_release_json_recovers_previous_snapshot_before_validation(tmp_path):
    game_path = tmp_path / "Game"
    backup_path = game_path / "StringScripts_Origin"
    previous_path = game_path / "StringScripts.release-previous"
    translated_json_path = tmp_path / "translation_translated.json"
    backup_path.mkdir(parents=True)
    (backup_path / "Map001.txt").write_text("backup", encoding="utf-8")
    previous_path.mkdir()
    (previous_path / "keep.txt").write_text("recovered", encoding="utf-8")
    translated_json_path.write_text("not json", encoding="utf-8")

    result = run_release_json(
        str(game_path), str(tmp_path), str(translated_json_path), queue.Queue()
    )

    assert result is False
    assert (game_path / "StringScripts" / "keep.txt").read_text(
        encoding="utf-8"
    ) == "recovered"
    assert not previous_path.exists()


def test_apply_translations_counts_missing_keys(tmp_path):
    script_path = tmp_path / "Map001.txt"
    script_path.write_text("#Message#\n第一行\n第二行\n##\n#Name#\n名字\n", encoding="utf-8")

    applied, skipped = _apply_translations_to_file(str(script_path), {})

    assert (applied, skipped) == (0, 2)
    assert script_path.read_text(encoding="utf-8") == "#Message#\n第一行\n第二行\n##\n#Name#\n名字\n"


def test_restore_string_scripts_removes_stale_files(tmp_path):
    backup = tmp_path / "origin"
    target = tmp_path / "current"
    backup.mkdir()
    target.mkdir()
    (backup / "current.txt").write_text("current", encoding="utf-8")
    (target / "stale.txt").write_text("stale", encoding="utf-8")

    count, _workers = json_release._restore_string_scripts_from_backup(
        str(backup), str(target)
    )

    assert count == 1
    assert {path.name for path in target.iterdir()} == {"current.txt"}


def test_apply_translations_write_failure_keeps_original(tmp_path, monkeypatch):
    script_path = tmp_path / "Map001.txt"
    original = "#Message#\n原文\n##\n"
    script_path.write_text(original, encoding="utf-8")

    def fail_replace(*_args):
        raise OSError("locked")

    monkeypatch.setattr(json_release.os, "replace", fail_replace)
    try:
        _apply_translations_to_file(
            str(script_path), {"原文": {"text": "译文"}}
        )
    except OSError as error:
        assert "locked" in str(error)
    else:
        raise AssertionError("write failure must propagate")

    assert script_path.read_text(encoding="utf-8") == original
    assert not (tmp_path / "Map001.txt.tmp").exists()


def test_wolf_release_validation_stops_before_restore(tmp_path):
    from core.engines import wolf

    game_path = tmp_path / "Game"
    backup_path = game_path / "StringScripts_Origin"
    current_path = game_path / "StringScripts"
    translated_json_path = tmp_path / "translation_translated.json"
    (game_path / "Game.exe").parent.mkdir(parents=True)
    (game_path / "Game.exe").write_bytes(b"")
    (game_path / "Data.wolf").write_bytes(b"")
    entries = []
    wolf._add_entry(entries, {"kind": "json", "file": "a.json", "path": [0]}, "日本語")
    wolf._write_string_script(str(backup_path / "WOLF" / "sample.txt"), entries)
    current_path.mkdir()
    (current_path / "keep.txt").write_text("keep", encoding="utf-8")
    translated_json_path.write_text("{}", encoding="utf-8")

    messages = queue.Queue()
    result = run_release_json(str(game_path), str(tmp_path), str(translated_json_path), messages)

    assert result is False
    assert (current_path / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert any(
        kind == "status" and payload == "释放 JSON 失败 (WOLF完整性校验)"
        for kind, payload in list(messages.queue)
    )
