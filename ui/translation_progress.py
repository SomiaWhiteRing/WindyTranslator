import queue
import threading
import tkinter as tk
from collections import defaultdict
from tkinter import ttk

from core.tasks.supplement import TYPES
from core.tasks.translation_progress import ProgressProject


def search_box(parent, hint, changed):
    value = tk.StringVar(parent)
    placeholder = tk.StringVar(parent, value=hint)
    ttk.Style(parent).configure("SearchHint.TEntry", foreground="#777777")
    entry = ttk.Entry(parent, textvariable=placeholder, style="SearchHint.TEntry")

    def update(*_):
        show_hint = not value.get() and entry.focus_get() != entry
        display = placeholder if show_hint else value
        if str(entry.cget("textvariable")) != str(display):
            entry.configure(textvariable=display, style="SearchHint.TEntry" if show_hint else "TEntry")

    entry.bind("<FocusIn>", update)
    entry.bind("<FocusOut>", update)
    value.trace_add("write", lambda *_: (update(), changed()))
    return entry, value


def bind_tooltip(button, text):
    tooltip = None

    def show(_event):
        nonlocal tooltip
        if tooltip is None:
            tooltip = tk.Toplevel(button)
            tooltip.overrideredirect(True)
            ttk.Label(tooltip, text=text, padding=(5, 2), relief="solid", borderwidth=1).pack()
            tooltip.geometry(f"+{button.winfo_rootx()}+{button.winfo_rooty() + button.winfo_height() + 2}")

    def hide(_event):
        nonlocal tooltip
        if tooltip is not None:
            tooltip.destroy()
            tooltip = None

    button.bind("<Enter>", show)
    for event in ("<Leave>", "<ButtonPress>", "<Unmap>"):
        button.bind(event, hide)


