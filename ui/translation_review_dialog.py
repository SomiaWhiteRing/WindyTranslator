"""GUI for reviewing RPG Maker translation issues.

Loads nested translation JSON files, checks Message line-width overflows, and
reviews translation fallback rows from fallback_corrections.csv.
"""

from __future__ import annotations

import argparse
import csv
import copy
import json
import os
import re
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    from scripts.optimize_linebreaks import calc_display_width
except ImportError:  # pragma: no cover - supports `python scripts/...`
    from optimize_linebreaks import calc_display_width


MESSAGE_MARKER = "Message"
NARRATION_SPEAKER_ID = "NARRATION"
NARRATION_LIMIT = 50
DEFAULT_LIMIT = 38
REQUIRED_ENTRY_KEYS = {"text", "original_marker", "speaker_id"}
RULE_FLAG_MAP = {
    "IGNORECASE": re.IGNORECASE,
    "MULTILINE": re.MULTILINE,
    "DOTALL": re.DOTALL,
    "VERBOSE": re.VERBOSE,
}
STATUS_MODE_LABELS = {
    "problem": "回退+超限",
    "fallback": "仅回退",
    "overflow": "仅超限",
    "dirty": "已修改",
    "all": "全部已加载",
}
SCOPE_MODE_LABELS = {
    "selected": "当前选中项",
    "filtered": "当前筛选结果",
}
FALLBACK_CSV_HEADER = ["源文件名", "原文", "原始标记", "最终尝试结果/原因", "修正译文"]
OLD_FALLBACK_CSV_HEADER = ["原文", "最终尝试结果", "修正译文"]

# Layout tuning knobs. Adjust these values when the review window needs resizing.
INTEGRATED_WINDOW_SIZE = (1280, 820)
INTEGRATED_WINDOW_MIN_SIZE = (960, 620)
STANDALONE_WINDOW_SIZE = (1500, 920)
STANDALONE_WINDOW_MIN_SIZE = (1180, 720)
WINDOW_SCREEN_MARGIN = (80, 100)
MAIN_PANE_WEIGHTS = (5, 6)

ENTRY_TREE_COLUMNS = (
    "map",
    "issue",
    "marker",
    "speaker",
    "limit",
    "max_width",
    "summary",
    "status",
)
ENTRY_TREE_HEADINGS = {
    "map": "文件",
    "issue": "类型",
    "marker": "标记",
    "speaker": "说话人",
    "limit": "上限",
    "max_width": "最宽行",
    "summary": "译文摘要",
    "status": "状态",
}
ENTRY_TREE_WIDTHS = {
    "map": 96,
    "issue": 40,
    "marker": 60,
    "speaker": 60,
    "limit": 40,
    "max_width": 60,
    "summary": 130,
    "status": 40,
}
ENTRY_TREE_STRETCH_COLUMNS = {"summary", "status"}

TOP_ACTION_BUTTONS = (
    ("auto_apply", "切换条目时自动应用更改", 190),
    ("restore", "恢复本条原译文", 120),
    ("apply", "应用本条修改", 120),
    ("save", "保存到翻译 JSON", 150),
)
TOP_ACTION_BUTTON_WEIGHTS = {
    "auto_apply": 0,
    "restore": 1,
    "apply": 1,
    "save": 1,
}

LINE_DETAIL_COLUMNS = (
    ("line_no", "行", 48, False),
    ("width", "宽度", 48, False),
    ("status", "状态", 48, False),
    ("content", "内容", 460, True),
)

RULE_TREE_COLUMNS = (
    ("enabled", "启用", 56, False),
    ("name", "名称", 90, False),
    ("flags", "Flags", 90, False),
    ("pattern", "Pattern", 160, True),
    ("replacement", "Replacement", 140, True),
)

RULE_OVERVIEW_HELP_TEXT = """规则功能
========

规则用于批量修正译文。你可以把它理解成：

    在一批译文里，自动找到“匹配模式”，再替换成“替换文本”。

它适合处理重复出现的小问题，例如标点、空格、固定译名、错误换行。不适合处理需要逐句判断的翻译质量问题。


基本流程
--------

1. 在左侧列表筛选要处理的条目。
2. 在“范围”里选择规则作用范围。
   - 当前选中项：只处理当前右侧打开的一条。
   - 当前筛选结果：处理左侧当前显示出来的所有条目。
3. 勾选要启用的规则。
4. 点击“预览启用规则”。
5. 确认命中数量、超限条目变化没有异常。
6. 点击“应用启用规则”。
7. 最后点击“保存到翻译 JSON”。

注意：应用规则只会改当前窗口里的数据。没有保存到 JSON 前，外部文件还不会改变。


字段含义
--------

名称
    给自己看的规则名。例如“英文省略号改中文省略号”。

匹配模式
    要查找的内容。这里使用 Python 正则表达式。

替换文本
    找到后替换成什么。可以为空，表示删除匹配到的内容。

Flags
    正则选项。新手通常不用勾选。


常用例子
--------

例 1：把英文三个点改成中文省略号

    名称: 英文省略号
    匹配模式: \\.{3}
    替换文本: ……

说明：
    . 在正则里有特殊含义，所以要写成 \\.
    {3} 表示连续出现 3 次。


例 2：把多个半角/全角空格压成一个半角空格

    名称: 多余空格
    匹配模式: [ 　]+
    替换文本:  

说明：
    方括号里有一个半角空格和一个全角空格。
    + 表示“连续一个或多个”。


例 3：去掉句号后多余换行

    名称: 句号后换行
    匹配模式: 。\\n
    替换文本: 。

说明：
    \\n 表示换行。
    这会把“。后面马上换行”的情况合并成同一行。


例 4：统一角色名

    名称: 统一布莱恩
    匹配模式: 布莱恩恩
    替换文本: 布莱恩

说明：
    这种普通文字替换不需要复杂正则。


安全建议
--------

- 不确定规则是否正确时，只对“当前选中项”预览。
- 先预览，再应用。
- 一次只启用少量规则，方便发现是哪条规则造成了问题。
- 规则会按列表顺序从上到下执行；前一条规则的结果会继续交给后一条规则处理。
"""

RULE_EDITOR_HELP_TEXT = """编辑规则
========

这个窗口用于新增或修改一条批量替换规则。


最小可用写法
------------

如果只是把固定文字 A 改成固定文字 B，直接填写：

    名称: 统一勇者名
    匹配模式: 勇者
    替换文本: 布莱恩

这样会把作用范围内所有译文里的“勇者”替换成“布莱恩”。


字段说明
--------

规则名称
    只给你自己识别用，不参与替换。

启用
    勾选后，这条规则才会被“预览启用规则”和“应用启用规则”使用。

匹配模式
    要查找的内容。支持正则表达式。

替换文本
    查找到之后替换成什么。
    如果这里留空，就是删除匹配到的内容。

Flags
    正则选项。新手可以全部不勾。


Flags 说明
----------

IGNORECASE
    忽略英文大小写。
    例如 pattern 写 hero，可以同时匹配 Hero、HERO、hero。

MULTILINE
    让 ^ 和 $ 按每一行判断，而不是只按整段文本判断。
    新手很少需要。

DOTALL
    让 . 可以匹配换行。
    很容易跨行误伤，不熟悉时不要勾。

VERBOSE
    允许在正则里写空白和注释。
    主要给复杂规则使用。


常用模板
--------

删除行尾多余空格：

    匹配模式: [ 　]+$
    替换文本:
    Flags: MULTILINE

把多个空格压成一个：

    匹配模式: [ 　]+
    替换文本:  

把英文三个点改成中文省略号：

    匹配模式: \\.{3}
    替换文本: ……

去掉逗号后的强制换行：

    匹配模式: ，\\n
    替换文本: ，


容易踩坑
--------

- .、?、*、+、[、]、(、)、{、}、\\ 在正则里有特殊含义。
- 想匹配普通句点 . 时，要写成 \\.
- 想匹配普通反斜杠 \\ 时，通常要写成 \\\\。
- 规则保存后不会自动应用，需要回到规则页预览和应用。
"""


class UnsupportedFallbackCsvError(ValueError):
    """Raised when a fallback CSV uses the retired three-column format."""


@dataclass
class FallbackRecord:
    """One row from the current fallback corrections CSV."""

    source_file_name: str
    original_text: str
    marker: str
    reason: str
    correction: str = ""
    raw_row: List[str] = field(default_factory=list)

    @property
    def key(self) -> Tuple[str, str]:
        return (self.source_file_name, self.original_text)


@dataclass
class ReviewSaveResult:
    """Summary returned after saving an integrated review session."""

    dirty_entries: int
    corrected_fallback_entries: int
    remaining_fallback_rows: int


def determine_line_limit(speaker_id: Optional[str]) -> int:
    """Return the per-line display width limit for a Message entry."""
    return NARRATION_LIMIT if speaker_id == NARRATION_SPEAKER_ID else DEFAULT_LIMIT


def _truncate_preview(text: str, max_chars: int = 70) -> str:
    preview = text.replace("\n", " / ").strip()
    if len(preview) <= max_chars:
        return preview
    return preview[: max_chars - 3] + "..."


