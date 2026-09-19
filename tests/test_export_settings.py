import hashlib

from core.tasks import import_task


class Queue:
    def __init__(self):
        self.messages = []

    def put(self, message):
        self.messages.append(message)


def _write_ini(path, title, encoding="932"):
    path.write_bytes((f"[RPG_RT]\nGameTitle={title}\n\n[EasyRPG]\nEncoding={encoding}\n").encode(f"cp{encoding}"))


def _write_title(path, title):
    path.write_text(f"#GameTitle#\n{title}\n", encoding="utf-8")


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
