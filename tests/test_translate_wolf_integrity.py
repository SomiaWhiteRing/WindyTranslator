import queue

from core.tasks import apply_base_dictionary
from core.tasks import translate
from core.engines import wolf
from core.utils import default_database
from core.utils.text_processing import (
    contains_japanese_kana,
    has_japanese_letters,
    validate_translation,
)


def test_worker_fallback_uses_original_json_key(monkeypatch):
    def fail(*_args, **_kwargs):
        raise RuntimeError("forced failure")

    monkeypatch.setattr(translate, "_translate_batch_with_retry", fail)
    monkeypatch.setattr(translate, "_log_batch_error", lambda *_args, **_kwargs: None)
    item = {
        "original_json_key": "\uff71\uff72\uff73",
        "text_to_translate": "アイウ",
        "original_marker": "Message",
        "speaker_id": "SYSTEM",
    }

    _file_name, result = translate._translation_worker(
        [item], [], "sample.txt", {}, {}, object(), {"model": "test"}, None, None
    )

    assert set(result) == {"\uff71\uff72\uff73"}
    assert result["\uff71\uff72\uff73"]["text"] == "アイウ"
    assert result["\uff71\uff72\uff73"]["status"] == "fallback"


def test_non_wolf_message_line_count_remains_unrestricted():
    valid, reason = validate_translation("一行\n二行", "合并为一行", "合并为一行")

    assert valid is True
    assert reason == ""


def test_kana_validation_ignores_punctuation_and_kaomoji():
    accepted = (
        (r"\f[14]・", r"\f[14]・"),
        ("・マラソン大会", "・马拉松大会"),
        ("わかるかな～？(*￣ー￣)ニヤリ", "明白了吗～？(*￣ー￣) 嘿嘿"),
        ("すごい！(*≧▽≦ノノ゛☆.+゜パチパチ", "厉害！(*≧▽≦ノノ゛☆.+゜啪啪啪"),
    )

    for original, text in accepted:
        valid, reason = validate_translation(original, text, text)
        assert valid is True, reason
        assert contains_japanese_kana(text) is False

    assert has_japanese_letters("・ー゛゜") is False
    assert has_japanese_letters("(*≧▽≦ﾉﾉﾞ☆.+ﾟ)") is False
    assert contains_japanese_kana("ノノ") is True


def test_kana_validation_still_rejects_real_kana_letters():
    for text in ("中文です", "游戏テスト", "接收しました"):
        valid, reason = validate_translation("原文", text, text)
        assert valid is False
        assert "残留日语假名" in reason
        assert contains_japanese_kana(text) is True


def test_kana_validation_allows_protected_logic_literal_with_bullets():
    original = "・新合言葉「アトガキ」"
    translated = "・新口令「アトガキ」"

    valid, reason = validate_translation(
        original,
        translated,
        translated,
        allowed_source_literals=("アトガキ",),
    )

    assert valid is True, reason


def test_wolf_logic_literal_only_protects_quoted_display_occurrences():
    literals = ("ソフィア",)

    assert translate._protected_literals_for_text("ソフィア", literals) == []
    assert translate._protected_literals_for_text("犯人は「ソフィア」です。", literals) == ["ソフィア"]


def test_wolf_logic_result_is_not_reused_after_display_promotion():
    old_logic_result = {
        "text": "原文",
        "status": "success",
        "original_marker": "WOLFLogic",
    }

    assert translate._is_reusable_translation_result(
        old_logic_result, {"original_marker": "WOLFText"}
    ) is False
    assert translate._is_reusable_translation_result(
        old_logic_result, {"original_marker": "WOLFLogic"}
    ) is True
    assert translate._is_reusable_translation_result(
        {"text": "译文", "status": "success", "original_marker": "Message"},
        {"original_marker": "Message"},
    ) is True


def test_wolf_transport_validator_rejects_api_result_before_checkpoint(tmp_path):
    class Client:
        def chat_completion(self, *_args, **_kwargs):
            return True, "<textarea>\n1.中文\n</textarea>", None

    tag = "{{WINDY_WOLF_0_01ba4719}}"
    item = {
        "original_json_key": tag + "原文",
        "text_to_translate": tag + "原文",
        "original_marker": "Message",
        "speaker_id": "SYSTEM",
    }
    result = translate._translate_batch_with_retry(
        [item], [], [], [], Client(),
        {
            "model": "test",
            "max_retries": 0,
            "_translation_validator": wolf.validate_translation_transport,
            "_translation_validator_instruction": wolf.TRANSLATION_TRANSPORT_INSTRUCTION,
        },
        str(tmp_path / "errors.log"),
        __import__("threading").Lock(),
        "sample.txt",
    )

    assert result[item["original_json_key"]]["status"] == "fallback"


def test_wolf_strict_line_fallback_runs_transport_validator(tmp_path):
    class Client:
        prompt = ""

        def chat_completion(self, _model, messages, **_kwargs):
            self.prompt = messages[0]["content"]
            return True, "<textarea>\n1.中文\n2.第二行\n</textarea>", None

    client = Client()
    tag = "{{WINDY_WOLF_0_01ba4719}}"
    success, _repaired, _translated, reason = translate._translate_strict_block_by_lines(
        original_block_text=f"{tag}原文\n第二行",
        marker_type="WOLFText",
        speaker_id=None,
        api_client=client,
        model_name="test",
        config={
            "_translation_validator": wolf.validate_translation_transport,
            "_translation_validator_instruction": wolf.TRANSLATION_TRANSPORT_INSTRUCTION,
        },
        prompt_template="{batch_text}",
        character_glossary_section="",
        entity_glossary_section="",
        context_section="",
        current_processing_file_name="sample.txt",
        error_log_path=str(tmp_path / "errors.log"),
        error_log_lock=__import__("threading").Lock(),
    )

    assert success is False
    assert "WOLF 控制码标签" in reason
    assert "WOLF 内部标签保护" in client.prompt


def test_wolf_skips_default_database_mapping(tmp_path, monkeypatch):
    game_path = tmp_path / "Game"
    game_path.mkdir()
    (game_path / "Game.exe").write_bytes(b"")
    (game_path / "Data.wolf").write_bytes(b"")

    monkeypatch.setattr(
        default_database,
        "_load_from_modules_csv",
        lambda: (_ for _ in ()).throw(AssertionError("WOLF must not load the default mapping")),
    )

    assert default_database.load_default_db_mapping(str(game_path)) == ({}, set())


def test_wolf_manual_base_dictionary_action_is_skipped(tmp_path, monkeypatch):
    game_path = tmp_path / "Game"
    game_path.mkdir()
    (game_path / "Game.exe").write_bytes(b"")
    (game_path / "Data.wolf").write_bytes(b"")
    messages = queue.Queue()
    monkeypatch.setattr(
        apply_base_dictionary.dictionary_manager,
        "load_base_dictionaries",
        lambda: (_ for _ in ()).throw(AssertionError("WOLF must not load bundled base dictionaries")),
    )

    apply_base_dictionary.run_apply_base_dictionary(
        str(game_path), str(tmp_path), {}, messages
    )

    emitted = list(messages.queue)
    assert ("status", "应用基础字典已跳过 (WOLF)") in emitted
    assert emitted[-1] == ("done", None)
