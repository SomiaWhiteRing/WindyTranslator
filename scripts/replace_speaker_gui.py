"""Batch-replace speaker name translations in RPG Maker translation JSON files.

Standalone Tkinter GUI tool. Supports both nested (Format A) and flat (Format B)
translation JSON structures used across the Works/ folder.
"""
from __future__ import annotations

import json
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class SpeakerMatch:
    """A single entry where a speaker name was found."""
    map_name: Optional[str]     # Format A: map filename; Format B: None
    original_key: str           # Full original Japanese key
    translated_speaker: str     # Current Chinese speaker name
    rest_of_text: str           # Everything after the speaker name in translated text
    separator: str              # How speaker is separated from text: "\n" or "「"


@dataclass
class SpeakerVariant:
    """A unique Chinese translation variant for a given Japanese speaker."""
    chinese_name: str
    count: int = 0
    entries: List[SpeakerMatch] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def detect_format(data: dict) -> str:
    """Detect JSON format. Returns 'A' (nested metadata) or 'B' (flat key-value)."""
    for value in data.values():
        if isinstance(value, dict):
            for sub_value in value.values():
                if isinstance(sub_value, dict) and "text" in sub_value:
                    return "A"
                break
        break
    return "B"


def count_entries(data: dict, fmt: str) -> int:
    """Count total translation entries."""
    if fmt == "A":
        return sum(len(entries) for entries in data.values() if isinstance(entries, dict))
    return len(data)


def _extract_speaker(orig_key: str, text: str, jp_speaker: str):
    """Try to match a Japanese speaker name in the key and extract the Chinese name.

    Supports two patterns:
      1. ``speaker\\nDialogue``  – name on its own line
      2. ``speaker「Dialogue``   – name and dialogue on the same line

    Returns ``(cn_speaker, rest_of_text, separator)`` or *None* if no match.
    """
    for sep in ("\n", "\u300c"):  # \u300c = 「
        if not orig_key.startswith(jp_speaker + sep):
            continue
        if not text:
            continue
        # Find the same separator in the translated text
        idx = text.find(sep)
        if idx == -1:
            # Fallback: for 「 pattern the translator may have used \n instead
            idx = text.find("\n")
            if idx == -1:
                continue
            cn_speaker = text[:idx]
            rest = text[idx + 1:]
            return cn_speaker, rest, sep
        cn_speaker = text[:idx]
        rest = text[idx + len(sep):]
        return cn_speaker, rest, sep
    return None


def find_speaker_variants(
    data: dict, jp_speaker: str, fmt: str
) -> Dict[str, SpeakerVariant]:
    """Find all unique Chinese translation variants for a Japanese speaker name."""
    variants: Dict[str, SpeakerVariant] = {}

    def _process(orig_key: str, text: str, map_name: Optional[str]) -> None:
        result = _extract_speaker(orig_key, text, jp_speaker)
        if result is None:
            return
        cn_speaker, rest, sep = result
        match = SpeakerMatch(
            map_name=map_name,
            original_key=orig_key,
            translated_speaker=cn_speaker,
            rest_of_text=rest,
            separator=sep,
        )
        variant = variants.setdefault(cn_speaker, SpeakerVariant(cn_speaker))
        variant.count += 1
        variant.entries.append(match)

    if fmt == "A":
        for map_name, entries in data.items():
            if not isinstance(entries, dict):
                continue
            for orig_key, entry in entries.items():
                text = entry.get("text", "") if isinstance(entry, dict) else ""
                _process(orig_key, text, map_name)
    else:
        for orig_key, translated in data.items():
            if isinstance(translated, str):
                _process(orig_key, translated, None)

    return variants


