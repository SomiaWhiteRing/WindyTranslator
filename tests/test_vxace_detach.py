import io

from rubymarshal import reader, writer
from rubymarshal.classes import RubyObject, RubyString, UserDef

import pytest

from core.engines.vxace import (
    VXAceError,
    _detach_event_command_parameter_aliases,
    _validate_no_corrupted_show_text_commands_in_common_events,
)


def _roundtrip(obj):
    buf = io.BytesIO()
    writer.write(buf, obj)
    buf.seek(0)
    return reader.load(buf)


def test_detach_preserves_ruby_string_values_after_marshal_roundtrip():
    cmd = RubyObject(
        "RPG::EventCommand",
        attributes={
            "@parameters": [
                RubyString("hello", attributes={"E": True}),
                [RubyString("choice", attributes={"E": True})],
            ]
        },
    )

    _detach_event_command_parameter_aliases([cmd])
    reloaded_params = _roundtrip(cmd.attributes["@parameters"])

    assert isinstance(reloaded_params[0], RubyString)
    assert reloaded_params[0].text == "hello"
    assert reloaded_params[0].attributes == {"E": True}
    assert isinstance(reloaded_params[1][0], RubyString)
    assert reloaded_params[1][0].text == "choice"


def test_detach_clones_nested_ruby_objects_and_breaks_shared_references():
    shared_move = RubyObject(
        "RPG::MoveCommand",
        attributes={"@code": 1, "@parameters": [RubyString("Move", attributes={"E": True})]},
    )
    move_route = RubyObject(
        "RPG::MoveRoute",
        attributes={"@list": [shared_move], "@repeat": True, "@skippable": False, "@wait": False},
    )
    cmd = RubyObject(
        "RPG::EventCommand",
        attributes={"@parameters": [move_route, shared_move]},
    )

    _detach_event_command_parameter_aliases([cmd])
    params = cmd.attributes["@parameters"]

    assert params[0].attributes["@list"][0] is not shared_move
    assert params[1] is not shared_move
    assert params[0].attributes["@list"][0] is not params[1]
    assert params[0].attributes["@list"][0].attributes["@parameters"][0].text == "Move"
    assert params[1].attributes["@parameters"][0].text == "Move"


def test_detach_preserves_userdef_objects_after_marshal_roundtrip():
    color = UserDef("Color")
    color._load(b"\x00" * 32)
    cmd = RubyObject("RPG::EventCommand", attributes={"@parameters": [color]})

    _detach_event_command_parameter_aliases([cmd])
    reloaded_params = _roundtrip(cmd.attributes["@parameters"])

    assert isinstance(reloaded_params[0], UserDef)
    assert reloaded_params[0].ruby_class_name == "Color"
    assert reloaded_params[0]._dump() == b"\x00" * 32


def test_common_event_validation_rejects_downgraded_color_objects():
    bad_color = RubyObject("Color", attributes={})
    cmd = RubyObject("RPG::EventCommand", attributes={"@code": 224, "@indent": 0, "@parameters": [bad_color]})
    common_event = RubyObject("RPG::CommonEvent", attributes={"@list": [cmd]})

    with pytest.raises(VXAceError, match="Color"):
        _validate_no_corrupted_show_text_commands_in_common_events([None, common_event], "CommonEvents.rvdata2")
