# tests/test_text_processing.py
"""文本处理工具测试。"""

import pytest

from core.utils.text_processing import (
    convert_half_to_full_katakana,
    has_japanese_letters,
    post_process_translation,
    pre_process_text_for_llm,
    repair_translation_format,
    restore_pua_placeholders,
    sanitize_filename,
    validate_translation,
)


# ---------------------------------------------------------------------------
# PUA 占位符往返
# ---------------------------------------------------------------------------

class TestPUARoundtrip:
    """pre_process → restore 应完全还原原始文本。"""

    @pytest.mark.parametrize("text", [
        r"「こんにちは」",
        r"『世界』",
        r"\!テスト\.\<\>\|\^",
        r"\!\nテスト",
        "普通文本",
        "",
    ])
    def test_roundtrip(self, text: str) -> None:
        processed = pre_process_text_for_llm(text)
        restored = restore_pua_placeholders(processed)
        assert restored == text

    def test_non_string_passthrough(self) -> None:
        assert pre_process_text_for_llm(None) is None
        assert restore_pua_placeholders(123) == 123


# ---------------------------------------------------------------------------
# validate_translation
# ---------------------------------------------------------------------------

class TestValidateTranslation:
    """翻译验证规则测试。"""

    def test_valid_simple(self) -> None:
        ok, reason = validate_translation("テスト", "测试", "测试")
        assert ok is True
        assert reason == ""

    def test_reject_kana_in_translation(self) -> None:
        """译文残留假名应被拒绝。"""
        ok, reason = validate_translation("テスト", "测试テスト", "测试テスト")
        assert ok is False
        assert "假名" in reason

    def test_reject_metadata_pollution(self) -> None:
        """译文包含 [MARKER:...] 污染应被拒绝。"""
        ok, reason = validate_translation(
            "原文", "翻译[MARKER: Message]", "翻译[MARKER: Message]"
        )
        assert ok is False
        assert "元数据" in reason or "MARKER" in reason

    def test_allow_metadata_if_original_has_it(self) -> None:
        """原文本身含 [MARKER:...] 时不应误判。"""
        text = "[MARKER: Message]テスト"
        ok, _ = validate_translation(text, "测试", "[MARKER: Message]测试")
        # 不应因 MARKER 失败（但可能因假名失败，这里原文有假名但译文没有）
        # 这里只验证 MARKER 规则不触发
        assert ok is True or "元数据" not in _

    def test_reject_number_prefix_pollution(self) -> None:
        """原文无编号但译文有编号前缀应被拒绝。"""
        ok, reason = validate_translation("原文", "1. 翻译", "1. 翻译")
        assert ok is False
        assert "编号" in reason

    def test_reject_missing_backslash_prefix(self) -> None:
        r"""原文以 \\ 开头但译文没有应被拒绝。"""
        ok, reason = validate_translation(r"\\テスト", "测试", "测试")
        assert ok is False
        assert "格式符" in reason or "\\\\" in reason

    def test_reject_control_code_mismatch(self) -> None:
        r"""控制码数量不匹配应被拒绝。"""
        ok, reason = validate_translation(r"\.テスト\.", r"测试\.", r"测试\.")
        assert ok is False
        assert "标记数量" in reason

    def test_reject_missing_bracket(self) -> None:
        """「 数量少于原文应被拒绝。"""
        ok, reason = validate_translation("「テスト」", "测试", "测试")
        assert ok is False
        assert "引号" in reason or "「" in reason

    def test_reject_pua_residue(self) -> None:
        """后处理后仍含 PUA 字符应被拒绝。"""
        ok, reason = validate_translation("テスト", "测试", "测试\uE000")
        assert ok is False
        assert "PUA" in reason


# ---------------------------------------------------------------------------
# post_process_translation
# ---------------------------------------------------------------------------

