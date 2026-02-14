# core/config.py
"""应用程序配置管理。

使用 Pydantic v2 模型定义配置结构和默认值，替代原有的
DEFAULT_* 字典和手写 merge_dicts 递归合并逻辑。

ConfigManager 提供两种加载方式：
- load_config()       → dict（向后兼容，现有代码无需修改）
- load_config_model() → AppConfig Pydantic 模型（新代码推荐使用）
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from core.models.config_models import (
    AppConfig,
    ProModeSettings,
    TranslateConfig,
    WorldDictConfig,
)
from core.utils import file_system

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 向后兼容：保留 DEFAULT_* 字典引用，供尚未迁移的代码使用
# 这些字典由 Pydantic 模型的默认值生成，确保单一数据源
# ---------------------------------------------------------------------------

DEFAULT_WORLD_DICT_CONFIG: dict[str, Any] = WorldDictConfig().model_dump()
DEFAULT_TRANSLATE_CONFIG: dict[str, Any] = TranslateConfig().model_dump()
DEFAULT_PRO_MODE_SETTINGS: dict[str, Any] = ProModeSettings().model_dump(
    by_alias=True
)
DEFAULT_CONFIG: dict[str, Any] = AppConfig().model_dump(by_alias=True)


# ---------------------------------------------------------------------------
# 配置管理类
# ---------------------------------------------------------------------------

class ConfigManager:
    """负责加载和保存应用程序配置 (app_config.json)。

    内部使用 Pydantic AppConfig 模型处理默认值填充和验证，
    替代原有的 merge_dicts 递归合并逻辑（约 60 行）。
    """

    def __init__(self, config_file_path: str) -> None:
        """初始化配置管理器。

        Args:
            config_file_path: 配置文件的完整路径。
        """
        self.config_file_path = config_file_path
        log.info(f"配置文件路径设置为: {self.config_file_path}")

    def load_config_model(self) -> AppConfig:
        """加载配置文件并返回 Pydantic 模型。

        如果文件不存在或无效，缺失字段由 Pydantic 默认值自动填充。

        Returns:
            AppConfig Pydantic 模型实例。
        """
        loaded_data: dict[str, Any] = {}

        if os.path.exists(self.config_file_path):
            try:
                with open(self.config_file_path, "r", encoding="utf-8") as f:
                    loaded_data = json.load(f)
                log.info(f"成功从 {self.config_file_path} 加载配置。")
            except (json.JSONDecodeError, IOError) as e:
                log.error(
                    f"加载配置文件 {self.config_file_path} 失败: {e}。"
                    "将使用默认配置。"
                )
                loaded_data = {}
        else:
            log.info(
                f"配置文件 {self.config_file_path} 不存在，将使用默认配置。"
            )

        # Pydantic model_validate 自动处理：
        # 1. 缺失字段 → 使用 Field(default=...) 填充
        # 2. 类型转换 → 自动 coerce（如字符串 "32" → int 32）
        # 3. 嵌套模型 → 递归验证和填充
        # 完全替代原有的 merge_dicts + setdefault 双重保险逻辑
        try:
            config = AppConfig.model_validate(loaded_data)
        except Exception as e:
            log.error(f"配置数据验证失败: {e}。将使用完全默认配置。")
            config = AppConfig()

        return config

    def load_config(self) -> dict[str, Any]:
        """加载配置文件并返回字典（向后兼容）。

        返回的字典结构与原有 DEFAULT_CONFIG 完全一致，
        现有使用 config.get() / config["key"] 的代码无需修改。

        Returns:
            配置字典。
        """
        model = self.load_config_model()
        # by_alias=True 确保 RTPOptions 的键名使用 "2000" 而非 "rm2000"
        return model.model_dump(by_alias=True)

    def save_config(self, config_data: dict[str, Any] | AppConfig) -> bool:
        """将配置数据保存到文件。

        同时接受 dict 和 AppConfig 实例，保持向后兼容。

        Args:
            config_data: 要保存的配置（dict 或 AppConfig 实例）。

        Returns:
            保存是否成功。
        """
        try:
            # 确保配置文件所在的目录存在
            config_dir = os.path.dirname(self.config_file_path)
            if config_dir and not os.path.exists(config_dir):
                file_system.ensure_dir_exists(config_dir)

            # 统一转换为 dict 进行序列化
            if isinstance(config_data, AppConfig):
                data_to_save = config_data.model_dump(by_alias=True)
            else:
                data_to_save = config_data

            with open(self.config_file_path, "w", encoding="utf-8") as f:
                json.dump(data_to_save, f, indent=4, ensure_ascii=False)

            log.info(f"配置已成功保存到: {self.config_file_path}")
            return True
        except (IOError, TypeError) as e:
            log.exception(f"保存配置到 {self.config_file_path} 失败: {e}")
            return False
