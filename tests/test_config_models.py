# tests/test_config_models.py
"""Pydantic 配置模型的序列化、反序列化、验证测试。"""

import json

import pytest
from pydantic import ValidationError

from core.models.config_models import (
    AppConfig,
    ProModeSettings,
    RTPOptions,
    TranslateConfig,
    WorldDictConfig,
)


# ---------------------------------------------------------------------------
# RTPOptions
# ---------------------------------------------------------------------------

class TestRTPOptions:
    """RTP 选项模型测试。"""

    def test_defaults(self) -> None:
        opts = RTPOptions()
        assert opts.rm2000 is True
        assert opts.rm2000en is False
        assert opts.rm2003 is False
        assert opts.rm2003steam is False

    def test_from_alias_keys(self) -> None:
        """使用 JSON 别名键（"2000" 等）构造。"""
        opts = RTPOptions.model_validate({"2000": False, "2003": True})
        assert opts.rm2000 is False
        assert opts.rm2003 is True

    def test_roundtrip_by_alias(self) -> None:
        """model_dump(by_alias=True) 输出的键应与 JSON 兼容。"""
        opts = RTPOptions(rm2000=False, rm2003steam=True)
        d = opts.model_dump(by_alias=True)
        assert d["2000"] is False
        assert d["2003steam"] is True
        # 可以用别名键重新构造
        restored = RTPOptions.model_validate(d)
        assert restored == opts


# ---------------------------------------------------------------------------
# ProModeSettings
# ---------------------------------------------------------------------------

class TestProModeSettings:
    """专业模式设置测试。"""

    def test_defaults(self) -> None:
        s = ProModeSettings()
        assert s.export_encoding == "932"
        assert s.import_encoding == "936"
        assert s.rewrite_rtp_fix is False
        assert isinstance(s.rtp_options, RTPOptions)

    def test_nested_rtp(self) -> None:
        data = {"rtp_options": {"2000": False, "2003": True}}
        s = ProModeSettings.model_validate(data)
        assert s.rtp_options.rm2000 is False
        assert s.rtp_options.rm2003 is True


# ---------------------------------------------------------------------------
# WorldDictConfig
# ---------------------------------------------------------------------------

class TestWorldDictConfig:
    """世界观字典配置测试。"""

    def test_defaults(self) -> None:
        c = WorldDictConfig()
        assert c.provider == "gemini"
        assert c.api_key == ""
        assert c.openai_temperature == 0.2
        assert c.enable_base_dictionary is True

    def test_custom_values(self) -> None:
        c = WorldDictConfig(api_key="test-key", model="gpt-4o")
        assert c.api_key == "test-key"
        assert c.model == "gpt-4o"


# ---------------------------------------------------------------------------
# TranslateConfig
# ---------------------------------------------------------------------------

class TestTranslateConfig:
    """翻译配置测试。"""

    def test_defaults(self) -> None:
        c = TranslateConfig()
        assert c.batch_size == 32
        assert c.context_lines == 8
        assert c.concurrency == 16
        assert c.source_language == "日语"
        assert c.target_language == "简体中文"

    def test_batch_size_ge_1(self) -> None:
        """batch_size 必须 >= 1。"""
        with pytest.raises(ValidationError):
            TranslateConfig(batch_size=0)

    def test_concurrency_ge_1(self) -> None:
        with pytest.raises(ValidationError):
            TranslateConfig(concurrency=0)

    def test_context_lines_ge_0(self) -> None:
        with pytest.raises(ValidationError):
            TranslateConfig(context_lines=-1)

    def test_max_retries_ge_0(self) -> None:
        with pytest.raises(ValidationError):
            TranslateConfig(max_retries=-1)


# ---------------------------------------------------------------------------
# AppConfig
# ---------------------------------------------------------------------------

class TestAppConfig:
    """顶层配置模型测试。"""

    def test_defaults(self) -> None:
        cfg = AppConfig()
        assert cfg.selected_mode == "easy"
        assert isinstance(cfg.world_dict_config, WorldDictConfig)
        assert isinstance(cfg.translate_config, TranslateConfig)
        assert isinstance(cfg.pro_mode_settings, ProModeSettings)

    def test_json_roundtrip(self) -> None:
        """JSON 序列化 → 反序列化应保持一致。"""
        cfg = AppConfig(
            selected_mode="pro",
            translate_config=TranslateConfig(batch_size=64, api_key="k"),
        )
        json_str = cfg.model_dump_json()
        restored = AppConfig.model_validate_json(json_str)
        assert restored.selected_mode == "pro"
        assert restored.translate_config.batch_size == 64
        assert restored.translate_config.api_key == "k"

    def test_partial_dict_fills_defaults(self) -> None:
        """只提供部分字段时，其余字段应使用默认值。"""
        data = {"selected_mode": "pro"}
        cfg = AppConfig.model_validate(data)
        assert cfg.selected_mode == "pro"
        assert cfg.translate_config.batch_size == 32  # 默认值

    def test_empty_dict_gives_full_defaults(self) -> None:
        cfg = AppConfig.model_validate({})
        assert cfg.selected_mode == "easy"
        assert cfg.pro_mode_settings.export_encoding == "932"

    def test_rtp_alias_through_app_config(self) -> None:
        """通过嵌套 JSON 别名键构造完整配置。"""
        data = {
            "pro_mode_settings": {
                "rtp_options": {"2000": False, "2003steam": True}
            }
        }
        cfg = AppConfig.model_validate(data)
        assert cfg.pro_mode_settings.rtp_options.rm2000 is False
        assert cfg.pro_mode_settings.rtp_options.rm2003steam is True
