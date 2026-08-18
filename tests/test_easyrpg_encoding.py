import pytest

from core.external import easyrpg
from core.utils import rpg_rt_ini


def test_resolve_explicit_encoding_skips_detection(monkeypatch, tmp_path):
    def fail(*_args):
        raise AssertionError("explicit encoding must not invoke EasyRPG")

    monkeypatch.setattr(easyrpg, "detect_game_encoding", fail)

    assert easyrpg.resolve_game_encoding(str(tmp_path), "936") == "936"


def test_detect_game_encoding_normalizes_player_output(monkeypatch, tmp_path):
    class Completed:
        returncode = 0
        stdout = "ibm-943_P15A-2003\n"
        stderr = ""

    player = tmp_path / "Player.exe"
    player.write_bytes(b"")
    monkeypatch.setattr(easyrpg, "EASYRPG_PLAYER_PATH", str(player))
    monkeypatch.setattr(easyrpg.subprocess, "run", lambda *_args, **_kwargs: Completed())

    assert easyrpg.detect_game_encoding(str(tmp_path)) == "932"


def test_detect_game_encoding_rejects_unknown_output(monkeypatch, tmp_path):
    class Completed:
        returncode = 0
        stdout = "windows-936\n"
        stderr = ""

    player = tmp_path / "Player.exe"
    player.write_bytes(b"")
    monkeypatch.setattr(easyrpg, "EASYRPG_PLAYER_PATH", str(player))
    monkeypatch.setattr(easyrpg.subprocess, "run", lambda *_args, **_kwargs: Completed())

    with pytest.raises(easyrpg.EncodingDetectionError, match="上报开发者或手动指定编码"):
        easyrpg.detect_game_encoding(str(tmp_path))


def test_set_easy_rpg_encoding_adds_or_replaces_and_removes_duplicates():
    original = "[RPG_RT]\nGameTitle=Title\n\n[EasyRPG]\nEncoding=932\nEncoding=936\n"

    updated = rpg_rt_ini.set_easy_rpg_encoding(original, "950")

    assert updated.count("Encoding=") == 1
    assert "Encoding=950" in updated

    added = rpg_rt_ini.set_easy_rpg_encoding("[RPG_RT]\nGameTitle=Title\n", "932")
    assert "[EasyRPG]" in added
    assert "Encoding=932" in added
