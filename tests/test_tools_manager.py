import json
import tempfile
from pathlib import Path

import pytest

from core.tools.manager import ToolManager, ToolSpecError, load_manifest


def test_load_manifest_rejects_entry_outside_tool_directory():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / "safe"
        root.mkdir()
        manifest = root / "manifest.json"
        manifest.write_text(json.dumps({
            "manifest_version": 1,
            "id": "safe",
            "entry": "../escape.py",
            "runtime": "host_python",
            "arguments": [],
        }), encoding="utf-8")

        with pytest.raises(ToolSpecError):
            load_manifest(manifest)


def test_modules_argument_requires_source_and_passes_resolved_value():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / "module_tool"
        root.mkdir()
        (root / "entry.py").write_text("", encoding="utf-8")
        manifest = root / "manifest.json"
        manifest.write_text(json.dumps({
            "manifest_version": 1,
            "id": "module_tool",
            "entry": "entry.py",
            "runtime": "host_python",
            "arguments": [{
                "name": "module_file",
                "flag": "--module-file",
                "type": "modules",
                "source": "RPGRewriter/RPGRewriter.exe",
                "required": True,
            }],
        }), encoding="utf-8")

        tool = load_manifest(manifest)
        module_file = Path("C:/runtime/modules/RPGRewriter/RPGRewriter.exe")
        command = ToolManager(root.parent).build_command(tool, {"module_file": str(module_file)}, host_executable="python")

        assert command[-2:] == ["--module-file", str(module_file)]
