from __future__ import annotations

import json
import queue
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, simpledialog, ttk
from types import SimpleNamespace

from workbench_app_v3 import (
    ConflictDialog,
    DICT_CATEGORIES,
    GROUP_LABELS,
    SOURCE_LABELS,
    WorkbenchApp as LegacyWorkbenchApp,
)
from workbench_core import (
    ALL_QUOTES,
    APP_NAME,
    CONFIDENCE_RANK,
    QUOTE_PAIRS,
    DataRecord,
    DatabaseProposal,
    DuplicateTextGroup,
    EditableDictionaryRow,
    EllipsisOccurrence,
    PunctuationIssue,
    QAError,
    QuoteAnalysisRow,
    SearchOptions,
    SearchResult,
    SpeakerGroup,
    TextChange,
    WidthIssue,
    analyze_alnum_occurrences,
    analyze_dictionary,
    analyze_duplicate_texts,
    analyze_ellipsis_occurrences,
    analyze_punctuation,
    analyze_quote_rows,
    analyze_speaker_groups,
    analyze_symbol_catalog,
    analyze_width,
    apply_translation_updates,
    build_alnum_changes,
    build_ellipsis_conversion,
    build_quote_situation_proposal,
    build_quote_style_proposal,
    build_scope_items,
    build_symbol_changes,
    compare_database,
    dictionary_conflicts,
    dictionary_entries_from_rows,
    display_text,
    duplicate_groups_to_dictionary,
    filter_records,
    fullwidth_units,
    is_face_message,
    is_narration_message,
    load_config,
    load_editable_dictionary,
    load_excel_records,
    load_json_records,
    load_symbol_catalog,
    load_txt_records,
    normalize_newlines,
    open_file,
    quote_styles,
    records_context,
    save_config,
    save_dictionary,
    search_records,
    strip_control_codes,
    transform_outside_controls,
    wrap_text_by_units,
)

APP_TITLE = f"{APP_NAME} v4.0"
CONFIG_NAME = "rpg_maker_proofreading_tool_config.json"
QUOTE_STYLE_LABELS = list(QUOTE_PAIRS.keys())

PAGE_LABELS = {
    "search": "文本查询", "width": "检查文本宽度", "editor": "单独文本处理", "speaker": "筛选说话人",
    "punctuation": "标点符号处理", "alnum": "英语数字格式", "duplicates": "重复文本检查",
    "dictionary": "生成辞典", "dictcheck": "匹配辞典检查", "database": "数据库直接翻译",
}
PAGE_KEYS_BY_LABEL = {v: k for k, v in PAGE_LABELS.items()}


