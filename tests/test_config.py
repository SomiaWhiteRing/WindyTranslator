import json

from core.config import ConfigManager


def test_config_manager_adds_auto_import_after_release_default(tmp_path):
    config_path = tmp_path / "app_config.json"
    config_path.write_text(
        json.dumps(
            {
                "pro_mode_settings": {
                    "export_encoding": "932",
                    "import_encoding": "936",
                    "rtp_options": {"2000": True},
                }
            }
        ),
        encoding="utf-8",
    )

    config = ConfigManager(str(config_path)).load_config()

    assert config["pro_mode_settings"]["auto_import_after_release"] is False
    assert config["pro_mode_settings"]["export_scope"] == {
        "game_text": True,
        "game_title": True,
        "map_names": False,
        "map_event_names": False,
        "switch_names": False,
        "variable_names": False,
        "common_event_names": False,
        "troop_names": False,
    }


def test_config_manager_adds_completion_notification_default(tmp_path):
    config_path = tmp_path / "app_config.json"
    config_path.write_text(json.dumps({"selected_mode": "pro"}), encoding="utf-8")

    config = ConfigManager(str(config_path)).load_config()

    assert config["enable_completion_notification"] is False
