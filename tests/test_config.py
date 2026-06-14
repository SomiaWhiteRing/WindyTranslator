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