class TestPostProcessTranslation:
    """翻译后处理测试。"""

    def test_punctuation_conversion(self) -> None:
        result = post_process_translation("テスト・ー♪~⋯", "原文")
        assert "·" in result
        assert "―" in result
        assert "～" in result
        assert "…" in result

    def test_remove_extra_brackets(self) -> None:
        """原文无「」但译文有时应移除。"""
        result = post_process_translation("「测试」", "原文")
        assert "「" not in result
        assert "」" not in result

    def test_keep_brackets_if_original_has(self) -> None:
        """原文有「」时译文应保留。"""
        result = post_process_translation("「测试」", "「原文」")
        assert "「" in result
        assert "」" in result

    def test_bracket_balancing(self) -> None:
        """缺少闭合引号时应自动补齐。"""
        result = post_process_translation("「测试", "「原文」")
        assert result.count("「") <= result.count("」") or result.endswith("」")

    def test_preserve_leading_newlines(self) -> None:
        """应保留原文的前导换行符。"""
        result = post_process_translation("翻译", "\n\n原文")
        assert result.startswith("\n\n")

    def test_metadata_cleanup(self) -> None:
        """清理 [MARKER:...] 污染。"""
        result = post_process_translation("[MARKER: Message]翻译", "原文")
        assert "[MARKER:" not in result

    def test_number_prefix_cleanup(self) -> None:
        """清理编号前缀污染。"""
        result = post_process_translation("1. 翻译", "原文")
        assert not result.lstrip().startswith("1.")

    def test_non_string_passthrough(self) -> None:
        assert post_process_translation(None, "x") is None


# ---------------------------------------------------------------------------
# repair_translation_format
# ---------------------------------------------------------------------------

class TestRepairTranslationFormat:
    """控制码修复测试。"""

    def test_remove_extra_codes(self) -> None:
        r"""译文多出的控制码应被移除。"""
        result = repair_translation_format(r"测试", r"\.\<测试")
        assert r"\." not in result
        assert r"\<" not in result

    def test_add_missing_codes(self) -> None:
        r"""译文缺少的控制码应被补齐。"""
        result = repair_translation_format(r"\.原文", "翻译")
        assert r"\." in result

    def test_no_change_when_matched(self) -> None:
        r"""控制码数量匹配时不应修改。"""
        text = r"\.测试\."
        result = repair_translation_format(r"\.原文\.", text)
        assert result == text

    def test_non_string_passthrough(self) -> None:
        assert repair_translation_format("x", 123) == 123


# ---------------------------------------------------------------------------
# sanitize_filename
# ---------------------------------------------------------------------------

class TestSanitizeFilename:
    """文件名清理测试。"""

    def test_normal_name(self) -> None:
        assert sanitize_filename("MyGame") == "MyGame"

    def test_illegal_chars(self) -> None:
        result = sanitize_filename('My:Game*"Test')
        assert ":" not in result
        assert "*" not in result
        assert '"' not in result

    def test_trailing_dot_space(self) -> None:
        result = sanitize_filename("game. ")
        assert not result.endswith(".")
        assert not result.endswith(" ")

    def test_reserved_name(self) -> None:
        result = sanitize_filename("CON")
        assert result != "CON"
        assert result.endswith("CON")

    def test_empty_gives_untitled(self) -> None:
        assert sanitize_filename("") == "untitled"

    def test_control_chars_removed(self) -> None:
        result = sanitize_filename("test\x00\x1f")
        assert "\x00" not in result
        assert "\x1f" not in result


# ---------------------------------------------------------------------------
# convert_half_to_full_katakana
# ---------------------------------------------------------------------------

class TestConvertHalfToFullKatakana:
    """半角→全角片假名转换测试。"""

    def test_basic_conversion(self) -> None:
        assert convert_half_to_full_katakana("ｱｲｳ") == "アイウ"

    def test_dakuten(self) -> None:
        """浊音组合应合并为单字符。"""
        result = convert_half_to_full_katakana("ｶﾞ")
        assert result == "ガ"

    def test_handakuten(self) -> None:
        """半浊音组合应合并为单字符。"""
        result = convert_half_to_full_katakana("ﾊﾟ")
        assert result == "パ"

    def test_mixed_text(self) -> None:
        """混合文本中只转换半角片假名。"""
        result = convert_half_to_full_katakana("Hello ｱｲｳ 世界")
        assert result == "Hello アイウ 世界"

    def test_non_string_passthrough(self) -> None:
        assert convert_half_to_full_katakana(None) is None

    def test_empty_string(self) -> None:
        assert convert_half_to_full_katakana("") == ""


# ---------------------------------------------------------------------------
# has_japanese_letters
# ---------------------------------------------------------------------------

class TestHasJapaneseLetters:
    """日文字符检测测试。"""

    def test_hiragana(self) -> None:
        assert has_japanese_letters("あいう") is True

    def test_katakana(self) -> None:
        assert has_japanese_letters("アイウ") is True

    def test_kanji(self) -> None:
        assert has_japanese_letters("漢字") is True

    def test_half_katakana(self) -> None:
        assert has_japanese_letters("ｱｲｳ") is True

    def test_no_japanese(self) -> None:
        assert has_japanese_letters("Hello World 123") is False

    def test_empty(self) -> None:
        assert has_japanese_letters("") is False

    def test_none(self) -> None:
        assert has_japanese_letters(None) is False
