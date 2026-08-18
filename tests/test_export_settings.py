import hashlib

from core.tasks import export, import_task


class Queue:
    def __init__(self):
        self.messages = []

    def put(self, message):
        self.messages.append(message)


def _scope():
    return {"game_text": True, "game_title": True, "map_names": False,
            "map_event_names": False, "switch_names": False,
            "variable_names": False, "common_event_names": False,
            "troop_names": False}


def _write_ini(path, title, encoding="932"):
    path.write_bytes((f"[RPG_RT]\nGameTitle={title}\n\n[EasyRPG]\nEncoding={encoding}\n").encode(f"cp{encoding}"))


def _write_title(path, title):
    path.write_text(f"#GameTitle#\n{title}\n", encoding="utf-8")


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


def test_import_without_ini_still_imports_text(monkeypatch, tmp_path):
    game_path = tmp_path / "game"
    (game_path / "StringScripts").mkdir(parents=True)
    lmt_path = game_path / "RPG_RT.lmt"
    lmt_path.write_bytes(b"LMT")
    called = []

    def import_text(path, encoding):
        called.append((path, encoding))
        return 0, "", ""

    monkeypatch.setattr(import_task.rpgrewriter, "import_text_command", import_text)

    import_task.run_import(str(game_path), "932", "936", Queue())

    assert called == [(str(lmt_path), "936")]
    assert not (game_path / "RPG_RT.ini").exists()


def test_import_rejects_unrepresentable_title_without_touching_ini(monkeypatch, tmp_path):
    game_path = tmp_path / "game"
    scripts = game_path / "StringScripts"
    origin = game_path / "StringScripts_Origin"
    scripts.mkdir(parents=True)
    origin.mkdir()
    (game_path / "RPG_RT.lmt").write_bytes(b"LMT")
    ini_path = game_path / "RPG_RT.ini"
    _write_ini(ini_path, "ゲーム")
    original_hash = hashlib.sha256(ini_path.read_bytes()).hexdigest()
    _write_title(scripts / "title.txt", "title 😀")
    _write_title(origin / "title.txt", "ゲーム")
    called = []
    monkeypatch.setattr(import_task.rpgrewriter, "import_text_command", lambda *_args: called.append(True))

    import_task.run_import(str(game_path), "932", "936", Queue())

    assert called == []
    assert hashlib.sha256(ini_path.read_bytes()).hexdigest() == original_hash
