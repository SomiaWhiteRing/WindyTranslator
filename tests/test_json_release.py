import queue

import core.tasks.json_release as json_release
from core.tasks.json_release import _format_schema_errors, _validate_translation_json_schema, run_release_json


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
