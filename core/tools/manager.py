from __future__ import annotations

import json
import locale
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


SUPPORTED_ARGUMENT_TYPES = {"game_path", "current_game_file", "fixed"}
SUPPORTED_RUNTIMES = {"host_python", "exe"}
SUPPORTED_CATEGORIES = {"gui", "cli"}


class ToolSpecError(ValueError):
    pass


@dataclass(frozen=True)
class ToolArgument:
    name: str
    flag: str
    type: str
    required: bool = False
    fixed: Any = None
    source: str | None = None
    label: str | None = None


@dataclass(frozen=True)
class ToolManifest:
    root: Path
    manifest_path: Path
    id: str
    name: str
    version: str
    author: str
    description: str
    runtime: str
    category: str
    entry: Path
    working_directory: Path
    requires_game_engine: tuple[str, ...] = ()
    resources: tuple[Path, ...] = ()
    arguments: tuple[ToolArgument, ...] = ()
    errors: tuple[str, ...] = field(default_factory=tuple)

    @property
    def available(self) -> bool:
        return not self.errors and self.entry.is_file()


@dataclass
class RunningTool:
    manifest: ToolManifest
    process: subprocess.Popen
    command: list[str]


def _safe_relative(root: Path, value: str, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ToolSpecError(f"{label} 必须是非空字符串")
    candidate = (root / value).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise ToolSpecError(f"{label} 不能位于工具目录外: {value}") from exc
    return candidate


def _parse_arguments(payload: Any) -> tuple[ToolArgument, ...]:
    if payload is None:
        return ()
    if not isinstance(payload, list):
        raise ToolSpecError("arguments 必须是数组")
    result: list[ToolArgument] = []
    seen: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            raise ToolSpecError("arguments 中的项目必须是对象")
        name, flag, arg_type = item.get("name"), item.get("flag"), item.get("type")
        if not all(isinstance(value, str) and value.strip() for value in (name, flag, arg_type)):
            raise ToolSpecError("参数必须包含 name、flag、type")
        if name in seen:
            raise ToolSpecError(f"参数重复: {name}")
        if arg_type not in SUPPORTED_ARGUMENT_TYPES:
            raise ToolSpecError(f"不支持的参数类型: {arg_type}")
        source = item.get("source")
        if arg_type == "current_game_file":
            if not isinstance(source, str) or not source.strip() or Path(source).is_absolute() or ".." in Path(source).parts:
                raise ToolSpecError(f"current_game_file 缺少安全的 source: {name}")
        if arg_type == "fixed" and "value" not in item:
            raise ToolSpecError(f"fixed 参数缺少 value: {name}")
        seen.add(name)
        result.append(ToolArgument(
            name=name,
            flag=flag,
            type=arg_type,
            required=bool(item.get("required", False)),
            fixed=item.get("value"),
            source=source,
            label=item.get("label"),
        ))
    return tuple(result)


def load_manifest(path: Path) -> ToolManifest:
    root = path.parent.resolve()
    errors: list[str] = []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolSpecError(f"无法读取 Manifest: {exc}") from exc
    if not isinstance(payload, dict):
        raise ToolSpecError("Manifest 根节点必须是对象")
    if payload.get("manifest_version") != 1:
        raise ToolSpecError("只支持 manifest_version=1")
    tool_id = payload.get("id")
    if not isinstance(tool_id, str) or not tool_id or tool_id != root.name or not tool_id.replace("_", "").isalnum():
        raise ToolSpecError("id 必须是安全字符串并与工具目录名一致")
    runtime = payload.get("runtime", "host_python")
    if runtime not in SUPPORTED_RUNTIMES:
        raise ToolSpecError(f"不支持的 runtime: {runtime}")
    category = payload.get("category", "gui")
    if category not in SUPPORTED_CATEGORIES:
        raise ToolSpecError(f"不支持的 category: {category}")
    entry = _safe_relative(root, payload.get("entry"), "entry")
    working_directory = _safe_relative(root, payload.get("working_directory", "."), "working_directory")
    resources = tuple(_safe_relative(root, value, "resource") for value in payload.get("resources", []))
    arguments = _parse_arguments(payload.get("arguments"))
    if not entry.is_file():
        errors.append(f"入口不存在: {entry.name}")
    errors.extend(f"资源不存在: {resource.relative_to(root)}" for resource in resources if not resource.exists())
    return ToolManifest(
        root=root,
        manifest_path=path.resolve(),
        id=tool_id,
        name=str(payload.get("name", tool_id)),
        version=str(payload.get("version", "")),
        author=str(payload.get("author", "")),
        description=str(payload.get("description", "")),
        runtime=runtime,
        category=category,
        entry=entry,
        working_directory=working_directory,
        requires_game_engine=tuple(str(value) for value in payload.get("requires_game_engine", [])),
        resources=resources,
        arguments=arguments,
        errors=tuple(errors),
    )


class ToolManager:
    def __init__(self, tools_root: str | Path):
        self.tools_root = Path(tools_root).resolve()
        self.running: dict[str, RunningTool] = {}

    def discover(self) -> tuple[list[ToolManifest], list[tuple[Path, str]]]:
        tools: list[ToolManifest] = []
        diagnostics: list[tuple[Path, str]] = []
        if not self.tools_root.is_dir():
            return tools, diagnostics
        for directory in sorted((path for path in self.tools_root.iterdir() if path.is_dir()), key=lambda p: p.name.casefold()):
            manifest_path = directory / "manifest.json"
            if not manifest_path.is_file():
                diagnostics.append((directory, "缺少 manifest.json"))
                continue
            try:
                tools.append(load_manifest(manifest_path))
            except ToolSpecError as exc:
                diagnostics.append((manifest_path, str(exc)))
        return tools, diagnostics

    def build_command(self, manifest: ToolManifest, values: dict[str, Any], host_executable: str | list[str] | None = None) -> list[str]:
        command: list[str]
        if manifest.runtime == "exe":
            command = [str(manifest.entry)]
        else:
            host = host_executable if isinstance(host_executable, list) else [host_executable or sys.executable]
            command = [*host, "--run-tool", str(manifest.root), str(manifest.entry)]
        for argument in manifest.arguments:
            value = argument.fixed if argument.type == "fixed" else values.get(argument.name)
            if value in (None, "") and argument.required:
                raise ToolSpecError(f"缺少{argument.label or argument.name}")
            if value in (None, ""):
                continue
            command.extend((argument.flag, str(value)))
        return command

    def start(self, manifest: ToolManifest, values: dict[str, Any], host_executable: str | list[str] | None = None) -> RunningTool:
        if not manifest.available:
            raise ToolSpecError("工具不可用: " + "; ".join(manifest.errors))
        command = self.build_command(manifest, values, host_executable)
        output = subprocess.PIPE if manifest.category == "cli" else subprocess.DEVNULL
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        process = subprocess.Popen(
            command,
            cwd=str(manifest.working_directory),
            stdout=output,
            stderr=subprocess.STDOUT if manifest.category == "cli" else subprocess.DEVNULL,
            text=True,
            encoding=locale.getpreferredencoding(False) if manifest.category == "cli" else None,
            errors="replace" if manifest.category == "cli" else None,
            shell=False,
            creationflags=creationflags,
        )
        running = RunningTool(manifest, process, command)
        self.running[manifest.id] = running
        return running

    def clear_finished(self) -> list[RunningTool]:
        finished: list[RunningTool] = []
        for tool_id, running in list(self.running.items()):
            if running.process.poll() is not None:
                finished.append(running)
                del self.running[tool_id]
        return finished


def run_tool_host(tool_root: str, entry: str, argv: Iterable[str]) -> int:
    root = Path(tool_root).resolve()
    entry_path = Path(entry).resolve()
    try:
        entry_path.relative_to(root)
    except ValueError as exc:
        raise ToolSpecError("工具入口必须位于工具目录内") from exc
    if not entry_path.is_file():
        raise ToolSpecError(f"工具入口不存在: {entry_path}")
    vendor = root / "vendor"
    # A source tool may legitimately have a top-level module named ``core``.
    # The host imported WindyTranslator's core package before dispatching, so
    # remove that package from this child interpreter's import cache first.
    for module_name in tuple(sys.modules):
        if module_name == "core" or module_name.startswith("core."):
            sys.modules.pop(module_name, None)
    sys.path.insert(0, str(root))
    if vendor.is_dir():
        sys.path.insert(0, str(vendor))
    os.chdir(root)
    sys.argv = [str(entry_path), *argv]
    import runpy
    runpy.run_path(str(entry_path), run_name="__main__")
    return 0
