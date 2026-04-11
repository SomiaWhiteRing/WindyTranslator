"""Standalone GUI for reviewing RPG Maker translation line-width overflows.

Loads nested translation JSON files, checks only Message entries, and provides
an editor focused on manually reviewing translations that still exceed display
width limits after line-break optimization.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Dict, Iterable, List, Optional, Sequence

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
    "overflow": "仅超限",
    "dirty": "已修改",
    "all": "全部已加载",
}
SCOPE_MODE_LABELS = {
    "selected": "当前选中项",
    "filtered": "当前筛选结果",
}


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
    """In-memory review model for a single Message entry."""

    entry_id: str
    map_name: str
    original_key: str
    speaker_id: Optional[str]
    limit: int
    initial_text: str
    current_text: str
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
    def status_label(self) -> str:
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
        self.per_line_widths = [calc_display_width(line) for line in lines]
        self.overflow_lines = [
            index
            for index, width in enumerate(self.per_line_widths, start=1)
            if width > self.limit
        ]
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


def scan_translation_data(data: dict) -> List[ReviewEntry]:
    """Build review entries from nested translation JSON."""
    validate_nested_translation_data(data)

    review_entries: List[ReviewEntry] = []
    for map_name, entries in data.items():
        for original_key, info in entries.items():
            if info.get("original_marker") != MESSAGE_MARKER:
                continue
            text = info.get("text", "")
            speaker_id = info.get("speaker_id")
            normalized_speaker = str(speaker_id) if speaker_id is not None else None
            review_entries.append(
                ReviewEntry(
                    entry_id=f"entry_{len(review_entries)}",
                    map_name=str(map_name),
                    original_key=str(original_key),
                    speaker_id=normalized_speaker,
                    limit=determine_line_limit(normalized_speaker),
                    initial_text=text,
                    current_text=text,
                )
            )
    return review_entries


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
            haystack = "\n".join((entry.original_key, entry.current_text)).lower()
            if keyword_filter not in haystack:
                continue
        visible.append(entry)
    return visible


def summarize_entries(entries: Sequence[ReviewEntry]) -> dict:
    """Return aggregate counts for a collection of review entries."""
    overflow_entries = sum(1 for entry in entries if entry.is_over_limit)
    overflow_lines = sum(entry.overflow_count for entry in entries)
    dirty_entries = sum(1 for entry in entries if entry.dirty)
    return {
        "total_entries": len(entries),
        "overflow_entries": overflow_entries,
        "overflow_lines": overflow_lines,
        "dirty_entries": dirty_entries,
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
        widths = [calc_display_width(line) for line in new_text.split("\n")]
        overflow_lines = sum(1 for width in widths if width > entry.limit)
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
    return cloned


def save_reviewed_output(output_path: Path, data: dict, entries: Iterable[ReviewEntry]) -> None:
    """Write updated translation JSON to disk."""
    output_path.write_text(
        json.dumps(export_reviewed_data(data, entries), ensure_ascii=False, indent=4),
        encoding="utf-8",
    )


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
        ttk.Button(footer, text="取消", command=self._on_cancel).pack(side=tk.RIGHT)
        ttk.Button(footer, text="确定", command=self._on_submit).pack(
            side=tk.RIGHT, padx=(0, 8)
        )

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

    def __init__(self, root: tk.Tk, initial_path: Optional[Path] = None) -> None:
        self.root = root
        self.root.title("译文行宽检查器")
        self.root.geometry("1500x920")
        self.root.minsize(1180, 720)

        self.data: Optional[dict] = None
        self.file_path: Optional[Path] = None
        self.rule_path: Optional[Path] = None
        self.entries: List[ReviewEntry] = []
        self.entries_by_id: Dict[str, ReviewEntry] = {}
        self.visible_entry_ids: List[str] = []
        self.current_entry_id: Optional[str] = None
        self.loading_editor = False
        self.rules: List[RegexRule] = []
        self.rules_dirty = False

        self.file_path_var = tk.StringVar()
        self.list_stats_var = tk.StringVar(value="未加载文件。")
        self.status_var = tk.StringVar(value="请选择一个翻译 JSON 文件。")
        self.overview_var = tk.StringVar(value="未选择条目。")
        self.preview_var = tk.StringVar(value="尚未执行规则预览。")

        self.status_mode_var = tk.StringVar(value="overflow")
        self.map_filter_var = tk.StringVar()
        self.speaker_filter_var = tk.StringVar()
        self.keyword_filter_var = tk.StringVar()
        self.rule_scope_var = tk.StringVar(value="selected")
        self.auto_apply_on_switch_var = tk.BooleanVar(value=False)

        self._build_ui()
        self._bind_shortcuts()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        if initial_path:
            self.load_file(initial_path)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=8)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.rowconfigure(1, weight=1)
        outer.columnconfigure(0, weight=1)

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
        ttk.Button(top_bar, text="保存另存为", command=self._save_as).grid(row=0, column=3)

        main_pane = ttk.Panedwindow(outer, orient=tk.HORIZONTAL)
        main_pane.grid(row=1, column=0, sticky="nsew")

        left = ttk.Frame(main_pane, padding=(0, 0, 8, 0))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)
        main_pane.add(left, weight=2)

        right = ttk.Frame(main_pane)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(2, weight=2)
        right.rowconfigure(3, weight=3)
        right.rowconfigure(4, weight=3)
        main_pane.add(right, weight=3)

        self._build_left_panel(left)
        self._build_right_panel(right)

        ttk.Label(
            outer,
            textvariable=self.status_var,
            relief="sunken",
            anchor="w",
        ).grid(row=2, column=0, sticky="ew", pady=(8, 0))

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
        status_combo.set(STATUS_MODE_LABELS["overflow"])
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

        list_frame = ttk.LabelFrame(parent, text="问题列表")
        list_frame.grid(row=1, column=0, sticky="nsew")
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)

        columns = ("map", "speaker", "limit", "max_width", "overflow_count", "summary", "status")
        self.entry_tree = ttk.Treeview(
            list_frame,
            columns=columns,
            show="headings",
            selectmode="browse",
        )
        headings = {
            "map": "文件",
            "speaker": "说话人",
            "limit": "上限",
            "max_width": "最宽行",
            "overflow_count": "超限行数",
            "summary": "译文摘要",
            "status": "状态",
        }
        widths = {
            "map": 160,
            "speaker": 110,
            "limit": 50,
            "max_width": 70,
            "overflow_count": 80,
            "summary": 370,
            "status": 100,
        }
        for column in columns:
            self.entry_tree.heading(column, text=headings[column])
            self.entry_tree.column(column, width=widths[column], anchor="w")

        tree_scroll_y = ttk.Scrollbar(
            list_frame, orient=tk.VERTICAL, command=self.entry_tree.yview
        )
        tree_scroll_x = ttk.Scrollbar(
            list_frame, orient=tk.HORIZONTAL, command=self.entry_tree.xview
        )
        self.entry_tree.configure(
            yscrollcommand=tree_scroll_y.set,
            xscrollcommand=tree_scroll_x.set,
        )
        self.entry_tree.grid(row=0, column=0, sticky="nsew")
        tree_scroll_y.grid(row=0, column=1, sticky="ns")
        tree_scroll_x.grid(row=1, column=0, sticky="ew")
        self.entry_tree.bind("<<TreeviewSelect>>", self._on_entry_selected)

        ttk.Label(parent, textvariable=self.list_stats_var, anchor="w").grid(
            row=2, column=0, sticky="ew", pady=(6, 0)
        )

    def _build_right_panel(self, parent: ttk.Frame) -> None:
        top_actions = ttk.Frame(parent)
        top_actions.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        top_actions.columnconfigure(0, weight=1)
        ttk.Label(top_actions, textvariable=self.overview_var, anchor="w").grid(
            row=0, column=0, sticky="ew", padx=(0, 12)
        )
        ttk.Checkbutton(
            top_actions,
            text="切换条目时自动应用更改",
            variable=self.auto_apply_on_switch_var,
        ).grid(row=0, column=1, padx=(0, 12))
        ttk.Button(top_actions, text="恢复本条原译文", command=self._restore_current_entry).grid(
            row=0, column=2, padx=(0, 6)
        )
        ttk.Button(top_actions, text="应用本条修改", command=self._apply_current_changes).grid(
            row=0, column=3
        )

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

        metric_columns = ("line_no", "width", "status", "content")
        self.line_tree = ttk.Treeview(
            metrics_tab,
            columns=metric_columns,
            show="headings",
            selectmode="none",
        )
        for column, heading, width in (
            ("line_no", "行", 50),
            ("width", "宽度", 60),
            ("status", "状态", 70),
            ("content", "内容", 650),
        ):
            self.line_tree.heading(column, text=heading)
            self.line_tree.column(column, width=width, anchor="w")
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
        left_controls.grid(row=0, column=0, sticky="w")
        ttk.Button(left_controls, text="新增", command=self._add_rule).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(left_controls, text="编辑", command=self._edit_rule).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(left_controls, text="删除", command=self._delete_rule).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(left_controls, text="启用/停用", command=self._toggle_rule_enabled).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(left_controls, text="保存规则", command=self._save_rules).pack(side=tk.LEFT)

        right_controls = ttk.Frame(controls)
        right_controls.grid(row=0, column=1, sticky="e")
        ttk.Label(right_controls, text="范围").pack(side=tk.LEFT, padx=(0, 4))
        self.rule_scope_combo = ttk.Combobox(
            right_controls,
            state="readonly",
            values=[SCOPE_MODE_LABELS[key] for key in SCOPE_MODE_LABELS],
            width=14,
        )
        self.rule_scope_combo.pack(side=tk.LEFT, padx=(0, 8))
        self.rule_scope_combo.set(SCOPE_MODE_LABELS["selected"])
        self.rule_scope_combo.bind("<<ComboboxSelected>>", self._on_rule_scope_changed)
        ttk.Button(right_controls, text="预览启用规则", command=self._preview_rules).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(right_controls, text="应用启用规则", command=self._apply_rules).pack(
            side=tk.LEFT
        )

        tree_frame = ttk.Frame(parent)
        tree_frame.grid(row=1, column=0, sticky="nsew", pady=(0, 6))
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)

        rule_columns = ("enabled", "name", "flags", "pattern", "replacement")
        self.rule_tree = ttk.Treeview(
            tree_frame,
            columns=rule_columns,
            show="headings",
            selectmode="browse",
        )
        for column, heading, width in (
            ("enabled", "启用", 50),
            ("name", "名称", 120),
            ("flags", "Flags", 120),
            ("pattern", "Pattern", 260),
            ("replacement", "Replacement", 220),
        ):
            self.rule_tree.heading(column, text=heading)
            self.rule_tree.column(column, width=width, anchor="w")
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
        self._save_as()
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
            entries = scan_translation_data(data)
            rules_path = default_rules_path(path)
            rules = load_rule_set(rules_path)
        except Exception as exc:
            messagebox.showerror("加载失败", f"无法加载文件:\n{exc}", parent=self.root)
            return

        self.data = data
        self.file_path = path
        self.rule_path = rules_path
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
            f"已加载 {path.name}。Message 条目 {stats['total_entries']} 条，"
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
        self.status_mode_var.set("overflow")
        self.status_combo.set(STATUS_MODE_LABELS["overflow"])
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
                    entry.speaker_id or "",
                    entry.limit,
                    entry.max_line_width,
                    entry.overflow_count,
                    entry.summary,
                    entry.status_label,
                ),
            )

        all_stats = summarize_entries(self.entries)
        self.list_stats_var.set(
            f"当前显示 {len(visible_entries)} / {all_stats['total_entries']} 条；"
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
        line_widths = [calc_display_width(line) for line in text.split("\n")]
        overflow_lines = [
            index for index, width in enumerate(line_widths, start=1) if width > entry.limit
        ]
        overflow_label = ", ".join(str(index) for index in overflow_lines) if overflow_lines else "无"
        unapplied = text != entry.current_text
        self.overview_var.set(
            f"{entry.map_name} | {entry.speaker_id or '无说话人'} | 限制 {entry.limit} | "
            f"当前行数 {len(line_widths)} / 原始行数 {entry.original_line_count} | "
            f"最宽行 {max(line_widths, default=0)} | 超限行 {overflow_label}"
            + (" | 有未应用修改" if unapplied else "")
        )

        self.line_tree.delete(*self.line_tree.get_children())
        for index, (line, width) in enumerate(zip(text.split("\n"), line_widths), start=1):
            is_over = width > entry.limit
            self.line_tree.insert(
                "",
                tk.END,
                values=(index, width, "超限" if is_over else "正常", line),
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
            f"已应用当前条目的修改。{entry.map_name} -> 最宽行 {entry.max_line_width}，"
            f"超限行数 {entry.overflow_count}。"
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
        if not self._ensure_editor_changes_resolved():
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
