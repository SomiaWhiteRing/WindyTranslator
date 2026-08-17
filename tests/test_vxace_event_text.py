from core.engines.vxace import _event_command_fields, _new_event_command, _update_event_command_list


def test_update_event_command_list_translates_scroll_text_and_preserves_original_marker():
    commands = [_new_event_command(405, 0, ["Line A"]), _new_event_command(405, 0, ["Line B"])]

    assert _update_event_command_list(commands, {"Line A\nLine B": "Translated A\nTranslated B"}) is True
    code, _indent, params = _event_command_fields(commands[0])
    next_code, _next_indent, next_params = _event_command_fields(commands[1])
    final_code, _final_indent, final_params = _event_command_fields(commands[2])

    assert code == 108
    assert params[0].startswith("<ORIGINAL_SCROLL_TEXT:")
    assert next_code == 405
    assert next_params == ["Translated A"]
    assert final_code == 405
    assert final_params == ["Translated B"]
