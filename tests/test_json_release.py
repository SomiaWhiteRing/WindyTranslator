import queue

from core.tasks.json_release import (
    _apply_translations_to_file,
    _validate_translation_json_schema,
    run_release_json,
)


def test_validate_translation_json_schema_rejects_path_traversal():
    errors = _validate_translation_json_schema(
        {"../outside.txt": {"原文": {"text": "译文"}}},
        '{"../outside.txt": {}}',
    )

    assert len(errors) == 1
    assert "StringScripts 内相对路径" in errors[0]["message"]


def test_run_release_json_releases_translation_successfully(tmp_path):
    game_path = tmp_path / "Game"
    (game_path / "StringScripts_Origin").mkdir(parents=True)
    (game_path / "StringScripts").mkdir()
    (game_path / "StringScripts_Origin" / "Map001.txt").write_text("#Name#\n原文\n", encoding="utf-8")
    json_path = tmp_path / "translation_translated.json"
    json_path.write_text('{"Map001.txt": {"原文": {"text": "译文", "status": "success"}}}', encoding="utf-8")

    assert run_release_json(str(game_path), str(tmp_path), str(json_path), queue.Queue()) is True
    assert (game_path / "StringScripts" / "Map001.txt").read_text(encoding="utf-8") == "#Name#\n译文\n"


def test_apply_translations_write_failure_keeps_original(tmp_path, monkeypatch):
    path = tmp_path / "Map001.txt"
    original = "#Message#\n原文\n##\n"
    path.write_text(original, encoding="utf-8")
    monkeypatch.setattr("core.tasks.json_release.os.replace", lambda *_args: (_ for _ in ()).throw(OSError("locked")))

    try:
        _apply_translations_to_file(str(path), {"原文": {"text": "译文"}})
    except OSError as error:
        assert "locked" in str(error)
    else:
        raise AssertionError("write failure must propagate")

    assert path.read_text(encoding="utf-8") == original
