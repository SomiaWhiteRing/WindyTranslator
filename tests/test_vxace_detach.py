import io

from rubymarshal import reader, writer
from rubymarshal.classes import RubyObject, RubyString

from core.engines.vxace import _detach_event_command_parameter_aliases


def _roundtrip(obj):
    stream = io.BytesIO()
    writer.write(stream, obj)
    stream.seek(0)
    return reader.load(stream)


def test_detach_clones_nested_objects_and_preserves_ruby_strings():
    shared = RubyObject("RPG::MoveCommand", attributes={"@parameters": [RubyString("Move", attributes={"E": True})]})
    route = RubyObject("RPG::MoveRoute", attributes={"@list": [shared], "@repeat": True, "@skippable": False, "@wait": False})
    command = RubyObject("RPG::EventCommand", attributes={"@parameters": [route, shared]})

    _detach_event_command_parameter_aliases([command])
    params = command.attributes["@parameters"]

    assert params[0].attributes["@list"][0] is not shared
    assert params[0].attributes["@list"][0] is not params[1]
    assert _roundtrip(params)[0].attributes["@list"][0].attributes["@parameters"][0].text == "Move"
