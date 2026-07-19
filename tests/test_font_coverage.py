from core.engines import wolf
from core.utils import font_coverage
import os
import shutil


def test_fusion_pixel_font_covers_simplified_chinese():
    font_path = wolf._fusion_font_path()

    assert font_coverage.missing_characters(font_path, "简体中文翻译游戏战斗伤害恢复") == set()
    assert font_coverage.missing_characters(font_path, "\U0010ffff") == {"\U0010ffff"}


def test_font_metadata_supports_ttc_on_windows():
    path = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "msyh.ttc")
    if not os.path.isfile(path):
        return

    info = font_coverage.font_file_info(path)

    assert "Microsoft YaHei" in info["families"]
    assert ord("中") in info["codepoints"]


def test_font_catalog_fingerprint_changes_with_module_fonts(tmp_path):
    source = wolf._fusion_font_path()
    destination = tmp_path / os.path.basename(source)
    shutil.copy2(source, destination)
    before = font_coverage.font_catalog_fingerprint(str(tmp_path), include_system=False)
    destination.touch()
    after = font_coverage.font_catalog_fingerprint(str(tmp_path), include_system=False)

    assert before != after


def test_visible_font_candidates_filter_system_coverage_and_later_duplicates(monkeypatch):
    candidates = [
        {"source": "module", "family": "Shared", "files": [{"path": "module.ttf"}]},
        {"source": "game", "family": "Shared", "files": [{"path": "game.ttf"}]},
        {"source": "system", "family": "Good", "files": [{"path": "good.ttf"}]},
        {"source": "system", "family": "Ten Missing", "files": [{"path": "bad.ttf"}]},
    ]
    missing = {"good.ttf": set("123456789"), "bad.ttf": set("1234567890")}
    monkeypatch.setattr(
        font_coverage,
        "missing_characters_in_files",
        lambda paths, _characters: missing.get(paths[0], set()),
    )

    visible = font_coverage.visible_font_candidates(candidates, "required")

    assert [(item["source"], item["family"]) for item in visible] == [
        ("module", "Shared"),
        ("system", "Good"),
    ]


def test_resolve_available_font_family_uses_installed_alias():
    candidate = {
        "family": "铁蒺藜体 简",
        "aliases": ("Tiejili SC", "铁蒺藜体 简"),
    }

    assert font_coverage.resolve_available_font_family(candidate, ("Tiejili SC",)) == "Tiejili SC"
