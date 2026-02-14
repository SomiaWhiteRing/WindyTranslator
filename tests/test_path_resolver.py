# tests/test_path_resolver.py
"""路径解析器测试。"""

import os

import pytest

from core.path_resolver import PathResolver


class TestPathResolver:
    """PathResolver 各方法测试。"""

    @pytest.fixture()
    def resolver(self, tmp_works_dir: str) -> PathResolver:
        return PathResolver(tmp_works_dir)

    def test_get_work_subfolder_normal(self, resolver: PathResolver) -> None:
        result = resolver.get_work_subfolder("/games/MyRPG")
        assert result == "MyRPG"

    def test_get_work_subfolder_sanitizes(self, resolver: PathResolver) -> None:
        """含非法字符的目录名应被清理。"""
        result = resolver.get_work_subfolder('/games/My:RPG*"Test')
        assert ":" not in result
        assert "*" not in result
        assert '"' not in result

    def test_get_work_subfolder_empty_basename(self, resolver: PathResolver) -> None:
        """空 basename 经 sanitize_filename 后返回 'untitled'。"""
        result = resolver.get_work_subfolder("/")
        # sanitize_filename("") → "untitled"，非空所以不触发 "UntitledGame" 兜底
        assert result == "untitled"

    def test_get_work_game_dir(self, resolver: PathResolver, tmp_works_dir: str) -> None:
        result = resolver.get_work_game_dir("/games/TestGame")
        assert result == os.path.join(tmp_works_dir, "TestGame")

    def test_get_untranslated_dir(self, resolver: PathResolver, tmp_works_dir: str) -> None:
        result = resolver.get_untranslated_dir("/games/TestGame")
        assert result.endswith(os.path.join("TestGame", "untranslated"))

    def test_get_translated_dir(self, resolver: PathResolver, tmp_works_dir: str) -> None:
        result = resolver.get_translated_dir("/games/TestGame")
        assert result.endswith(os.path.join("TestGame", "translated"))

    def test_get_fallback_csv_path(self, resolver: PathResolver) -> None:
        result = resolver.get_fallback_csv_path("/games/TestGame")
        assert result.endswith("fallback_corrections.csv")
        assert "translated" in result

    def test_get_translated_json_path(self, resolver: PathResolver) -> None:
        result = resolver.get_translated_json_path("/games/TestGame")
        assert result.endswith("translation_translated.json")

    def test_find_translated_json_files_empty(self, resolver: PathResolver) -> None:
        """翻译目录不存在时返回空列表。"""
        result = resolver.find_translated_json_files("/games/NoSuchGame")
        assert result == []

    def test_find_translated_json_files_with_files(
        self, resolver: PathResolver, tmp_works_dir: str
    ) -> None:
        """翻译目录中有 JSON 文件时应返回完整路径。"""
        translated_dir = os.path.join(tmp_works_dir, "TestGame", "translated")
        os.makedirs(translated_dir, exist_ok=True)
        # 创建测试文件
        for name in ["a.json", "b.json", "readme.txt"]:
            with open(os.path.join(translated_dir, name), "w") as f:
                f.write("{}")

        result = resolver.find_translated_json_files("/games/TestGame")
        basenames = [os.path.basename(p) for p in result]
        assert "a.json" in basenames
        assert "b.json" in basenames
        assert "readme.txt" not in basenames