def apply_replacements(
    data: dict, fmt: str, variants_to_replace: List[SpeakerVariant], target: str
) -> int:
    """Replace speaker names in-place. Returns count of modified entries."""
    changed = 0
    for variant in variants_to_replace:
        for match in variant.entries:
            # Reconstruct with the same separator that was found during search
            sep = match.separator
            new_text = target + sep + match.rest_of_text
            if fmt == "A":
                entry = data[match.map_name][match.original_key]
                if isinstance(entry, dict):
                    entry["text"] = new_text
            else:
                data[match.original_key] = new_text
            changed += 1
    return changed


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class SpeakerReplacerApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("说话人名称批量替换工具")
        self.root.geometry("750x520")

        # State
        self.data: Optional[dict] = None
        self.fmt: Optional[str] = None
        self.file_path: Optional[Path] = None
        self.variants: Dict[str, SpeakerVariant] = {}

        # Tk variables
        self.file_path_var = tk.StringVar()
        self.jp_name_var = tk.StringVar()
        self.target_name_var = tk.StringVar()

        self._build_ui()

    # ---- UI construction ----

    def _build_ui(self) -> None:
        pad = {"padx": 8, "pady": 4}

        # Row 1: file selection
        row1 = ttk.Frame(self.root)
        row1.pack(fill=tk.X, **pad)
        ttk.Label(row1, text="文件:").pack(side=tk.LEFT)
        ttk.Entry(row1, textvariable=self.file_path_var, state="readonly").pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 4)
        )
        ttk.Button(row1, text="选择文件...", command=self._select_file).pack(side=tk.RIGHT)

        # Row 2: speaker name input
        row2 = ttk.Frame(self.root)
        row2.pack(fill=tk.X, **pad)
        ttk.Label(row2, text="日文说话人名:").pack(side=tk.LEFT)
        jp_entry = ttk.Entry(row2, textvariable=self.jp_name_var)
        jp_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 4))
        jp_entry.bind("<Return>", lambda _: self._on_search())
        ttk.Button(row2, text="搜索", command=self._on_search).pack(side=tk.RIGHT)

        # Row 3: results treeview
        tree_frame = ttk.Frame(self.root)
        tree_frame.pack(fill=tk.BOTH, expand=True, **pad)

        columns = ("selected", "chinese_name", "count", "example")
        self.result_tree = ttk.Treeview(
            tree_frame, columns=columns, show="headings", selectmode="browse"
        )
        self.result_tree.heading("selected", text="选择")
        self.result_tree.heading("chinese_name", text="中文译名")
        self.result_tree.heading("count", text="出现次数")
        self.result_tree.heading("example", text="示例")

        self.result_tree.column("selected", width=50, anchor="center", stretch=False)
        self.result_tree.column("chinese_name", width=160, anchor="w")
        self.result_tree.column("count", width=80, anchor="center", stretch=False)
        self.result_tree.column("example", width=400, anchor="w")

        scrollbar = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.result_tree.yview)
        self.result_tree.configure(yscrollcommand=scrollbar.set)
        self.result_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.result_tree.bind("<ButtonRelease-1>", self._toggle_check)
        self.result_tree.bind("<Double-1>", self._fill_target_from_row)

        # Row 4: target name input
        row4 = ttk.Frame(self.root)
        row4.pack(fill=tk.X, **pad)
        ttk.Label(row4, text="目标中文名:").pack(side=tk.LEFT)
        ttk.Entry(row4, textvariable=self.target_name_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0)
        )

        # Row 5: action buttons
        row5 = ttk.Frame(self.root)
        row5.pack(fill=tk.X, **pad)
        ttk.Button(row5, text="全选", command=self._select_all).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(row5, text="取消全选", command=self._deselect_all).pack(side=tk.LEFT)
        ttk.Button(row5, text="执行替换", command=self._on_replace).pack(side=tk.RIGHT)

        # Row 6: status bar
        self.status_var = tk.StringVar(value="请选择一个翻译JSON文件。")
        ttk.Label(self.root, textvariable=self.status_var, relief="sunken", anchor="w").pack(
            fill=tk.X, side=tk.BOTTOM, padx=8, pady=(0, 8)
        )

    # ---- File selection ----

    def _select_file(self) -> None:
        path = filedialog.askopenfilename(
            title="选择翻译JSON文件",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        self.file_path = Path(path)
        self.file_path_var.set(str(self.file_path))
        try:
            self.data = json.loads(self.file_path.read_text(encoding="utf-8"))
            self.fmt = detect_format(self.data)
            total = count_entries(self.data, self.fmt)
            fmt_label = "嵌套(A)" if self.fmt == "A" else "扁平(B)"
            self.status_var.set(f"已加载: {self.file_path.name}  |  格式: {fmt_label}  |  条目数: {total}")
        except Exception as e:
            messagebox.showerror("加载失败", f"无法加载JSON文件:\n{e}")
            self.data = None
            self.fmt = None
        self._clear_results()

    # ---- Search ----

    def _on_search(self) -> None:
        if not self.data:
            messagebox.showwarning("提示", "请先选择一个翻译JSON文件。")
            return
        jp_name = self.jp_name_var.get().strip()
        if not jp_name:
            messagebox.showwarning("提示", "请输入日文说话人名。")
            return

        self.variants = find_speaker_variants(self.data, jp_name, self.fmt)
        if not self.variants:
            self._clear_results()
            self.status_var.set(f"未找到说话人「{jp_name}」的任何条目。")
            return

        total = sum(v.count for v in self.variants.values())
        self.status_var.set(
            f"找到「{jp_name}」共 {total} 条，{len(self.variants)} 种中文译名变体。"
        )
        self._populate_results()

    def _populate_results(self) -> None:
        self._clear_results()
        for variant in sorted(self.variants.values(), key=lambda v: -v.count):
            display_name = variant.chinese_name if variant.chinese_name else "(空)"
            entry0 = variant.entries[0]
            sep_display = "\\n" if entry0.separator == "\n" else entry0.separator
            example_text = entry0.translated_speaker + sep_display + entry0.rest_of_text
            example_text = example_text.replace("\n", "\\n")
            if len(example_text) > 60:
                example_text = example_text[:60] + "..."
            self.result_tree.insert(
                "", tk.END,
                values=("☑", display_name, variant.count, example_text),
            )

    def _clear_results(self) -> None:
        for item in self.result_tree.get_children():
            self.result_tree.delete(item)

    # ---- Checkbox toggling ----

    def _toggle_check(self, event: tk.Event) -> None:
        region = self.result_tree.identify_region(event.x, event.y)
        if region != "cell":
            return
        col = self.result_tree.identify_column(event.x)
        if col != "#1":
            return
        item_id = self.result_tree.identify_row(event.y)
        if not item_id:
            return
        current = self.result_tree.set(item_id, "selected")
        self.result_tree.set(item_id, "selected", "☐" if current == "☑" else "☑")

    def _fill_target_from_row(self, event: tk.Event) -> None:
        item_id = self.result_tree.identify_row(event.y)
        if not item_id:
            return
        display_name = self.result_tree.set(item_id, "chinese_name")
        if display_name and display_name != "(空)":
            self.target_name_var.set(display_name)

    def _select_all(self) -> None:
        for item in self.result_tree.get_children():
            self.result_tree.set(item, "selected", "☑")

    def _deselect_all(self) -> None:
        for item in self.result_tree.get_children():
            self.result_tree.set(item, "selected", "☐")

    # ---- Replacement ----

    def _on_replace(self) -> None:
        if not self.data or not self.file_path:
            messagebox.showwarning("提示", "请先选择文件并搜索说话人。")
            return
        target = self.target_name_var.get().strip()
        if not target:
            messagebox.showwarning("提示", "请输入目标中文名。")
            return

        selected: List[SpeakerVariant] = []
        for item_id in self.result_tree.get_children():
            if self.result_tree.set(item_id, "selected") != "☑":
                continue
            display_name = self.result_tree.set(item_id, "chinese_name")
            cn_name = "" if display_name == "(空)" else display_name
            if cn_name in self.variants:
                selected.append(self.variants[cn_name])

        if not selected:
            messagebox.showwarning("提示", "请至少选择一个要替换的译名变体。")
            return

        total_selected = sum(v.count for v in selected)
        if not messagebox.askyesno(
            "确认替换",
            f"将把 {len(selected)} 种译名变体（共 {total_selected} 条）\n"
            f"全部替换为「{target}」。\n\n确定执行？",
        ):
            return

        changed = apply_replacements(self.data, self.fmt, selected, target)
        try:
            self.file_path.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=4), encoding="utf-8"
            )
            self.status_var.set(f"替换完成！已修改 {changed} 条，文件已保存。")
            messagebox.showinfo("完成", f"成功替换 {changed} 条记录。")
            # Refresh search results
            self._on_search()
        except Exception as e:
            messagebox.showerror("保存失败", f"保存文件时出错:\n{e}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    root = tk.Tk()
    style = ttk.Style(root)
    for theme in ("breeze", "clam", "vista", "xpnative"):
        if theme in style.theme_names():
            try:
                style.theme_use(theme)
                break
            except tk.TclError:
                continue
    SpeakerReplacerApp(root)
    root.mainloop()
