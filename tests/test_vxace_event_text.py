from core.engines.vxace import (
    _event_command_fields,
    _export_command_list_to_lines,
    _extract_note_tag_values,
    _new_event_command,
    _replace_note_tag_values,
    _update_event_command_list,
)


def test_export_command_list_includes_scroll_text_blocks():
    cmd_list = [
        _new_event_command(405, 0, ["Line A"]),
        _new_event_command(405, 0, ["Line B"]),
    ]

    lines = _export_command_list_to_lines(cmd_list)

    assert lines == ["#ScrollText#\n", "Line A\n", "Line B\n", "##\n"]


def test_update_event_command_list_translates_scroll_text_and_preserves_original_marker():
    cmd_list = [
        _new_event_command(405, 0, ["Line A"]),
        _new_event_command(405, 0, ["Line B"]),
    ]

    modified = _update_event_command_list(cmd_list, {"Line A\nLine B": "Translated A\nTranslated B"})

    assert modified is True
    code0, _indent0, params0 = _event_command_fields(cmd_list[0])
    code1, _indent1, params1 = _event_command_fields(cmd_list[1])
    code2, _indent2, params2 = _event_command_fields(cmd_list[2])
    assert code0 == 108
    assert params0[0].startswith("<ORIGINAL_SCROLL_TEXT:")
    assert code1 == 405
    assert params1 == ["Translated A"]
    assert code2 == 405
    assert params2 == ["Translated B"]


def test_monster_book_note_helpers_only_touch_description_tags():
    note = "<オートステート 36>\n<図鑑説明:Line A>\n<図鑑説明:>\n<図鑑説明:Line B>\n<図鑑無効>"

    assert _extract_note_tag_values(note, "図鑑説明", include_empty=False) == ["Line A", "Line B"]

    new_note, changed = _replace_note_tag_values(note, "図鑑説明", ["Translated A", "Translated B"])

    assert changed is True
    assert new_note == (
        "<オートステート 36>\n"
        "<図鑑説明:Translated A>\n"
        "<図鑑説明:>\n"
        "<図鑑説明:Translated B>\n"
        "<図鑑無効>"
    )
