from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading
import time
import traceback
import webbrowser
from dataclasses import asdict
from pathlib import Path
from typing import Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from core import (
    APP_NAME,
    DEFAULT_DETAIL_CATEGORIES,
    RESOURCE_CATEGORIES,
    VERSION,
    AnalysisError,
    AnalysisResult,
    MapInfo,
    analyze_project,
    convert_lcf_files,
    detect_engine,
    discover_assets,
    export_full_report,
    find_lcf2xml,
    move_assets_to_backup,
    parse_map_tree,
    permanently_delete_assets,
    safe_int,
)

LCF2XML_DOWNLOAD_URL = "https://ci.easyrpg.org/view/liblcf/job/liblcf-win32/lastSuccessfulBuild/artifact/build/bin/lcf2xml.exe"
EASYRPG_TOOLS_URL = "https://easyrpg.org/tools/downloads/"


def app_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


class AuditorApp(tk.Tk):
    def __init__(self, initial_args: argparse.Namespace | None = None) -> None:
        super().__init__()
        self.title(f"{APP_NAME} v{VERSION}")
        self.geometry("1320x820")
        self.minsize(1040, 680)

        self.base_dir = app_base_dir()
        self.project_root: Optional[Path] = None
        self.map_infos: list[MapInfo] = []
        self.result: Optional[AnalysisResult] = None
        self.full_scope_result = False
        self.task_queue: queue.Queue = queue.Queue()
        self.worker: Optional[threading.Thread] = None
        self.current_categories: set[str] = set(DEFAULT_DETAIL_CATEGORIES)
        self.analysis_started_at: Optional[float] = None
        self.analysis_progress_text = ""
        self.refresh_after_id: Optional[str] = None

        self.project_var = tk.StringVar()
        self.parser_var = tk.StringVar()
        parser = find_lcf2xml(self.base_dir)
        if parser:
            self.parser_var.set(str(parser))
        self.engine_var = tk.StringVar(value="auto")
        self.map_encoding_var = tk.StringVar(value="932")
        self.event_encoding_var = tk.StringVar(value="932")
        self.filename_encoding_var = tk.StringVar(value="932")
        self.include_db_var = tk.BooleanVar(value=True)
        self.force_fallback_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="请选择 RPG Maker 2000/2003 工程文件夹。")
        self.search_var = tk.StringVar()
        self.detail_category_vars = {
            category: tk.BooleanVar(value=category in DEFAULT_DETAIL_CATEGORIES)
            for category in RESOURCE_CATEGORIES
        }
        self.unused_selection_var = tk.StringVar(value="已选择 0 项")
        self.external_roots: list[Path] = []

        if initial_args:
            if initial_args.initial_project:
                self.project_var.set(str(Path(initial_args.initial_project).resolve()))
            if initial_args.initial_lcf2xml:
                self.parser_var.set(str(Path(initial_args.initial_lcf2xml).resolve()))
            if initial_args.initial_engine:
                self.engine_var.set(initial_args.initial_engine)
            for variable, value in (
                (self.map_encoding_var, initial_args.initial_map_encoding),
                (self.event_encoding_var, initial_args.initial_event_encoding),
                (self.filename_encoding_var, initial_args.initial_filename_encoding),
            ):
                if value:
                    variable.set(value)

        self._build_ui()
        self.after(100, self._poll_queue)

    def _build_ui(self) -> None:
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")

        ttk.Label(top, text="工程文件夹：").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.project_var).grid(row=0, column=1, columnspan=5, sticky="ew", padx=4)
        ttk.Button(top, text="选择…", command=self.choose_project).grid(row=0, column=6, padx=2)
        ttk.Button(top, text="读取工程", command=self.load_project).grid(row=0, column=7, padx=2)

        ttk.Label(top, text="lcf2xml：").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(top, textvariable=self.parser_var).grid(row=1, column=1, columnspan=4, sticky="ew", padx=4, pady=(6, 0))
        ttk.Button(top, text="选择…", command=self.choose_parser).grid(row=1, column=5, padx=2, pady=(6, 0))
        ttk.Button(top, text="官方下载", command=lambda: webbrowser.open(LCF2XML_DOWNLOAD_URL)).grid(row=1, column=6, padx=2, pady=(6, 0))
        ttk.Button(top, text="工具说明", command=lambda: webbrowser.open(EASYRPG_TOOLS_URL)).grid(row=1, column=7, padx=2, pady=(6, 0))

        ttk.Label(top, text="引擎：").grid(row=2, column=0, sticky="w", pady=(6, 0))
        engine_box = ttk.Frame(top)
        engine_box.grid(row=2, column=1, sticky="w", pady=(6, 0))
        for text, value in (("自动", "auto"), ("RPG 2000", "2k"), ("RPG 2003", "2k3")):
            ttk.Radiobutton(engine_box, text=text, value=value, variable=self.engine_var).pack(side="left", padx=(0, 8))
        ttk.Checkbutton(top, text="强制快速扫描（近似）", variable=self.force_fallback_var).grid(row=2, column=4, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Button(top, text="添加 RTP/外部素材目录", command=self.add_external_root).grid(row=2, column=6, columnspan=2, sticky="e", pady=(6, 0))

        ttk.Label(top, text="编码：").grid(row=3, column=0, sticky="w", pady=(6, 0))
        encoding_frame = ttk.Frame(top)
        encoding_frame.grid(row=3, column=1, columnspan=7, sticky="ew", pady=(6, 0))
        presets = ("932", "936", "950", "1252", "65001", "auto")
        for column, (label, variable) in enumerate((
            ("地图名（LMT）", self.map_encoding_var),
            ("事件/数据库文字（LMU/LDB）", self.event_encoding_var),
            ("素材文件名", self.filename_encoding_var),
        )):
            group = ttk.Frame(encoding_frame)
            group.grid(row=0, column=column, sticky="w", padx=(0, 18))
            ttk.Label(group, text=label + "：").pack(side="left")
            ttk.Combobox(group, textvariable=variable, values=presets, width=8).pack(side="left")
        ttk.Label(
            encoding_frame,
            text="常用：932 日文；936 简体中文；950 繁体中文；auto 自动检测",
        ).grid(row=0, column=3, sticky="w")
        encoding_frame.columnconfigure(3, weight=1)
        top.columnconfigure(1, weight=1)
        top.columnconfigure(4, weight=1)

        body = ttk.Panedwindow(self, orient="horizontal")
        body.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        left = ttk.Frame(body, padding=(0, 4, 8, 0), width=370)
        body.add(left, weight=0)
        right = ttk.Frame(body)
        body.add(right, weight=1)
        self.after_idle(lambda: body.sashpos(0, 370))

        ttk.Label(left, text="检查范围", font=("TkDefaultFont", 10, "bold")).pack(anchor="w")
        ttk.Checkbutton(left, text="包含数据库 RPG_RT.ldb", variable=self.include_db_var).pack(anchor="w", pady=(4, 4))

        map_buttons = ttk.Frame(left)
        map_buttons.pack(fill="x")
        ttk.Button(map_buttons, text="全选地图", command=self.select_all_maps).pack(side="left", expand=True, fill="x", padx=(0, 2))
        ttk.Button(map_buttons, text="清除选择", command=self.clear_map_selection).pack(side="left", expand=True, fill="x", padx=(2, 0))

        map_frame = ttk.Frame(left)
        map_frame.pack(fill="both", expand=True, pady=4)
        self.map_list = tk.Listbox(map_frame, selectmode="extended", exportselection=False)
        map_scroll = ttk.Scrollbar(map_frame, orient="vertical", command=self.map_list.yview)
        self.map_list.configure(yscrollcommand=map_scroll.set)
        self.map_list.pack(side="left", fill="both", expand=True)
        map_scroll.pack(side="right", fill="y")

        category_box = ttk.LabelFrame(left, text="详细检查类别（标准素材目录）", padding=6)
        category_box.pack(fill="x", pady=(4, 4))
        category_buttons = ttk.Frame(category_box)
        category_buttons.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 4))
        ttk.Button(category_buttons, text="全选类别", command=self.select_all_categories).pack(side="left", expand=True, fill="x", padx=(0, 2))
        ttk.Button(category_buttons, text="全不选类别", command=self.clear_all_categories).pack(side="left", expand=True, fill="x", padx=2)
        ttk.Button(category_buttons, text="恢复默认", command=self.restore_default_categories).pack(side="left", expand=True, fill="x", padx=(2, 0))
        for index, (category, variable) in enumerate(self.detail_category_vars.items()):
            row = 1 + index // 3
            column = index % 3
            ttk.Checkbutton(category_box, text=category, variable=variable).grid(
                row=row, column=column, sticky="w", padx=(0, 8), pady=1,
            )
        category_box.columnconfigure(0, weight=1)
        category_box.columnconfigure(1, weight=1)
        category_box.columnconfigure(2, weight=1)

        scan_buttons = ttk.Frame(left)
        scan_buttons.pack(fill="x", pady=(8, 3))
        ttk.Button(scan_buttons, text="全工程总检查", command=self.run_full_scan).pack(side="left", expand=True, fill="x", padx=(0, 2))
        ttk.Button(scan_buttons, text="所选地图详细检查", command=self.run_detailed_scan).pack(side="left", expand=True, fill="x", padx=(2, 0))
        ttk.Button(left, text="导出当前完整报告", command=self.export_report).pack(fill="x", pady=3)

        self.notebook = ttk.Notebook(right)
        self.notebook.pack(fill="both", expand=True)

        self.summary_tree = self._make_tree_tab(
            "调用汇总",
            ("category", "asset_name", "file_names", "file_status", "file_paths", "reference_count", "sources", "confidence"),
            (90, 190, 210, 130, 500, 80, 380, 90),
            ("类别", "调用素材名", "实际文件名", "文件状态", "实际文件路径", "引用次数", "来源（去重）", "可信度"),
            asset_actions=True,
        )
        self.detail_tree = self._make_tree_tab(
            "调用明细",
            ("category", "asset_name", "file_names", "file_status", "file_paths", "source_id", "source_name", "location", "command_name", "confidence"),
            (80, 180, 200, 130, 480, 100, 150, 480, 150, 80),
            ("类别", "调用素材名", "实际文件名", "文件状态", "实际文件路径", "来源", "地图/对象名", "具体位置", "命令", "可信度"),
            asset_actions=True,
        )
        self.missing_tree = self._make_tree_tab(
            "缺失素材", ("category", "asset_name", "status", "source_id", "source_name", "location"),
            (80, 200, 220, 100, 160, 500),
            ("类别", "素材名", "状态", "来源", "地图/对象名", "具体位置"),
        )
        unused_frame, self.unused_tree = self._make_unused_tab()
        self.notebook.add(unused_frame, text="未使用素材")
        self.warning_text = tk.Text(self.notebook, wrap="word", state="disabled")
        self.notebook.add(self.warning_text, text="说明与警告")

        bottom = ttk.Frame(self, padding=(8, 0, 8, 8))
        bottom.pack(fill="x")
        self.progress = ttk.Progressbar(bottom, mode="indeterminate", length=180)
        self.progress.pack(side="left", padx=(0, 8))
        ttk.Label(bottom, textvariable=self.status_var).pack(side="left", fill="x", expand=True)
        ttk.Label(bottom, text=f"v{VERSION}").pack(side="right")

    def _make_tree_tab(self, title, columns, widths, labels, asset_actions=False):
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text=title)
        toolbar = ttk.Frame(frame, padding=4)
        toolbar.pack(fill="x")
        ttk.Label(toolbar, text="筛选：").pack(side="left")
        search = ttk.Entry(toolbar, textvariable=self.search_var)
        search.pack(side="left", fill="x", expand=True)
        search.bind("<KeyRelease>", lambda _e: self.schedule_refresh_views())
        if asset_actions:
            ttk.Button(toolbar, text="打开素材位置", command=lambda: self.open_selected_asset_location(tree)).pack(side="left", padx=(6, 2))
            ttk.Button(toolbar, text="复制文件名", command=lambda: self.copy_selected_asset_names(tree)).pack(side="left", padx=2)
            ttk.Button(toolbar, text="复制文件路径", command=lambda: self.copy_selected_asset_paths(tree)).pack(side="left", padx=2)
        tree_frame = ttk.Frame(frame)
        tree_frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="extended")
        for column, width, label in zip(columns, widths, labels):
            tree.heading(column, text=label, command=lambda c=column, t=tree: self.sort_tree(t, c, False))
            tree.column(column, width=width, minwidth=50, stretch=True)
        ybar = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
        xbar = ttk.Scrollbar(tree_frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        tree.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        if asset_actions:
            tree.bind("<Double-1>", lambda _e, t=tree: self.open_selected_asset_location(t))
        else:
            tree.bind("<Double-1>", self.open_selected_source)
        return tree

    def _make_unused_tab(self):
        frame = ttk.Frame(self.notebook)
        toolbar = ttk.Frame(frame, padding=4)
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="全选未使用素材", command=self.select_all_unused).pack(side="left", padx=(0, 2))
        ttk.Button(toolbar, text="全不选", command=self.clear_unused_selection).pack(side="left", padx=2)
        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=5)
        ttk.Button(toolbar, text="移到备份文件夹（推荐）", command=self.backup_selected_unused).pack(side="left", padx=(0, 4))
        ttk.Button(toolbar, text="永久删除所选", command=self.delete_selected_unused).pack(side="left", padx=4)
        ttk.Button(toolbar, text="打开文件位置", command=self.open_unused_location).pack(side="left", padx=4)
        ttk.Label(toolbar, textvariable=self.unused_selection_var).pack(side="left", padx=(8, 0))
        hintbar = ttk.Frame(frame, padding=(4, 0, 4, 4))
        hintbar.pack(fill="x")
        ttk.Label(hintbar, text="筛选：").pack(side="left")
        unused_search = ttk.Entry(hintbar, textvariable=self.search_var, width=28)
        unused_search.pack(side="left", fill="x", expand=True, padx=(0, 8))
        unused_search.bind("<KeyRelease>", lambda _e: self.schedule_refresh_views())
        self.cleanup_hint = ttk.Label(hintbar, text="只有全工程总检查完成后才允许清理。")
        self.cleanup_hint.pack(side="right")
        tree_frame = ttk.Frame(frame)
        tree_frame.pack(fill="both", expand=True)
        columns = ("category", "name", "extension", "size", "path")
        labels = ("类别", "素材名", "扩展名", "大小（字节）", "路径")
        widths = (90, 230, 70, 100, 620)
        tree = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="extended")
        for c, label, width in zip(columns, labels, widths):
            tree.heading(c, text=label, command=lambda col=c, t=tree: self.sort_tree(t, col, False))
            tree.column(c, width=width, minwidth=50, stretch=True)
        ybar = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
        xbar = ttk.Scrollbar(tree_frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        tree.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        tree.bind("<Double-1>", lambda _e: self.open_unused_location())
        tree.bind("<<TreeviewSelect>>", lambda _e: self.update_unused_selection_count())
        return frame, tree

    def choose_project(self) -> None:
        folder = filedialog.askdirectory(title="选择 RPG Maker 2000/2003 工程文件夹")
        if folder:
            self.project_var.set(folder)
            self.load_project()

    def choose_parser(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 lcf2xml",
            filetypes=[("Windows 可执行文件", "*.exe"), ("所有文件", "*.*")],
        )
        if path:
            self.parser_var.set(path)

    def add_external_root(self) -> None:
        folder = filedialog.askdirectory(title="选择 RTP 或其他外部素材根目录")
        if folder:
            path = Path(folder).resolve()
            if path not in self.external_roots:
                self.external_roots.append(path)
            self.status_var.set("外部素材目录：" + "；".join(str(p) for p in self.external_roots))

    def load_project(self) -> None:
        value = self.project_var.get().strip()
        if not value:
            messagebox.showwarning(APP_NAME, "请先选择工程文件夹。")
            return
        root = Path(value)
        if not (root / "RPG_RT.ldb").exists() or not (root / "RPG_RT.lmt").exists():
            messagebox.showerror(APP_NAME, "所选文件夹缺少 RPG_RT.ldb 或 RPG_RT.lmt。")
            return
        self.project_root = root.resolve()
        self.result = None
        self.full_scope_result = False
        self.map_infos = []
        self.map_list.delete(0, "end")
        parser = find_lcf2xml(self.base_dir, Path(self.parser_var.get()) if self.parser_var.get() else None)
        if parser and not self.force_fallback_var.get():
            try:
                map_encoding = self.map_encoding_var.get().strip() or "932"
                cache = self.project_root / ".rm_asset_auditor_cache" / ("map_" + map_encoding.replace("/", "_"))
                converted = convert_lcf_files(
                    parser, [self.project_root / "RPG_RT.lmt"], cache,
                    engine=self._resolved_engine(), encoding=map_encoding,
                )
                self.map_infos, _ = parse_map_tree(converted[self.project_root / "RPG_RT.lmt"])
            except Exception as exc:
                self.status_var.set(f"地图树读取失败，改用地图文件名：{exc}")
        if not self.map_infos:
            for path in sorted(self.project_root.glob("Map[0-9][0-9][0-9][0-9].lmu")):
                map_id = safe_int(path.stem[3:])
                self.map_infos.append(MapInfo(map_id, path.name, ""))
        for info in self.map_infos:
            indent = "　" * max(0, info.indentation - 1)
            self.map_list.insert("end", indent + info.display_name)
        self.select_all_maps()
        asset_count = len(discover_assets(self.project_root))
        mode_text = "精确解析可用" if parser and not self.force_fallback_var.get() else "快速扫描模式"
        self.status_var.set(f"已读取 {len(self.map_infos)} 个地图条目、{asset_count} 个工程素材；{mode_text}。")

    def _resolved_engine(self) -> str:
        if self.engine_var.get() in {"2k", "2k3"}:
            return self.engine_var.get()
        if self.project_root:
            return detect_engine(self.project_root)
        return "2k3"

    def select_all_maps(self) -> None:
        if self.map_list.size():
            self.map_list.selection_set(0, "end")

    def clear_map_selection(self) -> None:
        self.map_list.selection_clear(0, "end")

    def select_all_categories(self) -> None:
        for variable in self.detail_category_vars.values():
            variable.set(True)

    def clear_all_categories(self) -> None:
        for variable in self.detail_category_vars.values():
            variable.set(False)

    def restore_default_categories(self) -> None:
        defaults = set(DEFAULT_DETAIL_CATEGORIES)
        for category, variable in self.detail_category_vars.items():
            variable.set(category in defaults)

    def _selected_map_ids(self, default_all: bool = True) -> set[int]:
        indices = list(self.map_list.curselection())
        if not indices and default_all:
            return {m.map_id for m in self.map_infos}
        return {self.map_infos[i].map_id for i in indices if i < len(self.map_infos)}

    def run_full_scan(self) -> None:
        if not self._ensure_project():
            return
        self.current_categories = set(RESOURCE_CATEGORIES)
        self._start_analysis(selected_map_ids=None, include_database=True, full_scope=True)

    def run_detailed_scan(self) -> None:
        if not self._ensure_project():
            return
        categories = {c for c, v in self.detail_category_vars.items() if v.get()}
        if not categories:
            messagebox.showwarning(APP_NAME, "请至少选择一个详细检查类别。")
            return
        self.current_categories = categories
        selected = self._selected_map_ids(default_all=True)
        all_ids = {m.map_id for m in self.map_infos if m.map_type == 1 or not m.name}
        full_scope = self.include_db_var.get() and selected == all_ids and categories == set(RESOURCE_CATEGORIES)
        self._start_analysis(selected, self.include_db_var.get(), full_scope)

    def _ensure_project(self) -> bool:
        if self.project_root is None or Path(self.project_var.get()).resolve() != self.project_root:
            self.load_project()
        return self.project_root is not None

    def _start_analysis(self, selected_map_ids, include_database, full_scope) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo(APP_NAME, "已有检查任务正在运行。")
            return
        parser_value = self.parser_var.get().strip()
        parser = Path(parser_value) if parser_value else None
        self.full_scope_result = full_scope
        self.analysis_started_at = time.monotonic()
        self.analysis_progress_text = "准备检查"
        self.progress.start(10)
        self.status_var.set("正在检查：准备检查……")
        self._set_busy(True)

        def progress(text: str) -> None:
            self.task_queue.put(("progress", text))

        def work() -> None:
            try:
                result = analyze_project(
                    project_root=self.project_root,
                    selected_map_ids=selected_map_ids,
                    include_database=include_database,
                    external_roots=self.external_roots,
                    lcf2xml=parser,
                    app_dir=self.base_dir,
                    engine=self._resolved_engine(),
                    map_encoding=self.map_encoding_var.get().strip() or "932",
                    event_encoding=self.event_encoding_var.get().strip() or "932",
                    filename_encoding=self.filename_encoding_var.get().strip() or "932",
                    force_fallback=self.force_fallback_var.get(),
                    progress=progress,
                )
                self.task_queue.put(("done", result, full_scope))
            except Exception as exc:
                self.task_queue.put(("error", exc, traceback.format_exc()))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _set_busy(self, busy: bool) -> None:
        cursor = "watch" if busy else ""
        self.configure(cursor=cursor)

    def _poll_queue(self) -> None:
        try:
            while True:
                item = self.task_queue.get_nowait()
                kind = item[0]
                if kind == "progress":
                    self.analysis_progress_text = item[1]
                elif kind == "done":
                    self.progress.stop()
                    self._set_busy(False)
                    self.result = item[1]
                    self.full_scope_result = item[2]
                    self.status_var.set("分析完成，正在整理并显示结果……")
                    self.update_idletasks()
                    self.refresh_views()
                    mode = "精确解析" if self.result.mode == "exact" else "快速近似扫描"
                    completeness = "完整" if self.result.complete_scan else "部分地图转换失败，结果不完整"
                    self.status_var.set(
                        f"检查完成（{mode}，{completeness}）：{len(self.result.usage_summary())} 个去重素材名，"
                        f"{len(self.result.deduplicated_references())} 条引用，"
                        f"{len(self.result.missing_references())} 条缺失引用，"
                        f"{len(self.result.unused_assets())} 个未使用文件。"
                    )
                    self.analysis_started_at = None
                    self.analysis_progress_text = ""
                elif kind == "error":
                    self.progress.stop()
                    self._set_busy(False)
                    self.analysis_started_at = None
                    self.analysis_progress_text = ""
                    exc, details = item[1], item[2]
                    self.status_var.set("检查失败。")
                    messagebox.showerror(APP_NAME, f"{exc}\n\n详细信息：\n{details[-1600:]}")
        except queue.Empty:
            pass
        if self.analysis_started_at is not None and self.worker and self.worker.is_alive():
            elapsed = int(time.monotonic() - self.analysis_started_at)
            minutes, seconds = divmod(elapsed, 60)
            current = self.analysis_progress_text or "处理中"
            self.status_var.set(f"正在检查（已用时 {minutes:02d}:{seconds:02d}）：{current}")
        self.after(100, self._poll_queue)

    def schedule_refresh_views(self) -> None:
        """Debounce search filtering so every keypress does not rebuild huge tables."""
        if self.refresh_after_id is not None:
            try:
                self.after_cancel(self.refresh_after_id)
            except tk.TclError:
                pass
        self.refresh_after_id = self.after(250, self._run_scheduled_refresh)

    def _run_scheduled_refresh(self) -> None:
        self.refresh_after_id = None
        self.refresh_views()

    def refresh_views(self) -> None:
        for tree in (self.summary_tree, self.detail_tree, self.missing_tree, self.unused_tree):
            tree.delete(*tree.get_children())
        if not self.result:
            return
        categories = self.current_categories
        query = self.search_var.get().strip().casefold()

        for row in self.result.usage_summary(categories):
            values = (
                row["category"], row["asset_name"], row["file_names"], row["file_status"], row["file_paths"],
                row["reference_count"], row["sources"], row["confidence"],
            )
            if self._matches(values, query):
                self.summary_tree.insert("", "end", values=values)

        for ref in self.result.deduplicated_references():
            if ref.category not in categories:
                continue
            file_info = self.result.asset_file_info(ref.category, ref.asset_name)
            values = (
                ref.category, ref.asset_name, file_info["file_names"], file_info["file_status"], file_info["file_paths"],
                ref.source_id, ref.source_name, ref.location, ref.command_name, ref.confidence,
            )
            if self._matches(values, query):
                self.detail_tree.insert("", "end", values=values, tags=(ref.raw_source,))

        for row in self.result.missing_references(categories):
            values = (row["category"], row["asset_name"], row["status"], row["source_id"], row["source_name"], row["location"])
            if self._matches(values, query):
                self.missing_tree.insert("", "end", values=values)

        for asset in self.result.unused_assets(categories):
            values = (asset.category, asset.name, asset.extension, asset.size, asset.path)
            if self._matches(values, query):
                self.unused_tree.insert("", "end", iid=asset.path, values=values)
        self.update_unused_selection_count()

        self.warning_text.configure(state="normal")
        self.warning_text.delete("1.0", "end")
        if self.result.mode == "fallback":
            self.warning_text.insert("end", "【当前模式】快速字符串扫描（近似）\n\n")
        else:
            self.warning_text.insert("end", "【当前模式】liblcf/lcf2xml 精确解析\n\n")
        if self.result.encoding_settings:
            enc = self.result.encoding_settings
            self.warning_text.insert(
                "end",
                "编码设置：地图名=" + enc.get("map", "")
                + "；事件/数据库文字=" + enc.get("event_database", "")
                + "；素材文件名=" + enc.get("filename", "") + "\n\n",
            )
        if self.external_roots:
            self.warning_text.insert("end", "外部/RTP目录：\n" + "\n".join(str(p) for p in self.external_roots) + "\n\n")
        for warning in self.result.warnings:
            self.warning_text.insert("end", "• " + warning + "\n")
        if not self.result.warnings:
            self.warning_text.insert("end", "没有额外警告。\n")
        self.warning_text.insert(
            "end",
            "\n说明：RTP/外部素材与工程目录素材分开判断。默认的“项目目录缺失”不会自动视为 RTP。"
            "清理功能只根据当前扫描得到的静态引用判断；动态文件名和脚本/补丁扩展需要人工复核。",
        )
        self.warning_text.configure(state="disabled")
        if self.full_scope_result and self.result.mode == "exact" and self.result.complete_scan:
            self.cleanup_hint.configure(text="可清理；仍建议先移到备份文件夹。")
        elif self.result.mode == "exact" and not self.result.complete_scan:
            self.cleanup_hint.configure(text="清理已锁定：有地图转换失败，检查结果不完整。")
        else:
            self.cleanup_hint.configure(text="清理已锁定：需先完成精确的全工程总检查。")

    @staticmethod
    def _matches(values, query: str) -> bool:
        return not query or query in " ".join(str(v) for v in values).casefold()

    @staticmethod
    def sort_tree(tree: ttk.Treeview, column: str, reverse: bool) -> None:
        data = [(tree.set(item, column), item) for item in tree.get_children("")]
        def key(item):
            value = item[0]
            try:
                return (0, float(value))
            except ValueError:
                return (1, value.casefold())
        data.sort(key=key, reverse=reverse)
        for index, (_value, item) in enumerate(data):
            tree.move(item, "", index)
        tree.heading(column, command=lambda: AuditorApp.sort_tree(tree, column, not reverse))

    def select_all_unused(self) -> None:
        items = self.unused_tree.get_children("")
        if items:
            self.unused_tree.selection_set(*items)
            self.unused_tree.focus(items[0])
        self.update_unused_selection_count()

    def clear_unused_selection(self) -> None:
        self.unused_tree.selection_remove(*self.unused_tree.selection())
        self.update_unused_selection_count()

    def update_unused_selection_count(self) -> None:
        self.unused_selection_var.set(f"已选择 {len(self.unused_tree.selection())} 项")

    @staticmethod
    def _tree_asset_keys(tree: ttk.Treeview) -> list[tuple[str, str]]:
        keys: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for item in tree.selection():
            category = str(tree.set(item, "category"))
            asset_name = str(tree.set(item, "asset_name"))
            key = (category, asset_name)
            if category and asset_name and key not in seen:
                seen.add(key)
                keys.append(key)
        return keys

    def _selected_matching_assets(self, tree: ttk.Treeview):
        if not self.result:
            return []
        assets = []
        seen: set[str] = set()
        for category, asset_name in self._tree_asset_keys(tree):
            for asset in self.result.matching_assets(category, asset_name):
                if asset.path not in seen:
                    seen.add(asset.path)
                    assets.append(asset)
        return assets

    def open_selected_asset_location(self, tree: ttk.Treeview) -> None:
        keys = self._tree_asset_keys(tree)
        if not keys:
            messagebox.showwarning(APP_NAME, "请先在当前页面选择一项调用素材。")
            return
        assets = self._selected_matching_assets(tree)
        if not assets:
            messagebox.showwarning(APP_NAME, "所选调用素材没有找到实际工程文件或外部/RTP文件。")
            return
        self._open_in_file_manager(Path(assets[0].path))
        if len(assets) > 1:
            self.status_var.set(f"所选素材共对应 {len(assets)} 个实际文件；已打开第一个文件的位置。")

    def _copy_text(self, values: list[str], success_message: str) -> None:
        unique = list(dict.fromkeys(value for value in values if value))
        if not unique:
            messagebox.showwarning(APP_NAME, "没有可复制的内容。")
            return
        self.clipboard_clear()
        self.clipboard_append("\n".join(unique))
        self.update_idletasks()
        self.status_var.set(success_message.format(count=len(unique)))

    def copy_selected_asset_names(self, tree: ttk.Treeview) -> None:
        keys = self._tree_asset_keys(tree)
        if not keys:
            messagebox.showwarning(APP_NAME, "请先在当前页面选择一项调用素材。")
            return
        names: list[str] = []
        if self.result:
            for category, asset_name in keys:
                matches = self.result.matching_assets(category, asset_name)
                if matches:
                    names.extend(Path(asset.path).name for asset in matches)
                else:
                    names.append(asset_name)
        self._copy_text(names, "已复制 {count} 个文件名。")

    def copy_selected_asset_paths(self, tree: ttk.Treeview) -> None:
        if not self._tree_asset_keys(tree):
            messagebox.showwarning(APP_NAME, "请先在当前页面选择一项调用素材。")
            return
        paths = [asset.path for asset in self._selected_matching_assets(tree)]
        if not paths:
            messagebox.showwarning(APP_NAME, "所选调用素材没有找到可复制的实际文件路径。")
            return
        self._copy_text(paths, "已复制 {count} 条实际文件路径。")

    def export_report(self) -> None:
        if not self.result:
            messagebox.showwarning(APP_NAME, "请先完成一次检查。")
            return
        folder = filedialog.askdirectory(title="选择报告输出文件夹")
        if not folder:
            return
        paths = export_full_report(self.result, Path(folder), self.current_categories)
        messagebox.showinfo(APP_NAME, "已生成：\n" + "\n".join(str(p) for p in paths))

    def _selected_unused_assets(self):
        if not self.result:
            return []
        selected_paths = set(self.unused_tree.selection())
        index = {asset.path: asset for asset in self.result.unused_assets(self.current_categories)}
        return [index[p] for p in selected_paths if p in index]

    def _cleanup_allowed(self) -> bool:
        if (
            not self.result or not self.full_scope_result
            or self.result.mode != "exact" or not self.result.complete_scan
        ):
            messagebox.showwarning(
                APP_NAME,
                "为避免误删，只有所有地图均成功转换并完成“精确解析”的“全工程总检查”后才允许清理。",
            )
            return False
        return True

    def backup_selected_unused(self) -> None:
        if not self._cleanup_allowed():
            return
        assets = self._selected_unused_assets()
        if not assets:
            messagebox.showwarning(APP_NAME, "请在“未使用素材”页选择文件。")
            return
        if not messagebox.askyesno(APP_NAME, f"将 {len(assets)} 个文件移到工程内的时间戳备份文件夹？"):
            return
        backup, moved = move_assets_to_backup(assets, self.project_root)
        messagebox.showinfo(APP_NAME, f"已移动 {len(moved)} 个文件到：\n{backup}\n\n同时生成 move_manifest.csv。")
        self.run_full_scan()

    def delete_selected_unused(self) -> None:
        if not self._cleanup_allowed():
            return
        assets = self._selected_unused_assets()
        if not assets:
            messagebox.showwarning(APP_NAME, "请在“未使用素材”页选择文件。")
            return
        if not messagebox.askyesno(APP_NAME, f"将永久删除 {len(assets)} 个文件。此操作不可撤销，是否继续？"):
            return
        if not messagebox.askyesno(APP_NAME, "最后确认：确实永久删除所选素材？建议优先使用“移到备份文件夹”。"):
            return
        deleted = permanently_delete_assets(assets, self.project_root)
        messagebox.showinfo(APP_NAME, f"已永久删除 {len(deleted)} 个文件。")
        self.run_full_scan()

    def open_unused_location(self) -> None:
        selection = self.unused_tree.selection()
        if not selection:
            return
        path = Path(selection[0])
        self._open_in_file_manager(path)

    def open_selected_source(self, _event=None) -> None:
        tree = self.focus_get()
        if not isinstance(tree, ttk.Treeview):
            return
        selection = tree.selection()
        if not selection:
            return
        values = tree.item(selection[0], "values")
        source_id = values[2] if len(values) > 2 else ""
        if not self.project_root:
            return
        if str(source_id).startswith("Map"):
            path = self.project_root / f"{source_id}.lmu"
        elif source_id == "RPG_RT.ldb":
            path = self.project_root / "RPG_RT.ldb"
        else:
            path = self.project_root
        self._open_in_file_manager(path)

    @staticmethod
    def _open_in_file_manager(path: Path) -> None:
        try:
            if sys.platform.startswith("win"):
                if path.is_file():
                    os.system(f'explorer /select,"{path}"')
                else:
                    os.startfile(str(path))
            elif sys.platform == "darwin":
                os.system(f'open -R "{path}"')
            else:
                os.system(f'xdg-open "{path.parent if path.is_file() else path}"')
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"无法打开位置：{exc}")