@dataclass
class ReviewEntry:
    """In-memory review model for a translation entry that may need attention."""

    entry_id: str
    map_name: str
    original_key: str
    speaker_id: Optional[str]
    limit: Optional[int]
    initial_text: str
    current_text: str
    marker: str = MESSAGE_MARKER
    issue_kinds: List[str] = field(default_factory=list)
    fallback_reason: Optional[str] = None
    per_line_widths: List[int] = field(default_factory=list)
    overflow_lines: List[int] = field(default_factory=list)
    dirty: bool = False

    def __post_init__(self) -> None:
        self.recompute()

    @property
    def current_line_count(self) -> int:
        return len(self.current_text.split("\n"))

    @property
    def original_line_count(self) -> int:
        return len(self.initial_text.split("\n"))

    @property
    def max_line_width(self) -> int:
        return max(self.per_line_widths, default=0)

    @property
    def overflow_count(self) -> int:
        return len(self.overflow_lines)

    @property
    def is_over_limit(self) -> bool:
        return bool(self.overflow_lines)

    @property
    def is_fallback(self) -> bool:
        return "fallback" in self.issue_kinds

    @property
    def key(self) -> Tuple[str, str]:
        return (self.map_name, self.original_key)

    @property
    def issue_label(self) -> str:
        labels = []
        if self.is_fallback:
            labels.append("回退")
        if self.is_over_limit:
            labels.append("超限")
        return "+".join(labels) if labels else "候选"

    @property
    def status_label(self) -> str:
        if self.is_fallback and self.is_over_limit and self.dirty:
            return "已修改，回退，仍超限"
        if self.is_fallback and self.dirty:
            return "已修改，回退"
        if self.is_fallback and self.is_over_limit:
            return "回退，超限"
        if self.is_fallback:
            return "回退"
        if self.is_over_limit and self.dirty:
            return "已修改，仍超限"
        if self.is_over_limit:
            return "超限"
        if self.dirty:
            return "已修改"
        return "正常"

    @property
    def summary(self) -> str:
        return _truncate_preview(self.current_text)

    def recompute(self) -> None:
        lines = self.current_text.split("\n")
        if self.marker == MESSAGE_MARKER and self.limit is not None:
            self.per_line_widths = [calc_display_width(line) for line in lines]
            self.overflow_lines = [
                index
                for index, width in enumerate(self.per_line_widths, start=1)
                if width > self.limit
            ]
        else:
            self.per_line_widths = []
            self.overflow_lines = []
        self.dirty = self.current_text != self.initial_text

    def update_text(self, new_text: str) -> None:
        self.current_text = new_text
        self.recompute()

    def restore_initial(self) -> None:
        self.update_text(self.initial_text)


@dataclass
class RegexRule:
    """Serializable regex replacement rule."""

    name: str
    pattern: str
    replacement: str
    flags: List[str] = field(default_factory=list)
    enabled: bool = True

    def normalized_flags(self) -> List[str]:
        return [flag for flag in self.flags if flag in RULE_FLAG_MAP]

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "pattern": self.pattern,
            "replacement": self.replacement,
            "flags": self.normalized_flags(),
            "enabled": bool(self.enabled),
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "RegexRule":
        if not isinstance(payload, dict):
            raise ValueError("规则项必须是对象。")
        name = str(payload.get("name", "")).strip()
        pattern = payload.get("pattern", "")
        replacement = payload.get("replacement", "")
        flags = payload.get("flags", [])
        enabled = bool(payload.get("enabled", True))
        if not name:
            raise ValueError("规则名称不能为空。")
        if not isinstance(pattern, str) or not isinstance(replacement, str):
            raise ValueError("规则的 pattern 和 replacement 必须是字符串。")
        if not isinstance(flags, list):
            raise ValueError("规则 flags 必须是字符串数组。")
        normalized_flags = []
        for flag in flags:
            flag_name = str(flag).upper()
            if flag_name not in RULE_FLAG_MAP:
                raise ValueError(f"不支持的正则 flag: {flag_name}")
            normalized_flags.append(flag_name)
        return cls(
            name=name,
            pattern=pattern,
            replacement=replacement,
            flags=normalized_flags,
            enabled=enabled,
        )


@dataclass
class RulePreview:
    scope_size: int
    changed_entries: int
    total_substitutions: int
    overflow_entries_before: int
    overflow_entries_after: int
    overflow_lines_before: int
    overflow_lines_after: int


@dataclass
class RuleApplyResult:
    changed_entries: int
    total_substitutions: int


def validate_nested_translation_data(data: object) -> None:
    """Validate that data matches the nested translation JSON shape."""
    if not isinstance(data, dict) or not data:
        raise ValueError("JSON 顶层必须是非空对象。")

    found_entry = False
    for map_name, entries in data.items():
        if not isinstance(entries, dict):
            raise ValueError(f"顶层项 {map_name!r} 不是对象，无法识别为嵌套翻译 JSON。")
        for original_key, entry in entries.items():
            if not isinstance(entry, dict):
                raise ValueError(
                    f"条目 {map_name!r} -> {original_key!r} 不是对象，无法识别为嵌套翻译 JSON。"
                )
            missing = REQUIRED_ENTRY_KEYS.difference(entry.keys())
            if missing:
                missing_list = ", ".join(sorted(missing))
                raise ValueError(
                    f"条目 {map_name!r} -> {original_key!r} 缺少字段: {missing_list}"
                )
            if not isinstance(entry["text"], str):
                raise ValueError(
                    f"条目 {map_name!r} -> {original_key!r} 的 text 不是字符串。"
                )
            found_entry = True

    if not found_entry:
        raise ValueError("JSON 中没有可识别的翻译条目。")


def load_fallback_records(path: Path) -> List[FallbackRecord]:
    """Load current-format fallback correction rows."""
    if not path.exists():
        return []

    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header is None:
            return []
        if header == OLD_FALLBACK_CSV_HEADER:
            raise UnsupportedFallbackCsvError(
                "检测到旧版三列表 fallback_corrections.csv，当前审阅器只支持新五列表格式。"
            )
        if header != FALLBACK_CSV_HEADER:
            raise ValueError(
                "fallback_corrections.csv 表头无效。期望: "
                + ",".join(FALLBACK_CSV_HEADER)
            )

        records: List[FallbackRecord] = []
        for line_no, row in enumerate(reader, start=2):
            if not row:
                continue
            if len(row) < len(FALLBACK_CSV_HEADER):
                raise ValueError(f"fallback_corrections.csv 第 {line_no} 行列数不足。")
            records.append(
                FallbackRecord(
                    source_file_name=row[0],
                    original_text=row[1],
                    marker=row[2],
                    reason=row[3],
                    correction=row[4],
                    raw_row=list(row),
                )
            )
    return records


def _fallback_record_map(records: Sequence[FallbackRecord]) -> Dict[Tuple[str, str], FallbackRecord]:
    mapped: Dict[Tuple[str, str], FallbackRecord] = {}
    for record in records:
        mapped.setdefault(record.key, record)
    return mapped


def scan_translation_data(
    data: dict,
    fallback_records: Optional[Sequence[FallbackRecord]] = None,
) -> List[ReviewEntry]:
    """Build review entries from nested translation JSON and matching fallback rows."""
    validate_nested_translation_data(data)

    fallback_records = list(fallback_records or [])
    fallback_by_key = _fallback_record_map(fallback_records)
    seen_json_keys = set()
    review_entries: List[ReviewEntry] = []
    for map_name, entries in data.items():
        for original_key, info in entries.items():
            map_name_str = str(map_name)
            original_key_str = str(original_key)
            key = (map_name_str, original_key_str)
            seen_json_keys.add(key)

            marker = str(info.get("original_marker", ""))
            fallback_record = fallback_by_key.get(key)
            if marker != MESSAGE_MARKER and fallback_record is None:
                continue
            text = info.get("text", "")
            speaker_id = info.get("speaker_id")
            normalized_speaker = str(speaker_id) if speaker_id is not None else None
            issue_kinds = ["fallback"] if fallback_record else []
            review_entries.append(
                ReviewEntry(
                    entry_id=f"entry_{len(review_entries)}",
                    map_name=map_name_str,
                    original_key=original_key_str,
                    speaker_id=normalized_speaker,
                    limit=determine_line_limit(normalized_speaker) if marker == MESSAGE_MARKER else None,
                    initial_text=text,
                    current_text=text,
                    marker=marker,
                    issue_kinds=issue_kinds,
                    fallback_reason=fallback_record.reason if fallback_record else None,
                )
            )

    return review_entries


def translation_json_has_reviewable_issues(
    translated_json_path: Path,
    fallback_csv_path: Optional[Path] = None,
) -> bool:
    """Return whether the integrated review button should be enabled."""
    if not translated_json_path.exists():
        return False
    try:
        data = json.loads(translated_json_path.read_text(encoding="utf-8"))
        fallback_records: List[FallbackRecord] = []
        if fallback_csv_path and fallback_csv_path.exists():
            fallback_records = load_fallback_records(fallback_csv_path)
        entries = scan_translation_data(data, fallback_records)
    except UnsupportedFallbackCsvError:
        return True
    except Exception:
        return False
    return any(entry.is_fallback or entry.is_over_limit for entry in entries)