class RPGMakerProofreadingApp(LegacyWorkbenchApp):
    """v4 UI. The proven v3 data loaders/writers remain the persistence layer."""

    def __init__(self, initial_input: str | None = None, initial_origin_dir: str | None = None,
                 initial_translated_dir: str | None = None):
        tk.Tk.__init__(self)
        self.title(APP_TITLE)
        self.geometry("1660x1000")
        self.minsize(1260, 800)
        self.config_path = self._app_dir() / CONFIG_NAME
        self.catalog_path = self._app_dir() / "标点符号分类.json"

        self.all_records: list[DataRecord] = []
        self.record_by_uid: dict[str, DataRecord] = {}
        self.selected_file_keys: set[str] = set()
        self.workset: set[str] = set()
        self.scope_items = []
        self.current_record: DataRecord | None = None
        self.text_widgets: list[tk.Text] = []
        self.result_maps: dict[str, dict[str, object]] = {}
        self.check_sets: dict[str, set[str]] = {}
        self.dictionary_rows: list[EditableDictionaryRow] = []
        self.speaker_results: list[SpeakerGroup] = []
        self.duplicate_results: list[DuplicateTextGroup] = []
        self.worker_queue: queue.Queue = queue.Queue()
        self.busy = False

        self.source_type = tk.StringVar(value="txt")
        self.json_path = tk.StringVar()
        self.origin_dir = tk.StringVar()
        self.translated_dir = tk.StringVar()
        self.excel_dir = tk.StringVar()
        self.origin_encoding = tk.StringVar(value="936")
        self.translated_encoding = tk.StringVar(value="936")
        self.display_font = tk.StringVar(value="系统默认")
        self.text_font_size = tk.IntVar(value=11)
        self.list_font = tk.StringVar(value="系统默认")
        self.list_font_size = tk.IntVar(value=10)
        self.display_line_mode = tk.StringVar(value="集中一行")
        self.status_var = tk.StringVar(value="请先在第一页检查数据源。")
        self.scope_summary = tk.StringVar(value="尚未检查")
        self.column_visibility: dict[str, dict[str, bool]] = {}
        self.tree_registry: dict[ttk.Treeview, dict[str, object]] = {}
        self.page_trees: dict[str, list[ttk.Treeview]] = {}
        self._current_tree_page = ""
        self._sort_reverse: dict[tuple[int, str], bool] = {}
        self.editor_controls_hidden = False
        self.editor_last_side = "translated"
        self.editor_raw_original = ""
        self.editor_raw_translation = ""

        self._build_ui()
        self._load_settings()
        self._initial_input = initial_input
        self._initial_origin_dir = initial_origin_dir
        self._initial_translated_dir = initial_translated_dir
        self.after(100, self._poll_worker)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _apply_initial_input(self):
        initial_input = self._initial_input
        initial_origin_dir = self._initial_origin_dir
        initial_translated_dir = self._initial_translated_dir
        if initial_input:
            self.json_path.set(str(Path(initial_input).resolve()))
            self.source_type.set("json")
        if initial_origin_dir or initial_translated_dir:
            if initial_origin_dir:
                self.origin_dir.set(str(Path(initial_origin_dir).resolve()))
            if initial_translated_dir:
                self.translated_dir.set(str(Path(initial_translated_dir).resolve()))
            self.source_type.set("txt")
        if initial_input or initial_origin_dir or initial_translated_dir:
            self._update_source_visibility()
            self.after_idle(self._scan_source)

    @staticmethod
    def _app_dir() -> Path:
        return Path(__file__).resolve().parent

    # ------------------------------------------------------------------
    # General UI, tables, display configuration
    # ------------------------------------------------------------------
    def _build_ui(self):
        self.style = ttk.Style(self)
        self.style.configure("Treeview", rowheight=26)
        outer = ttk.Frame(self, padding=8)
        outer.pack(fill="both", expand=True)
        outer.rowconfigure(0, weight=1); outer.columnconfigure(0, weight=1)
        self.notebook = ttk.Notebook(outer)
        self.notebook.grid(row=0, column=0, sticky="nsew")
        self.tabs: dict[str, ttk.Frame] = {}
        tab_specs = [
            ("settings", "1. 格式设置"),
            ("search", "2. 文本查询"),
            ("width", "3. 检查文本宽度"),
            ("editor", "4. 单独文本处理"),
            ("speaker", "5. 筛选说话人"),
            ("punctuation", "6. 标点符号处理"),
            ("alnum", "7. 英语数字格式"),
            ("duplicates", "8. 重复文本检查"),
            ("dictionary", "9. 生成辞典"),
            ("dictcheck", "10. 匹配辞典检查"),
            ("database", "11. 数据库直接翻译"),
        ]
        for key, title in tab_specs:
            frame = ttk.Frame(self.notebook, padding=8)
            self.notebook.add(frame, text=title)
            self.tabs[key] = frame

        builders = [
            ("settings", self._build_settings),
            ("search", self._build_search),
            ("width", self._build_width),
            ("editor", self._build_editor),
            ("speaker", self._build_speaker),
            ("punctuation", self._build_punctuation_workspace),
            ("alnum", self._build_alnum_workspace),
            ("duplicates", self._build_duplicates),
            ("dictionary", self._build_dictionary),
            ("dictcheck", self._build_dictcheck),
            ("database", self._build_database),
        ]
        for key, builder in builders:
            self._current_tree_page = key
            builder()
        self._current_tree_page = ""
        self._refresh_column_page_choices()

        status = ttk.Frame(outer)
        status.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        ttk.Label(status, textvariable=self.status_var).pack(side="left")
        self.progress = ttk.Progressbar(status, mode="indeterminate", length=220)
        self.progress.pack(side="right")

    def _tree(self, parent, columns, headings, widths, checkbox=False):
        frame = ttk.Frame(parent)
        tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="extended")
        page = self._current_tree_page or "其他"
        for col, title in zip(columns, headings):
            tree.heading(col, text=title, command=lambda c=col, t=tree: self._sort_tree(t, c))
        for col, width in zip(columns, widths):
            tree.column(col, width=width, minwidth=0, anchor="w", stretch=True)
        y = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        x = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=y.set, xscrollcommand=x.set)
        tree.grid(row=0, column=0, sticky="nsew")
        y.grid(row=0, column=1, sticky="ns")
        x.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1); frame.columnconfigure(0, weight=1)
        if checkbox:
            tree.bind("<Button-1>", lambda e: self._toggle_tree_checkbox(tree, e))
        tree.bind("<Button-3>", lambda e, t=tree: self._tree_context_menu(t, e), add=True)
        tree.bind("<Control-c>", lambda e, t=tree: self._copy_tree_selection(t), add=True)
        meta = {
            "page": page,
            "columns": tuple(columns),
            "headings": dict(zip(columns, headings)),
            "widths": dict(zip(columns, widths)),
            "detached": [],
        }
        self.tree_registry[tree] = meta
        self.page_trees.setdefault(page, []).append(tree)
        return tree

    def _clear_tree(self, tree):
        meta = self.tree_registry.get(tree, {})
        all_ids = list(tree.get_children("")) + list(meta.get("detached", []))
        for iid in dict.fromkeys(all_ids):
            try: tree.delete(iid)
            except Exception: pass
        if meta is not None: meta["detached"] = []

    def _sort_tree(self, tree: ttk.Treeview, col: str):
        rows = [(tree.set(i, col), i) for i in tree.get_children("")]
        key = (id(tree), col); reverse = self._sort_reverse.get(key, False)
        def natural(value):
            try: return (0, float(str(value).replace("☑", "1").replace("☐", "0")))
            except Exception: return (1, str(value).casefold())
        rows.sort(key=lambda x: natural(x[0]), reverse=reverse)
        for pos, (_v, iid) in enumerate(rows): tree.move(iid, "", pos)
        self._sort_reverse[key] = not reverse

    def _tree_context_menu(self, tree: ttk.Treeview, event):
        region = tree.identify_region(event.x, event.y)
        col_id = tree.identify_column(event.x)
        columns = tree["columns"]
        idx = int(col_id[1:]) - 1 if col_id.startswith("#") else -1
        col = columns[idx] if 0 <= idx < len(columns) else None
        menu = tk.Menu(self, tearoff=False)
        if region == "heading" and col:
            visible = tree.column(col, "width") > 0
            menu.add_command(label=("隐藏列：" if visible else "显示列：") + str(tree.heading(col, "text")),
                             command=lambda: self._set_column_visible(tree, col, not visible))
            menu.add_separator()
            menu.add_command(label="显示全部列", command=lambda: self._set_all_columns(tree, True))
            menu.add_command(label="恢复本页默认列", command=lambda: self._restore_page_columns(self.tree_registry[tree]["page"]))
            menu.add_separator()
            menu.add_command(label="筛选当前列…", command=lambda: self._filter_tree_column(tree, col))
            menu.add_command(label="清除表格筛选", command=lambda: self._clear_tree_filter(tree))
        else:
            iid = tree.identify_row(event.y)
            if iid:
                tree.selection_set(iid)
                if col:
                    menu.add_command(label="复制当前单元格", command=lambda: self._copy_tree_cell(tree, iid, col))
                menu.add_command(label="复制选中行", command=lambda: self._copy_tree_selection(tree))
        if menu.index("end") is not None:
            menu.tk_popup(event.x_root, event.y_root)

    def _copy_tree_cell(self, tree, iid, col):
        self.clipboard_clear(); self.clipboard_append(tree.set(iid, col))

    def _copy_tree_selection(self, tree):
        rows = []
        for iid in tree.selection():
            rows.append("\t".join(str(x) for x in tree.item(iid, "values")))
        if rows:
            self.clipboard_clear(); self.clipboard_append("\n".join(rows))
        return "break"

    def _filter_tree_column(self, tree, col):
        value = simpledialog.askstring("筛选当前列", "只显示包含以下文字的行；留空表示清除筛选：", parent=self)
        if value is None: return
        self._clear_tree_filter(tree)
        if not value: return
        meta = self.tree_registry[tree]
        detached = []
        for iid in list(tree.get_children("")):
            if value.casefold() not in str(tree.set(iid, col)).casefold():
                tree.detach(iid); detached.append(iid)
        meta["detached"] = detached

    def _clear_tree_filter(self, tree):
        meta = self.tree_registry.get(tree, {})
        for iid in meta.get("detached", []):
            try: tree.reattach(iid, "", "end")
            except Exception: pass
        meta["detached"] = []

    def _set_column_visible(self, tree, col, visible: bool):
        meta = self.tree_registry[tree]
        page = str(meta["page"])
        self.column_visibility.setdefault(page, {})[str(col)] = bool(visible)
        width = int(meta["widths"].get(col, 120)) if visible else 0
        tree.column(col, width=width, minwidth=20 if visible else 0, stretch=visible)
        self._refresh_column_config_tree()

    def _set_all_columns(self, tree, visible: bool):
        for col in tree["columns"]: self._set_column_visible(tree, col, visible)

    def _restore_page_columns(self, page: str):
        self.column_visibility.pop(page, None)
        for tree in self.page_trees.get(page, []):
            meta = self.tree_registry[tree]
            for col in tree["columns"]:
                tree.column(col, width=int(meta["widths"].get(col, 120)), minwidth=20, stretch=True)
        self._refresh_column_config_tree()

    def _apply_saved_columns(self):
        for tree, meta in self.tree_registry.items():
            page = str(meta["page"])
            settings = self.column_visibility.get(page, {})
            for col in tree["columns"]:
                if col in settings:
                    self._set_column_visible(tree, col, bool(settings[col]))

    def _apply_font(self):
        text_family = self.display_font.get()
        if text_family == "系统默认": text_family = tkfont.nametofont("TkTextFont").actual("family")
        list_family = self.list_font.get()
        if list_family == "系统默认": list_family = tkfont.nametofont("TkDefaultFont").actual("family")
        text_size = max(6, int(self.text_font_size.get() or 11))
        list_size = max(6, int(self.list_font_size.get() or 10))
        for widget in self.text_widgets:
            try: widget.configure(font=(text_family, text_size))
            except Exception: pass
        self.style.configure("Treeview", font=(list_family, list_size), rowheight=(64 if self.display_line_mode.get() == "显示分行" else max(25, list_size + 14)))
        self.style.configure("Treeview.Heading", font=(list_family, list_size, "bold"))
        self._draw_width_preview()

    def _toggle_line_mode(self):
        multiline = self.display_line_mode.get() == "显示分行"
        for tree in self.tree_registry:
            for iid in tree.get_children(""):
                vals = list(tree.item(iid, "values"))
                vals = [str(v).replace(" ↵ ", "\n") if multiline else str(v).replace("\n", " ↵ ") for v in vals]
                tree.item(iid, values=vals)
        self._apply_font()

    def _display(self, text: str, limit: int = 420) -> str:
        return display_text(text, self.display_line_mode.get() == "显示分行", limit)

    def _compact(self, text, limit=180):
        return self._display(text, limit)

    # ------------------------------------------------------------------
    # Settings page
    # ------------------------------------------------------------------
    def _build_settings(self):
        tab = self.tabs["settings"]
        tab.columnconfigure(0, weight=1); tab.rowconfigure(2, weight=1)
        src = ttk.LabelFrame(tab, text="数据源、编码与显示格式", padding=8)
        src.grid(row=0, column=0, sticky="ew"); src.columnconfigure(1, weight=1)
        ttk.Label(src, text="提取格式：").grid(row=0, column=0, sticky="w")
        for i, (label, key) in enumerate(SOURCE_LABELS.items()):
            ttk.Radiobutton(src, text=label, value=key, variable=self.source_type, command=self._update_source_visibility).grid(row=0, column=i+1, sticky="w", padx=6)
        self.path_rows = {}
        self.path_rows["json"] = self._path_row(src, 1, "JSON 文件", self.json_path, lambda: self._choose_file(self.json_path, [("JSON", "*.json")]))
        self.path_rows["origin"] = self._path_row(src, 2, "原文 TXT 文件夹", self.origin_dir, lambda: self._choose_dir(self.origin_dir))
        self.path_rows["translated"] = self._path_row(src, 3, "译文 TXT 文件夹", self.translated_dir, lambda: self._choose_dir(self.translated_dir))
        self.path_rows["excel"] = self._path_row(src, 4, "Excel 文件夹", self.excel_dir, lambda: self._choose_dir(self.excel_dir))
        ttk.Label(src, text="原文读取编码：").grid(row=5, column=0, sticky="w", pady=3)
        self.oenc_combo = ttk.Combobox(src, textvariable=self.origin_encoding, state="readonly", values=["936", "932", "UTF-8", "自动"], width=12)
        self.oenc_combo.grid(row=5, column=1, sticky="w")
        ttk.Label(src, text="译文读取编码：").grid(row=5, column=2, sticky="e", padx=(10, 4))
        self.tenc_combo = ttk.Combobox(src, textvariable=self.translated_encoding, state="readonly", values=["936", "932", "UTF-8", "自动"], width=12)
        self.tenc_combo.grid(row=5, column=3, sticky="w")

        fonts = ["系统默认"] + sorted(set(tkfont.families()))
        ttk.Label(src, text="文本框字体/字号：").grid(row=6, column=0, sticky="w")
        ttk.Combobox(src, textvariable=self.display_font, values=fonts, state="readonly", width=27).grid(row=6, column=1, sticky="w")
        ttk.Spinbox(src, from_=6, to=72, textvariable=self.text_font_size, width=6, command=self._apply_font).grid(row=6, column=2, sticky="w")
        ttk.Label(src, text="列表字体/字号：").grid(row=7, column=0, sticky="w")
        ttk.Combobox(src, textvariable=self.list_font, values=fonts, state="readonly", width=27).grid(row=7, column=1, sticky="w")
        ttk.Spinbox(src, from_=6, to=40, textvariable=self.list_font_size, width=6, command=self._apply_font).grid(row=7, column=2, sticky="w")
        ttk.Combobox(src, textvariable=self.display_line_mode, state="readonly", values=["集中一行", "显示分行"], width=12).grid(row=7, column=3, sticky="w")
        ttk.Button(src, text="应用显示设置", command=lambda: (self._apply_font(), self._toggle_line_mode())).grid(row=6, column=3, sticky="e")
        ttk.Button(src, text="检查文件/文件夹", command=self._scan_source).grid(row=8, column=3, sticky="e", pady=(4, 0))

        quick = ttk.Frame(tab); quick.grid(row=1, column=0, sticky="ew", pady=6)
        ttk.Label(quick, text="文本范围：").pack(side="left")
        for text, cmd in [
            ("选择全文本", lambda: self._scope_quick("all")), ("选择全地图", lambda: self._scope_quick("map")),
            ("选择数据库", lambda: self._scope_quick("database")), ("选择数据库 Common Event", lambda: self._scope_quick("common_event")),
            ("清空选择", lambda: self._scope_quick("none")),
        ]:
            ttk.Button(quick, text=text, command=cmd).pack(side="left", padx=3)
        ttk.Label(quick, textvariable=self.scope_summary).pack(side="right")

        pane = ttk.PanedWindow(tab, orient="horizontal")
        pane.grid(row=2, column=0, sticky="nsew")
        scope_box = ttk.LabelFrame(pane, text="文件与范围", padding=4)
        column_box = ttk.LabelFrame(pane, text="各页面表格列显示设置", padding=4)
        pane.add(scope_box, weight=3); pane.add(column_box, weight=2)
        scope_box.rowconfigure(0, weight=1); scope_box.columnconfigure(0, weight=1)
        self.scope_tree = self._tree(scope_box, ("use", "group", "file", "count"), ("选择", "类别", "文件/逻辑文件", "条目数"), (65, 180, 780, 100), checkbox=True)
        self.scope_tree.master.grid(row=0, column=0, sticky="nsew")
        self.scope_tree.bind("<ButtonRelease-1>", lambda e: self.after(20, self._sync_scope_selection), add=True)

        column_box.columnconfigure(0, weight=1); column_box.rowconfigure(2, weight=1)
        self.column_page = tk.StringVar()
        self.column_page_combo = ttk.Combobox(column_box, textvariable=self.column_page, state="readonly")
        self.column_page_combo.grid(row=0, column=0, sticky="ew")
        self.column_page_combo.bind("<<ComboboxSelected>>", lambda e: self._refresh_column_config_tree())
        btns = ttk.Frame(column_box); btns.grid(row=1, column=0, sticky="ew", pady=4)
        ttk.Button(btns, text="显示本页全部列", command=lambda: self._column_page_all(True)).pack(side="left")
        ttk.Button(btns, text="隐藏本页全部列", command=lambda: self._column_page_all(False)).pack(side="left", padx=4)
        self.column_config_tree = ttk.Treeview(column_box, columns=("show", "column"), show="headings", height=12)
        self.column_config_tree.heading("show", text="显示"); self.column_config_tree.heading("column", text="列标题")
        self.column_config_tree.column("show", width=70); self.column_config_tree.column("column", width=260)
        self.column_config_tree.grid(row=2, column=0, sticky="nsew")
        self.column_config_tree.bind("<Button-1>", self._toggle_column_config)
        self._update_source_visibility()

    def _refresh_column_page_choices(self):
        pages = [p for p in self.page_trees if p != "settings"]
        labels = [PAGE_LABELS.get(p, p) for p in pages]
        self.column_page_combo.configure(values=labels)
        if labels and self.column_page.get() not in labels: self.column_page.set(labels[0])
        self._refresh_column_config_tree()

    def _column_page_key(self):
        return PAGE_KEYS_BY_LABEL.get(self.column_page.get(), self.column_page.get())

    def _refresh_column_config_tree(self):
        if not hasattr(self, "column_config_tree"): return
        self._clear_tree(self.column_config_tree)
        page = self._column_page_key()
        trees = self.page_trees.get(page, [])
        seen = set()
        for tree in trees:
            meta = self.tree_registry[tree]
            for col in tree["columns"]:
                if col in seen: continue
                seen.add(col)
                visible = any(t.column(col, "width") > 0 for t in trees if col in t["columns"])
                title = meta["headings"].get(col, col)
                self.column_config_tree.insert("", "end", iid=str(col), values=("☑" if visible else "☐", title))

    def _toggle_column_config(self, event):
        if self.column_config_tree.identify_column(event.x) != "#1": return
        iid = self.column_config_tree.identify_row(event.y)
        if not iid: return
        page = self._column_page_key(); current = self.column_config_tree.set(iid, "show") == "☑"
        for tree in self.page_trees.get(page, []):
            if iid in tree["columns"]: self._set_column_visible(tree, iid, not current)
        return "break"

    def _column_page_all(self, visible: bool):
        for tree in self.page_trees.get(self._column_page_key(), []): self._set_all_columns(tree, visible)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------
    def _build_search(self):
        tab = self.tabs["search"]
        tab.rowconfigure(4, weight=1); tab.columnconfigure(0, weight=1)
        modebar = ttk.Frame(tab); modebar.grid(row=0, column=0, sticky="ew")
        ttk.Label(modebar, text="搜索输入：").pack(side="left")
        self.search_input_mode = tk.StringVar(value="单行模式")
        ttk.Combobox(modebar, textvariable=self.search_input_mode, state="readonly", values=["单行模式", "大文本框模式"], width=14).pack(side="left")
        ttk.Button(modebar, text="切换输入框", command=self._toggle_search_input).pack(side="left", padx=4)
        self.search_entry_var = tk.StringVar()
        self.search_entry = ttk.Entry(modebar, textvariable=self.search_entry_var, width=58)
        self.search_entry.pack(side="left", fill="x", expand=True, padx=4)
        self.search_big_frame = ttk.Frame(tab)
        self.search_big_text = self._text(self.search_big_frame, 5, False)

        opts = ttk.Frame(tab); opts.grid(row=2, column=0, sticky="ew", pady=5)
        self.search_field = tk.StringVar(value="both")
        ttk.Combobox(opts, textvariable=self.search_field, state="readonly", values=["original", "translated", "both"], width=12).pack(side="left")
        self.search_mode = tk.StringVar(value="keyword")
        ttk.Combobox(opts, textvariable=self.search_mode, state="readonly", values=["keyword", "exact"], width=10).pack(side="left", padx=4)
        self.search_case = tk.BooleanVar(value=False); self.search_width = tk.BooleanVar(value=False)
        self.search_ignore_controls = tk.BooleanVar(value=True); self.search_strip_symbols = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="区分大小写", variable=self.search_case).pack(side="left", padx=5)
        ttk.Checkbutton(opts, text="区分全半角", variable=self.search_width).pack(side="left")
        ttk.Checkbutton(opts, text="查询时忽略 RPG Maker 通配符/操作符", variable=self.search_ignore_controls).pack(side="left", padx=5)
        ttk.Checkbutton(opts, text="忽略普通标点和特殊符号", variable=self.search_strip_symbols).pack(side="left")
        ttk.Button(opts, text="开始查询", command=self._do_search).pack(side="left", padx=8)

        act = ttk.Frame(tab); act.grid(row=3, column=0, sticky="ew", pady=4)
        ttk.Button(act, text="全选结果", command=lambda: self._check_all(self.search_tree, True)).pack(side="left")
        ttk.Button(act, text="全部取消", command=lambda: self._check_all(self.search_tree, False)).pack(side="left", padx=4)
        ttk.Button(act, text="将勾选结果送入后续处理", command=self._search_to_workset).pack(side="left")
        ttk.Button(act, text="清除查询处理集合", command=self._clear_workset).pack(side="left", padx=4)
        ttk.Button(act, text="打开原文文件", command=lambda: self._open_selected_result("search", "original")).pack(side="right")
        ttk.Button(act, text="打开译文文件", command=lambda: self._open_selected_result("search", "translated")).pack(side="right", padx=4)
        self.search_tree = self._tree(tab, ("use", "field", "file", "marker", "original", "translated"),
                                      ("选择", "命中", "文件", "标记", "原文", "译文"), (60, 90, 240, 90, 560, 560), checkbox=True)
        self.search_tree.master.grid(row=4, column=0, sticky="nsew")
        self.search_tree.bind("<Double-1>", lambda e: self._double_to_editor("search"))

    def _toggle_search_input(self):
        if self.search_input_mode.get() == "大文本框模式":
            self.search_entry.pack_forget(); self.search_big_frame.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        else:
            self.search_big_frame.grid_remove(); self.search_entry.pack(side="left", fill="x", expand=True, padx=4)

    def _search_query_value(self) -> str:
        return self.search_big_text.get("1.0", "end-1c") if self.search_input_mode.get() == "大文本框模式" else self.search_entry_var.get()

    def _set_search_query(self, text: str, field: str = "both", exact: bool = True):
        self.search_input_mode.set("大文本框模式"); self._toggle_search_input()
        self.search_big_text.delete("1.0", "end"); self.search_big_text.insert("1.0", text)
        self.search_field.set(field); self.search_mode.set("exact" if exact else "keyword")
        self.notebook.select(self.tabs["search"])
        self._do_search()

    def _do_search(self):
        try:
            opts = SearchOptions(self.search_field.get(), self.search_mode.get(), self.search_case.get(), self.search_width.get(),
                                 self.search_strip_symbols.get(), self.search_ignore_controls.get())
            results = search_records(self.active_records(), self._search_query_value(), opts)
            self.result_maps["search"] = {}; self._clear_tree(self.search_tree)
            for i, result in enumerate(results):
                iid = f"s{i}"; self.result_maps["search"][iid] = result.record
                self.search_tree.insert("", "end", iid=iid, values=("☐", result.matched_field, result.record.file_key, result.record.marker,
                                                                         self._display(result.record.original), self._display(result.record.translated)))
            self.status_var.set(f"查询命中 {len(results)} 条。")
        except Exception as exc: messagebox.showerror("查询失败", str(exc), parent=self)

    # ------------------------------------------------------------------
    # Width analysis and visual preview
    # ------------------------------------------------------------------
    def _build_width(self):
        tab = self.tabs["width"]
        tab.rowconfigure(1, weight=3); tab.rowconfigure(2, weight=2); tab.columnconfigure(0, weight=1)
        bar = ttk.Frame(tab); bar.grid(row=0, column=0, sticky="ew", pady=(0, 5))
        self.face_limit = tk.DoubleVar(value=19); self.narr_limit = tk.DoubleVar(value=25)
        self.check_face = tk.BooleanVar(value=True); self.check_narr = tk.BooleanVar(value=True); self.check_font_pixels = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="有头像", variable=self.check_face).pack(side="left")
        ttk.Spinbox(bar, from_=1, to=100, increment=.5, textvariable=self.face_limit, width=6).pack(side="left")
        ttk.Checkbutton(bar, text="无头像/NARRATION", variable=self.check_narr).pack(side="left", padx=(8, 0))
        ttk.Spinbox(bar, from_=1, to=100, increment=.5, textvariable=self.narr_limit, width=6).pack(side="left")
        ttk.Checkbutton(bar, text="同时按当前字体像素宽度警告", variable=self.check_font_pixels).pack(side="left", padx=8)
        ttk.Button(bar, text="检查宽度", command=self._do_width).pack(side="left")
        ttk.Button(bar, text="自动预览超字数分行", command=self._width_auto_wrap).pack(side="left", padx=5)
        ttk.Button(bar, text="打开文件", command=lambda: self._open_selected_result("width", "translated")).pack(side="right")
        self.width_font_label = tk.StringVar(value="当前文本框字体")
        ttk.Label(bar, textvariable=self.width_font_label).pack(side="right", padx=8)
        self.width_tree = self._tree(tab, ("type", "file", "line", "units", "pixels", "limit", "reason", "original", "translated"),
                                    ("类型", "文件", "行", "全角单位", "像素", "参考上限", "警告原因", "原文", "译文"),
                                    (100, 220, 55, 80, 80, 90, 180, 430, 430))
        self.width_tree.master.grid(row=1, column=0, sticky="nsew")
        self.width_tree.bind("<Double-1>", lambda e: self._double_to_editor("width"))
        self.width_tree.bind("<<TreeviewSelect>>", lambda e: self._draw_width_preview())
        preview = ttk.LabelFrame(tab, text="当前字体预览：蓝线=有头像上限，红线=无头像上限", padding=4)
        preview.grid(row=2, column=0, sticky="nsew", pady=(5, 0)); preview.rowconfigure(0, weight=1); preview.columnconfigure(0, weight=1)
        self.width_canvas = tk.Canvas(preview, background="white", height=250)
        self.width_canvas.grid(row=0, column=0, sticky="nsew")

    def _font_object(self):
        family = self.display_font.get()
        if family == "系统默认": family = tkfont.nametofont("TkTextFont").actual("family")
        return tkfont.Font(family=family, size=max(6, int(self.text_font_size.get())))

    def _do_width(self):
        try:
            records = self.active_records()
            unit_issues = analyze_width(records, self.face_limit.get(), self.narr_limit.get(), self.check_face.get(), self.check_narr.get())
            found: dict[tuple[str, int], SimpleNamespace] = {}
            for x in unit_issues:
                found[(x.record.uid, x.line_no)] = SimpleNamespace(record=x.record, line_no=x.line_no, face_type=x.face_type,
                                                                   units=x.width, limit=x.limit, pixels="", reason="全角单位超线")
            font = self._font_object(); ref_char = "汉"
            if self.check_font_pixels.get():
                for r in records:
                    if r.marker != "Message": continue
                    if is_face_message(r):
                        if not self.check_face.get(): continue
                        limit, typ = self.face_limit.get(), "有头像"
                    elif is_narration_message(r):
                        if not self.check_narr.get(): continue
                        limit, typ = self.narr_limit.get(), "无头像"
                    else: continue
                    ref_pixels = font.measure(ref_char * max(1, int(limit))) + font.measure(ref_char) * (limit % 1)
                    for n, line in enumerate(normalize_newlines(r.translated).split("\n"), 1):
                        vis = strip_control_codes(line); pixels = font.measure(vis)
                        if pixels > ref_pixels:
                            key = (r.uid, n)
                            if key in found:
                                found[key].pixels = f"{pixels:.0f}"; found[key].reason = "全角单位＋当前字体像素超线"
                            else:
                                found[key] = SimpleNamespace(record=r, line_no=n, face_type=typ, units=fullwidth_units(vis), limit=limit,
                                                             pixels=f"{pixels:.0f}", reason="当前字体像素超线")
            self.result_maps["width"] = {}; self._clear_tree(self.width_tree)
            for i, x in enumerate(found.values()):
                iid = f"w{i}"; self.result_maps["width"][iid] = x.record
                self.width_tree.insert("", "end", iid=iid, values=(x.face_type, x.record.file_key, x.line_no, f"{x.units:g}", x.pixels,
                                                                       f"{x.limit:g}", x.reason, self._display(x.record.original), self._display(x.record.translated)))
            self.width_font_label.set(f"当前字体：{font.actual('family')} {font.actual('size')} pt")
            self.status_var.set(f"发现 {len(found)} 条宽度警告。")
            self._draw_width_preview()
        except Exception as exc: messagebox.showerror("分析失败", str(exc), parent=self)

    def _draw_width_preview(self):
        if not hasattr(self, "width_canvas"): return
        canvas = self.width_canvas; canvas.delete("all")
        font = self._font_object(); family = font.actual("family"); size = font.actual("size")
        width = max(400, canvas.winfo_width()); canvas.configure(scrollregion=(0, 0, width * 2, 500))
        face_x = 20 + font.measure("汉" * max(1, int(self.face_limit.get())))
        narr_x = 20 + font.measure("汉" * max(1, int(self.narr_limit.get())))
        canvas.create_line(face_x, 10, face_x, 480, fill="#2b6cb0", width=2)
        canvas.create_line(narr_x, 10, narr_x, 480, fill="#c53030", width=2)
        canvas.create_text(20, 15, anchor="nw", text=f"当前字体：{family} {size} pt", font=(family, size))
        sel = self.width_tree.selection() if hasattr(self, "width_tree") else ()
        record = self.result_maps.get("width", {}).get(sel[0]) if sel else self.current_record
        if isinstance(record, DataRecord):
            y = 48
            canvas.create_text(20, y, anchor="nw", text="原文：", font=(family, size, "bold")); y += size + 14
            for line in normalize_newlines(record.original).split("\n"):
                canvas.create_text(20, y, anchor="nw", text=strip_control_codes(line), font=(family, size)); y += size + 12
            y += 10; canvas.create_text(20, y, anchor="nw", text="译文：", font=(family, size, "bold")); y += size + 14
            for line in normalize_newlines(record.translated).split("\n"):
                canvas.create_text(20, y, anchor="nw", text=strip_control_codes(line), font=(family, size)); y += size + 12

    def _width_auto_wrap(self):
        sel = self.width_tree.selection()
        record = self.result_maps.get("width", {}).get(sel[0]) if sel else self.current_record
        if not isinstance(record, DataRecord):
            messagebox.showinfo("自动分行预览", "请先选择一条宽度警告。", parent=self); return
        limit = self.face_limit.get() if is_face_message(record) else self.narr_limit.get()
        proposed = wrap_text_by_units(record.translated, limit)
        top = tk.Toplevel(self); top.title("自动分行预览（只预览，不写回）"); top.geometry("1050x650")
        pane = ttk.PanedWindow(top, orient="horizontal"); pane.pack(fill="both", expand=True, padx=8, pady=8)
        for title, value in [("当前译文", record.translated), (f"按 {limit:g} 全角单位分行", proposed)]:
            frame = ttk.LabelFrame(pane, text=title, padding=4); pane.add(frame, weight=1)
            txt = tk.Text(frame, wrap="none", font=self._font_object()); txt.pack(fill="both", expand=True); txt.insert("1.0", value); txt.configure(state="disabled")

    # ------------------------------------------------------------------
    # Editor
    # ------------------------------------------------------------------
    def _build_editor(self):
        tab = self.tabs["editor"]
        tab.columnconfigure(0, weight=1); tab.rowconfigure(2, weight=2); tab.rowconfigure(3, weight=3)
        nav = ttk.Frame(tab); nav.grid(row=0, column=0, sticky="ew")
        ttk.Label(nav, text="文件：").pack(side="left")
        self.editor_file = tk.StringVar(); self.editor_file_combo = ttk.Combobox(nav, textvariable=self.editor_file, state="readonly", width=55)
        self.editor_file_combo.pack(side="left")
        ttk.Label(nav, text="第几项：").pack(side="left", padx=(8, 2))
        self.editor_index = tk.IntVar(value=1); ttk.Spinbox(nav, from_=1, to=999999, textvariable=self.editor_index, width=8).pack(side="left")
        ttk.Button(nav, text="跳转", command=self._manual_editor_jump).pack(side="left", padx=5)
        ttk.Button(nav, text="打开原文文件", command=lambda: self.current_record and self._open_record_file(self.current_record, "original")).pack(side="right")
        ttk.Button(nav, text="打开译文文件", command=lambda: self.current_record and self._open_record_file(self.current_record, "translated")).pack(side="right", padx=4)
        self.editor_location = tk.StringVar(); ttk.Label(tab, textvariable=self.editor_location, foreground="#555").grid(row=1, column=0, sticky="ew", pady=4)

        ctx = ttk.LabelFrame(tab, text="上下文（双击切换；可显示较长范围）", padding=4)
        ctx.grid(row=2, column=0, sticky="nsew"); ctx.columnconfigure(0, weight=1); ctx.rowconfigure(0, weight=1)
        self.context_tree = self._tree(ctx, ("index", "marker", "original", "translated"), ("序号", "标记", "原文", "译文"), (70, 90, 670, 670))
        self.context_tree.master.grid(row=0, column=0, sticky="nsew"); self.context_tree.configure(height=10)
        self.context_tree.bind("<Double-1>", self._context_double)

        pane = ttk.PanedWindow(tab, orient="horizontal"); pane.grid(row=3, column=0, sticky="nsew", pady=5)
        of = ttk.LabelFrame(pane, text="原文", padding=3); tf = ttk.LabelFrame(pane, text="译文（可编辑）", padding=3)
        pane.add(of, weight=1); pane.add(tf, weight=1)
        self.editor_original = self._text(of, 11, True); self.editor_translation = self._text(tf, 11, False)
        self.editor_original.bind("<Button-1>", lambda e: setattr(self, "editor_last_side", "original"), add=True)
        self.editor_translation.bind("<Button-1>", lambda e: setattr(self, "editor_last_side", "translated"), add=True)
        bar = ttk.Frame(tab); bar.grid(row=4, column=0, sticky="ew")
        for label, mode in [("转全角", "全角"), ("转半角", "半角"), ("英文大写", "大写"), ("英文小写", "小写"), ("英文首字母大写", "首字母大写")]:
            ttk.Button(bar, text=label, command=lambda m=mode: self._editor_transform(m)).pack(side="left", padx=2)
        self.editor_control_button = ttk.Button(bar, text="隐藏 RPG Maker 通配符/操作符", command=self._toggle_editor_controls)
        self.editor_control_button.pack(side="left", padx=8)
        ttk.Button(bar, text="搜索划取内容", command=self._editor_search_selection).pack(side="left")
        ttk.Button(bar, text="保存译文", command=self._editor_save).pack(side="right")

    def _show_editor_record(self, record):
        self.current_record = record; self.editor_file.set(record.file_key)
        same = [r for r in self.all_records if r.file_key == record.file_key]
        idx = next((i for i, r in enumerate(same, 1) if r.uid == record.uid), 1); self.editor_index.set(idx)
        self.editor_location.set(f"原文：{record.original_location}    |    译文：{record.translated_location}    |    {record.marker} / {record.speaker_id}")
        self.editor_raw_original = record.original; self.editor_raw_translation = record.translated
        self.editor_controls_hidden = False; self.editor_control_button.configure(text="隐藏 RPG Maker 通配符/操作符")
        self._set_text(self.editor_original, record.original, True); self._set_text(self.editor_translation, record.translated, False)
        self._clear_tree(self.context_tree); self.result_maps["context"] = {}
        context = records_context(self.all_records, record, 8)
        for i, r in enumerate(context):
            iid = f"c{i}"; self.result_maps["context"][iid] = r
            seq = next((j for j, x in enumerate(same, 1) if x.uid == r.uid), 0)
            self.context_tree.insert("", "end", iid=iid, values=(seq, r.marker, self._display(r.original), self._display(r.translated)))

    def _editor_transform(self, mode):
        if self.editor_controls_hidden:
            messagebox.showinfo("不能编辑", "隐藏 RPG Maker 通配符/操作符时文本框为只读。请先显示操作符。", parent=self); return
        widget = self.editor_translation
        try:
            selected = widget.get("sel.first", "sel.last")
            replacement = transform_outside_controls(selected, mode)
            widget.delete("sel.first", "sel.last"); widget.insert("insert", replacement)
        except tk.TclError:
            value = widget.get("1.0", "end-1c")
            self._set_text(widget, transform_outside_controls(value, mode), False)

    def _toggle_editor_controls(self):
        if not self.current_record: return
        if not self.editor_controls_hidden:
            self.editor_raw_translation = self.editor_translation.get("1.0", "end-1c")
            self._set_text(self.editor_original, strip_control_codes(self.editor_raw_original), True)
            self._set_text(self.editor_translation, strip_control_codes(self.editor_raw_translation), True)
            self.editor_controls_hidden = True; self.editor_control_button.configure(text="显示 RPG Maker 通配符/操作符")
        else:
            self._set_text(self.editor_original, self.editor_raw_original, True)
            self._set_text(self.editor_translation, self.editor_raw_translation, False)
            self.editor_controls_hidden = False; self.editor_control_button.configure(text="隐藏 RPG Maker 通配符/操作符")

    def _editor_search_selection(self):
        widget = self.editor_original if self.editor_last_side == "original" else self.editor_translation
        try: value = widget.get("sel.first", "sel.last")
        except tk.TclError: value = widget.get("1.0", "end-1c")
        if not value.strip(): return
        self._set_search_query(value, "original" if self.editor_last_side == "original" else "translated", True)

    def _editor_save(self):
        if self.editor_controls_hidden:
            messagebox.showwarning("不能保存", "隐藏 RPG Maker 通配符/操作符时不能保存，请先显示操作符。", parent=self); return
        super()._editor_save()

    # ------------------------------------------------------------------
    # Speaker analysis: groups + examples
    # ------------------------------------------------------------------
    def _build_speaker(self):
        tab = self.tabs["speaker"]
        tab.rowconfigure(2, weight=3); tab.rowconfigure(3, weight=2); tab.columnconfigure(0, weight=1)
        bar = ttk.Frame(tab); bar.grid(row=0, column=0, sticky="ew")
        self.speaker_mode = tk.IntVar(value=0)
        labels = [("自动检查", 0), ("类型1", 1), ("类型2", 2), ("类型3", 3), ("类型4", 4)]
        for text, value in labels: ttk.Radiobutton(bar, text=text, value=value, variable=self.speaker_mode).pack(side="left", padx=3)
        self.speaker_full = tk.BooleanVar(value=False); self.speaker_first_n = tk.IntVar(value=100)
        ttk.Checkbutton(bar, text="全文检查", variable=self.speaker_full).pack(side="left", padx=(12, 2))
        ttk.Label(bar, text="否则前").pack(side="left"); ttk.Spinbox(bar, from_=1, to=999999, textvariable=self.speaker_first_n, width=8).pack(side="left")
        ttk.Label(bar, text="句").pack(side="left")
        ttk.Button(bar, text="开始筛选", command=self._do_speaker).pack(side="left", padx=8)
        ttk.Button(bar, text="所选说话人全部候选加入辞典", command=self._speaker_to_dictionary).pack(side="left")
        ttk.Button(bar, text="导出译名表", command=self._export_speakers).pack(side="right")

        filters = ttk.Frame(tab); filters.grid(row=1, column=0, sticky="ew", pady=4)
        ttk.Button(filters, text="全选", command=lambda: self._check_all(self.speaker_tree, True)).pack(side="left")
        ttk.Button(filters, text="全不选", command=lambda: self._check_all(self.speaker_tree, False)).pack(side="left", padx=3)
        ttk.Button(filters, text="全选只有一个译文", command=lambda: self._speaker_select_rule("single")).pack(side="left", padx=3)
        self.speaker_conf_filter = tk.StringVar(value="确定")
        ttk.Combobox(filters, textvariable=self.speaker_conf_filter, state="readonly", values=["确定", "较确定", "需检查", "手动规则"], width=10).pack(side="left", padx=(12, 3))
        ttk.Button(filters, text="全选最高置信度不高于所选", command=lambda: self._speaker_select_rule("max_at_most")).pack(side="left")
        ttk.Button(filters, text="全选最高置信度等于所选", command=lambda: self._speaker_select_rule("max_exact")).pack(side="left", padx=3)
        self.speaker_choice = tk.StringVar()
        ttk.Label(filters, text="当前说话人译文候选：").pack(side="left", padx=(12, 2))
        self.speaker_choice_combo = ttk.Combobox(filters, textvariable=self.speaker_choice, state="readonly", width=24)
        self.speaker_choice_combo.pack(side="left")
        ttk.Button(filters, text="当前候选加入辞典", command=self._speaker_current_choice_to_dictionary).pack(side="left", padx=3)
        self.speaker_summary = tk.StringVar(value="尚未筛选")
        ttk.Label(filters, textvariable=self.speaker_summary).pack(side="right")

        groupbox = ttk.LabelFrame(tab, text="说话人及译文候选（候选按使用次数降序）", padding=4)
        groupbox.grid(row=2, column=0, sticky="nsew"); groupbox.rowconfigure(0, weight=1); groupbox.columnconfigure(0, weight=1)
        self.speaker_tree = self._tree(groupbox, ("use", "original", "translated", "option_count", "types", "confidence", "count"),
                                       ("选择", "原文", "译文", "译文数", "类型", "最高置信度", "次数"),
                                       (60, 260, 620, 80, 120, 110, 70), checkbox=True)
        self.speaker_tree.master.grid(row=0, column=0, sticky="nsew")
        self.speaker_tree.bind("<<TreeviewSelect>>", lambda e: self._show_speaker_examples())
        examples = ttk.LabelFrame(tab, text="选中说话人的例句（双击进入单独文本处理）", padding=4)
        examples.grid(row=3, column=0, sticky="nsew", pady=(5, 0)); examples.rowconfigure(0, weight=1); examples.columnconfigure(0, weight=1)
        self.speaker_example_tree = self._tree(examples, ("file", "type", "confidence", "original", "translated"),
                                               ("文件", "类型", "置信度", "原文", "译文"), (230, 70, 100, 570, 570))
        self.speaker_example_tree.master.grid(row=0, column=0, sticky="nsew")
        self.speaker_example_tree.bind("<Double-1>", lambda e: self._double_to_editor("speaker_examples"))

    def _do_speaker(self):
        try:
            self.speaker_results = analyze_speaker_groups(self.active_records(), self.speaker_mode.get(), None if self.speaker_full.get() else self.speaker_first_n.get())
            self._clear_tree(self.speaker_tree); self.result_maps["speaker"] = {}
            for i, group in enumerate(self.speaker_results):
                iid = f"sp{i}"; self.result_maps["speaker"][iid] = group
                if len(group.options) == 1:
                    translations = group.options[0].translation or "（未识别）"
                else:
                    translations = "\n".join(f"{x.translation or '（未识别）'} ×{x.count}〔类型{','.join(map(str, x.pattern_types))}；{x.confidence}〕" for x in group.options)
                self.speaker_tree.insert("", "end", iid=iid, values=("☐", group.original_name, self._display(translations, 800), len(group.options),
                                                                          ",".join(map(str, group.pattern_types)), group.max_confidence, group.occurrences))
            total_options = sum(len(x.options) for x in self.speaker_results)
            self.speaker_summary.set(f"说话人 {len(self.speaker_results)} 个；译文选项 {total_options} 个")
            self.status_var.set(f"筛选出 {len(self.speaker_results)} 个说话人。")
            self._show_speaker_examples()
        except Exception as exc: messagebox.showerror("筛选失败", str(exc), parent=self)

    def _speaker_select_rule(self, mode: str):
        chosen = self.speaker_conf_filter.get(); threshold = CONFIDENCE_RANK.get(chosen, 0)
        for iid in self.speaker_tree.get_children(""):
            group = self.result_maps.get("speaker", {}).get(iid)
            check = False
            if isinstance(group, SpeakerGroup):
                if mode == "single": check = len(group.options) == 1
                elif mode == "max_exact": check = group.max_confidence == chosen
                elif mode == "max_at_most": check = CONFIDENCE_RANK.get(group.max_confidence, 0) <= threshold
            vals = list(self.speaker_tree.item(iid, "values")); vals[0] = "☑" if check else "☐"; self.speaker_tree.item(iid, values=vals)

    def _show_speaker_examples(self):
        self._clear_tree(self.speaker_example_tree); self.result_maps["speaker_examples"] = {}
        sel = self.speaker_tree.selection()
        if not sel: return
        group = self.result_maps.get("speaker", {}).get(sel[0])
        if not isinstance(group, SpeakerGroup): return
        choices = [x.translation for x in group.options]
        self.speaker_choice_combo.configure(values=choices)
        self.speaker_choice.set(choices[0] if choices else "")
        for i, record in enumerate(group.records):
            found = self._speaker_detection(record)
            iid = f"spe{i}"; self.result_maps["speaker_examples"][iid] = record
            self.speaker_example_tree.insert("", "end", iid=iid, values=(record.file_key, found[0], found[1], self._display(record.original), self._display(record.translated)))

    def _speaker_detection(self, record):
        from workbench_core import detect_speaker
        found = detect_speaker(record.original, self.speaker_mode.get())
        return (found[1], found[2]) if found else ("", "")

    def _speaker_current_choice_to_dictionary(self):
        sel = self.speaker_tree.selection()
        if not sel: return
        group = self.result_maps.get("speaker", {}).get(sel[0])
        if not isinstance(group, SpeakerGroup): return
        translation = self.speaker_choice.get()
        self.dictionary_rows.append(EditableDictionaryRow(group.original_name, translation, "人名", "说话人筛选-选定候选"))
        self._refresh_dictionary_tree(); self.notebook.select(self.tabs["dictionary"]); self.status_var.set("已将当前说话人与选定译文加入辞典。")

    def _speaker_to_dictionary(self):
        added = 0
        for iid in self._checked_iids(self.speaker_tree):
            group = self.result_maps.get("speaker", {}).get(iid)
            if not isinstance(group, SpeakerGroup): continue
            if group.options:
                for option in group.options:
                    self.dictionary_rows.append(EditableDictionaryRow(group.original_name, option.translation, "人名", "说话人筛选")); added += 1
            else:
                self.dictionary_rows.append(EditableDictionaryRow(group.original_name, "", "人名", "说话人筛选")); added += 1
        self._refresh_dictionary_tree(); self.notebook.select(self.tabs["dictionary"]); self.status_var.set(f"已加入辞典 {added} 行。")

    def _export_speakers(self):
        path = filedialog.asksaveasfilename(parent=self, defaultextension=".xlsx", filetypes=[("Excel", "*.xlsx"), ("CSV", "*.csv")])
        if not path: return
        rows = []
        for group in self.speaker_results:
            for option in group.options or ():
                rows.append(EditableDictionaryRow(group.original_name, option.translation, "人名", "说话人筛选"))
            if not group.options: rows.append(EditableDictionaryRow(group.original_name, "", "人名", "说话人筛选"))
        save_dictionary(Path(path), rows); messagebox.showinfo("导出完成", path, parent=self)

    # ------------------------------------------------------------------
    # Combined punctuation workspace
    # ------------------------------------------------------------------
    def _build_punctuation_workspace(self):
        tab = self.tabs["punctuation"]; tab.rowconfigure(0, weight=1); tab.columnconfigure(0, weight=1)
        self.punct_notebook = ttk.Notebook(tab); self.punct_notebook.grid(row=0, column=0, sticky="nsew")
        self.punct_tabs = {}
        for key, title in [("quote", "引号处理"), ("terminal", "句末符号"), ("symbols", "批量替换符号"), ("ellipsis", "省略号检查")]:
            frame = ttk.Frame(self.punct_notebook, padding=6); self.punct_notebook.add(frame, text=title); self.punct_tabs[key] = frame
        self._build_quote_panel(); self._build_terminal_panel(); self._build_symbol_panel(); self._build_ellipsis_panel()

    def _build_quote_panel(self):
        tab = self.punct_tabs["quote"]; tab.rowconfigure(2, weight=1); tab.columnconfigure(0, weight=1)
        bar = ttk.Frame(tab); bar.grid(row=0, column=0, sticky="ew")
        self.quote_extract_mode = tk.StringVar(value="全部")
        ttk.Combobox(bar, textvariable=self.quote_extract_mode, state="readonly", values=["全部", "引号状况", "引号样式"], width=12).pack(side="left")
        ttk.Button(bar, text="提取全部含引号文本", command=self._do_quote_analysis).pack(side="left", padx=5)
        ttk.Label(bar, text="状况处理：").pack(side="left", padx=(12, 2))
        self.quote_form = tk.StringVar(value="与原文一致")
        ttk.Combobox(bar, textvariable=self.quote_form, state="readonly", values=["与原文一致", "左右引号", "只有左引号", "删除引号"], width=12).pack(side="left")
        ttk.Label(bar, text="引号样式：").pack(side="left", padx=(8, 2))
        self.quote_style = tk.StringVar(value="与原文相同")
        ttk.Combobox(bar, textvariable=self.quote_style, state="readonly", values=["与原文相同"] + QUOTE_STYLE_LABELS, width=12).pack(side="left")
        ttk.Button(bar, text="生成处理预览", command=self._preview_quote_changes).pack(side="left", padx=5)
        ttk.Button(bar, text="应用勾选", command=self._apply_quote_changes).pack(side="right")
        act = ttk.Frame(tab); act.grid(row=1, column=0, sticky="ew", pady=4)
        ttk.Button(act, text="全选可批量项", command=lambda: self._check_all(self.quote_tree, True, lambda i, v: v[1] != "手动")).pack(side="left")
        ttk.Button(act, text="全部取消", command=lambda: self._check_all(self.quote_tree, False)).pack(side="left", padx=4)
        ttk.Button(act, text="仅选择引号状况", command=lambda: self._quote_check_mode("引号状况")).pack(side="left")
        ttk.Button(act, text="仅选择引号样式", command=lambda: self._quote_check_mode("引号样式")).pack(side="left", padx=4)
        self.quote_tree = self._tree(tab, ("use", "status", "mode", "file", "reason", "original", "translated", "proposed"),
                                     ("选择", "状态", "类型", "文件", "说明", "原文", "译文", "处理结果"),
                                     (60, 80, 100, 210, 300, 410, 410, 410), checkbox=True)
        self.quote_tree.master.grid(row=2, column=0, sticky="nsew"); self.quote_tree.bind("<Double-1>", lambda e: self._double_to_editor("quote"))
        self.quote_rows: dict[str, QuoteAnalysisRow] = {}

    def _do_quote_analysis(self):
        try:
            rows = analyze_quote_rows(self.active_records())
            if self.quote_extract_mode.get() != "全部": rows = [x for x in rows if x.mode == self.quote_extract_mode.get()]
            self.quote_rows = {}; self.result_maps["quote"] = {}; self._clear_tree(self.quote_tree)
            for i, row in enumerate(rows):
                iid = f"q{i}"; self.quote_rows[iid] = row; self.result_maps["quote"][iid] = row.record
                self.quote_tree.insert("", "end", iid=iid, values=("☐", row.status, row.mode, row.record.file_key, row.reason,
                                                                       self._display(row.record.original), self._display(row.record.translated), ""))
            self.status_var.set(f"提取 {len(rows)} 项引号状况/样式记录。")
        except Exception as exc: messagebox.showerror("引号检查失败", str(exc), parent=self)

    def _quote_pair_for_row(self, row: QuoteAnalysisRow):
        if self.quote_style.get() == "与原文相同":
            if len(row.source_styles) != 1: return None
            return QUOTE_PAIRS[row.source_styles[0]]
        return QUOTE_PAIRS[self.quote_style.get()]

    def _preview_quote_changes(self):
        if not self.quote_rows: self._do_quote_analysis()
        for iid, row in self.quote_rows.items():
            if not row.auto_eligible:
                continue
            pair = self._quote_pair_for_row(row)
            if pair is None: continue
            if row.mode == "引号样式":
                proposed = build_quote_style_proposal(row.record, pair)
            else:
                form = self.quote_form.get()
                if form == "与原文一致":
                    from workbench_core import _outer_quote_info
                    info = _outer_quote_info(row.record.original)
                    form = "左右引号" if info.get("close_at_end") else "只有左引号"
                proposed = build_quote_situation_proposal(row.record, form, pair)
            if proposed is not None:
                vals = list(self.quote_tree.item(iid, "values")); vals[-1] = self._display(proposed); self.quote_tree.item(iid, values=vals)
                self.quote_rows[iid] = QuoteAnalysisRow(row.record, row.mode, row.status, row.auto_eligible, row.reason,
                                                        row.source_styles, row.translated_styles, proposed)

    def _quote_check_mode(self, mode):
        for iid, row in self.quote_rows.items():
            vals = list(self.quote_tree.item(iid, "values")); vals[0] = "☑" if row.mode == mode and row.auto_eligible else "☐"; self.quote_tree.item(iid, values=vals)

    def _apply_quote_changes(self):
        updates = {}
        for iid in self._checked_iids(self.quote_tree):
            row = self.quote_rows.get(iid)
            if row and row.auto_eligible and row.proposal is not None and row.proposal != row.record.translated:
                updates[row.record.uid] = (row.record, row.proposal)
        self._apply_updates_dialog(updates, "引号处理")

    def _build_terminal_panel(self):
        tab = self.punct_tabs["terminal"]; tab.rowconfigure(2, weight=1); tab.columnconfigure(0, weight=1)
        bar = ttk.Frame(tab); bar.grid(row=0, column=0, sticky="ew")
        self.punct_mode = tk.StringVar(value="source")
        ttk.Combobox(bar, textvariable=self.punct_mode, state="readonly", values=["source", "missing", "delete"], width=12).pack(side="left")
        ttk.Label(bar, text="缺失时默认：").pack(side="left", padx=(8, 2)); self.punct_default = tk.StringVar(value="。")
        ttk.Combobox(bar, textvariable=self.punct_default, state="readonly", values=["。", "！", "？", "；"], width=5).pack(side="left")
        ttk.Button(bar, text="检查并预览", command=self._do_punct).pack(side="left", padx=8)
        ttk.Button(bar, text="应用勾选", command=lambda: self._apply_generic_changes("terminal")).pack(side="right")
        act = ttk.Frame(tab); act.grid(row=1, column=0, sticky="ew", pady=4)
        ttk.Button(act, text="全选", command=lambda: self._check_all(self.terminal_tree, True)).pack(side="left")
        ttk.Button(act, text="全不选", command=lambda: self._check_all(self.terminal_tree, False)).pack(side="left", padx=4)
        self.terminal_tree = self._tree(tab, ("use", "file", "reason", "original", "translated", "proposed"),
                                        ("选择", "文件", "说明", "原文", "译文", "处理结果"), (60, 220, 260, 450, 450, 450), checkbox=True)
        self.terminal_tree.master.grid(row=2, column=0, sticky="nsew"); self.terminal_tree.bind("<Double-1>", lambda e: self._double_to_editor("terminal"))

    def _do_punct(self):
        try: self._store_generic_changes("terminal", self.terminal_tree, analyze_punctuation(self.active_records(), self.punct_mode.get(), self.punct_default.get()))
        except Exception as exc: messagebox.showerror("检查失败", str(exc), parent=self)

    def _build_symbol_panel(self):
        tab = self.punct_tabs["symbols"]; tab.rowconfigure(2, weight=1); tab.columnconfigure(0, weight=1)
        bar = ttk.Frame(tab); bar.grid(row=0, column=0, sticky="ew")
        ttk.Button(bar, text="全文检查并刷新标点列表", command=self._refresh_symbol_catalog).pack(side="left")
        ttk.Button(bar, text="打开可编辑的标点分类文档", command=lambda: open_file(self.catalog_path)).pack(side="left", padx=5)
        ttk.Button(bar, text="应用选中符号替换", command=self._apply_symbol_rows).pack(side="right")
        ttk.Label(tab, text="双击“替换为”列打开下拉选项；双击“自定义”列填写自定义符号。RPG Maker 操作符本身不计入标点统计。", foreground="#555").grid(row=1, column=0, sticky="w", pady=4)
        self.symbol_tree = self._tree(tab, ("symbol", "description", "replacement", "custom", "replacement_desc", "original_count", "translated_count", "total"),
                                      ("原标点符号", "标点描述和 Unicode", "替换为", "自定义", "替换符号描述和 Unicode", "原文次数", "译文次数", "出现次数"),
                                      (110, 300, 130, 120, 300, 90, 90, 90))
        self.symbol_tree.master.grid(row=2, column=0, sticky="nsew")
        self.symbol_tree.bind("<Double-1>", self._symbol_cell_edit)
        self.symbol_rows: dict[str, dict[str, object]] = {}

    def _refresh_symbol_catalog(self):
        try:
            catalog = load_symbol_catalog(self.catalog_path); rows = analyze_symbol_catalog(self.active_records(), catalog)
            self.symbol_rows = {}; self._clear_tree(self.symbol_tree)
            for i, row in enumerate(rows):
                iid = f"sym{i}"; self.symbol_rows[iid] = row
                self.symbol_tree.insert("", "end", iid=iid, values=(row["symbol"], f"{row['description']} / {row['unicode']}", row["symbol"], "",
                                                                        f"{row['description']} / {row['unicode']}", row["original_count"], row["translated_count"], row["total_count"]))
            self.status_var.set(f"标点列表共 {len(rows)} 行。")
        except Exception as exc: messagebox.showerror("标点统计失败", str(exc), parent=self)

    def _symbol_cell_edit(self, event):
        iid = self.symbol_tree.identify_row(event.y); col = self.symbol_tree.identify_column(event.x)
        if not iid: return
        row = self.symbol_rows.get(iid); bbox = self.symbol_tree.bbox(iid, col)
        if not row or not bbox: return
        x, y, w, h = bbox
        if col == "#3":
            combo = ttk.Combobox(self.symbol_tree, state="readonly", values=row["choices"])
            combo.place(x=x, y=y, width=max(100, w), height=h); combo.set(self.symbol_tree.set(iid, "replacement")); combo.focus_set()
            def finish(_e=None):
                value = combo.get(); self.symbol_tree.set(iid, "replacement", value)
                desc = self._symbol_description(value, row)
                self.symbol_tree.set(iid, "replacement_desc", desc); combo.destroy()
            combo.bind("<<ComboboxSelected>>", finish); combo.bind("<FocusOut>", finish)
        elif col == "#4":
            value = simpledialog.askstring("自定义替换符号", "输入替换符号：", initialvalue=self.symbol_tree.set(iid, "custom"), parent=self)
            if value is not None:
                self.symbol_tree.set(iid, "custom", value); self.symbol_tree.set(iid, "replacement", "自定义")
                self.symbol_tree.set(iid, "replacement_desc", self._unicode_description(value))

    def _unicode_description(self, value: str) -> str:
        import unicodedata
        return "；".join(f"{unicodedata.name(ch, '未知字符')} / U+{ord(ch):04X}" for ch in value) if value else "删除"

    def _symbol_description(self, choice, row):
        if choice == "删除": return "删除"
        if choice == "自定义": return self._unicode_description("")
        return self._unicode_description(str(choice))

    def _apply_symbol_rows(self):
        selected = self.symbol_tree.selection()
        if not selected:
            messagebox.showinfo("符号替换", "请在表格中选择要应用的符号行。", parent=self); return
        proposed_by_uid = {r.uid: r.translated for r in self.active_records()}
        records = {r.uid: r for r in self.active_records()}
        for iid in selected:
            old = self.symbol_tree.set(iid, "symbol"); choice = self.symbol_tree.set(iid, "replacement")
            new = "" if choice == "删除" else (self.symbol_tree.set(iid, "custom") if choice == "自定义" else choice)
            if old == new: continue
            # Apply sequentially without reconstructing frozen DataRecord.
            from workbench_core import replace_outside_controls
            for uid in list(proposed_by_uid): proposed_by_uid[uid] = replace_outside_controls(proposed_by_uid[uid], old, new)
        updates = {uid: (records[uid], text) for uid, text in proposed_by_uid.items() if text != records[uid].translated}
        self._apply_updates_dialog(updates, "批量替换符号")

    def _build_ellipsis_panel(self):
        tab = self.punct_tabs["ellipsis"]; tab.rowconfigure(2, weight=1); tab.columnconfigure(0, weight=1)
        bar = ttk.Frame(tab); bar.grid(row=0, column=0, sticky="ew")
        self.ellipsis_side = tk.StringVar(value="both"); self.ellipsis_kind = tk.StringVar(value="全部")
        ttk.Combobox(bar, textvariable=self.ellipsis_side, state="readonly", values=["original", "translated", "both"], width=11).pack(side="left")
        ttk.Combobox(bar, textvariable=self.ellipsis_kind, state="readonly", values=["全部", "连续点", "省略号"], width=9).pack(side="left", padx=4)
        ttk.Button(bar, text="分类检查", command=self._do_ellipsis_scan).pack(side="left")
        ttk.Label(bar, text="转换：").pack(side="left", padx=(12, 2)); self.ellipsis_direction = tk.StringVar(value="连续点→省略号")
        ttk.Combobox(bar, textvariable=self.ellipsis_direction, state="readonly", values=["连续点→省略号", "省略号→连续点", "单省略号→双省略号", "省略号压缩"], width=16).pack(side="left")
        ttk.Label(bar, text="每").pack(side="left"); self.ellipsis_group_size = tk.IntVar(value=2)
        ttk.Spinbox(bar, from_=1, to=12, textvariable=self.ellipsis_group_size, width=4).pack(side="left"); ttk.Label(bar, text="点").pack(side="left")
        self.ellipsis_remainder = tk.StringVar(value="删除"); ttk.Combobox(bar, textvariable=self.ellipsis_remainder, state="readonly", values=["删除", "一个省略号"], width=10).pack(side="left", padx=3)
        self.ellipsis_style = tk.StringVar(value="……"); ttk.Combobox(bar, textvariable=self.ellipsis_style, values=["……", "…"], width=6).pack(side="left")
        self.ellipsis_dot_style = tk.StringVar(value="."); ttk.Combobox(bar, textvariable=self.ellipsis_dot_style, values=[".", "．", "·", "・", "。"], width=5).pack(side="left")
        self.ellipsis_max = tk.IntVar(value=2); ttk.Spinbox(bar, from_=1, to=12, textvariable=self.ellipsis_max, width=4).pack(side="left")
        ttk.Button(bar, text="预览勾选转换", command=self._preview_ellipsis).pack(side="left", padx=4)
        ttk.Button(bar, text="应用勾选", command=lambda: self._apply_generic_changes("ellipsis_v4")).pack(side="right")
        act = ttk.Frame(tab); act.grid(row=1, column=0, sticky="ew", pady=4)
        ttk.Button(act, text="全选可处理项", command=lambda: self._check_all(self.ellipsis_tree, True, lambda i, v: v[7] != "是")).pack(side="left")
        ttk.Button(act, text="全不选", command=lambda: self._check_all(self.ellipsis_tree, False)).pack(side="left", padx=4)
        self.ellipsis_tree = self._tree(tab, ("use", "side", "kind", "style", "count", "control", "file", "manual", "reason", "original", "translated", "proposed"),
                                        ("选择", "文本侧", "类型", "形式", "数量", "夹操作符", "文件", "仅手动", "说明", "原文", "译文", "处理结果"),
                                        (60, 70, 90, 100, 60, 80, 200, 80, 220, 340, 340, 340), checkbox=True)
        self.ellipsis_tree.master.grid(row=2, column=0, sticky="nsew"); self.ellipsis_tree.bind("<Double-1>", lambda e: self._double_to_editor("ellipsis_v4"))
        self.ellipsis_occurrences: dict[str, EllipsisOccurrence] = {}

    def _do_ellipsis_scan(self):
        try:
            rows = analyze_ellipsis_occurrences(self.active_records(), self.ellipsis_side.get())
            if self.ellipsis_kind.get() != "全部": rows = [x for x in rows if x.kind == self.ellipsis_kind.get()]
            self.ellipsis_occurrences = {}; self.result_maps["ellipsis_v4"] = {}; self._clear_tree(self.ellipsis_tree)
            for i, row in enumerate(rows):
                iid = f"el{i}"; self.ellipsis_occurrences[iid] = row; self.result_maps["ellipsis_v4"][iid] = row.record
                self.ellipsis_tree.insert("", "end", iid=iid, values=("☐", row.side, row.kind, row.visible_style, row.count,
                                                                          "是" if row.interrupted_by_control else "否", row.record.file_key,
                                                                          "是" if row.manual_only else "否", row.reason, self._display(row.record.original),
                                                                          self._display(row.record.translated), ""))
            self.status_var.set(f"省略号/连续点共 {len(rows)} 项。")
        except Exception as exc: messagebox.showerror("省略号检查失败", str(exc), parent=self)

    def _preview_ellipsis(self):
        records = {}
        for iid in self._checked_iids(self.ellipsis_tree):
            occ = self.ellipsis_occurrences.get(iid)
            if occ and not occ.manual_only and occ.side == "译文": records[occ.record.uid] = occ.record
        changes = []
        for record in records.values():
            proposed = build_ellipsis_conversion(record, direction=self.ellipsis_direction.get(), group_size=self.ellipsis_group_size.get(),
                                                 remainder=self.ellipsis_remainder.get(), ellipsis_style=self.ellipsis_style.get(),
                                                 dot_style=self.ellipsis_dot_style.get(), max_ellipsis=self.ellipsis_max.get())
            if proposed and proposed != record.translated: changes.append(TextChange(record, self.ellipsis_direction.get(), proposed))
        self.result_maps["ellipsis_v4_changes"] = {x.record.uid: x for x in changes}
        for iid, occ in self.ellipsis_occurrences.items():
            change = self.result_maps["ellipsis_v4_changes"].get(occ.record.uid)
            if change:
                vals = list(self.ellipsis_tree.item(iid, "values")); vals[-1] = self._display(change.proposed); self.ellipsis_tree.item(iid, values=vals)
        self.result_maps["ellipsis_v4_apply"] = {x.record.uid: x for x in changes}

    # ------------------------------------------------------------------
    # English/digit format workspace
    # ------------------------------------------------------------------
    def _build_alnum_workspace(self):
        tab = self.tabs["alnum"]; tab.rowconfigure(0, weight=1); tab.columnconfigure(0, weight=1)
        nb = ttk.Notebook(tab); nb.grid(row=0, column=0, sticky="nsew")
        case_tab = ttk.Frame(nb, padding=6); width_tab = ttk.Frame(nb, padding=6)
        nb.add(case_tab, text="英文大小写"); nb.add(width_tab, text="字符全半角")
        self._build_case_panel(case_tab); self._build_width_char_panel(width_tab)

    def _alnum_common_top(self, tab, prefix):
        tab.rowconfigure(2, weight=1); tab.columnconfigure(0, weight=1)
        top = ttk.Frame(tab); top.grid(row=0, column=0, sticky="ew")
        target = tk.StringVar(value="both"); setattr(self, prefix + "_target", target)
        ttk.Label(top, text="查询范围：").pack(side="left")
        ttk.Combobox(top, textvariable=target, state="readonly", values=["english", "digits", "both"], width=10).pack(side="left")
        query = tk.StringVar(); setattr(self, prefix + "_query", query)
        ttk.Label(top, text="仅处理包含：").pack(side="left", padx=(10, 2)); ttk.Entry(top, textvariable=query, width=25).pack(side="left")
        ttk.Button(top, text="扫描出现位置", command=lambda: self._scan_alnum(prefix)).pack(side="left", padx=5)
        edit = ttk.LabelFrame(tab, text="修改框：有划取内容时只处理划取部分；没有划取时处理全文", padding=4)
        edit.grid(row=1, column=0, sticky="ew", pady=5); edit.columnconfigure(0, weight=1)
        text = tk.Text(edit, height=4, wrap="none"); text.grid(row=0, column=0, sticky="ew"); self.text_widgets.append(text); setattr(self, prefix + "_edit", text)
        tree = self._tree(tab, ("use", "file", "found", "original", "translated", "proposed"),
                          ("选择", "文件", "匹配字符", "原文", "译文", "处理结果"), (60, 220, 180, 440, 440, 440), checkbox=True)
        tree.master.grid(row=2, column=0, sticky="nsew"); tree.bind("<Double-1>", lambda e, p=prefix: self._double_to_editor(p))
        tree.bind("<<TreeviewSelect>>", lambda e, p=prefix: self._alnum_select_to_edit(p))
        setattr(self, prefix + "_tree", tree)

    def _build_case_panel(self, tab):
        self._alnum_common_top(tab, "case")
        bar = ttk.Frame(tab); bar.grid(row=3, column=0, sticky="ew", pady=4)
        self.case_mode = tk.StringVar(value="首字母大写")
        ttk.Combobox(bar, textvariable=self.case_mode, state="readonly", values=["大写", "小写", "首字母大写"], width=12).pack(side="left")
        ttk.Button(bar, text="处理修改框划取/全文", command=lambda: self._transform_alnum_edit("case", self.case_mode.get())).pack(side="left", padx=4)
        ttk.Button(bar, text="修改框内容送入选中记录预览", command=lambda: self._alnum_edit_to_selected("case")).pack(side="left")
        ttk.Button(bar, text="预览勾选记录", command=lambda: self._preview_alnum_records("case")).pack(side="left", padx=4)
        ttk.Button(bar, text="应用勾选", command=lambda: self._apply_generic_changes("case")).pack(side="right")

    def _build_width_char_panel(self, tab):
        self._alnum_common_top(tab, "charwidth")
        bar = ttk.Frame(tab); bar.grid(row=3, column=0, sticky="ew", pady=4)
        self.english_width_mode = tk.StringVar(value="不变"); self.digit_width_mode = tk.StringVar(value="不变")
        ttk.Label(bar, text="英文：").pack(side="left"); ttk.Combobox(bar, textvariable=self.english_width_mode, state="readonly", values=["不变", "全角", "半角"], width=8).pack(side="left")
        ttk.Label(bar, text="数字：").pack(side="left", padx=(8, 2)); ttk.Combobox(bar, textvariable=self.digit_width_mode, state="readonly", values=["不变", "全角", "半角"], width=8).pack(side="left")
        ttk.Button(bar, text="处理修改框划取/全文", command=self._transform_width_edit).pack(side="left", padx=4)
        ttk.Button(bar, text="修改框内容送入选中记录预览", command=lambda: self._alnum_edit_to_selected("charwidth")).pack(side="left")
        ttk.Button(bar, text="预览勾选记录", command=lambda: self._preview_alnum_records("charwidth")).pack(side="left", padx=4)
        ttk.Button(bar, text="应用勾选", command=lambda: self._apply_generic_changes("charwidth")).pack(side="right")

    def _scan_alnum(self, prefix):
        try:
            target = getattr(self, prefix + "_target").get(); rows = analyze_alnum_occurrences(self.active_records(), target)
            query = getattr(self, prefix + "_query").get()
            if query: rows = [x for x in rows if query.casefold() in x.reason.casefold()]
            tree = getattr(self, prefix + "_tree"); self._clear_tree(tree); self.result_maps[prefix] = {}
            for i, row in enumerate(rows):
                iid = f"{prefix}{i}"; self.result_maps[prefix][iid] = row.record
                tree.insert("", "end", iid=iid, values=("☐", row.record.file_key, row.reason, self._display(row.record.original), self._display(row.record.translated), ""))
            self.status_var.set(f"{prefix} 扫描命中 {len(rows)} 条。")
        except Exception as exc: messagebox.showerror("扫描失败", str(exc), parent=self)

    def _alnum_select_to_edit(self, prefix):
        tree = getattr(self, prefix + "_tree"); sel = tree.selection()
        if not sel: return
        record = self.result_maps.get(prefix, {}).get(sel[0])
        if not isinstance(record, DataRecord): return
        widget = getattr(self, prefix + "_edit")
        widget.delete("1.0", "end"); widget.insert("1.0", record.translated)

    def _alnum_edit_to_selected(self, prefix):
        tree = getattr(self, prefix + "_tree"); sel = tree.selection()
        if len(sel) != 1:
            messagebox.showinfo("修改框预览", "请只选择一条记录。", parent=self); return
        record = self.result_maps.get(prefix, {}).get(sel[0])
        if not isinstance(record, DataRecord): return
        proposed = getattr(self, prefix + "_edit").get("1.0", "end-1c")
        change = TextChange(record, "修改框内容", proposed)
        changes = self.result_maps.setdefault(prefix + "_changes", {}); changes[sel[0]] = change
        vals = list(tree.item(sel[0], "values")); vals[-1] = self._display(proposed); tree.item(sel[0], values=vals)
        if vals and vals[0] != "☑": vals[0] = "☑"; tree.item(sel[0], values=vals)

    def _transform_alnum_edit(self, prefix, mode):
        widget = getattr(self, prefix + "_edit")
        try:
            value = widget.get("sel.first", "sel.last"); replacement = transform_outside_controls(value, mode)
            widget.delete("sel.first", "sel.last"); widget.insert("insert", replacement)
        except tk.TclError:
            value = widget.get("1.0", "end-1c"); widget.delete("1.0", "end"); widget.insert("1.0", transform_outside_controls(value, mode))

    def _transform_width_text(self, text):
        import re
        from workbench_core import to_fullwidth_ascii, to_halfwidth_ascii, _outside_controls_transform
        def fn(segment):
            if self.english_width_mode.get() != "不变":
                convert = to_fullwidth_ascii if self.english_width_mode.get() == "全角" else to_halfwidth_ascii
                segment = re.sub(r"[A-Za-zＡ-Ｚａ-ｚ]+", lambda m: convert(m.group(0)), segment)
            if self.digit_width_mode.get() != "不变":
                convert = to_fullwidth_ascii if self.digit_width_mode.get() == "全角" else to_halfwidth_ascii
                segment = re.sub(r"[0-9０-９]+", lambda m: convert(m.group(0)), segment)
            return segment
        return _outside_controls_transform(text, fn)

    def _transform_width_edit(self):
        widget = self.charwidth_edit
        try:
            value = widget.get("sel.first", "sel.last"); replacement = self._transform_width_text(value)
            widget.delete("sel.first", "sel.last"); widget.insert("insert", replacement)
        except tk.TclError:
            value = widget.get("1.0", "end-1c"); widget.delete("1.0", "end"); widget.insert("1.0", self._transform_width_text(value))

    def _preview_alnum_records(self, prefix):
        import re
        from workbench_core import _outside_controls_transform
        tree = getattr(self, prefix + "_tree"); changes = {}
        term = getattr(self, prefix + "_query").get()
        for iid in self._checked_iids(tree):
            record = self.result_maps.get(prefix, {}).get(iid)
            if not isinstance(record, DataRecord): continue
            if prefix == "case":
                if term:
                    pattern = re.compile(re.escape(term), re.I)
                    proposed = _outside_controls_transform(record.translated, lambda seg: pattern.sub(lambda m: transform_outside_controls(m.group(0), self.case_mode.get()), seg))
                else:
                    proposed = transform_outside_controls(record.translated, self.case_mode.get())
            else:
                if term:
                    pattern = re.compile(re.escape(term), re.I)
                    proposed = _outside_controls_transform(record.translated, lambda seg: pattern.sub(lambda m: self._transform_width_text(m.group(0)), seg))
                else:
                    proposed = self._transform_width_text(record.translated)
            if proposed != record.translated:
                change = TextChange(record, prefix, proposed); changes[iid] = change
                vals = list(tree.item(iid, "values")); vals[-1] = self._display(proposed); tree.item(iid, values=vals)
        self.result_maps[prefix + "_changes"] = changes

    # ------------------------------------------------------------------
    # Duplicate text analysis
    # ------------------------------------------------------------------
    def _build_duplicates(self):
        tab = self.tabs["duplicates"]; tab.rowconfigure(2, weight=1); tab.columnconfigure(0, weight=1)
        bar = ttk.Frame(tab); bar.grid(row=0, column=0, sticky="ew")
        self.duplicates_inconsistent_only = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="只显示译文不统一", variable=self.duplicates_inconsistent_only).pack(side="left")
        ttk.Button(bar, text="检查完全重复文本", command=self._do_duplicates).pack(side="left", padx=5)
        ttk.Button(bar, text="将勾选项加入辞典", command=self._duplicates_to_dictionary).pack(side="left")
        ttk.Button(bar, text="直接导出勾选项辞典", command=self._export_duplicates).pack(side="left", padx=4)
        act = ttk.Frame(tab); act.grid(row=1, column=0, sticky="ew", pady=4)
        ttk.Button(act, text="全部选择", command=lambda: self._check_all(self.duplicates_tree, True)).pack(side="left")
        ttk.Button(act, text="全部取消", command=lambda: self._check_all(self.duplicates_tree, False)).pack(side="left", padx=4)
        ttk.Label(act, text="双击：转到文本查询的大文本框，按原文精确匹配。右键或 Ctrl+C 可复制。", foreground="#555").pack(side="left", padx=12)
        self.duplicates_tree = self._tree(tab, ("use", "count", "status", "original", "translated", "files"),
                                          ("选择", "重复次数", "翻译状态", "原文", "译文及次数", "文件"),
                                          (60, 90, 100, 520, 620, 300), checkbox=True)
        self.duplicates_tree.master.grid(row=2, column=0, sticky="nsew"); self.duplicates_tree.bind("<Double-1>", self._duplicate_to_search)

    def _do_duplicates(self):
        try:
            groups = analyze_duplicate_texts(self.active_records())
            if self.duplicates_inconsistent_only.get(): groups = [x for x in groups if not x.consistent]
            self.duplicate_results = groups; self.result_maps["duplicates"] = {}; self._clear_tree(self.duplicates_tree)
            for i, group in enumerate(groups):
                iid = f"dup{i}"; self.result_maps["duplicates"][iid] = group
                trans = "\n".join(f"{t} ×{count}" for t, count in group.translations)
                files = "；".join(dict.fromkeys(r.file_key for r in group.records))
                self.duplicates_tree.insert("", "end", iid=iid, values=("☐", len(group.records), "统一" if group.consistent else "不统一",
                                                                             self._display(group.original), self._display(trans, 900), files))
            self.status_var.set(f"发现 {len(groups)} 组完全重复原文。")
        except Exception as exc: messagebox.showerror("重复文本检查失败", str(exc), parent=self)

    def _selected_duplicate_groups(self):
        return [self.result_maps.get("duplicates", {}).get(i) for i in self._checked_iids(self.duplicates_tree)
                if isinstance(self.result_maps.get("duplicates", {}).get(i), DuplicateTextGroup)]

    def _duplicates_to_dictionary(self):
        rows = duplicate_groups_to_dictionary(self._selected_duplicate_groups())
        self.dictionary_rows.extend(rows); self._refresh_dictionary_tree(); self.notebook.select(self.tabs["dictionary"])
        self.status_var.set(f"已将 {len(rows)} 行重复文本加入辞典页。")

    def _export_duplicates(self):
        rows = duplicate_groups_to_dictionary(self._selected_duplicate_groups())
        if not rows: return
        path = filedialog.asksaveasfilename(parent=self, defaultextension=".xlsx", filetypes=[("Excel", "*.xlsx"), ("CSV", "*.csv")])
        if path: save_dictionary(Path(path), rows); messagebox.showinfo("导出完成", path, parent=self)

    def _duplicate_to_search(self, event):
        iid = self.duplicates_tree.identify_row(event.y)
        group = self.result_maps.get("duplicates", {}).get(iid)
        if isinstance(group, DuplicateTextGroup): self._set_search_query(group.original, "original", True)

    # ------------------------------------------------------------------
    # Dictionary check and database: retain both source/translation columns
    # ------------------------------------------------------------------
    def _build_dictcheck(self):
        tab = self.tabs["dictcheck"]; tab.rowconfigure(1, weight=1); tab.columnconfigure(0, weight=1)
        bar = ttk.Frame(tab); bar.grid(row=0, column=0, sticky="ew")
        ttk.Button(bar, text="使用生成辞典页进行检查", command=self._do_dictcheck_current).pack(side="left")
        ttk.Button(bar, text="导入外部辞典并检查", command=self._do_dictcheck_external).pack(side="left", padx=5)
        self.dict_messages_only = tk.BooleanVar(value=False); ttk.Checkbutton(bar, text="仅 Message", variable=self.dict_messages_only).pack(side="left")
        ttk.Button(bar, text="打开文件", command=lambda: self._open_selected_result("dictcheck", "translated")).pack(side="right")
        self.dictcheck_tree = self._tree(tab, ("file", "term", "expected", "original", "translated"),
                                         ("文件", "命中原词", "期望译文", "原文", "译文"), (240, 220, 220, 500, 500))
        self.dictcheck_tree.master.grid(row=1, column=0, sticky="nsew"); self.dictcheck_tree.bind("<Double-1>", lambda e: self._double_to_editor("dictcheck"))

    def _show_dict_warnings(self, warnings):
        self._clear_tree(self.dictcheck_tree); self.result_maps["dictcheck"] = {}
        for i, x in enumerate(warnings):
            iid = f"dc{i}"; self.result_maps["dictcheck"][iid] = x.record
            self.dictcheck_tree.insert("", "end", iid=iid, values=(x.record.file_key, x.original_term, x.expected_translation,
                                                                       self._display(x.record.original), self._display(x.record.translated)))
        self.status_var.set(f"辞典匹配警告 {len(warnings)} 条。")

    def _build_database(self):
        tab = self.tabs["database"]; tab.rowconfigure(3, weight=1); tab.columnconfigure(0, weight=1)
        top = ttk.Frame(tab); top.grid(row=0, column=0, sticky="ew")
        self.db_source_type = tk.StringVar(value="dictionary")
        for text, value in [("辞典", "dictionary"), ("JSON", "json"), ("TXT", "txt"), ("Excel", "excel")]: ttk.Radiobutton(top, text=text, value=value, variable=self.db_source_type).pack(side="left")
        self.db_path1 = tk.StringVar(); self.db_path2 = tk.StringVar()
        ttk.Entry(tab, textvariable=self.db_path1).grid(row=1, column=0, sticky="ew", pady=2); ttk.Entry(tab, textvariable=self.db_path2).grid(row=2, column=0, sticky="ew", pady=2)
        buttons = ttk.Frame(top); buttons.pack(side="right")
        ttk.Button(buttons, text="选择默认来源…", command=self._choose_db_source).pack(side="left")
        ttk.Button(buttons, text="比较数据库", command=self._do_database).pack(side="left", padx=5)
        ttk.Button(buttons, text="应用勾选翻译", command=self._apply_database).pack(side="left")
        self.database_tree = self._tree(tab, ("use", "status", "file", "original", "translated", "baseline", "proposal", "reason"),
                                        ("选择", "状态", "文件", "原文", "当前译文", "默认原文", "建议译文", "说明"),
                                        (60, 100, 220, 330, 330, 330, 330, 300), checkbox=True)
        self.database_tree.master.grid(row=3, column=0, sticky="nsew"); self.database_tree.bind("<Double-1>", lambda e: self._double_to_editor("database"))

    def _do_database(self):
        try:
            kind = self.db_source_type.get()
            if kind == "dictionary": props = compare_database(self.active_records(), dictionary_rows=load_editable_dictionary(Path(self.db_path1.get())))
            else:
                if kind == "json": baseline = load_json_records(Path(self.db_path1.get())).records
                elif kind == "excel": baseline = load_excel_records(Path(self.db_path1.get())).records
                else: baseline = load_txt_records(Path(self.db_path1.get()), Path(self.db_path2.get()), self.origin_encoding.get(), self.translated_encoding.get()).records
                props = compare_database(self.active_records(), list(baseline))
            self._clear_tree(self.database_tree); self.result_maps["database"] = {}
            for i, x in enumerate(props):
                iid = f"db{i}"; self.result_maps["database"][iid] = x
                self.database_tree.insert("", "end", iid=iid, values=("☐", x.status, x.record.file_key, self._display(x.record.original),
                                                                          self._display(x.record.translated), self._display(x.baseline_original),
                                                                          self._display(x.proposed_translation), x.reason))
            self.status_var.set(f"数据库比较完成：{len(props)} 条；Common Event 已排除。")
        except Exception as exc: messagebox.showerror("比较失败", str(exc), parent=self)

    # ------------------------------------------------------------------
    # Generic change storage/apply
    # ------------------------------------------------------------------
    def _store_generic_changes(self, key, tree, items):
        self._clear_tree(tree); self.result_maps[key] = {}
        for i, item in enumerate(items):
            iid = f"{key}{i}"; self.result_maps[key][iid] = item
            tree.insert("", "end", iid=iid, values=("☐", item.record.file_key, item.reason, self._display(item.record.original),
                                                        self._display(item.record.translated), self._display(item.proposed)))

    def _apply_generic_changes(self, key):
        if key == "ellipsis_v4":
            changes = self.result_maps.get("ellipsis_v4_apply", {})
            selected_uids = set()
            for iid in self._checked_iids(self.ellipsis_tree):
                occ = self.ellipsis_occurrences.get(iid)
                if occ: selected_uids.add(occ.record.uid)
            updates = {x.record.uid: (x.record, x.proposed) for x in changes.values() if x.record.uid in selected_uids}
        else:
            tree = getattr(self, key + "_tree")
            mapping = self.result_maps.get(key + "_changes", self.result_maps.get(key, {}))
            updates = {}
            for iid in self._checked_iids(tree):
                item = mapping.get(iid)
                if isinstance(item, TextChange) or isinstance(item, PunctuationIssue): updates[item.record.uid] = (item.record, item.proposed)
        self._apply_updates_dialog(updates, key)

    def _apply_updates_dialog(self, updates, title):
        if not updates:
            messagebox.showinfo(title, "没有可应用的修改。", parent=self); return
        try:
            count, backup = apply_translation_updates(updates)
            messagebox.showinfo(title, f"已保存 {count} 条。\n备份：{backup}", parent=self)
            self._refresh_after_write()
        except Exception as exc: messagebox.showerror(title + "失败", str(exc), parent=self)

    # ------------------------------------------------------------------
    # General result interactions and configuration
    # ------------------------------------------------------------------
    def _open_selected_result(self, key, side):
        tree = getattr(self, key + "_tree"); sel = tree.selection()
        if not sel: return
        obj = self.result_maps.get(key, {}).get(sel[0])
        rec = obj.record if hasattr(obj, "record") else obj
        if isinstance(obj, DatabaseProposal): rec = obj.record
        if isinstance(rec, DataRecord): self._open_record_file(rec, side)

    def _double_to_editor(self, key):
        tree = getattr(self, key + "_tree"); sel = tree.selection()
        if not sel: return
        obj = self.result_maps.get(key, {}).get(sel[0])
        rec = obj.record if hasattr(obj, "record") else obj
        if isinstance(obj, DatabaseProposal): rec = obj.record
        if isinstance(rec, DataRecord): self._send_to_editor(rec)

    def _load_settings(self):
        cfg = load_config(self.config_path)
        self.source_type.set(cfg.get("source_type", "txt")); self.json_path.set(cfg.get("json_path", "")); self.origin_dir.set(cfg.get("origin_dir", ""))
        self.translated_dir.set(cfg.get("translated_dir", "")); self.excel_dir.set(cfg.get("excel_dir", "")); self.origin_encoding.set(cfg.get("origin_encoding", "936"))
        self.translated_encoding.set(cfg.get("translated_encoding", "936")); self.display_font.set(cfg.get("display_font", "系统默认"))
        self.text_font_size.set(cfg.get("text_font_size", 11)); self.list_font.set(cfg.get("list_font", "系统默认")); self.list_font_size.set(cfg.get("list_font_size", 10))
        self.display_line_mode.set(cfg.get("display_line_mode", "集中一行")); self.column_visibility = cfg.get("column_visibility", {}) or {}
        self._update_source_visibility(); self._apply_font(); self._apply_saved_columns(); self._refresh_column_config_tree()

    def _save_settings(self):
        save_config(self.config_path, {
            "source_type": self.source_type.get(), "json_path": self.json_path.get(), "origin_dir": self.origin_dir.get(),
            "translated_dir": self.translated_dir.get(), "excel_dir": self.excel_dir.get(), "origin_encoding": self.origin_encoding.get(),
            "translated_encoding": self.translated_encoding.get(), "display_font": self.display_font.get(), "text_font_size": self.text_font_size.get(),
            "list_font": self.list_font.get(), "list_font_size": self.list_font_size.get(), "display_line_mode": self.display_line_mode.get(),
            "column_visibility": self.column_visibility,
        })

    def _on_close(self):
        self._save_settings(); self.destroy()


def main():
    app = RPGMakerProofreadingApp(); app.mainloop()


if __name__ == "__main__":
    main()
