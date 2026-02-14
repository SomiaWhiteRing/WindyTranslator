# core/path_resolver.py
"""游戏相关文件路径解析器。

将 app.py 中散落的路径计算逻辑集中到单一职责类中，
消除 RPGTranslatorApp 对路径细节的直接依赖。
"""

from __future__ import annotations

import os
import logging

from core.utils import text_processing

log = logging.getLogger(__name__)


class PathResolver:
    """游戏相关文件路径解析器。

    所有路径方法均为纯计算，不产生副作用（不创建目录、不读写文件）。
    """

    def __init__(self, works_dir: str) -> None:
        """初始化路径解析器。

        Args:
            works_dir: Works 工作目录的根路径。
        """
        self.works_dir = works_dir

    def get_work_subfolder(self, game_path: str) -> str:
        """获取游戏对应的 Works 子目录名。

        Args:
            game_path: 游戏根目录路径。

        Returns:
            清理后的子目录名（如 "MyGame"），若无法解析则返回 "UntitledGame"。
        """
        game_folder_name = text_processing.sanitize_filename(
            os.path.basename(game_path)
        )
        return game_folder_name or "UntitledGame"

    def get_work_game_dir(self, game_path: str) -> str:
        """获取游戏对应的 Works 完整目录路径。

        Args:
            game_path: 游戏根目录路径。

        Returns:
            例如 "Works/MyGame"。
        """
        return os.path.join(self.works_dir, self.get_work_subfolder(game_path))

    def get_untranslated_dir(self, game_path: str) -> str:
        """获取未翻译文件存放目录。

        Args:
            game_path: 游戏根目录路径。

        Returns:
            例如 "Works/MyGame/untranslated"。
        """
        return os.path.join(self.get_work_game_dir(game_path), "untranslated")

    def get_translated_dir(self, game_path: str) -> str:
        """获取翻译文件存放目录。

        Args:
            game_path: 游戏根目录路径。

        Returns:
            例如 "Works/MyGame/translated"。
        """
        return os.path.join(self.get_work_game_dir(game_path), "translated")

    def get_fallback_csv_path(self, game_path: str) -> str:
        """获取回退修正 CSV 文件路径。

        Args:
            game_path: 游戏根目录路径。

        Returns:
            例如 "Works/MyGame/translated/fallback_corrections.csv"。
        """
        return os.path.join(
            self.get_translated_dir(game_path), "fallback_corrections.csv"
        )

    def get_translated_json_path(self, game_path: str) -> str:
        """获取翻译后 JSON 文件路径。

        Args:
            game_path: 游戏根目录路径。

        Returns:
            例如 "Works/MyGame/translated/translation_translated.json"。
        """
        return os.path.join(
            self.get_translated_dir(game_path), "translation_translated.json"
        )

    def find_translated_json_files(self, game_path: str) -> list[str]:
        """查找游戏已翻译的 JSON 文件列表。

        Args:
            game_path: 游戏根目录路径。

        Returns:
            JSON 文件完整路径列表，若目录不存在则返回空列表。
        """
        translated_dir = self.get_translated_dir(game_path)
        if not os.path.isdir(translated_dir):
            return []
        try:
            return [
                os.path.join(translated_dir, f)
                for f in os.listdir(translated_dir)
                if f.lower().endswith(".json")
            ]
        except OSError as e:
            log.error(f"无法读取翻译目录 {translated_dir}: {e}")
            return []
