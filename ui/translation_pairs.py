import bisect
import json
import re
import tkinter as tk
from tkinter import ttk

from .translation_progress import search_box


class TranslationPairs(tk.Toplevel):
    """Editable translation pairs. Only visible rows own Tk widgets."""
    def __init__(self, panel, group):
        super().__init__(panel)
        self.panel, self.group = panel, group
        self.title(group)
        self.geometry("900x530")
        self.transient(panel.winfo_toplevel())
        self.grab_set()
        self.rows, self.offsets = [], [0]
        self.widgets, self.boxes, self.marked = {}, {}, set()
        self.expanded = set()
        self.attempts = None
        self.spaces, self.only, self.all_marked = (tk.BooleanVar(self) for _ in range(3))
        bar = ttk.Frame(self, padding=6)
        bar.pack(fill="x")
        entry, self.query = search_box(bar, "查找原文、译文、文件或原因", self.refresh)
        entry.pack(side="left", expand=True, fill="x")
        for label, var in [("只看未译或待修正", self.only), ("显示原文空格", self.spaces)]:
            ttk.Checkbutton(bar, text=label, variable=var, command=self.refresh).pack(side="left", padx=4)
        header = ttk.Frame(self, height=23)
        header.pack(fill="x")
        self.mark_all = ttk.Checkbutton(header, variable=self.all_marked, command=self.toggle_all)
        self.mark_all.place(x=8, y=0)
        ttk.Label(header, text="原文").place(x=38, y=1)
        ttk.Label(header, text="译文").place(relx=.45, y=1)
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        self.sheet = tk.Canvas(body, highlightthickness=0, background="white")
        scroll = ttk.Scrollbar(body, command=self.sheet.yview)
        scroll.pack(side="right", fill="y")
        self.sheet.pack(fill="both", expand=True)
        self.sheet.configure(yscrollcommand=lambda a,b: (scroll.set(a,b), self.after_idle(self.draw)
                                                          if self.winfo_exists() else None))
        self.sheet.bind("<Configure>", lambda _: self.draw())
        self.bind("<MouseWheel>", lambda e: self.sheet.yview_scroll(-int(e.delta/120)*3, "units"))
        self.batch = ttk.Frame(self, padding=5)
        self.count = ttk.Label(self.batch)
        self.count.pack(side="left")
        self.retry = ttk.Button(self.batch, text="重试所选", command=lambda: self.retry_pairs(self.chosen()))
        self.retry.pack(side="right")
        self.keep = ttk.Button(self.batch, text="保留所选原文", command=lambda: self.save({p:p[1] for p in self.chosen()}))
        self.keep.pack(side="right", padx=4)
        self.refresh()

    @property
    def project(self):
        return self.panel.project

    def refresh(self):
        p = self.project
        if not p:
            self.destroy()
            return
        for frame in self.widgets.values():
            frame.destroy()
        self.sheet.delete("all")
        self.widgets.clear()
        self.boxes.clear()
        self.rows, self.offsets = [], [0]
        query = self.query.get().strip().casefold()
        for pair in p.groups.get(self.group, ()):
            name, key = pair
            if self.only.get() and p.state(pair) == "done" and pair not in self.panel.drafts:
                continue
            value = self.panel.drafts.get(pair, p.translation(pair))
            if query and query not in f"{name}\n{key}\n{value}\n{p.reason(pair)}".casefold():
                continue
            self.rows.append(pair)
            lines = max(key.count("\n"), value.count("\n")) + 1
            height = 93 + min(7, lines)*20 + (31 if p.state(pair) == "issue" else 0)
            self.offsets.append(self.offsets[-1] + height + (100 if pair in self.expanded else 0))
        self.sheet.configure(scrollregion=(0, 0, 1, self.offsets[-1]))
        self.batch_state()
        self.draw()

    def chosen(self):
        return self.marked.intersection(self.eligible)

    def batch_state(self):
        self.eligible = {p for p in self.rows if self.project.state(p) == "issue"}
        self.marked.intersection_update(self.eligible)
        chosen = self.chosen()
        self.all_marked.set(bool(chosen and chosen == self.eligible))
        self.mark_all.state(["alternate" if chosen and chosen != self.eligible else "!alternate",
                             "!disabled" if self.eligible and self.panel.enabled else "disabled"])
        if self.eligible:
            self.batch.pack(fill="x", before=self.sheet.master)
            self.count.configure(text=f"已选 {len(chosen)} 条待修正译文")
            self.retry.state(["!disabled" if self.panel.enabled and any(self.can_retry(p) for p in chosen) else "disabled"])
            self.keep.state(["!disabled" if chosen and self.panel.enabled else "disabled"])
        else:
            self.batch.pack_forget()

    def can_retry(self, pair):
        return not self.panel.fault and pair not in self.panel.drafts and "拒绝" not in str(self.project.reason(pair))

    def toggle_all(self):
        self.marked = set() if self.marked == self.eligible else self.eligible.copy()
        self.refresh()

    def retry_pairs(self, pairs):
        self.panel.translate([p for p in pairs if self.can_retry(p)], retry=True)

    def save(self, edits):
        if not self.panel.enabled or self.panel.fault == "busy":
            return
        try:
            self.project.save(edits)
        except (OSError, ValueError, RuntimeError) as error:
            self.panel.app.log_message(str(error), "error")
            return
        for pair in edits:
            self.panel.drafts.pop(pair, None)
        self.panel.recount()
        self.refresh()

    def draw(self):
        if not self.winfo_exists():
            return
        y = self.sheet.canvasy(0)
        lo = max(0, bisect.bisect_right(self.offsets, y)-1)
        hi = min(len(self.rows), bisect.bisect_right(self.offsets, y+self.sheet.winfo_height())+1)
        for i in list(self.widgets):
            if i < lo or i >= hi:
                self.sheet.delete(f"r{i}")
                self.widgets.pop(i).destroy()
                self.boxes.pop(self.rows[i], None)
        for i in range(lo, hi):
            if i not in self.widgets:
                frame = self.make_row(self.rows[i])
                self.widgets[i] = frame
                self.sheet.create_window(0, self.offsets[i], window=frame, anchor="nw", tags=f"r{i}",
                                         height=self.offsets[i+1]-self.offsets[i], width=self.sheet.winfo_width())
            elif int(float(self.sheet.itemcget(f"r{i}", "width"))) != self.sheet.winfo_width():
                self.sheet.itemconfigure(f"r{i}", width=self.sheet.winfo_width())

    def make_row(self, pair):
        p = self.project
        name, key = pair
        frame = ttk.Frame(self.sheet, padding=(5, 4), relief="groove", borderwidth=1)
        frame.columnconfigure(1, weight=4, uniform="pair")
        frame.columnconfigure(2, weight=5, uniform="pair")
        frame.rowconfigure(1, weight=1)
        state = p.state(pair)
        if state == "issue":
            var = tk.BooleanVar(frame, value=pair in self.marked)
            def mark():
                if var.get():
                    self.marked.add(pair)
                else:
                    self.marked.discard(pair)
                self.batch_state()
            ttk.Checkbutton(frame, variable=var, command=mark).grid(row=0, column=0)
        else:
            frame.grid_columnconfigure(0, minsize=25)
        ttk.Label(frame, text=f'{name}　{p.source[name][key].get("original_marker","")}').grid(row=0, column=1, sticky="w")
        source = tk.Text(frame, wrap="word", width=1, height=1, borderwidth=0, takefocus=False)
        source.grid(row=1, column=1, sticky="nsew", padx=(0,6))
        source.insert("1.0", key.replace(" ", "·").replace("　", "□") if self.spaces.get() else key)
        source.configure(state="disabled")
        box = tk.Text(frame, wrap="word", height=1, width=1, undo=True)
        box.grid(row=1, column=2, sticky="nsew")
        self.boxes[pair] = box
        base = p.translation(pair)
        box.insert("1.0", self.panel.drafts.get(pair, base))
        box.edit_modified(False)
        actions = ttk.Frame(frame)
        actions.grid(row=3, column=1, columnspan=2, sticky="ew", pady=2)
        status = ttk.Label(actions)
        status.pack(side="left")
        save = ttk.Button(actions, text="保存", width=6, command=lambda: self.save({pair:box.get("1.0","end-1c")}))
        undo = ttk.Button(actions, text="撤销修改", width=9,
                          command=lambda: (self.panel.drafts.pop(pair,None), self.panel.recount(), self.refresh()))
        save.pack(side="right")
        undo.pack(side="right")
        retry = None
        writable = self.panel.enabled and self.panel.fault != "busy"
        if state == "issue":
            reason = ttk.Label(frame, text=p.reason(pair), foreground="#83502c")
            reason.grid(row=2, column=1, columnspan=2, sticky="ew")
            for label, callback in [("上下文和尝试记录", lambda: self.show_details(pair, frame)),
                                    ("保留原文", lambda: self.save({pair:key})),
                                    ("重试本条", lambda: self.retry_pairs([pair]))]:
                button = ttk.Button(actions, text=label, command=callback)
                button.pack(side="right", padx=2)
                if label == "重试本条":
                    retry = button
                if not writable or (label == "重试本条" and not self.can_retry(pair)):
                    button.state(["disabled"])
        def changed(_=None):
            if not box.edit_modified() and _ is not None:
                return
            value = box.get("1.0", "end-1c")
            dirty = value != base
            had_draft = pair in self.panel.drafts
            if dirty:
                self.panel.drafts[pair] = value
            else:
                self.panel.drafts.pop(pair, None)
            box.edit_modified(False)
            problem = p.validate(pair, value) if dirty or state == "issue" else ""
            manual = p.results.get(name, {}).get(key, {}).get("manual")
            label = "未保存" if dirty else {"new":"未译", "issue":"待修正", "done":"手动已保存" if manual else "已译"}[state]
            if (dirty or state == "issue") and re.search(r"[ぁ-ゖァ-ヺ]", value):
                label += "　含日语假名，请核对后保存。"
            status.configure(text=problem or label)
            save.state(["!disabled" if writable and (dirty or state == "issue") and value.strip()
                        and not problem else "disabled"])
            undo.state(["!disabled" if dirty and writable else "disabled"])
            if retry is not None:
                retry.state(["!disabled" if writable and self.can_retry(pair) else "disabled"])
                if dirty != had_draft:
                    self.batch_state()
        changed()
        box.bind("<<Modified>>", changed)
        box.bind("<Control-Return>", lambda _: (save.invoke(), "break")[1])
        if not writable:
            box.configure(state="disabled")
        if pair in self.expanded:
            self.details_text(pair, frame)
        return frame

    def show_details(self, pair, frame):
        # Expand in the row instead of opening another dialog.
        text = getattr(frame, "details", None)
        index = self.rows.index(pair)
        delta = -100 if text else 100
        if text:
            text.destroy()
            frame.details = None
            self.expanded.discard(pair)
        else:
            self.expanded.add(pair)
            self.details_text(pair, frame)
        for i in range(index+1, len(self.offsets)):
            self.offsets[i] += delta
        for i in self.widgets:
            self.sheet.coords(f"r{i}", 0, self.offsets[i])
            self.sheet.itemconfigure(f"r{i}", height=self.offsets[i+1]-self.offsets[i])
        self.sheet.configure(scrollregion=(0,0,1,self.offsets[-1]))

    def details_text(self, pair, frame):
        name, key = pair
        if self.attempts is None:
            self.attempts = {}
            journal = self.project.output.with_name("translation_requests.jsonl")
            if journal.exists():
                with journal.open("rb") as stream:
                    for line in stream:
                        try:
                            row = json.loads(line)
                        except (UnicodeError, ValueError):
                            continue
                        for item in row.get("sources", ()):
                            reason = (row.get("issues") or {}).get(str(item["id"]))
                            if reason:
                                self.attempts.setdefault((row.get("file"), item["original"]), []).append(reason)
        keys = list(self.project.source[name])
        j = keys.index(key)
        previous = keys[j-1] if j else ""
        content = "\n".join(filter(None, [previous, self.project.translation((name, previous)),
                              *self.attempts.get(pair, [str(self.project.reason(pair))])]))
        text = frame.details = tk.Text(frame, height=4, wrap="word")
        text.grid(row=4, column=1, columnspan=2, sticky="ew")
        text.insert("1.0", content)
        text.configure(state="disabled")

    def destroy(self):
        self.panel.editor = None
        super().destroy()
        if self.panel.table.winfo_exists():
            self.panel.recount()
