from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from tkinter import Tk, messagebox
from typing import Callable


CP932 = "cp932"
CP936 = "cp936"
MARKER_RE = re.compile(r"^#([^#]+)#")
MAP_SCRIPT_RE = re.compile(r"^Map\d+\.txt$", re.IGNORECASE)
MAP_DATA_RE = re.compile(r"^Map\d+\.lmu$", re.IGNORECASE)


@dataclass(frozen=True)
class Candidate:
    script: Path
    line_index: int
    original: str
    replacement: str
    category: str
    encodes_to_question_mark: bool


class RepairError(RuntimeError):
    pass


def _has_kana(value: str) -> bool:
    return any("\u3040" <= char <= "\u30ff" for char in value)


def recover_japanese_mojibake(value: str) -> tuple[str, bool] | None:
    """Return a conservative CP936-to-CP932 repair candidate.

    This only decides whether the project has the known mojibake state. Once
    found, all internal names are repaired together by ``repair_all_names``.
    """
    if not value or _has_kana(value) or "\ufffd" in value:
        return None
    try:
        raw = value.encode(CP936, errors="strict")
        repaired = raw.decode(CP932, errors="strict")
    except UnicodeError:
        return None
    if repaired == value or not _has_kana(repaired):
        return None
    try:
        lossy = repaired.encode(CP936, errors="strict") != raw
    except UnicodeError:
        lossy = True
    return repaired, lossy


def repair_all_names(value: str) -> tuple[str, bool]:
    """Treat one confirmed mojibake field as proof for every internal name."""
    raw = value.encode(CP936, errors="replace")
    repaired = raw.decode(CP932, errors="replace")
    rewritten = repaired.encode(CP936, errors="replace")
    return repaired, rewritten != raw or "\ufffd" in repaired


def _category_for(script: Path, marker: str) -> str | None:
    parts = script.parts
    name = script.name.lower()
    if name == "maptree.txt" and marker.lower() == "name":
        return "地图名称"
    if name == "switches.txt" and marker.isdigit():
        return "开关名称"
    if name == "variables.txt" and marker.isdigit():
        return "变量名称"
    if "commons" in {part.lower() for part in parts} and marker.lower() == "commoneventname":
        return "公共事件名称"
    if name == "troops.txt" and marker.lower() == "troopname":
        return "敌群名称"
    if len(parts) == 1 and MAP_SCRIPT_RE.fullmatch(script.name) and marker.lower() == "eventname":
        return "地图事件名称"
    return None


def find_candidates(script_root: Path) -> list[Candidate]:
    fields: list[tuple[Path, int, str, str]] = []
    project_has_mojibake = False
    for path in script_root.rglob("*.txt"):
        relative = path.relative_to(script_root)
        lines = path.read_text(encoding="utf-8", errors="strict").splitlines(keepends=True)
        for index, line in enumerate(lines[:-1]):
            matched = MARKER_RE.match(line.rstrip("\r\n"))
            if not matched:
                continue
            category = _category_for(relative, matched.group(1))
            if category is None:
                continue
            original = lines[index + 1].rstrip("\r\n")
            if not original:
                continue
            fields.append((relative, index + 1, original, category))
            project_has_mojibake = project_has_mojibake or recover_japanese_mojibake(original) is not None
    if not project_has_mojibake:
        return []
    candidates: list[Candidate] = []
    for script, line_index, original, category in fields:
        replacement, lossy = repair_all_names(original)
        candidates.append(Candidate(script, line_index, original, replacement, category, lossy))
    return candidates


def apply_candidates(script_root: Path, candidates: list[Candidate]) -> None:
    by_script: dict[Path, list[Candidate]] = {}
    for candidate in candidates:
        by_script.setdefault(candidate.script, []).append(candidate)
    for relative, entries in by_script.items():
        path = script_root / relative
        lines = path.read_text(encoding="utf-8", errors="strict").splitlines(keepends=True)
        for candidate in entries:
            newline = "\r\n" if lines[candidate.line_index].endswith("\r\n") else "\n"
            if lines[candidate.line_index].rstrip("\r\n") != candidate.original:
                raise RepairError(f"临时脚本内容已变化: {relative}")
            lines[candidate.line_index] = candidate.replacement + newline
        path.write_text("".join(lines), encoding="utf-8", newline="")


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rpgrewriter_path() -> Path:
    path = Path(__file__).resolve().parents[2] / "modules" / "RPGRewriter" / "RPGRewriter.exe"
    if not path.is_file():
        raise RepairError(f"未找到 RPGRewriter.exe: {path}")
    return path