class TranslationProgress(ttk.Frame):
    row_height = 29

    def __init__(self, parent, app):
        super().__init__(parent, padding=6, takefocus=True)
        self.app = app
        self.enabled = True
        self.project = self.editor = None
        self.path = ""
        self.generation = 0
        self.loading = None
        self.selected, self.drafts = set(), {}
        self.visible, self.stats, self.index = [], {}, {}
        self.focus_index = 0
        self.fault = ""
        self.last_request = None
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        bar = ttk.Frame(self)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 5))
        bar.columnconfigure(0, weight=1)
        self.fault_bar = ttk.Frame(self)
        self.fault_label = ttk.Label(self.fault_bar)
        self.fault_label.pack(side="left")
        self.recover = ttk.Button(self.fault_bar, command=self.resume)
        self.recover.pack(side="right")
        self.configure_api = ttk.Button(self.fault_bar, text="接口设置",
                                        command=lambda: self.app.start_task("configure_deepseek"))
        entry, self.query = search_box(bar, "查找文件、地图名、原文或译文……", self.filter)
        entry.grid(row=0, column=0, sticky="nsew")
        self.extract = ttk.Button(bar, text="补充提取…", padding=(5, 0), command=self.choose_types)
        self.extract.grid(row=0, column=1, padx=5, sticky="ns")
        self.submit = ttk.Button(bar, text="提交翻译", padding=(5, 0), command=self.submit_files)
        self.submit.grid(row=0, column=2, sticky="ns")
        bind_tooltip(self.extract, "补充未导出的文本")
        bind_tooltip(self.submit, "将选中的条目提交翻译")
        header = ttk.Frame(self, height=26)
        header.grid(row=1, column=0, sticky="ew")
        self.all_var = tk.IntVar(self)
        self.all = ttk.Checkbutton(header, variable=self.all_var, command=self.toggle_all)
        self.all.place(x=5, rely=.5, anchor="w")
        for fraction, pos, label in [(0, 32, "源文件"), (.31, 0, "内容"), (.58, 0, "状态"),
                                     (.72, 0, "翻译进度（已完成 / 总数）")]:
            ttk.Label(header, text=label).place(relx=fraction, x=pos, rely=.5, anchor="w")
        self.table = tk.Canvas(self, background="white", highlightthickness=1, highlightbackground="#aaa",
                               height=170, width=300, takefocus=True, yscrollincrement=self.row_height)
        self.table.grid(row=2, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(self, command=self.table.yview)
        scroll.grid(row=2, column=1, sticky="ns")
        self.table.configure(yscrollcommand=lambda a, b: (scroll.set(a, b), self.draw()))
        self.table.bind("<Configure>", lambda _: self.draw())
        self.table.bind("<MouseWheel>", lambda e: self.table.yview_scroll(-int(e.delta / 120), "units"))
        self.table.bind("<Button-1>", self.click)
        self.table.bind("<Double-Button-1>", self.double_click)
        self.table.bind("<space>", lambda _: self.toggle(self.focus_index))
        self.table.bind("<Return>", lambda _: self.open_file(self.focus_index))
        for event, step in [("<Up>", -1), ("<Down>", 1)]:
            self.table.bind(event, lambda _, d=step: self.move(d))
        foot = ttk.Frame(self)
        foot.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        self.count = ttk.Label(foot)
        self.count.pack(side="left")
        ttk.Label(foot, text="双击以手动编辑").pack(side="right")
        self.filter()

    def refresh(self):
        path = self.app.get_game_path()
        files_only = self.master.select() != str(self)
        request = (path, files_only)
        if path and self.enabled and self.loading == request:
            return
        self.generation += 1
        self.loading = None
        generation = self.generation
        if path != self.path:
            self.path = path
            self.last_request = None
            self.show_fault(path, "")
            self.selected.clear()
            self.drafts.clear()
            self.project = None
            self.recount()
        if not path or not self.enabled:
            return
        self.loading = request
        loaded = queue.SimpleQueue()

        def worker():
            try:
                encoding = None if files_only else self.app.get_output_profile(path)["source_encoding"]
                loaded.put(ProgressProject(path, self.app.works_dir, encoding, files_only=files_only))
            except Exception as error:
                loaded.put(error)

        def ready():
            if generation != self.generation or self.app.get_game_path() != path:
                return
            if loaded.empty():
                self.after(80, ready)
                return
            self.loading = None
            result = loaded.get()
            readable = isinstance(result, ProgressProject) and result.readable
            self.app.main_window.set_progress_tab_visible(readable)
            if not readable:
                self.project = None
                if isinstance(result, Exception) and not files_only:
                    self.app.log_message(str(result), "error")
            elif files_only:
                return
            else:
                self.project = result
                if result.busy:
                    self.show_fault(path, "busy")
                elif self.fault == "busy":
                    self.show_fault(path, "")
                if getattr(result, "scan_error", ""):
                    self.app.log_message(result.scan_error, "warning")
            self.recount()
            if self.editor and self.editor.winfo_exists():
                self.editor.refresh()

        threading.Thread(target=worker, daemon=True).start()
        self.after(30, ready)

    def recount(self):
        self.stats, self.index = {}, {}
        p = self.project
        for name, pairs in (p.groups.items() if p else ()):
            states = [p.state(pair) for pair in pairs]
            self.stats[name] = (states.count("done"), states.count("issue"), len(pairs),
                                any(state == "new" and pair not in self.drafts for state, pair in zip(states, pairs)))
            self.index[name] = "\n".join([name, p.labels[name], *(f"{f}\n{k}\n{p.translation((f,k))}"
                                         for f, k in pairs)]).casefold()
        self.selected.intersection_update(k for k, (d, _, n, _) in self.stats.items() if d < n)
        self.filter()

    def filter(self):
        query = self.query.get().strip().casefold()
        self.visible = sorted(k for k in self.stats if query in self.index[k])
        self.focus_index = min(self.focus_index, max(0, len(self.visible) - 1))
        self.table.configure(scrollregion=(0, 0, 0, len(self.visible) * self.row_height))
        self.controls()

    def controls(self):
        eligible = {k for k in self.visible if self.stats[k][0] < self.stats[k][2]}
        chosen = eligible & self.selected
        self.count.configure(text=f"已选 {len(chosen)} 个文件")
        self.all_var.set(int(bool(eligible and eligible == chosen)))
        self.all.state(["alternate" if chosen and chosen != eligible else "!alternate",
                        "!disabled" if eligible and self.enabled else "disabled"])
        self.submit.state(["!disabled" if self.enabled and not self.fault and any(self.stats[k][3] for k in chosen)
                           else "disabled"])
        self.extract.state(["!disabled" if self.enabled and self.fault != "busy" and self.project
                            and self.project.missing else "disabled"])
        self.recover.state(["!disabled" if self.enabled else "disabled"])
        self.draw()

    def draw(self):
        c = self.table
        c.delete("row")
        h, w = self.row_height, c.winfo_width()
        start = max(0, int(c.canvasy(0) // h))
        for i in range(start, min(len(self.visible), start + c.winfo_height() // h + 2)):
            name = self.visible[i]
            done, issues, total, _ = self.stats[name]
            y, mid = i * h + 1, (i + .5) * h
            checked = name in self.selected
            c.create_rectangle(1, y, w, y+h-1, fill="#eff5fc" if checked else "white", outline="", tags="row")
            if done:
                c.create_rectangle(1, y, w*done/total, y+h-1, fill="#d5ebdb" if checked else "#e2f1e5",
                                   outline="", tags="row")
            if checked or (i == self.focus_index and c.focus_get() == c):
                c.create_rectangle(1, y, w-1, y+h-1, outline="#8baec5", tags="row")
            c.create_rectangle(7, mid-6, 19, mid+6, fill="#eee" if done == total else "white",
                               outline="#ccc" if done == total else "#777", tags="row")
            if checked:
                points = (9, mid, 17, mid) if done else (9, mid, 12, mid+3, 18, mid-4)
                c.create_line(*points, width=2, fill="#365a77", tags="row")
            status = "需修正" if issues else "已完成" if done == total else "待翻译"
            percent = 100 if done == total else min(99, round(100*done/total))
            for x, end, value in [(32,.31*w,name),(.31*w,.58*w,self.project.labels[name]),
                                  (.58*w,.72*w,status),(.72*w,w,f"{done} / {total}   {percent}%")]:
                # Clip long filenames and map labels before the next column.
                font = self.tk.call("ttk::style", "lookup", "TLabel", "-font") or "TkDefaultFont"
                if self.tk.call("font", "measure", font, value) > end-x-4:
                    while value and self.tk.call("font", "measure", font, value+"…") > end-x-4:
                        value = value[:-1]
                    value += "…"
                c.create_text(x, mid, text=value, anchor="w", font=font, tags="row")

    def toggle(self, index):
        if self.enabled and 0 <= index < len(self.visible):
            name = self.visible[index]
            if self.stats[name][0] != self.stats[name][2]:
                self.selected.symmetric_difference_update({name})
            self.controls()

    def click(self, event):
        self.table.focus_set()
        index = int(self.table.canvasy(event.y) // self.row_height)
        if 0 <= index < len(self.visible):
            self.focus_index = index
            self.before_click = self.selected.copy()
            self.toggle(index)

    def double_click(self, event):
        index = int(self.table.canvasy(event.y) // self.row_height)
        if 0 <= index < len(self.visible):
            self.selected = getattr(self, "before_click", self.selected)
            self.controls()
            self.open_file(index)
        return "break"

    def move(self, step):
        if self.visible:
            self.focus_index = max(0, min(self.focus_index+step, len(self.visible)-1))
            y = self.focus_index * self.row_height
            if not self.table.canvasy(0) <= y <= self.table.canvasy(self.table.winfo_height()-self.row_height):
                self.table.yview_scroll(step, "units")
            self.draw()
        return "break"

    def toggle_all(self):
        eligible = {k for k in self.visible if self.stats[k][0] < self.stats[k][2]}
        if eligible <= self.selected:
            self.selected.difference_update(eligible)
        else:
            self.selected.update(eligible)
        self.controls()

    def open_file(self, index):
        if self.editor and self.editor.winfo_exists():
            self.editor.lift()
            return
        if self.project and 0 <= index < len(self.visible):
            from .translation_pairs import TranslationPairs
            self.editor = TranslationPairs(self, self.visible[index])

    def submit_files(self):
        pairs = (p for k in self.visible if k in self.selected for p in self.project.groups[k]
                 if self.project.state(p) == "new" and p not in self.drafts)
        self.translate(pairs)

    def translate(self, pairs, retry=False):
        selected = defaultdict(set)
        for name, key in pairs:
            selected[name].add(key)
        if selected and self.enabled and not self.fault and self.path == self.app.get_game_path():
            self.last_request = (selected, retry)
            self.app.start_task("translate_selected", game_path=self.path,
                                task_payload=dict(selected_entries=dict(selected), retry=retry))

    def show_fault(self, path, kind):
        if path != self.path:
            return
        self.fault = kind
        if kind:
            label = {"authentication": "密钥或权限异常", "quota": "余额不足", "configuration": "接口配置异常",
                     "busy": "另一实例正在处理此作品", "transient": "连接失败或响应不完整",
                     "rate": "接口限流", "empty": "接口未返回有效译文"}.get(kind, "接口调用失败")
            self.fault_label.configure(text=f"任务暂停：{label}")
            self.recover.configure(text={"busy":"检查占用", "rate":"稍后续跑"}.get(kind, "检查并继续"))
            self.fault_bar.grid(row=4, column=0, columnspan=2, sticky="ew")
            if kind == "busy":
                self.configure_api.pack_forget()
            else:
                self.configure_api.pack(side="right")
        else:
            self.fault_bar.grid_remove()
        self.controls()

    def resume(self):
        self.show_fault(self.path, "")
        if self.last_request and self.project:
            selected, retry = self.last_request
            target = "issue" if retry else "new"
            self.translate(((name, key) for name, keys in selected.items() for key in keys
                            if key in self.project.source.get(name, {}) and (name, key) not in self.drafts
                            and self.project.state((name,key)) == target), retry)
        else:
            self.refresh()

    def choose_types(self):
        dialog = tk.Toplevel(self)
        dialog.title("补充提取")
        dialog.transient(self.winfo_toplevel())
        dialog.grab_set()
        box = ttk.LabelFrame(dialog, text="未提取的文本类型", padding=10)
        box.pack(fill="x", padx=10, pady=8)
        choices = {}
        for key, title in TYPES.items():
            if key in self.project.missing:
                count = sum(map(len, self.project.missing[key].values()))
                choices[key] = tk.BooleanVar(dialog)
                ttk.Checkbutton(box, text=f"{title}（{count}）", variable=choices[key],
                                command=lambda: confirm.state(["!disabled" if any(v.get() for v in choices.values())
                                                               else "disabled"])).pack(anchor="w")
        def apply():
            kinds = [k for k,v in choices.items() if v.get()]
            dialog.destroy()
            self.last_request = None
            self.app.start_task("supplement", game_path=self.path, task_payload=kinds)
        confirm = ttk.Button(dialog, text="补充提取", command=apply, state="disabled")
        confirm.pack(side="right", padx=8, pady=(0, 8))
        ttk.Button(dialog, text="取消", command=dialog.destroy).pack(side="right", pady=(0, 8))
