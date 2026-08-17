from __future__ import annotations

import queue
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, simpledialog, ttk

from workbench_core import (
    APP_NAME,
    QUOTE_PAIRS,
    DataRecord,
    DatabaseProposal,
    DictionaryWarning,
    EditableDictionaryRow,
    PunctuationIssue,
    QAError,
    QuoteIssue,
    SearchOptions,
    SearchResult,
    SpeakerCandidate,
    TextChange,
    WidthIssue,
    analyze_dictionary,
    analyze_ellipsis,
    analyze_punctuation,
    analyze_quotes,
    analyze_speakers,
    analyze_symbol_counts,
    analyze_width,
    apply_translation_updates,
    build_alnum_changes,
    build_quote_proposal,
    build_scope_items,
    build_symbol_changes,
    build_used_dictionary,
    classify_file_key,
    compare_database,
    dictionary_conflicts,
    dictionary_entries_from_rows,
    filter_records,
    load_config,
    load_editable_dictionary,
    load_excel_records,
    load_json_records,
    load_txt_records,
    open_file,
    records_context,
    save_config,
    save_dictionary,
    search_records,
    transform_alnum_segment,
)

APP_TITLE = f"{APP_NAME} v3.0"
CONFIG_NAME = "rpg_translation_qa_workbench_config.json"
SOURCE_LABELS = {"TXT（两个文件夹）": "txt", "JSON（单个文件）": "json", "Excel（单个文件夹）": "excel"}
GROUP_LABELS = {"map": "地图", "database": "数据库", "common_event": "数据库 / Common Event", "other": "其他"}
DICT_CATEGORIES = ["人名", "地名", "物品", "术语", "其他"]
SYMBOL_PRESETS = [
    "自定义", "「 → “", "」 → ”", "『 → “", "』 → ”", "（ → (", "） → )", "( → （", ") → ）",
    "【 → [", "】 → ]", "《 → “", "》 → ”", "、 → ，", ", → ，", "： → :", ": → ：",
    "。 → .", ". → 。", "？ → ?", "? → ？", "！ → !", "! → ！", "； → ;", "; → ；",
]


class ConflictDialog(tk.Toplevel):
    def __init__(self, master, conflicts):
        super().__init__(master)
        self.title("辞典包含多个译文")
        self.geometry("760x420")
        self.result = "abort"
        ttk.Label(self, text=f"发现 {len(conflicts)} 个原文对应多个译文。请选择处理方式：", font=("", 11, "bold")).pack(anchor="w", padx=12, pady=(12, 6))
        text = tk.Text(self, height=14, wrap="word")
        text.pack(fill="both", expand=True, padx=12)
        for c in conflicts[:100]:
            text.insert("end", f"{c.original}  →  {' / '.join(c.translations)}\n")
        text.configure(state="disabled")
        bar = ttk.Frame(self); bar.pack(fill="x", padx=12, pady=12)
        ttk.Button(bar, text="跳转生成辞典页面", command=lambda:self._finish("dictionary")).pack(side="left")
        ttk.Button(bar, text="终止匹配", command=lambda:self._finish("abort")).pack(side="left", padx=8)
        ttk.Button(bar, text="用第一个译文继续", command=lambda:self._finish("first")).pack(side="right")
        self.transient(master); self.grab_set(); self.protocol("WM_DELETE_WINDOW", lambda:self._finish("abort"))

    def _finish(self, value):
        self.result = value; self.destroy()


class WorkbenchApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1550x960")
        self.minsize(1180, 760)
        self.config_path = self._app_dir() / CONFIG_NAME
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
        self.speaker_results: list[SpeakerCandidate] = []
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
        self.status_var = tk.StringVar(value="请先在第一页检查数据源。")
        self.scope_summary = tk.StringVar(value="尚未检查")

        self._build_ui()
        self._load_settings()
        self.after(100, self._poll_worker)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    @staticmethod
    def _app_dir() -> Path:
        return Path(__file__).resolve().parent

    # ---------- general UI helpers ----------
    def _build_ui(self):
        style = ttk.Style(self)
        style.configure("Treeview", rowheight=25)
        outer = ttk.Frame(self, padding=8); outer.pack(fill="both", expand=True)
        outer.rowconfigure(0, weight=1); outer.columnconfigure(0, weight=1)
        self.notebook = ttk.Notebook(outer); self.notebook.grid(row=0, column=0, sticky="nsew")
        self.tabs = {}
        for key, title in [
            ("settings", "1. 格式设置"), ("search", "2. 文本查询"), ("width", "3. 检查文本宽度"),
            ("editor", "4. 单独文本处理"), ("speaker", "5. 筛选说话人"), ("quote", "6. 引号处理"),
            ("punct", "7. 句尾符号"), ("symbol", "8. 批量替换符号"), ("alnum", "9. 英语数字格式"),
            ("ellipsis", "10. 省略号检查"), ("dictionary", "11. 生成辞典"),
            ("dictcheck", "12. 匹配辞典检查"), ("database", "13. 数据库直接翻译")]:
            frame = ttk.Frame(self.notebook, padding=8); self.notebook.add(frame, text=title); self.tabs[key] = frame
        self._build_settings(); self._build_search(); self._build_width(); self._build_editor(); self._build_speaker()
        self._build_quote(); self._build_punctuation(); self._build_symbol(); self._build_alnum(); self._build_ellipsis()
        self._build_dictionary(); self._build_dictcheck(); self._build_database()
        status = ttk.Frame(outer); status.grid(row=1, column=0, sticky="ew", pady=(6,0))
        ttk.Label(status, textvariable=self.status_var).pack(side="left")
        self.progress = ttk.Progressbar(status, mode="indeterminate", length=220); self.progress.pack(side="right")

    def _tree(self, parent, columns, headings, widths, checkbox=False):
        frame = ttk.Frame(parent)
        tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="extended")
        for col, title in zip(columns, headings): tree.heading(col, text=title)
        for col, width in zip(columns, widths): tree.column(col, width=width, anchor="w")
        y = ttk.Scrollbar(frame, orient="vertical", command=tree.yview); x = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=y.set, xscrollcommand=x.set)
        tree.grid(row=0,column=0,sticky="nsew");y.grid(row=0,column=1,sticky="ns");x.grid(row=1,column=0,sticky="ew")
        frame.rowconfigure(0,weight=1);frame.columnconfigure(0,weight=1)
        if checkbox: tree.bind("<Button-1>", lambda e:self._toggle_tree_checkbox(tree,e))
        return tree

    def _text(self, parent, height=8, readonly=False):
        text = tk.Text(parent, height=height, wrap="none", undo=not readonly)
        y=ttk.Scrollbar(parent,orient="vertical",command=text.yview);x=ttk.Scrollbar(parent,orient="horizontal",command=text.xview)
        text.configure(yscrollcommand=y.set,xscrollcommand=x.set)
        text.grid(row=0,column=0,sticky="nsew");y.grid(row=0,column=1,sticky="ns");x.grid(row=1,column=0,sticky="ew")
        parent.rowconfigure(0,weight=1);parent.columnconfigure(0,weight=1)
        if readonly: text.configure(state="disabled")
        self.text_widgets.append(text); return text

    @staticmethod
    def _clear_tree(tree):
        for iid in tree.get_children(): tree.delete(iid)

    def _set_text(self, widget, value, readonly=True):
        widget.configure(state="normal"); widget.delete("1.0","end"); widget.insert("1.0", value or "")
        if readonly: widget.configure(state="disabled")

    def _toggle_tree_checkbox(self, tree, event):
        if tree.identify_region(event.x,event.y)!="cell" or tree.identify_column(event.x)!="#1": return
        iid=tree.identify_row(event.y)
        if not iid:return
        values=list(tree.item(iid,"values")); values[0]="☐" if values and values[0]=="☑" else "☑";tree.item(iid,values=values)
        return "break"

    def _checked_iids(self, tree):
        return [iid for iid in tree.get_children() if tree.item(iid,"values") and tree.item(iid,"values")[0]=="☑"]

    def _check_all(self, tree, checked=True, predicate=None):
        for iid in tree.get_children():
            vals=list(tree.item(iid,"values"))
            if not vals: continue
            if predicate is None or predicate(iid,vals): vals[0]="☑" if checked else "☐";tree.item(iid,values=vals)

    def _run(self, label, fn, done):
        if self.busy:return
        self.busy=True; self.status_var.set(label+"…"); self.progress.start(10)
        import threading
        def work():
            try:self.worker_queue.put(("ok",done,fn()))
            except Exception as exc:self.worker_queue.put(("err",None,exc))
        threading.Thread(target=work,daemon=True).start()

    def _poll_worker(self):
        try:
            while True:
                kind,done,payload=self.worker_queue.get_nowait()
                self.busy=False;self.progress.stop()
                if kind=="err":
                    self.status_var.set("操作失败");messagebox.showerror("错误",str(payload),parent=self)
                else:
                    done(payload);self.status_var.set("完成")
        except queue.Empty:pass
        self.after(100,self._poll_worker)

    def _source_snapshot(self):
        return {
            "kind":self.source_type.get(),"json":Path(self.json_path.get()),"origin":Path(self.origin_dir.get()),
            "translated":Path(self.translated_dir.get()),"excel":Path(self.excel_dir.get()),
            "oenc":self.origin_encoding.get(),"tenc":self.translated_encoding.get(),
        }

    @staticmethod
    def _load_snapshot(snap):
        if snap["kind"]=="json":return load_json_records(snap["json"])
        if snap["kind"]=="excel":return load_excel_records(snap["excel"])
        return load_txt_records(snap["origin"],snap["translated"],snap["oenc"],snap["tenc"])

    def active_records(self):
        if not self.all_records: raise QAError("请先在第一页检查数据源。")
        return filter_records(self.all_records,self.selected_file_keys,self.workset or None)

    def _refresh_after_write(self):
        snap=self._source_snapshot(); result=self._load_snapshot(snap)
        self.all_records=list(result.records);self.record_by_uid={r.uid:r for r in self.all_records}
        old=set(self.selected_file_keys); self.scope_items=build_scope_items(self.all_records)
        self.selected_file_keys={x.file_key for x in self.scope_items if x.file_key in old} or {x.file_key for x in self.scope_items}
        self.workset.clear();self._populate_scope_tree();self._refresh_editor_file_combo()

    def _open_record_file(self, record, side="translated"):
        try:open_file(record.original_open_path if side=="original" else record.translated_open_path)
        except Exception as exc:messagebox.showerror("打开失败",str(exc),parent=self)

    def _send_to_editor(self, record):
        self.current_record=record;self.notebook.select(self.tabs["editor"]);self._show_editor_record(record)

    # ---------- page 1 ----------
    def _build_settings(self):
        tab=self.tabs["settings"];tab.columnconfigure(0,weight=1);tab.rowconfigure(2,weight=1)
        src=ttk.LabelFrame(tab,text="数据源、显示与编码",padding=8);src.grid(row=0,column=0,sticky="ew");src.columnconfigure(1,weight=1)
        ttk.Label(src,text="提取格式：").grid(row=0,column=0,sticky="w")
        for i,(label,key) in enumerate(SOURCE_LABELS.items()):ttk.Radiobutton(src,text=label,value=key,variable=self.source_type,command=self._update_source_visibility).grid(row=0,column=i+1,sticky="w",padx=6)
        self.path_rows={}
        self.path_rows["json"]=self._path_row(src,1,"JSON 文件",self.json_path,lambda:self._choose_file(self.json_path,[("JSON","*.json")]))
        self.path_rows["origin"]=self._path_row(src,2,"原文 TXT 文件夹",self.origin_dir,lambda:self._choose_dir(self.origin_dir))
        self.path_rows["translated"]=self._path_row(src,3,"译文 TXT 文件夹",self.translated_dir,lambda:self._choose_dir(self.translated_dir))
        self.path_rows["excel"]=self._path_row(src,4,"Excel 文件夹",self.excel_dir,lambda:self._choose_dir(self.excel_dir))
        ttk.Label(src,text="原文显示/读取编码：").grid(row=5,column=0,sticky="w",pady=3)
        self.oenc_combo=ttk.Combobox(src,textvariable=self.origin_encoding,state="readonly",values=["936","932","UTF-8","自动"],width=12);self.oenc_combo.grid(row=5,column=1,sticky="w")
        ttk.Label(src,text="译文显示/读取编码：").grid(row=5,column=2,sticky="e",padx=(10,4));self.tenc_combo=ttk.Combobox(src,textvariable=self.translated_encoding,state="readonly",values=["936","932","UTF-8","自动"],width=12);self.tenc_combo.grid(row=5,column=3,sticky="w")
        ttk.Label(src,text="文本框显示字体：").grid(row=6,column=0,sticky="w",pady=3)
        fonts=["系统默认"]+sorted(set(tkfont.families()))
        self.font_combo=ttk.Combobox(src,textvariable=self.display_font,state="readonly",values=fonts,width=32);self.font_combo.grid(row=6,column=1,sticky="w");self.font_combo.bind("<<ComboboxSelected>>",lambda e:self._apply_font())
        ttk.Button(src,text="检查文件/文件夹",command=self._scan_source).grid(row=6,column=3,sticky="e")

        quick=ttk.Frame(tab);quick.grid(row=1,column=0,sticky="ew",pady=6)
        ttk.Label(quick,text="文本范围：").pack(side="left")
        for text,cmd in [("选择全文本",lambda:self._scope_quick("all")),("选择全地图",lambda:self._scope_quick("map")),("选择数据库",lambda:self._scope_quick("database")),("选择数据库 Common Event",lambda:self._scope_quick("common_event")),("清空选择",lambda:self._scope_quick("none"))]:ttk.Button(quick,text=text,command=cmd).pack(side="left",padx=3)
        ttk.Label(quick,textvariable=self.scope_summary).pack(side="right")
        self.scope_tree=self._tree(tab,("use","group","file","count"),("选择","类别","文件/逻辑文件","条目数"),(65,180,900,100),checkbox=True);self.scope_tree.master.grid(row=2,column=0,sticky="nsew")
        self.scope_tree.bind("<ButtonRelease-1>",lambda e:self.after(20,self._sync_scope_selection),add=True)
        self._update_source_visibility()

    def _path_row(self,parent,row,label,var,cmd):
        widgets=[]
        l=ttk.Label(parent,text=label+"：");l.grid(row=row,column=0,sticky="w",pady=2)
        e=ttk.Entry(parent,textvariable=var);e.grid(row=row,column=1,columnspan=2,sticky="ew",padx=4)
        b=ttk.Button(parent,text="浏览…",command=cmd);b.grid(row=row,column=3,sticky="e")
        return [l,e,b]

    def _choose_file(self,var,types):
        p=filedialog.askopenfilename(parent=self,filetypes=types)
        if p:var.set(p)
    def _choose_dir(self,var):
        p=filedialog.askdirectory(parent=self)
        if p:var.set(p)

    def _update_source_visibility(self):
        kind=self.source_type.get()
        for key,widgets in self.path_rows.items():
            show=(kind=="json" and key=="json") or (kind=="excel" and key=="excel") or (kind=="txt" and key in {"origin","translated"})
            for w in widgets:
                if show:w.grid()
                else:w.grid_remove()
        state="readonly" if kind=="txt" else "disabled";self.oenc_combo.configure(state=state);self.tenc_combo.configure(state=state)

    def _scan_source(self):
        snap=self._source_snapshot();self._run("检查数据源",lambda:self._load_snapshot(snap),self._scan_done)

    def _scan_done(self,result):
        self.all_records=list(result.records);self.record_by_uid={r.uid:r for r in self.all_records};self.scope_items=build_scope_items(self.all_records)
        self.selected_file_keys={x.file_key for x in self.scope_items};self.workset.clear();self._populate_scope_tree();self._refresh_editor_file_combo()
        self.scope_summary.set(f"{len(self.scope_items)} 个文件，{len(self.all_records)} 条文本；当前全选")
        if result.warnings:messagebox.showwarning("读取提示","\n".join(result.warnings[:30]),parent=self)

    def _populate_scope_tree(self):
        self._clear_tree(self.scope_tree)
        for i,item in enumerate(self.scope_items):self.scope_tree.insert("","end",iid=f"scope{i}",values=("☑" if item.file_key in self.selected_file_keys else "☐",GROUP_LABELS.get(item.group,item.group),item.file_key,item.record_count))

    def _sync_scope_selection(self):
        self.selected_file_keys={self.scope_tree.item(i,"values")[2] for i in self.scope_tree.get_children() if self.scope_tree.item(i,"values")[0]=="☑"}
        count=sum(1 for r in self.all_records if r.file_key in self.selected_file_keys);self.scope_summary.set(f"已选 {len(self.selected_file_keys)} 个文件，{count} 条文本"+(f"；查询处理集合 {len(self.workset)} 条" if self.workset else ""))

    def _scope_quick(self,mode):
        for iid,item in zip(self.scope_tree.get_children(),self.scope_items):
            checked=mode=="all" or item.group==mode
            if mode=="database":checked=item.group in {"database","common_event"}
            if mode=="none":checked=False
            vals=list(self.scope_tree.item(iid,"values"));vals[0]="☑" if checked else "☐";self.scope_tree.item(iid,values=vals)
        self._sync_scope_selection()

    def _apply_font(self):
        family=self.display_font.get();family=None if family=="系统默认" else family
        for w in self.text_widgets:w.configure(font=(family or tkfont.nametofont("TkTextFont").actual("family"),11))

    # ---------- page 2 search ----------
    def _build_search(self):
        tab=self.tabs["search"];tab.rowconfigure(2,weight=1);tab.columnconfigure(0,weight=1)
        bar=ttk.Frame(tab);bar.grid(row=0,column=0,sticky="ew")
        self.search_query=tk.StringVar();ttk.Label(bar,text="查询：").pack(side="left");ttk.Entry(bar,textvariable=self.search_query,width=38).pack(side="left")
        self.search_field=tk.StringVar(value="both");ttk.Combobox(bar,textvariable=self.search_field,state="readonly",values=["original","translated","both"],width=12).pack(side="left",padx=4)
        self.search_mode=tk.StringVar(value="keyword");ttk.Combobox(bar,textvariable=self.search_mode,state="readonly",values=["keyword","exact"],width=10).pack(side="left")
        self.search_case=tk.BooleanVar(value=False);self.search_width=tk.BooleanVar(value=False)
        ttk.Checkbutton(bar,text="区分大小写",variable=self.search_case).pack(side="left",padx=6);ttk.Checkbutton(bar,text="区分全半角",variable=self.search_width).pack(side="left")
        ttk.Button(bar,text="开始查询",command=self._do_search).pack(side="left",padx=6)
        act=ttk.Frame(tab);act.grid(row=1,column=0,sticky="ew",pady=5)
        ttk.Button(act,text="全选结果",command=lambda:self._check_all(self.search_tree,True)).pack(side="left")
        ttk.Button(act,text="全部取消",command=lambda:self._check_all(self.search_tree,False)).pack(side="left",padx=4)
        ttk.Button(act,text="将勾选结果送入后续处理",command=self._search_to_workset).pack(side="left",padx=4)
        ttk.Button(act,text="清除查询处理集合",command=self._clear_workset).pack(side="left")
        ttk.Button(act,text="打开原文文件",command=lambda:self._open_selected_result("search","original")).pack(side="right")
        ttk.Button(act,text="打开译文文件",command=lambda:self._open_selected_result("search","translated")).pack(side="right",padx=4)
        self.search_tree=self._tree(tab,("use","field","file","marker","original","translated"),("选择","命中","文件","标记","原文","译文"),(60,80,220,90,510,510),checkbox=True);self.search_tree.master.grid(row=2,column=0,sticky="nsew")
        self.search_tree.bind("<Double-1>",lambda e:self._double_to_editor("search"))

    def _do_search(self):
        try:
            records=self.active_records();opts=SearchOptions(self.search_field.get(),self.search_mode.get(),self.search_case.get(),self.search_width.get(),True);results=search_records(records,self.search_query.get(),opts)
            self.result_maps["search"]={};self._clear_tree(self.search_tree)
            for i,x in enumerate(results):
                iid=f"s{i}";self.result_maps["search"][iid]=x.record;self.search_tree.insert("","end",iid=iid,values=("☐",x.matched_field,x.record.file_key,x.record.marker,self._compact(x.record.original),self._compact(x.record.translated)))
            self.status_var.set(f"查询命中 {len(results)} 条。")
        except Exception as exc:messagebox.showerror("查询失败",str(exc),parent=self)

    def _search_to_workset(self):
        mapping=self.result_maps.get("search",{});ids=self._checked_iids(self.search_tree);self.workset={mapping[i].uid for i in ids if i in mapping};self._sync_scope_selection();self.status_var.set(f"后续处理范围限定为 {len(self.workset)} 条查询结果。")
    def _clear_workset(self):self.workset.clear();self._sync_scope_selection();self.status_var.set("已清除查询处理集合。")

    # ---------- page 3 width ----------
    def _build_width(self):
        tab=self.tabs["width"];tab.rowconfigure(1,weight=1);tab.columnconfigure(0,weight=1)
        bar=ttk.Frame(tab);bar.grid(row=0,column=0,sticky="ew",pady=(0,5))
        self.face_limit=tk.DoubleVar(value=19);self.narr_limit=tk.DoubleVar(value=25);self.check_face=tk.BooleanVar(value=True);self.check_narr=tk.BooleanVar(value=True)
        ttk.Checkbutton(bar,text="有头像",variable=self.check_face).pack(side="left");ttk.Spinbox(bar,from_=1,to=100,increment=.5,textvariable=self.face_limit,width=6).pack(side="left")
        ttk.Checkbutton(bar,text="无头像/NARRATION",variable=self.check_narr).pack(side="left",padx=(8,0));ttk.Spinbox(bar,from_=1,to=100,increment=.5,textvariable=self.narr_limit,width=6).pack(side="left")
        ttk.Button(bar,text="检查宽度",command=self._do_width).pack(side="left",padx=8);ttk.Button(bar,text="打开文件",command=lambda:self._open_selected_result("width","translated")).pack(side="right")
        self.width_tree=self._tree(tab,("type","file","line","width","limit","text"),("类型","文件","行","宽度","上限","超长文本"),(100,260,60,80,80,850));self.width_tree.master.grid(row=1,column=0,sticky="nsew")
        self.width_tree.bind("<Double-1>",lambda e:self._double_to_editor("width"))
    def _do_width(self):
        try:
            issues=analyze_width(self.active_records(),self.face_limit.get(),self.narr_limit.get(),self.check_face.get(),self.check_narr.get());self.result_maps["width"]={};self._clear_tree(self.width_tree)
            for i,x in enumerate(issues):
                iid=f"w{i}";self.result_maps["width"][iid]=x.record;self.width_tree.insert("","end",iid=iid,values=(x.face_type,x.record.file_key,x.line_no,f"{x.width:g}",f"{x.limit:g}",x.visible_line))
            self.status_var.set(f"发现 {len(issues)} 条超宽文本。")
        except Exception as exc:messagebox.showerror("分析失败",str(exc),parent=self)

    # ---------- page 4 editor ----------
    def _build_editor(self):
        tab=self.tabs["editor"];tab.columnconfigure(0,weight=1);tab.rowconfigure(3,weight=1)
        nav=ttk.Frame(tab);nav.grid(row=0,column=0,sticky="ew")
        ttk.Label(nav,text="文件：").pack(side="left");self.editor_file=tk.StringVar();self.editor_file_combo=ttk.Combobox(nav,textvariable=self.editor_file,state="readonly",width=55);self.editor_file_combo.pack(side="left")
        ttk.Label(nav,text="第几项：").pack(side="left",padx=(8,2));self.editor_index=tk.IntVar(value=1);ttk.Spinbox(nav,from_=1,to=999999,textvariable=self.editor_index,width=8).pack(side="left")
        ttk.Button(nav,text="跳转",command=self._manual_editor_jump).pack(side="left",padx=5);ttk.Button(nav,text="打开原文文件",command=lambda:self.current_record and self._open_record_file(self.current_record,"original")).pack(side="right");ttk.Button(nav,text="打开译文文件",command=lambda:self.current_record and self._open_record_file(self.current_record,"translated")).pack(side="right",padx=4)
        self.editor_location=tk.StringVar();ttk.Label(tab,textvariable=self.editor_location,foreground="#555").grid(row=1,column=0,sticky="ew",pady=4)
        ctx=ttk.LabelFrame(tab,text="上下文（双击切换）",padding=4);ctx.grid(row=2,column=0,sticky="ew");self.context_tree=self._tree(ctx,("index","marker","original","translated"),("序号","标记","原文","译文"),(70,90,600,600));self.context_tree.master.pack_forget();self.context_tree.master.grid(row=0,column=0,sticky="ew");ctx.columnconfigure(0,weight=1);self.context_tree.configure(height=5);self.context_tree.bind("<Double-1>",self._context_double)
        pane=ttk.PanedWindow(tab,orient="horizontal");pane.grid(row=3,column=0,sticky="nsew",pady=5)
        of=ttk.LabelFrame(pane,text="原文",padding=3);tf=ttk.LabelFrame(pane,text="译文（可编辑）",padding=3);pane.add(of,weight=1);pane.add(tf,weight=1)
        self.editor_original=self._text(of,18,True);self.editor_translation=self._text(tf,18,False)
        bar=ttk.Frame(tab);bar.grid(row=4,column=0,sticky="ew")
        for label,mode in [("转全角","全角"),("转半角","半角"),("英文大写","大写"),("英文小写","小写"),("英文首字母大写","首字母大写")]:ttk.Button(bar,text=label,command=lambda m=mode:self._editor_transform(m)).pack(side="left",padx=2)
        ttk.Button(bar,text="保存译文",command=self._editor_save).pack(side="right")

    def _refresh_editor_file_combo(self):
        keys=sorted({r.file_key for r in self.all_records if r.file_key in self.selected_file_keys});self.editor_file_combo.configure(values=keys)
        if keys and self.editor_file.get() not in keys:self.editor_file.set(keys[0])

    def _show_editor_record(self,record):
        self.current_record=record;self.editor_file.set(record.file_key)
        same=[r for r in self.all_records if r.file_key==record.file_key]
        idx=next((i for i,r in enumerate(same,1) if r.uid==record.uid),1);self.editor_index.set(idx)
        self.editor_location.set(f"原文：{record.original_location}    |    译文：{record.translated_location}    |    {record.marker} / {record.speaker_id}")
        self._set_text(self.editor_original,record.original,True);self._set_text(self.editor_translation,record.translated,False)
        self._clear_tree(self.context_tree);self.result_maps["context"]={}
        for i,r in enumerate(records_context(self.all_records,record,3)):
            iid=f"c{i}";self.result_maps["context"][iid]=r;seq=next((j for j,x in enumerate(same,1) if x.uid==r.uid),0);self.context_tree.insert("","end",iid=iid,values=(seq,r.marker,self._compact(r.original),self._compact(r.translated)))

    def _manual_editor_jump(self):
        rows=[r for r in self.all_records if r.file_key==self.editor_file.get()]
        idx=self.editor_index.get()-1
        if 0<=idx<len(rows):self._show_editor_record(rows[idx])
        else:messagebox.showwarning("位置不存在",f"该文件只有 {len(rows)} 条文本。",parent=self)
    def _context_double(self,event):
        iid=self.context_tree.identify_row(event.y);r=self.result_maps.get("context",{}).get(iid)
        if r:self._show_editor_record(r)
    def _editor_transform(self,mode):
        s=self.editor_translation.get("1.0","end-1c");self._set_text(self.editor_translation,transform_alnum_segment(s,mode),False)
    def _editor_save(self):
        if not self.current_record:return
        proposed=self.editor_translation.get("1.0","end-1c")
        if proposed==self.current_record.translated:return
        try:
            count,backup=apply_translation_updates({self.current_record.uid:(self.current_record,proposed)});messagebox.showinfo("保存完成",f"已保存 {count} 条。\n备份：{backup}",parent=self);self._refresh_after_write();new=self.record_by_uid.get(self.current_record.uid);self._show_editor_record(new) if new else None
        except Exception as exc:messagebox.showerror("保存失败",str(exc),parent=self)

    # ---------- page 5 speaker ----------
    def _build_speaker(self):
        tab=self.tabs["speaker"];tab.rowconfigure(2,weight=1);tab.columnconfigure(0,weight=1)
        bar=ttk.Frame(tab);bar.grid(row=0,column=0,sticky="ew")
        self.speaker_mode=tk.IntVar(value=0);labels=[("自动检查",0),("类型1：独立行＋后文有引号",1),("类型2：独立行＋后文无引号",2),("类型3：独立行＋冒号",3),("类型4：同一行分隔",4)]
        for text,val in labels:ttk.Radiobutton(bar,text=text,value=val,variable=self.speaker_mode).pack(side="left",padx=3)
        sample=ttk.Frame(tab);sample.grid(row=1,column=0,sticky="ew",pady=5)
        self.speaker_full=tk.BooleanVar(value=False);self.speaker_first_n=tk.IntVar(value=100)
        ttk.Checkbutton(sample,text="全文检查",variable=self.speaker_full).pack(side="left");ttk.Label(sample,text="否则只检查前").pack(side="left");ttk.Spinbox(sample,from_=1,to=999999,textvariable=self.speaker_first_n,width=8).pack(side="left");ttk.Label(sample,text="句").pack(side="left")
        ttk.Button(sample,text="开始筛选",command=self._do_speaker).pack(side="left",padx=8);ttk.Button(sample,text="把所选加入辞典页",command=self._speaker_to_dictionary).pack(side="left")
        self.speaker_export_mode=tk.StringVar(value="原文＋译文");ttk.Combobox(sample,textvariable=self.speaker_export_mode,state="readonly",values=["仅原文","原文＋译文"],width=12).pack(side="left",padx=(8,2));ttk.Button(sample,text="导出译名表",command=self._export_speakers).pack(side="left",padx=5)
        self.speaker_tree=self._tree(tab,("use","name","translations","type","confidence","count"),("选择","原文说话人","译文候选","类型","置信度","次数"),(60,260,500,70,100,70),checkbox=True);self.speaker_tree.master.grid(row=2,column=0,sticky="nsew")
    def _do_speaker(self):
        try:
            self.speaker_results=analyze_speakers(self.active_records(),self.speaker_mode.get(),None if self.speaker_full.get() else self.speaker_first_n.get());self._clear_tree(self.speaker_tree);self.result_maps["speaker"]={}
            for i,x in enumerate(self.speaker_results):iid=f"sp{i}";self.result_maps["speaker"][iid]=x;self.speaker_tree.insert("","end",iid=iid,values=("☐",x.original_name," / ".join(x.translated_names),x.pattern_type,x.confidence,x.occurrences))
            self.status_var.set(f"筛选出 {len(self.speaker_results)} 个说话人候选。")
        except Exception as exc:messagebox.showerror("筛选失败",str(exc),parent=self)
    def _speaker_to_dictionary(self):
        selected=[self.result_maps.get("speaker",{}).get(i) for i in self._checked_iids(self.speaker_tree)];added=0
        for x in selected:
            if not x:continue
            if x.translated_names:
                for t in x.translated_names:self.dictionary_rows.append(EditableDictionaryRow(x.original_name,t,"人名","说话人筛选"));added+=1
            else:self.dictionary_rows.append(EditableDictionaryRow(x.original_name,"","人名","说话人筛选"));added+=1
        self._refresh_dictionary_tree();self.notebook.select(self.tabs["dictionary"]);self.status_var.set(f"已向辞典页加入 {added} 行。")
    def _export_speakers(self):
        path=filedialog.asksaveasfilename(parent=self,defaultextension=".xlsx",filetypes=[("Excel","*.xlsx"),("CSV","*.csv")])
        if not path:return
        rows=[]
        for x in self.speaker_results:
            if self.speaker_export_mode.get()=="仅原文":
                rows.append(EditableDictionaryRow(x.original_name,"","人名","说话人筛选"))
            elif x.translated_names:
                for t in x.translated_names:rows.append(EditableDictionaryRow(x.original_name,t,"人名","说话人筛选"))
            else:rows.append(EditableDictionaryRow(x.original_name,"","人名","说话人筛选"))
        save_dictionary(Path(path),rows);messagebox.showinfo("导出完成",path,parent=self)

    # ---------- common preview/apply pages ----------
    def _build_change_page(self,key,title,controls_builder=None):
        tab=self.tabs[key];tab.rowconfigure(2,weight=1);tab.columnconfigure(0,weight=1)
        controls=ttk.Frame(tab);controls.grid(row=0,column=0,sticky="ew")
        if controls_builder:controls_builder(controls)
        bar=ttk.Frame(tab);bar.grid(row=1,column=0,sticky="ew",pady=5)
        ttk.Button(bar,text="全选可处理项",command=lambda:self._check_all(getattr(self,key+"_tree"),True,lambda i,v:v[1]!="手动")).pack(side="left")
        ttk.Button(bar,text="全部取消",command=lambda:self._check_all(getattr(self,key+"_tree"),False)).pack(side="left",padx=4)
        ttk.Button(bar,text="应用勾选修改",command=lambda:self._apply_change_results(key)).pack(side="left")
        ttk.Button(bar,text="打开文件",command=lambda:self._open_selected_result(key,"translated")).pack(side="right")
        tree=self._tree(tab,("use","category","file","reason","current","proposed"),("选择","类别","文件","问题","当前译文","处理结果"),(60,100,230,350,420,420),checkbox=True);tree.master.grid(row=2,column=0,sticky="nsew");setattr(self,key+"_tree",tree);tree.bind("<Double-1>",lambda e,k=key:self._double_to_editor(k))

    def _store_changes(self,key,items):
        tree=getattr(self,key+"_tree");self._clear_tree(tree);mapping={}
        for i,item in enumerate(items):
            rec=item.record;proposed=getattr(item,"proposed",None);reason=getattr(item,"reason","");category=getattr(item,"category",item.__class__.__name__)
            iid=f"{key}{i}";mapping[iid]=item;auto=proposed is not None and category!="手动";tree.insert("","end",iid=iid,values=("☐",category,rec.file_key,reason,self._compact(rec.translated),self._compact(proposed or "")))
        self.result_maps[key]=mapping;self.status_var.set(f"{key}：发现 {len(items)} 项。")

    def _apply_change_results(self,key):
        mapping=self.result_maps.get(key,{});updates={}
        for iid in self._checked_iids(getattr(self,key+"_tree")):
            item=mapping.get(iid);rec=getattr(item,"record",None);prop=getattr(item,"proposed",None)
            if rec and prop is not None:updates[rec.uid]=(rec,prop)
        try:
            count,backup=apply_translation_updates(updates);messagebox.showinfo("处理完成",f"已处理 {count} 条。\n备份：{backup}",parent=self);self._refresh_after_write()
        except Exception as exc:messagebox.showerror("处理失败",str(exc),parent=self)

    # ---------- page 6 quote ----------
    def _build_quote(self):
        def controls(bar):
            ttk.Button(bar,text="检查引号",command=self._do_quote).pack(side="left")
            ttk.Label(bar,text="批量形式：").pack(side="left",padx=(10,2));self.quote_form=tk.StringVar(value="按原文形式");ttk.Combobox(bar,textvariable=self.quote_form,state="readonly",values=["按原文形式","只有左引号","左右引号","删除引号"],width=14).pack(side="left")
            ttk.Label(bar,text="引号样式：").pack(side="left",padx=(10,2));self.quote_style=tk.StringVar(value="按原文样式");ttk.Combobox(bar,textvariable=self.quote_style,state="readonly",values=["按原文样式"]+list(QUOTE_PAIRS),width=14).pack(side="left")
            ttk.Button(bar,text="只选左右引号",command=lambda:self._check_all(self.quote_tree,True,lambda i,v:v[1]=="左右引号")).pack(side="left",padx=(10,2))
            ttk.Button(bar,text="只选左引号",command=lambda:self._check_all(self.quote_tree,True,lambda i,v:v[1]=="只有左引号")).pack(side="left")
        self._build_change_page("quote","引号",controls)
    def _do_quote(self):
        try:
            issues=analyze_quotes(self.active_records());items=[]
            for x in issues:
                if not x.auto_eligible:items.append(TextChange(x.record,"手动："+x.reason,x.record.translated));continue
                form=self.quote_form.get();form=x.category if form=="按原文形式" else form
                if form=="手动":items.append(TextChange(x.record,"手动："+x.reason,x.record.translated));continue
                pair=(x.source_open or "「",x.source_close or "」") if self.quote_style.get()=="按原文样式" else QUOTE_PAIRS[self.quote_style.get()]
                proposed=build_quote_proposal(x.record,form,pair);items.append(TextChange(x.record,x.reason,proposed))
            # preserve manual category via wrapper attribute-like object
            from types import SimpleNamespace
            final=[]
            for x,item in zip(issues,items):final.append(SimpleNamespace(record=item.record,reason=item.reason,proposed=None if not x.auto_eligible else item.proposed,category=x.category if x.auto_eligible else "手动"))
            self._store_changes("quote",final)
        except Exception as exc:messagebox.showerror("检查失败",str(exc),parent=self)

    # ---------- page 7 punctuation ----------
    def _build_punctuation(self):
        def controls(bar):
            self.punct_mode=tk.StringVar(value="source");ttk.Combobox(bar,textvariable=self.punct_mode,state="readonly",values=["source","missing","delete"],width=12).pack(side="left")
            ttk.Label(bar,text="缺失时默认：").pack(side="left",padx=(8,2));self.punct_default=tk.StringVar(value="。");ttk.Combobox(bar,textvariable=self.punct_default,state="readonly",values=["。","！","？","；"],width=5).pack(side="left")
            ttk.Button(bar,text="检查句尾符号",command=self._do_punct).pack(side="left",padx=8)
        self._build_change_page("punct","句尾",controls)
    def _do_punct(self):
        try:self._store_changes("punct",analyze_punctuation(self.active_records(),self.punct_mode.get(),self.punct_default.get()))
        except Exception as exc:messagebox.showerror("检查失败",str(exc),parent=self)

    # ---------- page 8 symbols ----------
    def _build_symbol(self):
        tab=self.tabs["symbol"];tab.rowconfigure(2,weight=1);tab.columnconfigure(0,weight=1)
        bar=ttk.Frame(tab);bar.grid(row=0,column=0,sticky="ew")
        self.symbol_preset=tk.StringVar(value="自定义");combo=ttk.Combobox(bar,textvariable=self.symbol_preset,state="readonly",values=SYMBOL_PRESETS,width=18);combo.pack(side="left");combo.bind("<<ComboboxSelected>>",self._symbol_preset_changed)
        self.symbol_old=tk.StringVar();self.symbol_new=tk.StringVar();ttk.Label(bar,text="查找").pack(side="left",padx=(8,2));ttk.Entry(bar,textvariable=self.symbol_old,width=8).pack(side="left");ttk.Label(bar,text="替换为").pack(side="left",padx=(8,2));ttk.Entry(bar,textvariable=self.symbol_new,width=8).pack(side="left")
        ttk.Button(bar,text="统计标点",command=self._symbol_counts).pack(side="left",padx=6);ttk.Button(bar,text="生成替换预览",command=self._symbol_preview).pack(side="left");ttk.Button(bar,text="应用全部预览",command=lambda:self._apply_change_results("symbol")).pack(side="left",padx=5)
        self.symbol_tree=self._tree(tab,("use","category","file","reason","current","proposed"),("选择","类别","文件","说明","当前译文","处理结果"),(60,100,230,300,430,430),checkbox=True);self.symbol_tree.master.grid(row=2,column=0,sticky="nsew")
        self.symbol_tree.bind("<Double-1>",lambda e:self._double_to_editor("symbol"))
        self.symbol_count_text=tk.StringVar(value="尚未统计");ttk.Label(tab,textvariable=self.symbol_count_text).grid(row=1,column=0,sticky="w",pady=5)
    def _symbol_preset_changed(self,event=None):
        value=self.symbol_preset.get()
        if " → " in value:self.symbol_old.set(value.split(" → ")[0]);self.symbol_new.set(value.split(" → ")[1])
    def _symbol_counts(self):
        try:
            rows=analyze_symbol_counts(self.active_records());self.symbol_count_text.set("；".join(f"{s!r}: 原{a}/译{b}" for s,a,b in rows[:30]) or "未发现标点")
        except Exception as exc:messagebox.showerror("统计失败",str(exc),parent=self)
    def _symbol_preview(self):
        try:
            items=build_symbol_changes(self.active_records(),self.symbol_old.get(),self.symbol_new.get());self._store_changes("symbol",items);self._check_all(self.symbol_tree,True)
        except Exception as exc:messagebox.showerror("预览失败",str(exc),parent=self)

    # ---------- page 9 alnum ----------
    def _build_alnum(self):
        def controls(bar):
            self.alnum_mode=tk.StringVar(value="半角");ttk.Combobox(bar,textvariable=self.alnum_mode,state="readonly",values=["全角","半角","大写","小写","首字母大写"],width=12).pack(side="left")
            ttk.Label(bar,text="仅处理特定词（留空为全文）：").pack(side="left",padx=(8,2));self.alnum_term=tk.StringVar();ttk.Entry(bar,textvariable=self.alnum_term,width=20).pack(side="left")
            self.alnum_case=tk.BooleanVar(value=False);ttk.Checkbutton(bar,text="词语匹配区分大小写",variable=self.alnum_case).pack(side="left",padx=6);ttk.Button(bar,text="检查并预览",command=self._do_alnum).pack(side="left")
        self._build_change_page("alnum","英语数字",controls)
    def _do_alnum(self):
        try:self._store_changes("alnum",build_alnum_changes(self.active_records(),self.alnum_mode.get(),self.alnum_term.get(),self.alnum_case.get()))
        except Exception as exc:messagebox.showerror("检查失败",str(exc),parent=self)

    # ---------- page 10 ellipsis ----------
    def _build_ellipsis(self):
        def controls(bar):
            ttk.Label(bar,text="统一为：").pack(side="left");self.ellipsis_style=tk.StringVar(value="……");ttk.Combobox(bar,textvariable=self.ellipsis_style,state="readonly",values=["……","…","...","・・・","······"],width=10).pack(side="left");ttk.Button(bar,text="检查省略号",command=self._do_ellipsis).pack(side="left",padx=8)
        self._build_change_page("ellipsis","省略号",controls)
    def _do_ellipsis(self):
        try:self._store_changes("ellipsis",analyze_ellipsis(self.active_records(),self.ellipsis_style.get()))
        except Exception as exc:messagebox.showerror("检查失败",str(exc),parent=self)

    # ---------- page 11 dictionary ----------
    def _build_dictionary(self):
        tab=self.tabs["dictionary"];tab.rowconfigure(2,weight=1);tab.columnconfigure(0,weight=1)
        bar=ttk.Frame(tab);bar.grid(row=0,column=0,sticky="ew")
        ttk.Button(bar,text="导入辞典…",command=self._import_dictionary).pack(side="left");ttk.Button(bar,text="从当前原文匹配外部辞典",command=self._match_external_to_dictionary).pack(side="left",padx=4);ttk.Button(bar,text="删除所选",command=self._delete_dictionary_rows).pack(side="left");ttk.Button(bar,text="查看下一个多译词",command=self._next_dict_conflict).pack(side="left",padx=4);ttk.Button(bar,text="导出辞典…",command=self._export_dictionary).pack(side="right")
        edit=ttk.LabelFrame(tab,text="直接编辑（选择表格行后修改）",padding=5);edit.grid(row=1,column=0,sticky="ew",pady=5)
        self.dict_original=tk.StringVar();self.dict_translation=tk.StringVar();self.dict_category=tk.StringVar(value="其他")
        ttk.Label(edit,text="原文").pack(side="left");ttk.Entry(edit,textvariable=self.dict_original,width=28).pack(side="left",padx=3);ttk.Label(edit,text="译文").pack(side="left");ttk.Entry(edit,textvariable=self.dict_translation,width=28).pack(side="left",padx=3);ttk.Label(edit,text="类别").pack(side="left");ttk.Combobox(edit,textvariable=self.dict_category,values=DICT_CATEGORIES,width=12).pack(side="left",padx=3);ttk.Button(edit,text="新增",command=self._dict_add).pack(side="left",padx=3);ttk.Button(edit,text="更新所选",command=self._dict_update).pack(side="left")
        self.dictionary_tree=self._tree(tab,("original","translation","category","source"),("原文","译文","类别","来源"),(420,420,140,260));self.dictionary_tree.master.grid(row=2,column=0,sticky="nsew");self.dictionary_tree.bind("<<TreeviewSelect>>",self._dict_select)
        self.dict_conflict_index=0
    def _refresh_dictionary_tree(self):
        self._clear_tree(self.dictionary_tree)
        for i,r in enumerate(self.dictionary_rows):self.dictionary_tree.insert("","end",iid=f"d{i}",values=(r.original,r.translation,r.category,r.source))
    def _import_dictionary(self):
        paths=filedialog.askopenfilenames(parent=self,filetypes=[("辞典","*.xlsx *.xlsm *.csv")])
        try:
            for p in paths:self.dictionary_rows.extend(load_editable_dictionary(Path(p)))
            self._refresh_dictionary_tree();self.status_var.set(f"辞典页共 {len(self.dictionary_rows)} 行。")
        except Exception as exc:messagebox.showerror("导入失败",str(exc),parent=self)
    def _match_external_to_dictionary(self):
        paths=filedialog.askopenfilenames(parent=self,filetypes=[("辞典","*.xlsx *.xlsm *.csv")])
        if not paths:return
        try:
            rows=[]
            for p in paths:rows.extend(load_editable_dictionary(Path(p)))
            used=build_used_dictionary(self.active_records(),rows);self.dictionary_rows.extend(used);self._refresh_dictionary_tree();self.status_var.set(f"匹配并加入 {len(used)} 行。")
        except Exception as exc:messagebox.showerror("匹配失败",str(exc),parent=self)
    def _dict_select(self,event=None):
        sel=self.dictionary_tree.selection()
        if not sel:return
        idx=int(sel[0][1:]);r=self.dictionary_rows[idx];self.dict_original.set(r.original);self.dict_translation.set(r.translation);self.dict_category.set(r.category)
    def _dict_add(self):
        if not self.dict_original.get().strip():return
        self.dictionary_rows.append(EditableDictionaryRow(self.dict_original.get().strip(),self.dict_translation.get().strip(),self.dict_category.get().strip() or "其他","手动"));self._refresh_dictionary_tree()
    def _dict_update(self):
        sel=self.dictionary_tree.selection()
        if not sel:return
        idx=int(sel[0][1:]);self.dictionary_rows[idx]=EditableDictionaryRow(self.dict_original.get().strip(),self.dict_translation.get().strip(),self.dict_category.get().strip() or "其他",self.dictionary_rows[idx].source);self._refresh_dictionary_tree();self.dictionary_tree.selection_set(f"d{idx}")
    def _delete_dictionary_rows(self):
        indexes=sorted([int(i[1:]) for i in self.dictionary_tree.selection()],reverse=True)
        for i in indexes:del self.dictionary_rows[i]
        self._refresh_dictionary_tree()
    def _next_dict_conflict(self):
        conflicts=dictionary_conflicts(self.dictionary_rows)
        if not conflicts:messagebox.showinfo("辞典检查","没有一词多译。",parent=self);return
        c=conflicts[self.dict_conflict_index%len(conflicts)];self.dict_conflict_index+=1
        for i,r in enumerate(self.dictionary_rows):
            if r.original==c.original:self.dictionary_tree.see(f"d{i}");self.dictionary_tree.selection_set(f"d{i}");break
        messagebox.showwarning("一词多译",f"{c.original}\n{' / '.join(c.translations)}",parent=self)
    def _export_dictionary(self):
        conflicts=dictionary_conflicts(self.dictionary_rows)
        if conflicts and not messagebox.askyesno("一词多译提醒",f"有 {len(conflicts)} 个原文含多个译文。是否仍然排序并导出全部译文？",parent=self):self._next_dict_conflict();return
        path=filedialog.asksaveasfilename(parent=self,defaultextension=".xlsx",filetypes=[("Excel","*.xlsx"),("CSV","*.csv")])
        if path:save_dictionary(Path(path),self.dictionary_rows);messagebox.showinfo("导出完成",path,parent=self)

    # ---------- page 12 dictionary check ----------
    def _build_dictcheck(self):
        tab=self.tabs["dictcheck"];tab.rowconfigure(1,weight=1);tab.columnconfigure(0,weight=1)
        bar=ttk.Frame(tab);bar.grid(row=0,column=0,sticky="ew")
        ttk.Button(bar,text="使用生成辞典页进行检查",command=self._do_dictcheck_current).pack(side="left");ttk.Button(bar,text="导入外部辞典并检查",command=self._do_dictcheck_external).pack(side="left",padx=5);self.dict_messages_only=tk.BooleanVar(value=False);ttk.Checkbutton(bar,text="仅 Message",variable=self.dict_messages_only).pack(side="left");ttk.Button(bar,text="打开文件",command=lambda:self._open_selected_result("dictcheck","translated")).pack(side="right")
        self.dictcheck_tree=self._tree(tab,("file","term","expected","translation"),("文件","命中原词","期望译文","当前译文"),(280,260,260,650));self.dictcheck_tree.master.grid(row=1,column=0,sticky="nsew");self.dictcheck_tree.bind("<Double-1>",lambda e:self._double_to_editor("dictcheck"))
    def _resolve_conflicts(self,rows):
        entries,conflicts=dictionary_entries_from_rows(rows,"first")
        if not conflicts:return entries
        dlg=ConflictDialog(self,conflicts);self.wait_window(dlg)
        if dlg.result=="dictionary":self.notebook.select(self.tabs["dictionary"]);return None
        if dlg.result=="abort":return None
        return entries
    def _do_dictcheck_current(self):
        entries=self._resolve_conflicts(self.dictionary_rows)
        if entries is not None:self._show_dict_warnings(analyze_dictionary(self.active_records(),entries,self.dict_messages_only.get()))
    def _do_dictcheck_external(self):
        paths=filedialog.askopenfilenames(parent=self,filetypes=[("辞典","*.xlsx *.xlsm *.csv")])
        if not paths:return
        try:
            rows=[]
            for p in paths:rows.extend(load_editable_dictionary(Path(p)))
            entries=self._resolve_conflicts(rows)
            if entries is not None:self._show_dict_warnings(analyze_dictionary(self.active_records(),entries,self.dict_messages_only.get()))
        except Exception as exc:messagebox.showerror("检查失败",str(exc),parent=self)
    def _show_dict_warnings(self,warnings):
        self._clear_tree(self.dictcheck_tree);self.result_maps["dictcheck"]={}
        for i,x in enumerate(warnings):iid=f"dc{i}";self.result_maps["dictcheck"][iid]=x.record;self.dictcheck_tree.insert("","end",iid=iid,values=(x.record.file_key,x.original_term,x.expected_translation,self._compact(x.record.translated)))
        self.status_var.set(f"辞典匹配警告 {len(warnings)} 条。")

    # ---------- page 13 database ----------
    def _build_database(self):
        tab=self.tabs["database"];tab.rowconfigure(3,weight=1);tab.columnconfigure(0,weight=1)
        top=ttk.Frame(tab);top.grid(row=0,column=0,sticky="ew")
        self.db_source_type=tk.StringVar(value="dictionary")
        for text,val in [("辞典", "dictionary"),("JSON", "json"),("TXT", "txt"),("Excel", "excel")]:ttk.Radiobutton(top,text=text,value=val,variable=self.db_source_type).pack(side="left")
        self.db_path1=tk.StringVar();self.db_path2=tk.StringVar();ttk.Entry(tab,textvariable=self.db_path1).grid(row=1,column=0,sticky="ew",pady=2);ttk.Entry(tab,textvariable=self.db_path2).grid(row=2,column=0,sticky="ew",pady=2)
        buttons=ttk.Frame(top);buttons.pack(side="right");ttk.Button(buttons,text="选择默认来源…",command=self._choose_db_source).pack(side="left");ttk.Button(buttons,text="比较数据库",command=self._do_database).pack(side="left",padx=5);ttk.Button(buttons,text="应用勾选翻译",command=self._apply_database).pack(side="left")
        self.database_tree=self._tree(tab,("use","status","file","original","baseline","proposal","reason"),("选择","状态","文件","当前原文","默认原文","建议译文","说明"),(60,100,220,300,300,300,350),checkbox=True);self.database_tree.master.grid(row=3,column=0,sticky="nsew");self.database_tree.bind("<Double-1>",lambda e:self._double_to_editor("database"))
    def _choose_db_source(self):
        kind=self.db_source_type.get()
        if kind=="dictionary":
            p=filedialog.askopenfilename(parent=self,filetypes=[("辞典","*.xlsx *.xlsm *.csv")]);self.db_path1.set(p or "");self.db_path2.set("")
        elif kind=="json":
            p=filedialog.askopenfilename(parent=self,filetypes=[("JSON","*.json")]);self.db_path1.set(p or "");self.db_path2.set("")
        elif kind=="excel":self.db_path1.set(filedialog.askdirectory(parent=self) or "");self.db_path2.set("")
        else:
            self.db_path1.set(filedialog.askdirectory(parent=self,title="默认原文 TXT 文件夹") or "");self.db_path2.set(filedialog.askdirectory(parent=self,title="默认译文 TXT 文件夹") or "")
    def _do_database(self):
        try:
            kind=self.db_source_type.get()
            if kind=="dictionary":props=compare_database(self.active_records(),dictionary_rows=load_editable_dictionary(Path(self.db_path1.get())))
            else:
                if kind=="json":baseline=load_json_records(Path(self.db_path1.get())).records
                elif kind=="excel":baseline=load_excel_records(Path(self.db_path1.get())).records
                else:baseline=load_txt_records(Path(self.db_path1.get()),Path(self.db_path2.get()),self.origin_encoding.get(),self.translated_encoding.get()).records
                props=compare_database(self.active_records(),list(baseline))
            self._clear_tree(self.database_tree);self.result_maps["database"]={}
            for i,x in enumerate(props):
                iid=f"db{i}";self.result_maps["database"][iid]=x;checked="☐";self.database_tree.insert("","end",iid=iid,values=(checked,x.status,x.record.file_key,self._compact(x.record.original),self._compact(x.baseline_original),self._compact(x.proposed_translation),x.reason))
            self.status_var.set(f"数据库比较完成：{len(props)} 条；Common Event 已排除。")
        except Exception as exc:messagebox.showerror("比较失败",str(exc),parent=self)
    def _apply_database(self):
        updates={}
        for iid in self._checked_iids(self.database_tree):
            x=self.result_maps.get("database",{}).get(iid)
            if isinstance(x,DatabaseProposal) and x.status=="可翻译":updates[x.record.uid]=(x.record,x.proposed_translation)
        try:
            count,backup=apply_translation_updates(updates);messagebox.showinfo("翻译完成",f"已翻译 {count} 条。\n备份：{backup}",parent=self);self._refresh_after_write()
        except Exception as exc:messagebox.showerror("翻译失败",str(exc),parent=self)

    # ---------- generic result interactions ----------
    def _open_selected_result(self,key,side):
        tree=getattr(self,key+"_tree");sel=tree.selection()
        if not sel:return
        obj=self.result_maps.get(key,{}).get(sel[0]);rec=obj.record if hasattr(obj,"record") else obj
        if isinstance(obj,DatabaseProposal):rec=obj.record
        if isinstance(rec,DataRecord):self._open_record_file(rec,side)
    def _double_to_editor(self,key):
        tree=getattr(self,key+"_tree");sel=tree.selection()
        if not sel:return
        obj=self.result_maps.get(key,{}).get(sel[0]);rec=obj.record if hasattr(obj,"record") else obj
        if isinstance(obj,DatabaseProposal):rec=obj.record
        if isinstance(rec,DataRecord):self._send_to_editor(rec)
    @staticmethod
    def _compact(text,limit=180):
        value=" ".join((text or "").replace("\r"," ").replace("\n"," ").split())
        return value if len(value)<=limit else value[:limit-1]+"…"

    # ---------- config ----------
    def _load_settings(self):
        cfg=load_config(self.config_path)
        self.source_type.set(cfg.get("source_type","txt"));self.json_path.set(cfg.get("json_path",""));self.origin_dir.set(cfg.get("origin_dir",""));self.translated_dir.set(cfg.get("translated_dir",""));self.excel_dir.set(cfg.get("excel_dir",""));self.origin_encoding.set(cfg.get("origin_encoding","936"));self.translated_encoding.set(cfg.get("translated_encoding","936"));self.display_font.set(cfg.get("display_font","系统默认"));self._update_source_visibility();self._apply_font()
    def _save_settings(self):
        save_config(self.config_path,{"source_type":self.source_type.get(),"json_path":self.json_path.get(),"origin_dir":self.origin_dir.get(),"translated_dir":self.translated_dir.get(),"excel_dir":self.excel_dir.get(),"origin_encoding":self.origin_encoding.get(),"translated_encoding":self.translated_encoding.get(),"display_font":self.display_font.get()})
    def _on_close(self):
        self._save_settings();self.destroy()


def main():
    app=WorkbenchApp();app.mainloop()


if __name__=="__main__":main()