def _run(executable: Path, game_root: Path, arguments: list[str]) -> None:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    completed = subprocess.run(
        [str(executable), str(game_root / "RPG_RT.lmt"), *arguments],
        cwd=game_root,
        capture_output=True,
        text=True,
        encoding="mbcs",
        errors="replace",
        creationflags=flags,
        check=False,
    )
    if completed.returncode != 0:
        details = (completed.stderr or completed.stdout).strip()
        raise RepairError(f"RPGRewriter 退出码 {completed.returncode}: {details[:500]}")


def _export_arguments() -> list[str]:
    return [
        "-export", "-readcode", "936", "-filereadcode", "936", "-miscreadcode", "936",
        "-mapnames", "Y", "-mapeventnames", "Y", "-switchnames", "Y",
        "-variablenames", "Y", "-commoneventnames", "Y", "-troopnames", "Y",
    ]


def _import_arguments() -> list[str]:
    return [
        "-import", "-readcode", "936", "-filereadcode", "936", "-miscreadcode", "936",
        "-writecode", "936", "-filewritecode", "936", "-miscwritecode", "936", "-nolimit", "1",
    ]


class RepairSession:
    def __init__(self, project: Path):
        self.project = project.resolve()
        self.temp_root: Path | None = None
        self.initial_hashes: dict[Path, str] = {}
        self.candidates: list[Candidate] = []

    def scan(self, report: Callable[[str], None] = lambda _message: None) -> list[Candidate]:
        required = (self.project / "RPG_RT.lmt", self.project / "RPG_RT.ldb")
        if not all(path.is_file() for path in required):
            raise RepairError("当前项目缺少 RPG_RT.lmt 或 RPG_RT.ldb。")
        report("正在创建临时工程...")
        self.temp_root = Path(tempfile.mkdtemp(prefix=".database_name_repair_", dir=self.project.parent))
        for path in [*required, *sorted(self.project.glob("Map*.lmu"))]:
            if path.is_file():
                target = self.temp_root / path.name
                shutil.copy2(path, target)
                self.initial_hashes[path] = _hash(path)
        report("正在导出六类内部字段...")
        _run(_rpgrewriter_path(), self.temp_root, _export_arguments())
        scripts = self.temp_root / "StringScripts"
        if not scripts.is_dir():
            raise RepairError("RPGRewriter 未生成 StringScripts。")
        report("正在扫描日文转码乱码...")
        self.candidates = find_candidates(scripts)
        return self.candidates

    def apply(self, report: Callable[[str], None] = lambda _message: None) -> list[Path]:
        if self.temp_root is None:
            raise RepairError("尚未完成扫描。")
        for path, digest in self.initial_hashes.items():
            if not path.is_file() or _hash(path) != digest:
                raise RepairError(f"扫描后项目文件已变化，已取消覆盖: {path.name}")
        report("正在准备修正脚本...")
        apply_candidates(self.temp_root / "StringScripts", self.candidates)
        report("正在导入修正后的内部字段...")
        _run(_rpgrewriter_path(), self.temp_root, _import_arguments())
        report("正在覆盖项目数据文件...")
        changed: list[Path] = []
        for original, digest in self.initial_hashes.items():
            updated = self.temp_root / original.name
            if updated.is_file() and _hash(updated) != digest:
                os.replace(updated, original)
                changed.append(original)
        if not changed:
            raise RepairError("导入未生成可写回的修正文件。")
        return changed

    def close(self) -> None:
        if self.temp_root is not None:
            shutil.rmtree(self.temp_root, ignore_errors=True)
            self.temp_root = None


def _confirm(candidate_count: int) -> bool:
    root = Tk()
    root.withdraw()
    try:
        return messagebox.askyesno(
            "确认修正",
            f"将有{candidate_count}条内部字段的名称被改写，此操作不可逆且可能会令特殊符号变为问号，是否继续？",
            parent=root,
        )
    finally:
        root.destroy()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    args = parser.parse_args()
    session = RepairSession(Path(args.project))
    try:
        print("正在读取当前项目的内部字段...", flush=True)
        candidates = session.scan(lambda message: print(message, flush=True))
        if not candidates:
            print("没有需要修正的内部字段。", flush=True)
            return 0

        counts = Counter(candidate.category for candidate in candidates)
        print("可修正字段：" + "；".join(f"{name} {count} 条" for name, count in sorted(counts.items())), flush=True)
        lossy = sum(candidate.encodes_to_question_mark for candidate in candidates)
        if lossy:
            print(f"其中 {lossy} 条包含无法用 CP936 表示的字符，写入后可能变为问号。", flush=True)
        if not _confirm(len(candidates)):
            print("已取消修正。", flush=True)
            return 0

        changed = session.apply(lambda message: print(message, flush=True))
        print(f"已改写 {len(candidates)} 条内部字段。", flush=True)
        print("已直接覆盖：" + "、".join(path.name for path in changed), flush=True)
        return 0
    except (OSError, RepairError, UnicodeError) as exc:
        print(f"错误：{exc}", flush=True)
        return 1
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
