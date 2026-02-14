# tests/test_engine_detection.py
"""引擎检测测试。"""

import os

import pytest

from core.utils.engine_detection import DetectedGame, detect_game_engine


class TestDetectGameEngine:
    """detect_game_engine() 测试。"""

    def test_empty_path_returns_none(self) -> None:
        assert detect_game_engine("") is None

    def test_nonexistent_path_returns_none(self, tmp_path: os.PathLike) -> None:
        fake = os.path.join(str(tmp_path), "no_such_dir")
        assert detect_game_engine(fake) is None

    def test_rm200x_detected(self, tmp_path: os.PathLike) -> None:
        """存在 RPG_RT.lmt 时应检测为 rm200x。"""
        game_dir = str(tmp_path)
        lmt = os.path.join(game_dir, "RPG_RT.lmt")
        with open(lmt, "w") as f:
            f.write("dummy")

        result = detect_game_engine(game_dir)
        assert result is not None
        assert result.engine == "rm200x"
        assert "RPG_RT.lmt" in result.reason

    def test_vxace_detected(self, tmp_path: os.PathLike) -> None:
        """存在 Data/MapInfos.rvdata2 时应检测为 vxace。"""
        game_dir = str(tmp_path)
        data_dir = os.path.join(game_dir, "Data")
        os.makedirs(data_dir, exist_ok=True)
        with open(os.path.join(data_dir, "MapInfos.rvdata2"), "w") as f:
            f.write("dummy")

        result = detect_game_engine(game_dir)
        assert result is not None
        assert result.engine == "vxace"

    def test_rm200x_takes_priority(self, tmp_path: os.PathLike) -> None:
        """同时存在两种标志文件时，rm200x 优先。"""
        game_dir = str(tmp_path)
        with open(os.path.join(game_dir, "RPG_RT.lmt"), "w") as f:
            f.write("dummy")
        data_dir = os.path.join(game_dir, "Data")
        os.makedirs(data_dir, exist_ok=True)
        with open(os.path.join(data_dir, "MapInfos.rvdata2"), "w") as f:
            f.write("dummy")

        result = detect_game_engine(game_dir)
        assert result is not None
        assert result.engine == "rm200x"

    def test_no_marker_files(self, tmp_path: os.PathLike) -> None:
        """目录存在但无标志文件时返回 None。"""
        assert detect_game_engine(str(tmp_path)) is None
