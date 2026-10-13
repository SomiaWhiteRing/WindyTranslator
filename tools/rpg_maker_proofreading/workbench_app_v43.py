from __future__ import annotations

import csv
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk


from workbench_app_v42 import RPGMakerProofreadingApp as V42App
from workbench_core import (
    BATCH_QA_CHECK_LABELS,
    DataRecord,
    DictionaryEntry,
    DictionaryWarning,
    EditableDictionaryRow,
    EllipsisMatchProposal,
    ErrorAlias,
    QAError,
    SearchOptions,
    TextChange,
    VersionDiffRow,
    analyze_dictionary,
    analyze_ellipsis_occurrences,
    analyze_missing_translations,
    analyze_space_dunhao,
    build_ellipsis_conversion,
    build_ellipsis_match_to_source,
    build_error_alias_changes,
    build_text_replacement_changes,
    compare_translation_versions,
    dictionary_entries_from_rows,
    load_config,
    save_config,
    save_dictionary,
)

APP_TITLE = "RPG制作大师校对工具 v4.3.0"
CONFIG_NAME = "rpg_maker_proofreading_tool_config_v42.json"  # preserve existing user settings


class RPGMakerProofreadingApp(V42App):
    def __init__(self):
        self.current_dict_entries: list[DictionaryEntry] = []
        self.current_dict_warnings: list[DictionaryWarning] = []
        self.dict_review_states: dict[str, str] = {}
        self.error_aliases: list[ErrorAlias] = []
        self.search_replace_changes: dict[str, TextChange] = {}
        super().__init__()
        self.title(APP_TITLE)

    # ------------------------------------------------------------------
    # Search + translation replacement
    # ------------------------------------------------------------------
    def _build_search(self):
        super()._build_search()
        tab = self.tabs["search"]
        tab.rowconfigure(4, weight=1)
        cols = tuple(self.search_tree["columns"])
        if "replace_preview" not in cols:
            self.search_tree.configure(columns=cols + ("replace_preview",))
            self.search_tree.heading("replace_preview", text="替换预览")
            self.search_tree.column("replace_preview", width=520, minwidth=0, anchor="w")
            meta = self.tree_registry.get(self.search_tree)
            if meta:
                meta["columns"] = cols + ("replace_preview",)
                meta["headings"]["replace_preview"] = "替换预览"
                meta["widths"]["replace_preview"] = 520

        box = ttk.LabelFrame(tab, text="文本替换（只修改译文；只处理当前列表中已勾选且可见的记录）", padding=5)
        box.grid(row=5, column=0, sticky="ew", pady=(6, 0))
        row1 = ttk.Frame(box); row1.pack(fill="x")
        self.replace_find = tk.StringVar(); self.replace_with = tk.StringVar()
        ttk.Label(row1, text="查找：").pack(side="left")
        ttk.Entry(row1, textvariable=self.replace_find, width=30).pack(side="left", padx=(0, 4))
        ttk.Button(row1, text="使用当前查询词", command=lambda: self.replace_find.set(self._search_query_value())).pack(side="left")
        ttk.Label(row1, text="替换为：").pack(side="left", padx=(10, 2))
        ttk.Entry(row1, textvariable=self.replace_with, width=30).pack(side="left", padx=(0, 6))
        ttk.Button(row1, text="预览勾选替换", command=self._preview_search_replace).pack(side="left")
        ttk.Button(row1, text="应用勾选替换", command=self._apply_search_replace).pack(side="left", padx=4)

        row2 = ttk.Frame(box); row2.pack(fill="x", pady=(5, 0))
        self.replace_source_exclude_on = tk.BooleanVar(value=False); self.replace_source_exclude = tk.StringVar()
        self.replace_full_exclude_on = tk.BooleanVar(value=False); self.replace_full_exclude = tk.StringVar()
        ttk.Checkbutton(row2, text="原文包含以下内容时不替换", variable=self.replace_source_exclude_on).pack(side="left")
        ttk.Entry(row2, textvariable=self.replace_source_exclude, width=28).pack(side="left", padx=(3, 10))
        ttk.Checkbutton(row2, text="本条原文或译文全文包含以下内容时不替换", variable=self.replace_full_exclude_on).pack(side="left")
        ttk.Entry(row2, textvariable=self.replace_full_exclude, width=28).pack(side="left", padx=3)

    def _preview_search_replace(self):
        old = self.replace_find.get()
        if not old:
            messagebox.showwarning("文本替换", "请填写替换查找词，或点击“使用当前查询词”。", parent=self); return
        iids = self._checked_iids(self.search_tree)
        records = []
        for iid in iids:
            rec = self.result_maps.get("search", {}).get(iid)
            if isinstance(rec, DataRecord): records.append(rec)
        if not records:
            messagebox.showinfo("文本替换", "请先在查询结果中勾选需要替换的可见文本。", parent=self); return
        try:
            changes = build_text_replacement_changes(
                records, old, self.replace_with.get(),
                source_exclude=self.replace_source_exclude.get() if self.replace_source_exclude_on.get() else "",
                full_exclude=self.replace_full_exclude.get() if self.replace_full_exclude_on.get() else "",
                case_sensitive=self.search_case.get(),
            )
            self.search_replace_changes = {x.record.uid: x for x in changes}
            for iid in self.search_tree.get_children(""):
                rec = self.result_maps.get("search", {}).get(iid)
                change = self.search_replace_changes.get(rec.uid) if isinstance(rec, DataRecord) else None
                self.search_tree.set(iid, "replace_preview", self._display(change.proposed) if change else "")
            self.status_var.set(f"文本替换预览：{len(changes)} 条会发生修改；排除或无匹配的记录保持不变。")
        except Exception as exc:
            messagebox.showerror("替换预览失败", str(exc), parent=self)

    def _apply_search_replace(self):
        checked_uids = set()
        for iid in self._checked_iids(self.search_tree):
            rec = self.result_maps.get("search", {}).get(iid)
            if isinstance(rec, DataRecord): checked_uids.add(rec.uid)
        updates = {uid: (x.record, x.proposed) for uid, x in self.search_replace_changes.items() if uid in checked_uids}
        if self._save_updates_v42(updates, "文本替换"):
            self.search_replace_changes.clear(); self._do_search()

    # ------------------------------------------------------------------
    # Ellipsis: source-matched translation correction
    # ------------------------------------------------------------------
    def _build_ellipsis_panel(self):
        tab = self.punct_tabs["ellipsis"]; tab.rowconfigure(3, weight=1); tab.columnconfigure(0, weight=1)
        top = ttk.Frame(tab); top.grid(row=0, column=0, sticky="ew")
        self.ellipsis_side = tk.StringVar(value="both"); self.ellipsis_kind = tk.StringVar(value="全部")
        ttk.Label(top, text="检查：").pack(side="left")
        ttk.Combobox(top, textvariable=self.ellipsis_side, state="readonly", values=["original", "translated", "both"], width=11).pack(side="left")
        ttk.Combobox(top, textvariable=self.ellipsis_kind, state="readonly", values=["全部", "连续点", "省略号"], width=9).pack(side="left", padx=4)
        ttk.Button(top, text="分类检查", command=self._do_ellipsis_scan).pack(side="left")
        ttk.Button(top, text="全选当前可处理项", command=lambda: self._check_all(self.ellipsis_tree, True, lambda i, v: v[8] != "是")).pack(side="left", padx=(12, 3))
        ttk.Button(top, text="全不选", command=lambda: self._check_all(self.ellipsis_tree, False)).pack(side="left")
        ttk.Button(top, text="应用勾选预览", command=lambda: self._apply_generic_changes("ellipsis_v4")).pack(side="right")

        conv = ttk.LabelFrame(tab, text="转换规则", padding=4); conv.grid(row=1, column=0, sticky="ew", pady=(5, 0))
        r1 = ttk.Frame(conv); r1.pack(fill="x")
        ttk.Label(r1, text="转换：").pack(side="left")
        self.ellipsis_direction = tk.StringVar(value="根据原文匹配译文")
        ttk.Combobox(r1, textvariable=self.ellipsis_direction, state="readonly",
                     values=["根据原文匹配译文", "连续点→省略号", "省略号→连续点", "单省略号→双省略号", "省略号压缩"], width=20).pack(side="left")
        ttk.Button(r1, text="预览勾选转换", command=self._preview_ellipsis).pack(side="left", padx=6)
        ttk.Label(r1, text="“根据原文匹配译文”只有在原文/译文省略号段数与顺序可一一对应时才允许自动处理。", foreground="#555").pack(side="left", padx=8)

        r2 = ttk.Frame(conv); r2.pack(fill="x", pady=(4, 0))
        ttk.Label(r2, text="连续点→省略号：每").pack(side="left")
        self.ellipsis_group_size = tk.IntVar(value=3)
        ttk.Spinbox(r2, from_=1, to=12, textvariable=self.ellipsis_group_size, width=4).pack(side="left")
        ttk.Label(r2, text="点对应一个省略号；多余点：").pack(side="left")
        self.ellipsis_remainder = tk.StringVar(value="删除")
        ttk.Combobox(r2, textvariable=self.ellipsis_remainder, state="readonly", values=["删除", "一个省略号"], width=10).pack(side="left")
        ttk.Label(r2, text="；需要转换的省略号为：").pack(side="left")
        self.ellipsis_style = tk.StringVar(value="…")
        ttk.Combobox(r2, textvariable=self.ellipsis_style, state="readonly", values=["…", "……"], width=6).pack(side="left")
        ttk.Label(r2, text="（默认：3点→单省略号 …）").pack(side="left", padx=5)

        r3 = ttk.Frame(conv); r3.pack(fill="x", pady=(4, 0))
        ttk.Label(r3, text="省略号→连续点：每一个省略号对应").pack(side="left")
        self.ellipsis_to_dot_count = tk.IntVar(value=3)
        ttk.Spinbox(r3, from_=1, to=12, textvariable=self.ellipsis_to_dot_count, width=4).pack(side="left")
        ttk.Label(r3, text="点；选择点：").pack(side="left")
        self.ellipsis_dot_style = tk.StringVar(value=".")
        ttk.Combobox(r3, textvariable=self.ellipsis_dot_style, values=[".", "．", "·", "・", "。"], width=5).pack(side="left")
        ttk.Label(r3, text="；省略号压缩最大连续个数：").pack(side="left", padx=(10, 2))
        self.ellipsis_max = tk.IntVar(value=2)
        ttk.Spinbox(r3, from_=1, to=12, textvariable=self.ellipsis_max, width=4).pack(side="left")

        ttk.Label(tab, text="连续点=至少两个完全相同的点连续出现；单个点不算连续点；省略号字符 … 本身不算连续点。",
                  foreground="#555").grid(row=2, column=0, sticky="w", pady=4)
        self.ellipsis_tree = self._tree(tab,
            ("use", "side", "kind", "style", "count", "control", "match", "file", "manual", "reason", "original", "translated", "proposed"),
            ("选择", "文本侧", "类型", "形式", "数量", "夹操作符", "原译排序匹配", "文件", "仅手动", "说明", "原文", "译文", "处理结果"),
            (60, 70, 90, 100, 60, 80, 100, 200, 80, 260, 340, 340, 340), checkbox=True)
        self.ellipsis_tree.master.grid(row=3, column=0, sticky="nsew")
        self.ellipsis_tree.bind("<Double-1>", lambda e: self._double_to_editor_event("ellipsis_v4", self.ellipsis_tree, e))
        self.ellipsis_occurrences = {}

    def _do_ellipsis_scan(self, result=None):
        try:
            if result is None:
                records = self.active_records()
                rows = analyze_ellipsis_occurrences(records, self.ellipsis_side.get())
                match_by_uid = {r.uid: build_ellipsis_match_to_source(r,
                                group_size=self.ellipsis_group_size.get(), remainder=self.ellipsis_remainder.get(),
                                ellipsis_style=self.ellipsis_style.get()) for r in records}
                self._mark_manual_check("ellipsis")
            else:
                rows, match_by_uid = result
            if self.ellipsis_kind.get() != "全部": rows = [x for x in rows if x.kind == self.ellipsis_kind.get()]
            self.ellipsis_occurrences = {}; self.result_maps["ellipsis_v4"] = {}; self._clear_tree(self.ellipsis_tree)
            for i, row in enumerate(rows):
                iid=f"el{i}"; self.ellipsis_occurrences[iid]=row; self.result_maps["ellipsis_v4"][iid]=row.record
                m = match_by_uid[row.record.uid]
                self.ellipsis_tree.insert("", "end", iid=iid, values=("☐", row.side, row.kind, row.visible_style, row.count,
                    "是" if row.interrupted_by_control else "否", "是" if m.eligible else "否", row.record.file_key,
                    "是" if row.manual_only else "否", row.reason or m.reason, self._display(row.record.original),
                    self._display(row.record.translated), ""))
            self.status_var.set(f"省略号/连续点共 {len(rows)} 项。")
        except Exception as exc: messagebox.showerror("省略号检查失败", str(exc), parent=self)

    def _preview_ellipsis(self):
        records = {}
        for iid in self._checked_iids(self.ellipsis_tree):
            occ = self.ellipsis_occurrences.get(iid)
            if occ and occ.side == "译文" and not occ.manual_only:
                records[occ.record.uid] = occ.record
        changes: list[TextChange] = []
        for rec in records.values():
            if self.ellipsis_direction.get() == "根据原文匹配译文":
                match = build_ellipsis_match_to_source(rec, group_size=self.ellipsis_group_size.get(),
                    remainder=self.ellipsis_remainder.get(), ellipsis_style=self.ellipsis_style.get())
                if match.eligible and match.proposed is not None and match.proposed != rec.translated:
                    changes.append(TextChange(rec, match.reason, match.proposed))
            else:
                group_size = self.ellipsis_to_dot_count.get() if self.ellipsis_direction.get() == "省略号→连续点" else self.ellipsis_group_size.get()
                proposed = build_ellipsis_conversion(rec, direction=self.ellipsis_direction.get(), group_size=group_size,
                    remainder=self.ellipsis_remainder.get(), ellipsis_style=self.ellipsis_style.get(),
                    dot_style=self.ellipsis_dot_style.get(), max_ellipsis=self.ellipsis_max.get())
                if proposed is not None and proposed != rec.translated:
                    changes.append(TextChange(rec, self.ellipsis_direction.get(), proposed))
        cmap={x.record.uid:x for x in changes}; self.result_maps["ellipsis_v4_changes"] = cmap
        self.result_maps["ellipsis_v4_apply"] = cmap
        for iid, occ in self.ellipsis_occurrences.items():
            change=cmap.get(occ.record.uid)
            self.ellipsis_tree.set(iid, "proposed", self._display(change.proposed) if change else "")
        self.status_var.set(f"省略号转换预览：{len(changes)} 条译文会修改。")

    # ------------------------------------------------------------------
    # Punctuation manual checks: space source/translation consistency
    # ------------------------------------------------------------------
    def _build_terminal_panel(self):
        # Skip v4.2's old extra warning row and start from the underlying terminal panel,
        # then add the v4.3 consistency checker once.
        super(V42App, self)._build_terminal_panel()
        tab = self.punct_tabs["terminal"]
        extra = ttk.Frame(tab); extra.grid(row=3, column=0, sticky="ew", pady=(4, 0))
        ttk.Button(extra, text="检查英文句号结尾／句中顿号／原译空格一致性", command=self._do_special_format_scan).pack(side="left")
        ttk.Label(extra, text="空格只在原文与译文的半角/全角空格分布不一致时报警。", foreground="#555").pack(side="left", padx=8)

    def _do_special_format_scan(self):
        issues = analyze_space_dunhao(self.active_records())
        self._clear_tree(self.terminal_tree); self.result_maps["terminal"] = {}
        for i, issue in enumerate(issues):
            iid=f"fmt{i}"; self.result_maps["terminal"][iid]=issue.record
            self.terminal_tree.insert("", "end", iid=iid, values=("☐", issue.record.file_key,
                f"{issue.side}｜{issue.issue_type}：{issue.detail}", self._display(issue.record.original),
                self._display(issue.record.translated), "（手动检查）"))
        self.status_var.set(f"标点/空格专项警告 {len(issues)} 条。")

    # ------------------------------------------------------------------
    # Missing translation: multi-select source content types
    # ------------------------------------------------------------------
    def _build_missing(self):
        tab=self.tabs["missing"]; tab.rowconfigure(1, weight=1); tab.columnconfigure(0, weight=1)
        bar=ttk.Frame(tab); bar.grid(row=0,column=0,sticky="ew",pady=(0,5))
        ttk.Label(bar,text="原文允许包含的内容类型：").pack(side="left")
        self.missing_type_vars={k:tk.BooleanVar(value=True) for k in ["汉字","英文","数字","其它"]}
        for k,v in self.missing_type_vars.items(): ttk.Checkbutton(bar,text=k,variable=v).pack(side="left",padx=3)
        ttk.Label(bar,text="（未勾选=候选原文不得含该类型；标点/空白不计入类型）",foreground="#555").pack(side="left",padx=6)
        ttk.Button(bar,text="快速检查",command=self._do_missing).pack(side="left",padx=5)
        ttk.Button(bar,text="全选当前可见行",command=lambda:self._check_all(self.missing_tree,True)).pack(side="left",padx=(10,3))
        ttk.Button(bar,text="全不选",command=lambda:self._check_all(self.missing_tree,False)).pack(side="left")
        ttk.Button(bar,text="打开译文文件",command=lambda:self._open_selected_result("missing","translated")).pack(side="right")
        self.missing_tree=self._tree(tab,("use","file","kind","marker","reason","original","translated"),
            ("选择","文件","原文内容类型","标记","原因","原文","译文"),(60,240,150,90,240,520,520),checkbox=True)
        self.missing_tree.master.grid(row=1,column=0,sticky="nsew")
        self.missing_tree.bind("<Double-1>",lambda e:self._double_to_editor_event("missing",self.missing_tree,e))

    def _do_missing(self):
        try:
            selected={k for k,v in self.missing_type_vars.items() if v.get()}
            issues=analyze_missing_translations(self.active_records(),selected)
            self._clear_tree(self.missing_tree); self.result_maps["missing"]={}
            for i,x in enumerate(issues):
                iid=f"miss{i}"; self.result_maps["missing"][iid]=x.record
                self.missing_tree.insert("","end",iid=iid,values=("☐",x.record.file_key,x.text_kind,x.record.marker,x.reason,
                    self._display(x.record.original),self._display(x.record.translated)))
            self.status_var.set(f"疑似日文未翻译：{len(issues)} 条。")
        except Exception as exc: messagebox.showerror("缺译检查失败",str(exc),parent=self)

    # ------------------------------------------------------------------
    # Version diff: show exactly what changed
    # ------------------------------------------------------------------
    def _build_versions(self):
        super()._build_versions()
        cols=tuple(self.version_tree["columns"])
        for col,title,width in [("diff","具体修改位置",460),("inline","字符级差异",560)]:
            if col not in self.version_tree["columns"]:
                cols=tuple(self.version_tree["columns"])+(col,)
                self.version_tree.configure(columns=cols); self.version_tree.heading(col,text=title); self.version_tree.column(col,width=width,minwidth=0,anchor="w")
                meta=self.tree_registry.get(self.version_tree)
                if meta:
                    meta["columns"]=cols; meta["headings"][col]=title; meta["widths"][col]=width

    def _do_version_compare(self):
        try:
            baseline=list(self._load_version_baseline()); current=self.active_records()
            self.version_results=compare_translation_versions(current,baseline)
            self._clear_tree(self.version_tree); self.result_maps["versions"]={}
            for i,x in enumerate(self.version_results):
                iid=f"ver{i}"; self.result_maps["versions"][iid]=x.record
                self.version_tree.insert("","end",iid=iid,values=(x.record.file_key,self._display(x.record.original),
                    self._display(x.baseline_translation),self._display(x.current_translation),x.baseline_location,
                    x.record.translated_location,x.diff_summary,self._display(x.inline_diff,1200)))
            self.status_var.set(f"原文一致且译文发生修改：{len(self.version_results)} 条；已生成字符级差异。")
        except Exception as exc: messagebox.showerror("版本比较失败",str(exc),parent=self)

    def _export_version_report(self):
        if not self.version_results:
            messagebox.showinfo("版本报告","请先比较版本。",parent=self); return
        path=filedialog.asksaveasfilename(parent=self,defaultextension=".xlsx",filetypes=[("Excel","*.xlsx"),("CSV","*.csv"),("日志","*.log")])
        if not path:return
        headers=["文件","原文","旧译文","新译文","具体修改位置","字符级差异","旧版位置","新版位置"]
        rows=[[x.record.file_key,x.record.original,x.baseline_translation,x.current_translation,x.diff_summary,x.inline_diff,x.baseline_location,x.record.translated_location] for x in self.version_results]
        self._write_report(Path(path),headers,rows,"版本译文差异")
        messagebox.showinfo("导出完成",path,parent=self)

    # ------------------------------------------------------------------
    # Dictionary correction review / error-alias workflow
    # ------------------------------------------------------------------
    def _dictionary_warnings(self, entries):
        warnings = analyze_dictionary(self.active_records(),entries,self.dict_messages_only.get())
        self._mark_manual_check("dictionary")
        self.current_dict_entries=list(entries)
        return warnings

    def _show_dict_warnings(self, warnings):
        self.current_dict_warnings=list(warnings)
        super()._show_dict_warnings(warnings)

    def _build_dictcheck(self):
        super()._build_dictcheck()
        tab=self.tabs["dictcheck"]
        tools=ttk.Frame(tab); tools.grid(row=2,column=0,sticky="ew",pady=(5,0))
        ttk.Button(tools,text="逐句确认 / 错误译名修正…",command=self._open_dictionary_review).pack(side="left")
        ttk.Button(tools,text="导出错误译名列表…",command=self._export_error_aliases).pack(side="left",padx=5)
        ttk.Label(tools,text="人工确认误命中可跳过；翻错词可划选加入错误译名；新增辞典项后会重新识别。",foreground="#555").pack(side="left",padx=8)

    def _open_dictionary_review(self):
        if not self.current_dict_warnings and not self.error_aliases:
            messagebox.showinfo("辞典逐句确认","请先执行一次辞典匹配检查。",parent=self); return
        win=tk.Toplevel(self); win.title("辞典匹配逐句确认 / 错误译名修正"); win.geometry("1500x850"); win.transient(self)
        pan=ttk.PanedWindow(win,orient="horizontal"); pan.pack(fill="both",expand=True,padx=6,pady=6)
        left=ttk.Frame(pan); right=ttk.Frame(pan); pan.add(left,weight=2); pan.add(right,weight=3)
        left.rowconfigure(1,weight=1); left.columnconfigure(0,weight=1)
        top=ttk.Frame(left); top.grid(row=0,column=0,sticky="ew")
        ttk.Button(top,text="重新按当前辞典识别",command=lambda:self._review_rescan_warnings(win)).pack(side="left")
        ttk.Button(top,text="跳过当前误命中",command=lambda:self._review_skip(win)).pack(side="left",padx=4)
        self.review_tree=ttk.Treeview(left,columns=("status","term","expected","file"),show="headings",selectmode="browse")
        for c,t,w in [("status","状态",90),("term","辞典原词",180),("expected","正确译名",180),("file","文件",260)]: self.review_tree.heading(c,text=t);self.review_tree.column(c,width=w,anchor="w")
        self.review_tree.grid(row=1,column=0,sticky="nsew"); self.review_tree.bind("<<TreeviewSelect>>",lambda e:self._review_show_current())
        scr=ttk.Scrollbar(left,orient="vertical",command=self.review_tree.yview);scr.grid(row=1,column=1,sticky="ns");self.review_tree.configure(yscrollcommand=scr.set)

        right.rowconfigure(1,weight=1); right.rowconfigure(3,weight=1); right.columnconfigure(0,weight=1)
        ttk.Label(right,text="原文（可划选新辞典原词）").grid(row=0,column=0,sticky="w")
        self.review_original=tk.Text(right,height=7,wrap="word");self.review_original.grid(row=1,column=0,sticky="nsew")
        ttk.Label(right,text="译文（可划选错误译名；或划选新辞典译文）").grid(row=2,column=0,sticky="w")
        self.review_translated=tk.Text(right,height=7,wrap="word");self.review_translated.grid(row=3,column=0,sticky="nsew")
        actions=ttk.Frame(right);actions.grid(row=4,column=0,sticky="ew",pady=5)
        ttk.Button(actions,text="将译文选区加入错误译名",command=lambda:self._review_add_error_alias(win)).pack(side="left")
        ttk.Button(actions,text="将原文/译文选区新增为辞典项",command=lambda:self._review_add_dictionary_entry(win)).pack(side="left",padx=5)
        ttk.Button(actions,text="生成/刷新自动替换预览",command=lambda:self._review_refresh_auto(win)).pack(side="left",padx=5)
        ttk.Button(actions,text="应用勾选自动替换",command=lambda:self._review_apply_auto(win)).pack(side="right")

        bottom=ttk.Notebook(right);bottom.grid(row=5,column=0,sticky="nsew",pady=(5,0));right.rowconfigure(5,weight=2)
        af=ttk.Frame(bottom);pf=ttk.Frame(bottom);bottom.add(af,text="错误译名列表");bottom.add(pf,text="自动替换预览")
        af.rowconfigure(1,weight=1);af.columnconfigure(0,weight=1)
        ab=ttk.Frame(af);ab.grid(row=0,column=0,sticky="ew")
        ttk.Button(ab,text="删除选中错误译名",command=lambda:self._review_delete_alias(win)).pack(side="left")
        self.alias_tree=ttk.Treeview(af,columns=("wrong","correct","source_term","source"),show="headings",selectmode="extended")
        for c,t,w in [("wrong","错误译名",180),("correct","正确译名",180),("source_term","对应辞典原词",180),("source","来源",160)]:self.alias_tree.heading(c,text=t);self.alias_tree.column(c,width=w,anchor="w")
        self.alias_tree.grid(row=1,column=0,sticky="nsew")
        pf.rowconfigure(1,weight=1);pf.columnconfigure(0,weight=1)
        pb=ttk.Frame(pf);pb.grid(row=0,column=0,sticky="ew")
        ttk.Button(pb,text="全选当前可见行",command=lambda:self._simple_tree_check_all(self.auto_tree,True)).pack(side="left")
        ttk.Button(pb,text="全不选",command=lambda:self._simple_tree_check_all(self.auto_tree,False)).pack(side="left",padx=4)
        ttk.Label(pb,text="双击句子可返回上方人工确认并继续补充更长的错误译名。",foreground="#555").pack(side="left",padx=8)
        self.auto_tree=ttk.Treeview(pf,columns=("use","file","reason","original","translated","proposed"),show="headings",selectmode="browse")
        for c,t,w in [("use","选择",60),("file","文件",180),("reason","命中错误译名",180),("original","原文",330),("translated","当前译文",330),("proposed","替换预览",330)]:self.auto_tree.heading(c,text=t);self.auto_tree.column(c,width=w,anchor="w")
        self.auto_tree.grid(row=1,column=0,sticky="nsew");self.auto_tree.bind("<Button-1>",self._auto_toggle);self.auto_tree.bind("<Double-1>",lambda e:self._review_auto_double(e))
        self.review_warning_map={};self.auto_change_map={}
        self._review_refresh_warning_tree();self._review_refresh_alias_tree();self._review_refresh_auto(win)

    def _review_refresh_warning_tree(self):
        if not hasattr(self,"review_tree"): return
        for i in self.review_tree.get_children(""):self.review_tree.delete(i)
        self.review_warning_map={}
        for n,w in enumerate(self.current_dict_warnings):
            state=self.dict_review_states.get(w.key,"待确认")
            # Once the user has classified a warning (false hit / error alias / new entry),
            # it leaves the manual queue. The automatic preview remains available below.
            if state != "待确认": continue
            iid=f"w{n}";self.review_warning_map[iid]=w
            self.review_tree.insert("","end",iid=iid,values=(state,w.original_term,w.expected_translation,w.record.file_key))
        kids=self.review_tree.get_children("")
        if kids:self.review_tree.selection_set(kids[0]);self.review_tree.focus(kids[0]);self._review_show_current()

    def _review_show_current(self):
        sel=self.review_tree.selection() if hasattr(self,"review_tree") else ()
        if not sel:return
        w=self.review_warning_map.get(sel[0])
        if not w:return
        for box,text in [(self.review_original,w.record.original),(self.review_translated,w.record.translated)]:
            box.delete("1.0","end");box.insert("1.0",text)

    def _review_current_warning(self):
        sel=self.review_tree.selection() if hasattr(self,"review_tree") else ()
        return self.review_warning_map.get(sel[0]) if sel else None

    def _review_skip(self,win):
        w=self._review_current_warning()
        if not w:return
        self.dict_review_states[w.key]="已跳过（辞典词在此处并非目标词）";self._review_refresh_warning_tree()

    @staticmethod
    def _selected_text(widget):
        try:return widget.get("sel.first","sel.last")
        except tk.TclError:return ""

    def _review_add_error_alias(self,win):
        w=self._review_current_warning(); wrong=self._selected_text(self.review_translated).strip()
        if not w or not wrong:
            messagebox.showinfo("错误译名","请先在译文框中划选实际翻错的词。",parent=win);return
        correct = w.expected_translation.strip()
        if w.original_term == "（错误译名二次确认）":
            inferred = next((a.correct for a in sorted(self.error_aliases, key=lambda x: -len(x.wrong)) if a.wrong in wrong), "")
            if inferred: correct = inferred
        if not correct:
            correct = simpledialog.askstring("错误译名", f"请输入“{wrong}”应统一为的正确译名：", parent=win) or ""
        if not correct: return
        self._add_error_alias(ErrorAlias(wrong,correct,w.original_term,"辞典逐句确认"))
        self.dict_review_states[w.key]=f"已建立错误译名：{wrong}→{correct}"
        self._review_refresh_warning_tree();self._review_refresh_alias_tree();self._review_refresh_auto(win)

    def _review_add_dictionary_entry(self,win):
        current = self._review_current_warning()
        o=self._selected_text(self.review_original).strip();t=self._selected_text(self.review_translated).strip()
        if not o:
            messagebox.showinfo("新增辞典项","请在原文框划选新的辞典原词。",parent=win);return
        if not t:
            t=simpledialog.askstring("新增辞典项",f"请输入“{o}”的正确译文：",parent=win) or ""
        if not t:return
        cat=simpledialog.askstring("辞典类别","类别（人名/地名/物品/术语/其他，或自定义）：",initialvalue="其他",parent=win) or "其他"
        self.dictionary_rows.append(EditableDictionaryRow(o,t,cat,"逐句确认新增"));self._refresh_dictionary_tree()
        self.current_dict_entries.append(DictionaryEntry(o,t,Path("逐句确认新增")))
        if current:
            self.dict_review_states[current.key] = f"已新增辞典项：{o}→{t}"
        self._review_rescan_warnings(win)

    def _review_rescan_warnings(self,win):
        self.current_dict_warnings=analyze_dictionary(self.active_records(),self.current_dict_entries,self.dict_messages_only.get())
        self._show_dict_warnings(self.current_dict_warnings);self._review_refresh_warning_tree();self._review_refresh_auto(win)

    def _add_error_alias(self,alias: ErrorAlias):
        self.error_aliases=[x for x in self.error_aliases if x.wrong!=alias.wrong]+[alias]

    def _review_refresh_alias_tree(self):
        if not hasattr(self,"alias_tree"):return
        for i in self.alias_tree.get_children(""):self.alias_tree.delete(i)
        for n,x in enumerate(sorted(self.error_aliases,key=lambda z:(-len(z.wrong),z.wrong))):
            self.alias_tree.insert("","end",iid=f"a{n}",values=(x.wrong,x.correct,x.source_term,x.source))

    def _review_delete_alias(self,win):
        selected={self.alias_tree.set(i,"wrong") for i in self.alias_tree.selection()}
        self.error_aliases=[x for x in self.error_aliases if x.wrong not in selected]
        self._review_refresh_alias_tree();self._review_refresh_auto(win)

    def _review_refresh_auto(self,win):
        if not hasattr(self,"auto_tree"):return
        for i in self.auto_tree.get_children(""):self.auto_tree.delete(i)
        changes=build_error_alias_changes(self.active_records(),self.error_aliases);self.auto_change_map={}
        for n,c in enumerate(changes):
            iid=f"p{n}";self.auto_change_map[iid]=c
            self.auto_tree.insert("","end",iid=iid,values=("☑",c.record.file_key,c.reason,self._display(c.record.original),self._display(c.record.translated),self._display(c.proposed)))

    def _simple_tree_check_all(self,tree,value):
        for iid in tree.get_children(""):
            tree.set(iid,"use","☑" if value else "☐")

    def _auto_toggle(self,event):
        if self.auto_tree.identify_column(event.x)!="#1":return
        iid=self.auto_tree.identify_row(event.y)
        if iid:self.auto_tree.set(iid,"use","☐" if self.auto_tree.set(iid,"use")=="☑" else "☑");return "break"

    def _review_auto_double(self,event):
        iid=self.auto_tree.identify_row(event.y);change=self.auto_change_map.get(iid)
        if not change:return
        # Re-open a concrete auto candidate for manual refinement. If a short alias
        # caused the preview, inherit its correct target so a longer selected wrong
        # form (e.g. 沃塔莉 after 沃塔) can be added safely.
        used = [x.strip() for x in change.reason.split("：",1)[-1].split("、") if x.strip()]
        amap = {x.wrong:x.correct for x in self.error_aliases}
        expected = next((amap[x] for x in used if x in amap), "")
        pseudo=DictionaryWarning(change.record,"（错误译名二次确认）",expected,Path("错误译名列表"),change.record.original,change.record.translated)
        key=pseudo.key+"|"+iid; self.review_warning_map[key]=pseudo
        self.review_tree.insert("","end",iid=key,values=("二次确认","（从译文选区添加更长错误译名）","请手动指定",change.record.file_key))
        self.review_tree.selection_set(key);self.review_tree.focus(key);self._review_show_current()

    def _review_apply_auto(self,win):
        updates={}
        for iid in self.auto_tree.get_children(""):
            if self.auto_tree.set(iid,"use")!="☑":continue
            c=self.auto_change_map.get(iid)
            if c:updates[c.record.uid]=(c.record,c.proposed)
        if self._save_updates_v42(updates,"错误译名自动替换"):
            self._review_refresh_auto(win)

    def _export_error_aliases(self):
        if not self.error_aliases:
            messagebox.showinfo("错误译名列表","当前没有错误译名。",parent=self);return
        path=filedialog.asksaveasfilename(parent=self,defaultextension=".csv",filetypes=[("CSV","*.csv"),("Excel","*.xlsx")])
        if not path:return
        p=Path(path)
        rows=[[x.wrong,x.correct,x.source_term,x.source] for x in sorted(self.error_aliases,key=lambda z:(-len(z.wrong),z.wrong))]
        if p.suffix.lower()==".csv":
            with p.open("w",encoding="utf-8-sig",newline="") as f:w=csv.writer(f);w.writerow(["错误译名","正确译名","对应辞典原词","来源"]);w.writerows(rows)
        else:
            from openpyxl import Workbook
            wb=Workbook();ws=wb.active;ws.title="ErrorAliases";ws.append(["错误译名","正确译名","对应辞典原词","来源"])
            for row in rows:ws.append(row)
            wb.save(p);wb.close()
        messagebox.showinfo("导出完成",str(p),parent=self)

    # ------------------------------------------------------------------
    # Speaker candidates -> error alias list
    # ------------------------------------------------------------------
    def _build_speaker(self):
        super()._build_speaker()
        tab=self.tabs["speaker"]
        extra=ttk.Frame(tab);extra.grid(row=4,column=0,sticky="ew",pady=(4,0))
        ttk.Button(extra,text="将勾选说话人的候补译名加入错误译名列表",command=self._speaker_candidates_to_aliases).pack(side="left")
        ttk.Label(extra,text="每个说话人默认以使用次数最多的候补为正确译名；其他候补作为错误译名，之后仍可在逐句确认窗口逐条取消。",foreground="#555").pack(side="left",padx=8)

    def _speaker_candidates_to_aliases(self):
        groups=[]
        for iid in self._checked_iids(self.speaker_tree):
            g=self.result_maps.get("speaker",{}).get(iid)
            if g:groups.append(g)
        if not groups:
            messagebox.showinfo("说话人候补","请先勾选说话人。",parent=self);return
        added=0
        for g in groups:
            opts=list(getattr(g,"options",()) or ())
            if not opts:continue
            correct=opts[0].translation
            for opt in opts[1:]:
                if opt.translation and opt.translation!=correct:
                    self._add_error_alias(ErrorAlias(opt.translation,correct,g.original_name,"说话人候补"));added+=1
        self.status_var.set(f"已从说话人候补加入 {added} 个错误译名映射。")
        if added and messagebox.askyesno("说话人候补",f"已加入 {added} 个错误译名。\n是否现在打开逐句确认/自动替换窗口？",parent=self):
            self._open_dictionary_review()


def main():
    app=RPGMakerProofreadingApp();app.mainloop()


if __name__=="__main__":main()