def run_cli(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    parser = Path(args.lcf2xml).resolve() if args.lcf2xml else None
    categories = set(args.category) if args.category else set(RESOURCE_CATEGORIES)
    maps = {int(x) for x in args.maps.split(",") if x.strip()} if args.maps else None
    external_roots = [Path(x).resolve() for x in (args.external_root or [])]
    result = analyze_project(
        project_root=root,
        selected_map_ids=maps,
        include_database=not args.no_database,
        external_roots=external_roots,
        lcf2xml=parser,
        app_dir=app_base_dir(),
        engine=None if args.engine == "auto" else args.engine,
        encoding=args.encoding,
        map_encoding=args.map_encoding,
        event_encoding=args.event_encoding,
        filename_encoding=args.filename_encoding,
        force_fallback=args.fallback,
        progress=lambda text: print(text, file=sys.stderr),
    )
    if args.output:
        paths = export_full_report(result, Path(args.output), categories)
        for path in paths:
            print(path)
    else:
        payload = result.to_dict(categories)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--project", help="RPG Maker 2000/2003 工程文件夹")
    parser.add_argument("--lcf2xml", help="lcf2xml.exe 路径")
    parser.add_argument("--engine", choices=("auto", "2k", "2k3"), default="auto")
    parser.add_argument(
        "--encoding",
        help="兼容旧版：同时设置地图名、事件/数据库文字和素材文件名编码",
    )
    parser.add_argument("--map-encoding", default="932", help="RPG_RT.lmt 地图名编码")
    parser.add_argument("--event-encoding", default="932", help="LMU 事件名及 LDB 对象名编码")
    parser.add_argument("--filename-encoding", default="932", help="素材文件名引用编码")
    parser.add_argument("--maps", help="逗号分隔的地图编号，例如 1,2,6")
    parser.add_argument("--no-database", action="store_true")
    parser.add_argument("--external-root", action="append", help="RTP 或其他外部素材根目录；可重复指定")
    parser.add_argument("--fallback", action="store_true", help="强制快速近似扫描")
    parser.add_argument("--category", action="append", choices=tuple(RESOURCE_CATEGORIES))
    parser.add_argument("--output", help="输出报告文件夹")
    parser.add_argument("--initial-project", help="启动 GUI 时预填工程文件夹")
    parser.add_argument("--initial-lcf2xml", help="启动 GUI 时预填 lcf2xml.exe")
    parser.add_argument("--initial-engine", choices=("auto", "2k", "2k3"))
    parser.add_argument("--initial-map-encoding")
    parser.add_argument("--initial-event-encoding")
    parser.add_argument("--initial-filename-encoding")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    if args.project:
        try:
            return run_cli(args)
        except (AnalysisError, OSError, ValueError) as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 2
    app = AuditorApp(args)
    if args.initial_project:
        app.after_idle(app.load_project)
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
