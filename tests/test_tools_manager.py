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
