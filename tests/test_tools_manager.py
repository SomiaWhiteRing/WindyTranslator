import json
import tempfile
import unittest
from pathlib import Path

from core.tools.manager import ToolManager, ToolSpecError, load_manifest
from ui.tools_panel import ToolsPanel


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

    def test_builds_selected_launch_option_command(self):
        tool = next(x for x in ToolManager("tools").discover()[0] if x.id == "text_word_counter")
        original_path = r"C:\游戏 目录\translation.json"
        command = ToolManager("tools").build_command(
            tool,
            {"original_json": original_path},
            ["WindyTranslator.exe"],
            "original",
        )
        self.assertIn("--mode", command)
        self.assertEqual(command[command.index("--mode") + 1], "original")
        self.assertEqual(command[command.index("--original-json") + 1], original_path)
        self.assertNotIn("--translated-json", command)

    def test_game_path_source_resolves_a_game_subdirectory(self):
        tool = next(x for x in ToolManager("tools").discover()[0] if x.id == "rpg_maker_proofreading")
        arguments = tool.arguments_for("txt")
        app = type("App", (), {"get_game_path": lambda _self: r"C:\游戏目录"})()
        panel = type("Panel", (), {"app": app})()
        values = ToolsPanel._values(panel, arguments)
        self.assertEqual(values["origin_dir"], r"C:\游戏目录\StringScripts_Origin")
        self.assertEqual(values["translated_dir"], r"C:\游戏目录\StringScripts")

    def test_uses_configured_launch_options_label(self):
        tool = next(x for x in ToolManager("tools").discover()[0] if x.id == "rpg_maker_proofreading")
        self.assertEqual(tool.launch_options_label, "校对来源")

    def test_rejects_launch_options_label_without_options(self):
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
                "arguments": [],
                "launch_options_label": "来源",
            }), encoding="utf-8")
            with self.assertRaisesRegex(ToolSpecError, "只能与 launch_options"):
                load_manifest(manifest)

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

    def test_rejects_value_on_game_path(self):
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
                "arguments": [{"name": "path", "flag": "--path", "type": "game_path", "value": "ignored"}],
            }), encoding="utf-8")
            with self.assertRaisesRegex(ToolSpecError, "game_path 参数不能包含 value"):
                load_manifest(manifest)

    def test_rejects_conflicting_launch_options_and_arguments(self):
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
                "arguments": [{"name": "project", "flag": "--project", "type": "game_path"}],
                "launch_options": [{
                    "id": "default",
                    "name": "默认",
                    "arguments": [{"name": "project", "flag": "--project", "type": "game_path"}],
                }],
            }), encoding="utf-8")
            with self.assertRaisesRegex(ToolSpecError, "不能同时使用"):
                load_manifest(manifest)

    def test_rejects_invalid_launch_options(self):
        cases = [
            (
                "duplicate",
                [
                    {"id": "same", "name": "一", "arguments": [{"name": "project", "flag": "--project", "type": "game_path"}]},
                    {"id": "same", "name": "二", "arguments": [{"name": "project", "flag": "--project", "type": "game_path"}]},
                ],
                "启动选项重复",
            ),
            (
                "unsafe",
                [{"id": "not-safe", "name": "默认", "arguments": [{"name": "project", "flag": "--project", "type": "game_path"}]}],
                "启动选项 id",
            ),
            (
                "empty",
                [{"id": "empty", "name": "默认", "arguments": []}],
                "启动选项参数不能为空",
            ),
        ]
        for case_name, options, error in cases:
            with self.subTest(case_name):
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
                        "launch_options": options,
                    }), encoding="utf-8")
                    with self.assertRaisesRegex(ToolSpecError, error):
                        load_manifest(manifest)


if __name__ == "__main__":
    unittest.main()