def filter_entries(
    entries: Sequence[ReviewEntry],
    *,
    status_mode: str,
    map_filter: str = "",
    speaker_filter: str = "",
    keyword_filter: str = "",
) -> List[ReviewEntry]:
    """Filter review entries using UI criteria."""
    map_filter = map_filter.strip().lower()
    speaker_filter = speaker_filter.strip().lower()
    keyword_filter = keyword_filter.strip().lower()

    visible: List[ReviewEntry] = []
    for entry in entries:
        if status_mode == "problem" and not (entry.is_fallback or entry.is_over_limit):
            continue
        if status_mode == "fallback" and not entry.is_fallback:
            continue
        if status_mode == "overflow" and not entry.is_over_limit:
            continue
        if status_mode == "dirty" and not entry.dirty:
            continue

        if map_filter and map_filter not in entry.map_name.lower():
            continue
        speaker_text = (entry.speaker_id or "").lower()
        if speaker_filter and speaker_filter not in speaker_text:
            continue
        if keyword_filter:
            haystack = "\n".join(
                (
                    entry.original_key,
                    entry.current_text,
                    entry.marker,
                    entry.fallback_reason or "",
                )
            ).lower()
            if keyword_filter not in haystack:
                continue
        visible.append(entry)
    return visible


def summarize_entries(entries: Sequence[ReviewEntry]) -> dict:
    """Return aggregate counts for a collection of review entries."""
    overflow_entries = sum(1 for entry in entries if entry.is_over_limit)
    overflow_lines = sum(entry.overflow_count for entry in entries)
    dirty_entries = sum(1 for entry in entries if entry.dirty)
    fallback_entries = sum(1 for entry in entries if entry.is_fallback)
    return {
        "total_entries": len(entries),
        "overflow_entries": overflow_entries,
        "overflow_lines": overflow_lines,
        "dirty_entries": dirty_entries,
        "fallback_entries": fallback_entries,
    }


def compile_rule(rule: RegexRule) -> re.Pattern[str]:
    """Compile a rule into a regex pattern."""
    flags = 0
    for flag_name in rule.normalized_flags():
        flags |= RULE_FLAG_MAP[flag_name]
    try:
        return re.compile(rule.pattern, flags)
    except re.error as exc:
        raise ValueError(f"规则 {rule.name!r} 的正则无效: {exc}") from exc


def _enabled_rules(rules: Sequence[RegexRule]) -> List[RegexRule]:
    return [rule for rule in rules if rule.enabled]


def apply_rules_to_text(
    text: str,
    rules: Sequence[RegexRule],
) -> tuple[str, int]:
    """Apply enabled rules to text and return the new text plus substitution count."""
    new_text = text
    substitutions = 0
    for rule in _enabled_rules(rules):
        pattern = compile_rule(rule)
        new_text, count = pattern.subn(rule.replacement, new_text)
        substitutions += count
    return new_text, substitutions


def preview_rule_application(
    entries: Sequence[ReviewEntry],
    rules: Sequence[RegexRule],
) -> RulePreview:
    """Preview enabled rules without mutating entries."""
    enabled_rules = _enabled_rules(rules)
    if not enabled_rules:
        raise ValueError("没有启用中的规则可供预览。")

    changed_entries = 0
    substitutions = 0
    overflow_entries_before = sum(1 for entry in entries if entry.is_over_limit)
    overflow_lines_before = sum(entry.overflow_count for entry in entries)
    overflow_entries_after = 0
    overflow_lines_after = 0

    for entry in entries:
        new_text, count = apply_rules_to_text(entry.current_text, enabled_rules)
        if count:
            changed_entries += 1
            substitutions += count
        if entry.marker == MESSAGE_MARKER and entry.limit is not None:
            widths = [calc_display_width(line) for line in new_text.split("\n")]
            overflow_lines = sum(1 for width in widths if width > entry.limit)
        else:
            overflow_lines = 0
        overflow_lines_after += overflow_lines
        if overflow_lines:
            overflow_entries_after += 1

    return RulePreview(
        scope_size=len(entries),
        changed_entries=changed_entries,
        total_substitutions=substitutions,
        overflow_entries_before=overflow_entries_before,
        overflow_entries_after=overflow_entries_after,
        overflow_lines_before=overflow_lines_before,
        overflow_lines_after=overflow_lines_after,
    )


def apply_rules_to_entries(
    entries: Sequence[ReviewEntry],
    rules: Sequence[RegexRule],
) -> RuleApplyResult:
    """Apply enabled rules to a set of entries in place."""
    enabled_rules = _enabled_rules(rules)
    if not enabled_rules:
        raise ValueError("没有启用中的规则可供应用。")

    changed_entries = 0
    substitutions = 0
    for entry in entries:
        new_text, count = apply_rules_to_text(entry.current_text, enabled_rules)
        if not count:
            continue
        entry.update_text(new_text)
        changed_entries += 1
        substitutions += count

    return RuleApplyResult(
        changed_entries=changed_entries,
        total_substitutions=substitutions,
    )


def default_output_path(source_path: Path) -> Path:
    """Return the default reviewed output path."""
    return source_path.with_name(f"{source_path.stem}.linecheck_reviewed.json")


def default_rules_path(source_path: Path) -> Path:
    """Return the sidecar path used to persist regex rules."""
    return source_path.with_name(f"{source_path.stem}.linecheck_rules.json")


def load_rule_set(path: Path) -> List[RegexRule]:
    """Load rules from a sidecar JSON file."""
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("规则文件顶层必须是对象。")
    rules_payload = payload.get("rules", [])
    if not isinstance(rules_payload, list):
        raise ValueError("规则文件中的 rules 必须是数组。")
    return [RegexRule.from_dict(item) for item in rules_payload]


def save_rule_set(path: Path, rules: Sequence[RegexRule]) -> None:
    """Persist rules to the sidecar JSON file."""
    payload = {"rules": [rule.to_dict() for rule in rules]}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=4), encoding="utf-8")


def export_reviewed_data(data: dict, entries: Iterable[ReviewEntry]) -> dict:
    """Return a deep-copied JSON payload with updated translation texts."""
    cloned = copy.deepcopy(data)
    for entry in entries:
        cloned[entry.map_name][entry.original_key]["text"] = entry.current_text
        if entry.is_fallback and entry.dirty:
            cloned[entry.map_name][entry.original_key]["status"] = "success"
            cloned[entry.map_name][entry.original_key]["failure_context"] = None
    return cloned


def save_reviewed_output(output_path: Path, data: dict, entries: Iterable[ReviewEntry]) -> None:
    """Write updated translation JSON to disk."""
    output_path.write_text(
        json.dumps(export_reviewed_data(data, entries), ensure_ascii=False, indent=4),
        encoding="utf-8",
    )


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=4)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def rewrite_fallback_csv(
    fallback_csv_path: Path,
    records: Sequence[FallbackRecord],
    corrected_keys: Iterable[Tuple[str, str]],
) -> int:
    """Rewrite fallback CSV after removing corrected rows. Returns remaining row count."""
    corrected_key_set = set(corrected_keys)
    remaining_records = [record for record in records if record.key not in corrected_key_set]
    fallback_csv_path.parent.mkdir(parents=True, exist_ok=True)
    with fallback_csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_ALL)
        writer.writerow(FALLBACK_CSV_HEADER)
        for record in remaining_records:
            row = list(record.raw_row[: len(FALLBACK_CSV_HEADER)])
            if len(row) < len(FALLBACK_CSV_HEADER):
                row.extend([""] * (len(FALLBACK_CSV_HEADER) - len(row)))
            writer.writerow(row)
    return len(remaining_records)


def save_integrated_review(
    translated_json_path: Path,
    data: dict,
    entries: Sequence[ReviewEntry],
    fallback_csv_path: Optional[Path] = None,
    fallback_records: Optional[Sequence[FallbackRecord]] = None,
) -> ReviewSaveResult:
    """Save an integrated review session back to translation JSON and fallback CSV."""
    dirty_entries = [entry for entry in entries if entry.dirty]
    corrected_fallback_keys = {entry.key for entry in dirty_entries if entry.is_fallback}

    updated_data = export_reviewed_data(data, entries)
    _write_json_atomic(translated_json_path, updated_data)

    remaining_fallback_rows = 0
    if (
        fallback_csv_path
        and fallback_records is not None
        and (fallback_records or fallback_csv_path.exists())
    ):
        remaining_fallback_rows = rewrite_fallback_csv(
            fallback_csv_path,
            fallback_records,
            corrected_fallback_keys,
        )

    return ReviewSaveResult(
        dirty_entries=len(dirty_entries),
        corrected_fallback_entries=len(corrected_fallback_keys),
        remaining_fallback_rows=remaining_fallback_rows,
    )


def show_help_window(parent: tk.Widget, title: str, content: str) -> None:
    window = tk.Toplevel(parent)
    window.title(title)
    window.geometry("760x620")
    window.minsize(560, 420)
    window.transient(parent)

    outer = ttk.Frame(window, padding=10)
    outer.pack(fill=tk.BOTH, expand=True)
    outer.columnconfigure(0, weight=1)
    outer.rowconfigure(0, weight=1)

    text_frame = ttk.Frame(outer)
    text_frame.grid(row=0, column=0, sticky="nsew")
    text_frame.columnconfigure(0, weight=1)
    text_frame.rowconfigure(0, weight=1)

    text = tk.Text(text_frame, wrap=tk.WORD, height=28, padx=8, pady=8)
    text.grid(row=0, column=0, sticky="nsew")
    scrollbar = ttk.Scrollbar(text_frame, orient=tk.VERTICAL, command=text.yview)
    scrollbar.grid(row=0, column=1, sticky="ns")
    text.configure(yscrollcommand=scrollbar.set)
    text.insert("1.0", content.strip() + "\n")
    text.configure(state=tk.DISABLED)

    footer = ttk.Frame(outer)
    footer.grid(row=1, column=0, sticky="ew", pady=(8, 0))
    ttk.Button(footer, text="关闭", command=window.destroy).pack(side=tk.RIGHT)
    window.bind("<Escape>", lambda _event: window.destroy())
    window.focus_set()


