import hashlib
from pathlib import Path

import pytest

from core.external import rpgrewriter
from core.tasks import export, import_task, json_creation, json_release
from core.utils import rpg_rt_ini


class Queue:
    def __init__(self):
        self.messages = []

    def put(self, message):
        self.messages.append(message)


def _scope(**enabled):
    result = {
        "game_text": True,
        "game_title": True,
        "map_names": False,
        "map_event_names": False,
        "switch_names": False,
        "variable_names": False,
        "common_event_names": False,
        "troop_names": False,
    }
    result.update(enabled)
    return result


def _write_ini(path, title, encoding="932"):
    path.write_bytes((
        "[RPG_RT]\n"
        f"GameTitle={title}\n"
        "\n"
        "[EasyRPG]\n"
        f"Encoding={encoding}\n"
    ).encode(f"cp{encoding}"))


def _write_title(path, title):
    path.write_text(f"#GameTitle#\n{title}\n", encoding="utf-8")


def test_export_command_passes_every_scope_flag(monkeypatch, tmp_path):
    captured = {}

    def run(_lmt_path, args):
        captured["args"] = args
        return 0, "", ""

    monkeypatch.setattr(rpgrewriter, "run_rpgrewriter_command", run)
    rpgrewriter.export_text_command(
        str(tmp_path / "RPG_RT.lmt"), "932",
        _scope(map_names=True, switch_names=True, common_event_names=True),
    )

    assert captured["args"][-12:] == [
        "-mapnames", "Y", "-mapeventnames", "N", "-switchnames", "Y",
        "-variablenames", "N", "-commoneventnames", "Y", "-troopnames", "N",
    ]


def test_export_writes_title_before_origin_backup(monkeypatch, tmp_path):
    game_path = tmp_path / "game"
    game_path.mkdir()
    (game_path / "RPG_RT.lmt").write_bytes(b"LMT")
    _write_ini(game_path / "RPG_RT.ini", "ゲーム")

    def run(_lmt_path, _encoding, _scope_value):
        scripts = game_path / "StringScripts"
        scripts.mkdir()
        (scripts / "Map0001.txt").write_text("#Message#\n本文\n##\n", encoding="utf-8")
        return 0, "", ""

    monkeypatch.setattr(export.rpgrewriter, "export_text_command", run)
    export.run_export(str(game_path), "932", _scope(), Queue())

    expected = "#GameTitle#\nゲーム\n"
    assert (game_path / "StringScripts" / "title.txt").read_text(encoding="utf-8") == expected
    assert (game_path / "StringScripts_Origin" / "title.txt").read_text(encoding="utf-8") == expected


def test_event_name_is_extracted_and_released(tmp_path):
    path = tmp_path / "Map0001.txt"
    path.write_text("#EventName#\n原事件名\n", encoding="utf-8")

    extracted = json_creation._extract_strings_from_file(str(path))
    assert extracted["原事件名"]["original_marker"] == "EventName"

    json_release._apply_translations_to_file(
        str(path), {"原事件名": {"text": "译事件名"}},
    )
    assert path.read_text(encoding="utf-8") == "#EventName#\n译事件名\n"


def test_import_updates_title_and_transcodes_ini(monkeypatch, tmp_path):
    game_path = tmp_path / "game"
    scripts = game_path / "StringScripts"
    origin = game_path / "StringScripts_Origin"
    scripts.mkdir(parents=True)
    origin.mkdir()
    (game_path / "RPG_RT.lmt").write_bytes(b"LMT")
    _write_ini(game_path / "RPG_RT.ini", "ゲーム")
    _write_title(scripts / "title.txt", "游戏标题")
    _write_title(origin / "title.txt", "ゲーム")
    monkeypatch.setattr(import_task.rpgrewriter, "import_text_command", lambda *_args: (0, "", ""))

    import_task.run_import(str(game_path), "932", "936", Queue())

    text = (game_path / "RPG_RT.ini").read_bytes().decode("cp936")
    assert "GameTitle=游戏标题" in text
    assert "Encoding=932" in text


def test_import_without_title_only_transcodes_ini(monkeypatch, tmp_path):
    game_path = tmp_path / "game"
    scripts = game_path / "StringScripts"
    scripts.mkdir(parents=True)
    (game_path / "RPG_RT.lmt").write_bytes(b"LMT")
    _write_ini(game_path / "RPG_RT.ini", "ゲーム")
    monkeypatch.setattr(import_task.rpgrewriter, "import_text_command", lambda *_args: (0, "", ""))

    import_task.run_import(str(game_path), "932", "936", Queue())

    text = (game_path / "RPG_RT.ini").read_bytes().decode("cp936")
    assert "GameTitle=ゲーム" in text
    assert "Encoding=932" in text


def test_import_rejects_title_not_representable_by_target_encoding(monkeypatch, tmp_path):
    game_path = tmp_path / "game"
    scripts = game_path / "StringScripts"
    origin = game_path / "StringScripts_Origin"
    scripts.mkdir(parents=True)
    origin.mkdir()
    (game_path / "RPG_RT.lmt").write_bytes(b"LMT")
    ini_path = game_path / "RPG_RT.ini"
    _write_ini(ini_path, "ゲーム")
    original_bytes = ini_path.read_bytes()
    _write_title(scripts / "title.txt", "title 😀")
    _write_title(origin / "title.txt", "ゲーム")
    called = []
    monkeypatch.setattr(import_task.rpgrewriter, "import_text_command", lambda *_args: called.append(True))

    import_task.run_import(str(game_path), "932", "936", Queue())

    assert called == []
    assert ini_path.read_bytes() == original_bytes


def test_import_failure_keeps_original_ini(monkeypatch, tmp_path):
    game_path = tmp_path / "game"
    scripts = game_path / "StringScripts"
    scripts.mkdir(parents=True)
    (game_path / "RPG_RT.lmt").write_bytes(b"LMT")
    ini_path = game_path / "RPG_RT.ini"
    _write_ini(ini_path, "ゲーム")
    original_hash = hashlib.sha256(ini_path.read_bytes()).hexdigest()
    monkeypatch.setattr(import_task.rpgrewriter, "import_text_command", lambda *_args: (1, "", "failed"))

    import_task.run_import(str(game_path), "932", "936", Queue())

    assert hashlib.sha256(ini_path.read_bytes()).hexdigest() == original_hash
    assert not (game_path / "RPG_RT.ini.import.tmp").exists()


@pytest.mark.parametrize("source_encoding,target_encoding", [("932", "936"), ("936", "950"), ("950", "932")])
def test_ini_transcoding_uses_selected_codepages(tmp_path, source_encoding, target_encoding):
    ini_path = tmp_path / "RPG_RT.ini"
    _write_ini(ini_path, "中文", source_encoding)

    text, _encoding = rpg_rt_ini.read_ini(ini_path, source_encoding)
    converted = rpg_rt_ini.encode_ini(text, target_encoding)

    assert converted.decode(f"cp{target_encoding}") == text


def test_ini_prefers_export_encoding_over_easyrpg_setting(tmp_path):
    ini_path = tmp_path / "RPG_RT.ini"
    ini_path.write_bytes(
        "[RPG_RT]\nGameTitle=死目芳名帳ver1.2\n\n[EasyRPG]\nEncoding=936\n".encode("cp932")
    )

    text, encoding = rpg_rt_ini.read_ini(ini_path, "932")

    assert encoding == "932"
    assert rpg_rt_ini.get_game_title(text) == "死目芳名帳ver1.2"
