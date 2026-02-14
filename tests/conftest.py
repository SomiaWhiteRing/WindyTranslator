# tests/conftest.py
"""共享 pytest fixtures。"""

from __future__ import annotations

import os
import queue
import tempfile
from typing import Generator

import pytest

from core.message_broker import MessageBroker
from core.models.config_models import AppConfig


# ---------------------------------------------------------------------------
# MessageBroker fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def message_queue() -> queue.Queue:
    """返回一个空的消息队列。"""
    return queue.Queue()


@pytest.fixture()
def broker(message_queue: queue.Queue) -> MessageBroker:
    """返回一个绑定到 message_queue 的 MessageBroker 实例。"""
    return MessageBroker(message_queue)


# ---------------------------------------------------------------------------
# 配置 fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def default_config() -> AppConfig:
    """返回一个使用全部默认值的 AppConfig 实例。"""
    return AppConfig()


# ---------------------------------------------------------------------------
# 临时目录 fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_works_dir(tmp_path: os.PathLike) -> str:
    """返回一个临时 Works 目录路径。"""
    works = os.path.join(str(tmp_path), "Works")
    os.makedirs(works, exist_ok=True)
    return works


@pytest.fixture()
def tmp_game_dir(tmp_path: os.PathLike) -> str:
    """返回一个临时游戏目录路径（含 Data 子目录）。"""
    game = os.path.join(str(tmp_path), "TestGame")
    os.makedirs(os.path.join(game, "Data"), exist_ok=True)
    return game