class RuleEditorDialog(tk.Toplevel):
    """Modal dialog for creating or editing a regex rule."""

    def __init__(
        self,
        parent: tk.Widget,
        initial_rule: Optional[RegexRule] = None,
    ) -> None:
        super().__init__(parent)
        self.title("编辑正则规则" if initial_rule else "新增正则规则")
        self.geometry("560x420")
        self.transient(parent)
        self.grab_set()

        self.result: Optional[RegexRule] = None

        self.name_var = tk.StringVar(value=initial_rule.name if initial_rule else "")
        self.enabled_var = tk.BooleanVar(
            value=initial_rule.enabled if initial_rule else True
        )
        self.flag_vars = {
            flag: tk.BooleanVar(value=initial_rule is not None and flag in initial_rule.flags)
            for flag in RULE_FLAG_MAP
        }

        self._build_ui(initial_rule)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

    def _build_ui(self, initial_rule: Optional[RegexRule]) -> None:
        outer = ttk.Frame(self, padding=10)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(2, weight=1)
        outer.rowconfigure(4, weight=1)

        ttk.Label(outer, text="规则名称").grid(row=0, column=0, sticky="w", pady=(0, 6))
        ttk.Entry(outer, textvariable=self.name_var).grid(
            row=0, column=1, sticky="ew", pady=(0, 6)
        )

        flags_frame = ttk.Frame(outer)
        flags_frame.grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 6))
        ttk.Checkbutton(flags_frame, text="启用", variable=self.enabled_var).pack(
            side=tk.LEFT, padx=(0, 12)
        )
        for flag_name, flag_var in self.flag_vars.items():
            ttk.Checkbutton(flags_frame, text=flag_name, variable=flag_var).pack(
                side=tk.LEFT, padx=(0, 8)
            )

        ttk.Label(outer, text="匹配模式").grid(row=2, column=0, sticky="nw", pady=(0, 6))
        self.pattern_text = tk.Text(outer, wrap=tk.WORD, height=8)
        self.pattern_text.grid(row=2, column=1, sticky="nsew", pady=(0, 6))
        if initial_rule:
            self.pattern_text.insert("1.0", initial_rule.pattern)

        ttk.Label(outer, text="替换文本").grid(row=4, column=0, sticky="nw", pady=(0, 6))
        self.replacement_text = tk.Text(outer, wrap=tk.WORD, height=8)
        self.replacement_text.grid(row=4, column=1, sticky="nsew", pady=(0, 6))
        if initial_rule:
            self.replacement_text.insert("1.0", initial_rule.replacement)

        footer = ttk.Frame(outer)
        footer.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(footer, text="字段说明", command=self._show_help).pack(side=tk.LEFT)
        ttk.Button(footer, text="取消", command=self._on_cancel).pack(side=tk.RIGHT)
        ttk.Button(footer, text="确定", command=self._on_submit).pack(
            side=tk.RIGHT, padx=(0, 8)
        )

    def _show_help(self) -> None:
        show_help_window(self, "规则字段说明", RULE_EDITOR_HELP_TEXT)

    def _on_submit(self) -> None:
        name = self.name_var.get().strip()
        pattern = self.pattern_text.get("1.0", tk.END + "-1c")
        replacement = self.replacement_text.get("1.0", tk.END + "-1c")
        flags = [flag for flag, var in self.flag_vars.items() if var.get()]

        try:
            rule = RegexRule(
                name=name,
                pattern=pattern,
                replacement=replacement,
                flags=flags,
                enabled=self.enabled_var.get(),
            )
            if not rule.name:
                raise ValueError("规则名称不能为空。")
            compile_rule(rule)
        except ValueError as exc:
            messagebox.showerror("规则无效", str(exc), parent=self)
            return

        self.result = rule
        self.destroy()

    def _on_cancel(self) -> None:
        self.result = None
        self.destroy()


