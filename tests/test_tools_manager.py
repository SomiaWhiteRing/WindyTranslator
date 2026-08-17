import json
import tempfile
import unittest
from pathlib import Path

from core.tools.manager import ToolManager, ToolSpecError, load_manifest


class ToolManagerTests(unittest.TestCase):
    def test_discovers_bundled_manifests(self):
        tools, diagnostics = ToolManager("tools").discover()
        self.assertEqual(
            {tool.id for tool in tools},
            {"rm2k3_asset_auditor", "rpg_maker_proofreading", "text_word_counter", "translation_issue_review"},
        )
        self.assertEqual(diagnostics, [])

    def test_builds_source_host_command_without_shell(self):
        tool = next(x for x in ToolManager("tools").discover()[0] if x.id == "rm2k3_asset_auditor")
        command = ToolManager("tools").build_command(tool, {"project": r"C:\Game"}, ["WindyTranslator.exe"])
        self.assertEqual(command[0:4], ["WindyTranslator.exe", "--run-tool", str(tool.root), str(tool.entry)])
        self.assertIn(r"C:\Game", command)

    def test_rejects_entry_outside_tool_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "safe"
            root.mkdir()
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({
                "manifest_version": 1,
                "id": "safe",
                "name": "Safe",
                "version": "1",
                "author": "test",
                "entry": "../escape.py",
                "runtime": "host_python",
                "arguments": [],
            }), encoding="utf-8")
            with self.assertRaises(ToolSpecError):
                load_manifest(manifest)

    def test_rejects_unknown_argument_types(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "safe"
            root.mkdir()
            (root / "entry.py").write_text("", encoding="utf-8")
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({
                "manifest_version": 1,
                "id": "safe",
                "entry": "entry.py",
                "runtime": "host_python",
                "arguments": [{"name": "input", "flag": "--input", "type": "unknown"}],
            }), encoding="utf-8")
            with self.assertRaisesRegex(ToolSpecError, "不支持的参数类型: unknown"):
                load_manifest(manifest)


if __name__ == "__main__":
    unittest.main()