class LineLimitCheckerApp:
    """Standalone Tkinter application for line-limit review."""

    def __init__(
        self,
        root: tk.Tk,
        initial_path: Optional[Path] = None,
        *,
        fallback_csv_path: Optional[Path] = None,
        integrated_mode: bool = False,
        on_saved: Optional[Callable[[], None]] = None,
    ) -> None:
        self.root = root
        self.integrated_mode = integrated_mode
        self.fallback_csv_path = fallback_csv_path
        self.on_saved = on_saved
        self.root.title("译文问题审阅器" if integrated_mode else "译文行宽检查器")
        self._set_initial_window_geometry()

        self.data: Optional[dict] = None
        self.file_path: Optional[Path] = None
        self.rule_path: Optional[Path] = None
        self.fallback_records: List[FallbackRecord] = []
        self.entries: List[ReviewEntry] = []
        self.entries_by_id: Dict[str, ReviewEntry] = {}
        self.visible_entry_ids: List[str] = []
        self.current_entry_id: Optional[str] = None
        self.loading_editor = False
        self.rules: List[RegexRule] = []
        self.rules_dirty = False

        self.file_path_var = tk.StringVar()
        self.list_stats_var = tk.StringVar(value="未加载文件。")
        self.status_var = tk.StringVar(
            value="正在加载翻译 JSON。" if integrated_mode else "请选择一个翻译 JSON 文件。"
        )
        self.overview_var = tk.StringVar(value="未选择条目。")
        self.preview_var = tk.StringVar(value="尚未执行规则预览。")

        self.status_mode_var = tk.StringVar(value="problem" if integrated_mode else "overflow")
        self.map_filter_var = tk.StringVar()
        self.speaker_filter_var = tk.StringVar()
        self.keyword_filter_var = tk.StringVar()
        self.rule_scope_var = tk.StringVar(value="selected")
        self.auto_apply_on_switch_var = tk.BooleanVar(value=False)

        self._build_ui()
        self._bind_shortcuts()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        if initial_path:
            self.root.after_idle(lambda: self._load_initial_file(initial_path))

    def _load_initial_file(self, initial_path: Path) -> None:
        self.load_file(initial_path)
        if self.integrated_mode and self.data is None:
            self.root.after(0, self.root.destroy)

    def _set_initial_window_geometry(self) -> None:
        desired_width, desired_height = (
            INTEGRATED_WINDOW_SIZE if self.integrated_mode else STANDALONE_WINDOW_SIZE
        )
        min_width, min_height = (
            INTEGRATED_WINDOW_MIN_SIZE if self.integrated_mode else STANDALONE_WINDOW_MIN_SIZE
        )
        margin_width, margin_height = WINDOW_SCREEN_MARGIN
        screen_width = max(self.root.winfo_screenwidth(), 1024)
        screen_height = max(self.root.winfo_screenheight(), 720)
        width = min(desired_width, max(900, screen_width - margin_width))
        height = min(desired_height, max(620, screen_height - margin_height))
        min_width = min(min_width, width)
        min_height = min(min_height, height)
        x = max(0, (screen_width - width) // 2)
        y = max(0, (screen_height - height) // 2)
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.root.minsize(min_width, min_height)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=8)
        outer.pack(fill=tk.BOTH, expand=True)
        content_row = 0 if self.integrated_mode else 1
        status_row = 1 if self.integrated_mode else 2
        outer.rowconfigure(content_row, weight=1)
        outer.columnconfigure(0, weight=1)

        if not self.integrated_mode:
            top_bar = ttk.Frame(outer)
            top_bar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
            top_bar.columnconfigure(1, weight=1)

            ttk.Label(top_bar, text="文件").grid(row=0, column=0, sticky="w", padx=(0, 6))
            ttk.Entry(top_bar, textvariable=self.file_path_var, state="readonly").grid(
                row=0, column=1, sticky="ew", padx=(0, 6)
            )
            ttk.Button(top_bar, text="选择文件...", command=self._choose_file).grid(
                row=0, column=2, padx=(0, 6)
            )
            ttk.Button(top_bar, text="保存另存为", command=self._save).grid(row=0, column=3)

        main_pane = ttk.Panedwindow(outer, orient=tk.HORIZONTAL)
        main_pane.grid(row=content_row, column=0, sticky="nsew")

        left = ttk.Frame(main_pane, padding=(0, 0, 8, 0))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)
        main_pane.add(left, weight=MAIN_PANE_WEIGHTS[0])

        right = ttk.Frame(main_pane)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(2, weight=2)
        right.rowconfigure(3, weight=3)
        right.rowconfigure(4, weight=3)
        main_pane.add(right, weight=MAIN_PANE_WEIGHTS[1])

        self._build_left_panel(left)
        self._build_right_panel(right)
        self.root.after_idle(lambda: self._apply_main_pane_ratio(main_pane))

        ttk.Label(
            outer,
            textvariable=self.status_var,
            relief="sunken",
            anchor="w",
        ).grid(row=status_row, column=0, sticky="ew", pady=(8, 0))

    def _apply_main_pane_ratio(self, main_pane: ttk.Panedwindow, attempts: int = 5) -> None:
        total_weight = sum(MAIN_PANE_WEIGHTS)
        if total_weight <= 0:
            return

        pane_width = main_pane.winfo_width()
        if pane_width <= 1 and attempts > 0:
            self.root.after(50, lambda: self._apply_main_pane_ratio(main_pane, attempts - 1))
            return

        left_ratio = MAIN_PANE_WEIGHTS[0] / total_weight
        left_width = max(1, int(pane_width * left_ratio))
        try:
            main_pane.sashpos(0, left_width)
        except tk.TclError:
            pass

    def _build_left_panel(self, parent: ttk.Frame) -> None:
        filters = ttk.LabelFrame(parent, text="筛选")
        filters.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        for column in range(2):
            filters.columnconfigure(column, weight=1 if column == 1 else 0)

        ttk.Label(filters, text="视图").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        status_combo = ttk.Combobox(
            filters,
            state="readonly",
            values=[STATUS_MODE_LABELS[key] for key in STATUS_MODE_LABELS],
            width=16,
        )
        status_combo.grid(row=0, column=1, sticky="ew", padx=(0, 8), pady=6)
        status_combo.set(STATUS_MODE_LABELS[self.status_mode_var.get()])
        status_combo.bind("<<ComboboxSelected>>", self._on_status_mode_changed)
        self.status_combo = status_combo

        ttk.Label(filters, text="文件名").grid(row=1, column=0, sticky="w", padx=8, pady=6)
        map_entry = ttk.Entry(filters, textvariable=self.map_filter_var)
        map_entry.grid(row=1, column=1, sticky="ew", padx=(0, 8), pady=6)

        ttk.Label(filters, text="说话人").grid(row=2, column=0, sticky="w", padx=8, pady=6)
        speaker_entry = ttk.Entry(filters, textvariable=self.speaker_filter_var)
        speaker_entry.grid(row=2, column=1, sticky="ew", padx=(0, 8), pady=6)

        ttk.Label(filters, text="关键词").grid(row=3, column=0, sticky="w", padx=8, pady=6)
        keyword_entry = ttk.Entry(filters, textvariable=self.keyword_filter_var)
        keyword_entry.grid(row=3, column=1, sticky="ew", padx=(0, 8), pady=6)

        action_row = ttk.Frame(filters)
        action_row.grid(row=4, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 8))
        ttk.Button(action_row, text="应用筛选", command=self._request_refresh_entry_list).pack(
            side=tk.RIGHT
        )
        ttk.Button(action_row, text="清空", command=self._clear_filters).pack(
            side=tk.RIGHT, padx=(0, 6)
        )

        map_entry.bind("<Return>", lambda _event: self._request_refresh_entry_list())
        speaker_entry.bind("<Return>", lambda _event: self._request_refresh_entry_list())
        keyword_entry.bind("<Return>", lambda _event: self._request_refresh_entry_list())

        list_frame = ttk.LabelFrame(parent, text="问题列表（pageUP/pageDown键可快速切换）")
        list_frame.grid(row=1, column=0, sticky="nsew")
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)

        self.entry_tree = ttk.Treeview(
            list_frame,
            columns=ENTRY_TREE_COLUMNS,
            show="headings",
            selectmode="browse",
        )
        for column in ENTRY_TREE_COLUMNS:
            self.entry_tree.heading(column, text=ENTRY_TREE_HEADINGS[column])
            self.entry_tree.column(
                column,
                width=ENTRY_TREE_WIDTHS[column],
                minwidth=36,
                anchor="w",
                stretch=(column in ENTRY_TREE_STRETCH_COLUMNS),
            )

        tree_scroll_y = ttk.Scrollbar(
            list_frame, orient=tk.VERTICAL, command=self.entry_tree.yview
        )
        self.entry_tree.configure(yscrollcommand=tree_scroll_y.set)
        self.entry_tree.grid(row=0, column=0, sticky="nsew")
        tree_scroll_y.grid(row=0, column=1, sticky="ns")
        self.entry_tree.bind("<<TreeviewSelect>>", self._on_entry_selected)

        ttk.Label(parent, textvariable=self.list_stats_var, anchor="w").grid(
            row=2, column=0, sticky="ew", pady=(6, 0)
        )

    def _build_right_panel(self, parent: ttk.Frame) -> None:
        top_actions = ttk.Frame(parent)
        top_actions.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        top_actions.columnconfigure(0, weight=1)
        top_actions.rowconfigure(0, weight=0)
        ttk.Label(top_actions, textvariable=self.overview_var, anchor="w").grid(
            row=0, column=0, sticky="ew", pady=(0, 6)
        )
        button_row = ttk.Frame(top_actions)
        button_row.grid(row=1, column=0, sticky="ew")
        top_action_commands = {
            "restore": self._restore_current_entry,
            "apply": self._apply_current_changes,
            "save": self._save,
        }
        for column, (action_id, label, min_width) in enumerate(TOP_ACTION_BUTTONS):
            button_row.columnconfigure(
                column,
                weight=TOP_ACTION_BUTTON_WEIGHTS.get(action_id, 1),
                uniform="top_actions" if TOP_ACTION_BUTTON_WEIGHTS.get(action_id, 1) else "",
                minsize=min_width,
            )
            padx = (0, 6) if column < len(TOP_ACTION_BUTTONS) - 1 else (0, 0)
            if action_id == "auto_apply":
                ttk.Checkbutton(
                    button_row,
                    text=label,
                    variable=self.auto_apply_on_switch_var,
                ).grid(row=0, column=column, sticky="w", padx=padx)
            else:
                button_label = "保存另存为" if action_id == "save" and not self.integrated_mode else label
                ttk.Button(
                    button_row,
                    text=button_label,
                    command=top_action_commands[action_id],
                ).grid(row=0, column=column, sticky="ew", padx=padx)

        original_frame = ttk.LabelFrame(parent, text="原文")
        original_frame.grid(row=2, column=0, sticky="nsew", pady=(0, 8))
        original_frame.columnconfigure(0, weight=1)
        original_frame.rowconfigure(0, weight=1)

        self.original_text = tk.Text(original_frame, wrap=tk.WORD, state=tk.DISABLED, height=10)
        original_scroll = ttk.Scrollbar(
            original_frame, orient=tk.VERTICAL, command=self.original_text.yview
        )
        self.original_text.configure(yscrollcommand=original_scroll.set)
        self.original_text.grid(row=0, column=0, sticky="nsew")
        original_scroll.grid(row=0, column=1, sticky="ns")

        translation_frame = ttk.LabelFrame(parent, text="译文")
        translation_frame.grid(row=3, column=0, sticky="nsew", pady=(0, 8))
        translation_frame.columnconfigure(0, weight=1)
        translation_frame.rowconfigure(0, weight=1)

        self.translation_text = tk.Text(translation_frame, wrap=tk.WORD, undo=True, height=12)
        self.translation_text.tag_configure("overflow_line", background="#ffe6e6")
        translation_scroll = ttk.Scrollbar(
            translation_frame, orient=tk.VERTICAL, command=self.translation_text.yview
        )
        self.translation_text.configure(yscrollcommand=translation_scroll.set)
        self.translation_text.grid(row=0, column=0, sticky="nsew")
        translation_scroll.grid(row=0, column=1, sticky="ns")
        self.translation_text.bind("<<Modified>>", self._on_translation_modified)

        notebook = ttk.Notebook(parent)
        notebook.grid(row=4, column=0, sticky="nsew")

        metrics_tab = ttk.Frame(notebook, padding=6)
        metrics_tab.columnconfigure(0, weight=1)
        metrics_tab.rowconfigure(0, weight=1)
        notebook.add(metrics_tab, text="行宽明细")

        metric_columns = tuple(column for column, _heading, _width, _stretch in LINE_DETAIL_COLUMNS)
        self.line_tree = ttk.Treeview(
            metrics_tab,
            columns=metric_columns,
            show="headings",
            selectmode="none",
        )
        for column, heading, width, stretch in LINE_DETAIL_COLUMNS:
            self.line_tree.heading(column, text=heading)
            self.line_tree.column(
                column,
                width=width,
                minwidth=56,
                anchor="w",
                stretch=stretch,
            )
        self.line_tree.tag_configure("overflow", background="#ffe6e6")
        self.line_tree.tag_configure("ok", background="#ecfff1")
        line_scroll = ttk.Scrollbar(metrics_tab, orient=tk.VERTICAL, command=self.line_tree.yview)
        self.line_tree.configure(yscrollcommand=line_scroll.set)
        self.line_tree.grid(row=0, column=0, sticky="nsew")
        line_scroll.grid(row=0, column=1, sticky="ns")

        rules_tab = ttk.Frame(notebook, padding=6)
        rules_tab.columnconfigure(0, weight=1)
        rules_tab.rowconfigure(1, weight=1)
        rules_tab.rowconfigure(2, weight=1)
        notebook.add(rules_tab, text="规则")
        self._build_rules_tab(rules_tab)

    def _build_rules_tab(self, parent: ttk.Frame) -> None:
        controls = ttk.Frame(parent)
        controls.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        controls.columnconfigure(0, weight=1)

        left_controls = ttk.Frame(controls)
        left_controls.grid(row=0, column=0, sticky="ew")
        for column in range(6):
            left_controls.columnconfigure(column, weight=1, uniform="rule_buttons")
        for column, (label, command) in enumerate(
            (
                ("新增", self._add_rule),
                ("编辑", self._edit_rule),
                ("删除", self._delete_rule),
                ("启用/停用", self._toggle_rule_enabled),
                ("保存规则", self._save_rules),
                ("规则说明", self._show_rules_help),
            )
        ):
            ttk.Button(left_controls, text=label, command=command).grid(
                row=0,
                column=column,
                sticky="ew",
                padx=(0, 6 if column < 5 else 0),
            )

        right_controls = ttk.Frame(controls)
        right_controls.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        right_controls.columnconfigure(1, weight=1)
        right_controls.columnconfigure(2, weight=1, uniform="rule_apply")
        right_controls.columnconfigure(3, weight=1, uniform="rule_apply")
        ttk.Label(right_controls, text="范围").grid(row=0, column=0, sticky="w", padx=(0, 4))
        self.rule_scope_combo = ttk.Combobox(
            right_controls,
            state="readonly",
            values=[SCOPE_MODE_LABELS[key] for key in SCOPE_MODE_LABELS],
            width=14,
        )
        self.rule_scope_combo.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        self.rule_scope_combo.set(SCOPE_MODE_LABELS["selected"])
        self.rule_scope_combo.bind("<<ComboboxSelected>>", self._on_rule_scope_changed)
        ttk.Button(right_controls, text="预览启用规则", command=self._preview_rules).grid(
            row=0, column=2, sticky="ew", padx=(0, 6)
        )
        ttk.Button(right_controls, text="应用启用规则", command=self._apply_rules).grid(
            row=0, column=3, sticky="ew"
        )

        tree_frame = ttk.Frame(parent)
        tree_frame.grid(row=1, column=0, sticky="nsew", pady=(0, 6))
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)

        rule_columns = tuple(column for column, _heading, _width, _stretch in RULE_TREE_COLUMNS)
        self.rule_tree = ttk.Treeview(
            tree_frame,
            columns=rule_columns,
            show="headings",
            selectmode="browse",
        )
        for column, heading, width, stretch in RULE_TREE_COLUMNS:
            self.rule_tree.heading(column, text=heading)
            self.rule_tree.column(
                column,
                width=width,
                minwidth=48,
                anchor="w",
                stretch=stretch,
            )
        rule_scroll = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.rule_tree.yview)
        self.rule_tree.configure(yscrollcommand=rule_scroll.set)
        self.rule_tree.grid(row=0, column=0, sticky="nsew")
        rule_scroll.grid(row=0, column=1, sticky="ns")
        self.rule_tree.bind("<Double-1>", lambda _event: self._edit_rule())

        preview_frame = ttk.LabelFrame(parent, text="预览结果")
        preview_frame.grid(row=2, column=0, sticky="nsew")
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(1, weight=1)
        ttk.Label(preview_frame, textvariable=self.preview_var, anchor="w").grid(
            row=0, column=0, sticky="ew", pady=(0, 6)
        )
        self.preview_text = tk.Text(preview_frame, wrap=tk.WORD, height=8, state=tk.DISABLED)
        self.preview_text.grid(row=1, column=0, sticky="nsew")

    def _bind_shortcuts(self) -> None:
        self.root.bind("<Control-s>", self._on_shortcut_save)
        self.root.bind("<Control-Return>", self._on_shortcut_apply)
        self.root.bind("<Prior>", self._on_shortcut_previous)
        self.root.bind("<Next>", self._on_shortcut_next)

    def _on_shortcut_save(self, _event: tk.Event) -> str:
        self._save()
        return "break"

    def _on_shortcut_apply(self, _event: tk.Event) -> str:
        self._apply_current_changes()
        return "break"

    def _on_shortcut_next(self, _event: tk.Event) -> str:
        self._select_next_visible()
        return "break"

    def _on_shortcut_previous(self, _event: tk.Event) -> str:
        self._select_previous_visible()
        return "break"

    def _choose_file(self) -> None:
        path = filedialog.askopenfilename(
            title="选择翻译 JSON 文件",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if path:
            self.load_file(Path(path))

    def load_file(self, path: Path) -> None:
        """Load translation JSON and its sidecar rules."""
        if not path.exists():
            messagebox.showerror("加载失败", f"文件不存在:\n{path}", parent=self.root)
            return

        if not self._confirm_discard_unsaved_context():
            return

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            fallback_records = (
                load_fallback_records(self.fallback_csv_path)
                if self.fallback_csv_path is not None
                else []
            )
            entries = scan_translation_data(data, fallback_records)
            rules_path = default_rules_path(path)
            rules = load_rule_set(rules_path)
        except Exception as exc:
            messagebox.showerror("加载失败", f"无法加载文件:\n{exc}", parent=self.root)
            return

        self.data = data
        self.file_path = path
        self.rule_path = rules_path
        self.fallback_records = fallback_records
        self.entries = entries
        self.entries_by_id = {entry.entry_id: entry for entry in entries}
        self.rules = rules
        self.rules_dirty = False
        self.current_entry_id = None
        self.file_path_var.set(str(path))
        self._refresh_rule_tree()
        self._refresh_entry_list()

        rules_note = f"已加载 {len(rules)} 条规则" if rules else "未找到规则侧车文件"
        stats = summarize_entries(entries)
        self.status_var.set(
            f"已加载 {path.name}。候选条目 {stats['total_entries']} 条，"
            f"回退条目 {stats['fallback_entries']} 条，"
            f"超限条目 {stats['overflow_entries']} 条，超限行 {stats['overflow_lines']} 行；{rules_note}。"
        )

    def _confirm_discard_unsaved_context(self) -> bool:
        dirty_entries = any(entry.dirty for entry in self.entries)
        if dirty_entries or self.rules_dirty or self._has_unapplied_editor_changes():
            return messagebox.askyesno(
                "确认切换",
                "当前存在未导出的修改或未保存的规则。切换文件会保留不了这些界面状态，是否继续？",
                parent=self.root,
            )
        return True

    def _on_status_mode_changed(self, _event: tk.Event) -> None:
        label = self.status_combo.get()
        for mode, mode_label in STATUS_MODE_LABELS.items():
            if label == mode_label:
                self.status_mode_var.set(mode)
                break
        self._request_refresh_entry_list()

    def _on_rule_scope_changed(self, _event: tk.Event) -> None:
        label = self.rule_scope_combo.get()
        for scope, scope_label in SCOPE_MODE_LABELS.items():
            if label == scope_label:
                self.rule_scope_var.set(scope)
                break

    def _clear_filters(self) -> None:
        if not self._ensure_editor_changes_resolved(switching_entries=True):
            return
        self.map_filter_var.set("")
        self.speaker_filter_var.set("")
        self.keyword_filter_var.set("")
        default_mode = "problem" if self.integrated_mode else "overflow"
        self.status_mode_var.set(default_mode)
        self.status_combo.set(STATUS_MODE_LABELS[default_mode])
        self._refresh_entry_list()

    def _request_refresh_entry_list(self) -> None:
        if not self._ensure_editor_changes_resolved(switching_entries=True):
            return
        self._refresh_entry_list()

    def _refresh_entry_list(self, select_entry_id: Optional[str] = None) -> None:
        visible_entries = filter_entries(
            self.entries,
            status_mode=self.status_mode_var.get(),
            map_filter=self.map_filter_var.get(),
            speaker_filter=self.speaker_filter_var.get(),
            keyword_filter=self.keyword_filter_var.get(),
        )
        self.visible_entry_ids = [entry.entry_id for entry in visible_entries]

        self.entry_tree.delete(*self.entry_tree.get_children())
        for entry in visible_entries:
            self.entry_tree.insert(
                "",
                tk.END,
                iid=entry.entry_id,
                values=(
                    entry.map_name,
                    entry.issue_label,
                    entry.marker,
                    entry.speaker_id or "",
                    entry.limit if entry.limit is not None else "-",
                    entry.max_line_width if entry.marker == MESSAGE_MARKER else "-",
                    entry.summary,
                    entry.status_label,
                ),
            )

        all_stats = summarize_entries(self.entries)
        self.list_stats_var.set(
            f"当前显示 {len(visible_entries)} / {all_stats['total_entries']} 条；"
            f"回退 {all_stats['fallback_entries']} 条，"
            f"全部超限 {all_stats['overflow_entries']} 条，已修改 {all_stats['dirty_entries']} 条。"
        )

        if not visible_entries:
            self._clear_detail_view()
            return

        target_id = select_entry_id or self.current_entry_id
        if target_id in self.visible_entry_ids:
            self.entry_tree.selection_set(target_id)
            self.entry_tree.focus(target_id)
            self.entry_tree.see(target_id)
            self._load_entry_into_editor(target_id)
            return

        first_id = self.visible_entry_ids[0]
        self.entry_tree.selection_set(first_id)
        self.entry_tree.focus(first_id)
        self.entry_tree.see(first_id)
        self._load_entry_into_editor(first_id)

    def _clear_detail_view(self) -> None:
        self.current_entry_id = None
        self._set_readonly_text(self.original_text, "")
        self.loading_editor = True
        self.translation_text.delete("1.0", tk.END)
        self.translation_text.edit_modified(False)
        self.loading_editor = False
        self.translation_text.tag_remove("overflow_line", "1.0", tk.END)
        self.line_tree.delete(*self.line_tree.get_children())
        self.overview_var.set("没有符合筛选条件的条目。")

    def _on_entry_selected(self, _event: tk.Event) -> None:
        selection = self.entry_tree.selection()
        if not selection:
            return
        target_id = selection[0]
        if target_id == self.current_entry_id:
            return
        if not self._ensure_editor_changes_resolved(switching_entries=True):
            if self.current_entry_id and self.current_entry_id in self.visible_entry_ids:
                self.entry_tree.selection_set(self.current_entry_id)
                self.entry_tree.focus(self.current_entry_id)
            return
        self._load_entry_into_editor(target_id)

    def _load_entry_into_editor(self, entry_id: str) -> None:
        entry = self.entries_by_id.get(entry_id)
        if not entry:
            return
        self.current_entry_id = entry_id
        self._set_readonly_text(self.original_text, entry.original_key)
        self.loading_editor = True
        self.translation_text.delete("1.0", tk.END)
        self.translation_text.insert("1.0", entry.current_text)
        self.translation_text.edit_modified(False)
        self.loading_editor = False
        self._refresh_current_entry_metrics(editor_text=entry.current_text)
        self._focus_problem_line(entry.overflow_lines)

    def _set_readonly_text(self, widget: tk.Text, text: str) -> None:
        widget.configure(state=tk.NORMAL)
        widget.delete("1.0", tk.END)
        widget.insert("1.0", text)
        widget.configure(state=tk.DISABLED)

    def _on_translation_modified(self, _event: tk.Event) -> None:
        if self.loading_editor:
            self.translation_text.edit_modified(False)
            return
        if self.current_entry_id:
            self._refresh_current_entry_metrics(editor_text=self._get_editor_text())
        self.translation_text.edit_modified(False)

    def _refresh_current_entry_metrics(self, editor_text: Optional[str] = None) -> None:
        entry = self.entries_by_id.get(self.current_entry_id or "")
        if not entry:
            return

        text = editor_text if editor_text is not None else self._get_editor_text()
        lines = text.split("\n")
        if entry.marker == MESSAGE_MARKER and entry.limit is not None:
            line_widths: List[Optional[int]] = [calc_display_width(line) for line in lines]
            overflow_lines = [
                index
                for index, width in enumerate(line_widths, start=1)
                if width is not None and width > entry.limit
            ]
            limit_label = str(entry.limit)
            max_width_label = str(max((width or 0) for width in line_widths)) if line_widths else "0"
        else:
            line_widths = [None for _line in lines]
            overflow_lines = []
            limit_label = "-"
            max_width_label = "-"
        overflow_label = ", ".join(str(index) for index in overflow_lines) if overflow_lines else "无"
        unapplied = text != entry.current_text
        reason_label = f" | 原因 {_truncate_preview(entry.fallback_reason, 80)}" if entry.fallback_reason else ""
        self.overview_var.set(
            f"{entry.map_name} | {entry.issue_label} | {entry.marker} | "
            f"{entry.speaker_id or '无说话人'} | 限制 {limit_label} | "
            f"当前行数 {len(line_widths)} / 原始行数 {entry.original_line_count} | "
            f"最宽行 {max_width_label} | 超限行 {overflow_label}"
            + reason_label
            + (" | 有未应用修改" if unapplied else "")
        )

        self.line_tree.delete(*self.line_tree.get_children())
        for index, (line, width) in enumerate(zip(text.split("\n"), line_widths), start=1):
            is_over = width is not None and entry.limit is not None and width > entry.limit
            self.line_tree.insert(
                "",
                tk.END,
                values=(index, width if width is not None else "-", "超限" if is_over else "正常", line),
                tags=("overflow" if is_over else "ok",),
            )

        self.translation_text.tag_remove("overflow_line", "1.0", tk.END)
        for line_no in overflow_lines:
            self.translation_text.tag_add(
                "overflow_line", f"{line_no}.0", f"{line_no}.end"
            )

    def _get_editor_text(self) -> str:
        return self.translation_text.get("1.0", tk.END + "-1c")

    def _focus_problem_line(self, overflow_lines: Sequence[int]) -> None:
        line_no = overflow_lines[0] if overflow_lines else 1
        target_index = f"{line_no}.0"
        self.translation_text.mark_set(tk.INSERT, target_index)
        self.translation_text.see(target_index)
        self.translation_text.focus_set()

    def _has_unapplied_editor_changes(self) -> bool:
        entry = self.entries_by_id.get(self.current_entry_id or "")
        if not entry:
            return False
        return self._get_editor_text() != entry.current_text

    def _ensure_editor_changes_resolved(self, switching_entries: bool = False) -> bool:
        if not self._has_unapplied_editor_changes():
            return True
        entry = self.entries_by_id.get(self.current_entry_id or "")
        if not entry:
            return True

        if switching_entries and self.auto_apply_on_switch_var.get():
            self._apply_current_changes()
            return True

        response = messagebox.askyesnocancel(
            "未应用修改",
            "当前条目有未应用的修改。是否先应用？\n"
            "选择“是”会保存到本次会话数据；选择“否”会丢弃编辑器里的未应用内容。",
            parent=self.root,
        )
        if response is None:
            return False
        if response:
            self._apply_current_changes()
            return True

        self.loading_editor = True
        self.translation_text.delete("1.0", tk.END)
        self.translation_text.insert("1.0", entry.current_text)
        self.translation_text.edit_modified(False)
        self.loading_editor = False
        self._refresh_current_entry_metrics(editor_text=entry.current_text)
        return True

    def _apply_current_changes(self) -> None:
        entry = self.entries_by_id.get(self.current_entry_id or "")
        if not entry:
            return

        new_text = self._get_editor_text()
        entry.update_text(new_text)
        self._refresh_current_entry_metrics(editor_text=entry.current_text)

        current_visible_index = (
            self.visible_entry_ids.index(entry.entry_id)
            if entry.entry_id in self.visible_entry_ids
            else None
        )
        self._refresh_entry_list(select_entry_id=entry.entry_id)
        if self.current_entry_id != entry.entry_id and current_visible_index is not None:
            self._select_visible_by_index(current_visible_index)
        self.status_var.set(
            f"已应用当前条目的修改。{entry.map_name} -> {entry.issue_label}，"
            f"最宽行 {entry.max_line_width if entry.marker == MESSAGE_MARKER else '-'}，"
            f"超限行数 {entry.overflow_count if entry.marker == MESSAGE_MARKER else '-'}。"
        )

    def _restore_current_entry(self) -> None:
        entry = self.entries_by_id.get(self.current_entry_id or "")
        if not entry:
            return
        if not messagebox.askyesno(
            "确认恢复",
            "将当前条目恢复为文件中最初加载的译文，是否继续？",
            parent=self.root,
        ):
            return
        entry.restore_initial()
        self.loading_editor = True
        self.translation_text.delete("1.0", tk.END)
        self.translation_text.insert("1.0", entry.current_text)
        self.translation_text.edit_modified(False)
        self.loading_editor = False
        self._refresh_current_entry_metrics(editor_text=entry.current_text)
        self._refresh_entry_list(select_entry_id=entry.entry_id)
        self.status_var.set(f"已恢复 {entry.map_name} 中当前条目的原始译文。")

    def _select_visible_by_index(self, index: int) -> None:
        if not self.visible_entry_ids:
            return
        index = max(0, min(index, len(self.visible_entry_ids) - 1))
        target_id = self.visible_entry_ids[index]
        self._select_visible_by_id(target_id)

    def _select_visible_by_id(self, target_id: str) -> None:
        if target_id not in self.visible_entry_ids:
            return
        self.entry_tree.selection_set(target_id)
        self.entry_tree.focus(target_id)
        self.entry_tree.see(target_id)
        self._load_entry_into_editor(target_id)

    def _neighbor_entry_id(self, direction: int) -> Optional[str]:
        if not self.visible_entry_ids or not self.current_entry_id:
            return None
        try:
            current_index = self.visible_entry_ids.index(self.current_entry_id)
        except ValueError:
            return None
        target_index = current_index + direction
        if 0 <= target_index < len(self.visible_entry_ids):
            return self.visible_entry_ids[target_index]
        return None

    def _select_previous_visible(self) -> None:
        target_id = self._neighbor_entry_id(-1)
        if not target_id:
            return
        if not self._ensure_editor_changes_resolved(switching_entries=True):
            return
        self._select_visible_by_id(target_id)

    def _select_next_visible(self) -> None:
        target_id = self._neighbor_entry_id(1)
        if not target_id:
            return
        if not self._ensure_editor_changes_resolved(switching_entries=True):
            return
        self._select_visible_by_id(target_id)

    def _current_scope_entries(self) -> List[ReviewEntry]:
        if self.rule_scope_var.get() == "selected":
            entry = self.entries_by_id.get(self.current_entry_id or "")
            return [entry] if entry else []
        return [self.entries_by_id[entry_id] for entry_id in self.visible_entry_ids]

    def _refresh_rule_tree(self) -> None:
        self.rule_tree.delete(*self.rule_tree.get_children())
        for index, rule in enumerate(self.rules):
            self.rule_tree.insert(
                "",
                tk.END,
                iid=f"rule_{index}",
                values=(
                    "☑" if rule.enabled else "☐",
                    rule.name,
                    ",".join(rule.normalized_flags()),
                    _truncate_preview(rule.pattern, 48),
                    _truncate_preview(rule.replacement, 48),
                ),
            )

    def _selected_rule_index(self) -> Optional[int]:
        selection = self.rule_tree.selection()
        if not selection:
            return None
        iid = selection[0]
        if not iid.startswith("rule_"):
            return None
        return int(iid.split("_", 1)[1])

    def _show_rules_help(self) -> None:
        show_help_window(self.root, "规则说明", RULE_OVERVIEW_HELP_TEXT)

    def _add_rule(self) -> None:
        dialog = RuleEditorDialog(self.root)
        self.root.wait_window(dialog)
        if dialog.result is None:
            return
        self.rules.append(dialog.result)
        self.rules_dirty = True
        self._refresh_rule_tree()
        self.status_var.set(f"已新增规则：{dialog.result.name}")

    def _edit_rule(self) -> None:
        index = self._selected_rule_index()
        if index is None:
            messagebox.showinfo("提示", "请先选择一条规则。", parent=self.root)
            return
        dialog = RuleEditorDialog(self.root, initial_rule=self.rules[index])
        self.root.wait_window(dialog)
        if dialog.result is None:
            return
        self.rules[index] = dialog.result
        self.rules_dirty = True
        self._refresh_rule_tree()
        self.rule_tree.selection_set(f"rule_{index}")
        self.status_var.set(f"已更新规则：{dialog.result.name}")

    def _delete_rule(self) -> None:
        index = self._selected_rule_index()
        if index is None:
            messagebox.showinfo("提示", "请先选择一条规则。", parent=self.root)
            return
        rule_name = self.rules[index].name
        if not messagebox.askyesno(
            "确认删除",
            f"确定删除规则“{rule_name}”吗？",
            parent=self.root,
        ):
            return
        del self.rules[index]
        self.rules_dirty = True
        self._refresh_rule_tree()
        self.status_var.set(f"已删除规则：{rule_name}")

    def _toggle_rule_enabled(self) -> None:
        index = self._selected_rule_index()
        if index is None:
            messagebox.showinfo("提示", "请先选择一条规则。", parent=self.root)
            return
        self.rules[index].enabled = not self.rules[index].enabled
        self.rules_dirty = True
        self._refresh_rule_tree()
        self.rule_tree.selection_set(f"rule_{index}")
        state = "启用" if self.rules[index].enabled else "停用"
        self.status_var.set(f"已{state}规则：{self.rules[index].name}")

    def _save_rules(self) -> None:
        if not self.rule_path:
            messagebox.showinfo("提示", "请先加载翻译文件，再保存规则。", parent=self.root)
            return
        try:
            save_rule_set(self.rule_path, self.rules)
        except Exception as exc:
            messagebox.showerror("保存失败", f"无法保存规则文件:\n{exc}", parent=self.root)
            return
        self.rules_dirty = False
        self.status_var.set(f"规则已保存到 {self.rule_path.name}")

    def _set_preview_text(self, text: str) -> None:
        self.preview_text.configure(state=tk.NORMAL)
        self.preview_text.delete("1.0", tk.END)
        self.preview_text.insert("1.0", text)
        self.preview_text.configure(state=tk.DISABLED)

    def _preview_rules(self) -> None:
        if not self._ensure_editor_changes_resolved():
            return
        scope_entries = self._current_scope_entries()
        if not scope_entries:
            messagebox.showinfo("提示", "当前规则范围内没有可处理的条目。", parent=self.root)
            return
        try:
            preview = preview_rule_application(scope_entries, self.rules)
        except Exception as exc:
            messagebox.showerror("预览失败", str(exc), parent=self.root)
            return

        self.preview_var.set(
            f"作用范围 {preview.scope_size} 条；命中 {preview.changed_entries} 条；"
            f"替换次数 {preview.total_substitutions}。"
        )
        self._set_preview_text(
            "\n".join(
                [
                    f"超限条目：{preview.overflow_entries_before} -> {preview.overflow_entries_after}",
                    f"超限行数：{preview.overflow_lines_before} -> {preview.overflow_lines_after}",
                    "",
                    "说明：预览按启用中的规则顺序串行应用，不会修改当前数据。",
                ]
            )
        )
        self.status_var.set("规则预览完成。")

    def _apply_rules(self) -> None:
        if not self._ensure_editor_changes_resolved():
            return
        scope_entries = self._current_scope_entries()
        if not scope_entries:
            messagebox.showinfo("提示", "当前规则范围内没有可处理的条目。", parent=self.root)
            return

        try:
            preview = preview_rule_application(scope_entries, self.rules)
        except Exception as exc:
            messagebox.showerror("无法应用规则", str(exc), parent=self.root)
            return

        if preview.changed_entries == 0:
            messagebox.showinfo("提示", "启用中的规则不会命中当前范围。", parent=self.root)
            return

        if not messagebox.askyesno(
            "确认应用规则",
            f"将对 {preview.scope_size} 条中的 {preview.changed_entries} 条执行规则替换，"
            f"预计替换 {preview.total_substitutions} 次。是否继续？",
            parent=self.root,
        ):
            return

        try:
            result = apply_rules_to_entries(scope_entries, self.rules)
        except Exception as exc:
            messagebox.showerror("应用失败", str(exc), parent=self.root)
            return

        current_id = self.current_entry_id
        self._refresh_entry_list(select_entry_id=current_id)
        if self.current_entry_id:
            current_entry = self.entries_by_id[self.current_entry_id]
            self._refresh_current_entry_metrics(editor_text=current_entry.current_text)
        self.status_var.set(
            f"规则应用完成：修改 {result.changed_entries} 条，替换 {result.total_substitutions} 次。"
        )

    def _save(self) -> None:
        if self.integrated_mode:
            self._save_integrated()
        else:
            self._save_as()

    def _save_integrated(self, *, show_message: bool = True) -> bool:
        if not self.file_path or self.data is None:
            messagebox.showinfo("提示", "请先加载一个翻译 JSON 文件。", parent=self.root)
            return False
        if self._has_unapplied_editor_changes():
            self._apply_current_changes()

        try:
            result = save_integrated_review(
                self.file_path,
                self.data,
                self.entries,
                self.fallback_csv_path,
                self.fallback_records,
            )
            self.data = json.loads(self.file_path.read_text(encoding="utf-8"))
            self.fallback_records = (
                load_fallback_records(self.fallback_csv_path)
                if self.fallback_csv_path is not None
                else []
            )
            remaining_fallback_keys = {record.key for record in self.fallback_records}
            for entry in self.entries:
                entry.initial_text = entry.current_text
                entry.recompute()
                if entry.key in remaining_fallback_keys:
                    entry.issue_kinds = list(dict.fromkeys([*entry.issue_kinds, "fallback"]))
                elif entry.is_fallback:
                    entry.issue_kinds = [kind for kind in entry.issue_kinds if kind != "fallback"]
                    entry.fallback_reason = None
        except Exception as exc:
            messagebox.showerror("保存失败", f"无法保存审阅结果:\n{exc}", parent=self.root)
            return False

        self._refresh_entry_list(select_entry_id=self.current_entry_id)
        if self.on_saved:
            self.on_saved()
        self.status_var.set(
            f"已保存到 {self.file_path.name}；修改 {result.dirty_entries} 条，"
            f"移除回退 {result.corrected_fallback_entries} 条，"
            f"剩余回退 {result.remaining_fallback_rows} 条。"
        )
        if show_message:
            messagebox.showinfo("保存完成", self.status_var.get(), parent=self.root)
        return True

    def _save_as(self) -> None:
        if not self.file_path or self.data is None:
            messagebox.showinfo("提示", "请先加载一个翻译 JSON 文件。", parent=self.root)
            return
        if not self._ensure_editor_changes_resolved():
            return

        suggested = default_output_path(self.file_path)
        path = filedialog.asksaveasfilename(
            title="另存为",
            defaultextension=".json",
            initialdir=str(suggested.parent),
            initialfile=suggested.name,
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return

        try:
            save_reviewed_output(Path(path), self.data, self.entries)
        except Exception as exc:
            messagebox.showerror("保存失败", f"无法保存输出文件:\n{exc}", parent=self.root)
            return

        dirty_entries = sum(1 for entry in self.entries if entry.dirty)
        self.status_var.set(f"已导出审阅结果到 {Path(path).name}；会话内修改条目 {dirty_entries} 条。")
        messagebox.showinfo("导出完成", f"已保存到:\n{path}", parent=self.root)

    def _on_close(self) -> None:
        if self.integrated_mode:
            if not self._save_integrated(show_message=False):
                return
        elif not self._ensure_editor_changes_resolved():
            return
        if self.rules_dirty:
            save_rules = messagebox.askyesnocancel(
                "未保存规则",
                "规则有未保存修改。关闭前是否保存？",
                parent=self.root,
            )
            if save_rules is None:
                return
            if save_rules:
                self._save_rules()
                if self.rules_dirty:
                    return
        self.root.destroy()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="译文行宽检查器")
    parser.add_argument("json_path", nargs="?", help="可选：待审阅的翻译 JSON 路径")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    initial_path = Path(args.json_path).resolve() if args.json_path else None

    root = tk.Tk()
    style = ttk.Style(root)
    for theme in ("breeze", "clam", "vista", "xpnative"):
        if theme in style.theme_names():
            try:
                style.theme_use(theme)
                break
            except tk.TclError:
                continue

    app = LineLimitCheckerApp(root, initial_path=initial_path)
    if initial_path and not initial_path.exists():
        messagebox.showerror("加载失败", f"文件不存在:\n{initial_path}", parent=root)
        app.status_var.set(f"启动时未找到文件：{initial_path}")
    root.mainloop()


if __name__ == "__main__":
    main()
