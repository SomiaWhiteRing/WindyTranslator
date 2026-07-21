"""仅在识别到 WOLF 游戏后挂载的隐藏实验性字体面板。"""

import os
import threading
import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox, ttk

from core.engines import wolf
from core.utils import file_system, font_coverage


SLOT_NAMES = ("主字体  font[0]", "二号字体  font[1]", "三号字体  font[2]", "四号字体  font[3]")
SOURCE_NAMES = {"module": "内置", "game": "游戏", "system": "系统"}
KEEP_CURRENT = "保持当前字体"
CLEAR_SLOT = "清空槽位"
DEFAULT_SAMPLE = "中文：风雪之夜，勇者获得新技能。  日本語：風雪の夜、勇者は新しい技を覚えた。  ABC 123"


class WolfFontPanel(ttk.Frame):
    def __init__(self, parent, app_controller):
        super().__init__(parent, padding="8")
        self.app = app_controller
        self.pack(fill=tk.BOTH, expand=True)
        self.game_path = ""
        self.context = None
        self.candidates = []
        self.visible_candidates = []
        self.candidate_by_label = {}
        self.catalog_fingerprint = None
        self._load_token = 0
        self._loading = False
        self._visible = False
        self._poll_after_id = None
        self._font_apply_active = False
        self._registered_fonts = set()
        self._preloaded_context = None
        self.required_characters = set()
        self.required_from_scripts = False

        self.sample_var = tk.StringVar(value=DEFAULT_SAMPLE)
        self.size_var = tk.IntVar(value=12)
        self.status_var = tk.StringVar(value="正在等待 WOLF 项目")
        self.selection_vars = [tk.StringVar(value=KEEP_CURRENT) for _ in range(4)]
        self.original_family_vars = [tk.StringVar(value="-") for _ in range(4)]
        self.coverage_vars = [tk.StringVar(value="") for _ in range(4)]
        self.preview_fonts = []
        self._active_preview_fonts = []
        self.combos = []
        self.controls = []
        self._build()
        self.sample_var.trace_add("write", lambda *_: self._update_previews())
        self.size_var.trace_add("write", lambda *_: self._update_previews())

    def _build(self):
        preview_background = ttk.Style(self).lookup("TFrame", "background") or "SystemButtonFace"
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.font_area = ttk.Frame(self)
        self.font_area.grid(row=0, column=0, sticky="nsew")
        self.canvas = tk.Canvas(
            self.font_area,
            height=1,
            highlightthickness=0,
            background=preview_background,
        )
        scrollbar = ttk.Scrollbar(self.font_area, orient=tk.VERTICAL, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        content = ttk.Frame(self.canvas, padding=(0, 0, 6, 0))
        content_window = self.canvas.create_window((0, 0), window=content, anchor="nw")
        content.bind(
            "<Configure>",
            lambda _event: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self.canvas.bind(
            "<Configure>",
            lambda event: self.canvas.itemconfigure(content_window, width=event.width),
        )
        self.canvas.bind("<Enter>", lambda _event: self.canvas.bind_all("<MouseWheel>", self._on_mousewheel))
        self.canvas.bind("<Leave>", lambda _event: self.canvas.unbind_all("<MouseWheel>"))

        tools = ttk.Frame(content)
        tools.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        tools.columnconfigure(1, weight=1)
        ttk.Label(tools, text="示例文本").grid(row=0, column=0, padx=(0, 6))
        sample_entry = ttk.Entry(tools, textvariable=self.sample_var)
        sample_entry.grid(row=0, column=1, sticky="ew", padx=(0, 10))
        ttk.Label(tools, text="字号").grid(row=0, column=2, padx=(0, 4))
        size_spin = ttk.Spinbox(tools, from_=8, to=48, width=5, textvariable=self.size_var)
        size_spin.grid(row=0, column=3)
        self.controls.extend((sample_entry, size_spin))

        ttk.Label(content, text="字体槽位", anchor="w").grid(row=1, column=0, sticky="ew", padx=(0, 8))
        ttk.Label(content, text="项目最初字体", anchor="w").grid(row=1, column=1, sticky="ew", padx=8)
        ttk.Label(content, text="待应用字体", anchor="w").grid(row=1, column=2, sticky="ew", padx=(8, 0))
        content.columnconfigure(1, weight=1, uniform="preview")
        content.columnconfigure(2, weight=1, uniform="preview")

        for index, slot_name in enumerate(SLOT_NAMES):
            row = 2 + index
            ttk.Label(content, text=slot_name, width=18).grid(row=row, column=0, sticky="nw", pady=5, padx=(0, 8))

            original = ttk.Frame(content)
            original.grid(row=row, column=1, sticky="nsew", padx=8, pady=5)
            original.columnconfigure(0, weight=1)
            ttk.Label(original, textvariable=self.original_family_vars[index]).grid(row=0, column=0, sticky="w")
            original_sample = ttk.Label(
                original,
                anchor="nw",
                justify=tk.LEFT,
                wraplength=320,
            )
            original_sample.grid(row=1, column=0, sticky="ew", pady=(3, 0))

            replacement = ttk.Frame(content)
            replacement.grid(row=row, column=2, sticky="nsew", padx=(8, 0), pady=5)
            replacement.columnconfigure(0, weight=1)
            combo = ttk.Combobox(replacement, textvariable=self.selection_vars[index], state="readonly")
            combo.grid(row=0, column=0, sticky="ew")
            combo.bind("<<ComboboxSelected>>", lambda _event, i=index: self._on_selection(i))
            replacement_sample = ttk.Label(
                replacement,
                anchor="nw",
                justify=tk.LEFT,
                wraplength=320,
            )
            replacement_sample.grid(row=1, column=0, sticky="ew", pady=(3, 0))
            ttk.Label(
                replacement,
                textvariable=self.coverage_vars[index],
                wraplength=320,
            ).grid(row=2, column=0, sticky="w")
            self.preview_fonts.append((original_sample, replacement_sample))
            self.combos.append(combo)
            self.controls.append(combo)

        self.actions_frame = ttk.Frame(self)
        self.actions_frame.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self.actions_frame.columnconfigure(0, weight=1)
        ttk.Label(self.actions_frame, textvariable=self.status_var).grid(row=0, column=0, sticky="w")
        self.restore_button = ttk.Button(self.actions_frame, text="恢复项目最初字体", command=self.restore_original)
        self.restore_button.grid(row=0, column=1, padx=5)
        self.start_button = ttk.Button(
            self.actions_frame,
            text="启动游戏",
            command=lambda: self.app.start_task("start_game"),
        )
        self.start_button.grid(row=0, column=2, padx=5)
        self.apply_button = ttk.Button(self.actions_frame, text="应用字体修订", command=self.apply_revision)
        self.apply_button.grid(row=0, column=3, padx=(5, 0))
        self.controls.extend((self.restore_button, self.start_button, self.apply_button))
        self._set_loaded(False)

    def _on_mousewheel(self, event):
        self.canvas.yview_scroll(int(-event.delta / 120), "units")

    def get_controls(self):
        return self.controls

    def set_game_path(self, game_path, context=None):
        game_path = os.path.abspath(game_path) if game_path else ""
        if os.path.normcase(game_path) == os.path.normcase(self.game_path):
            if context is not None:
                self._preloaded_context = context
                self.refresh()
            return
        self.game_path = game_path
        self._preloaded_context = context
        self.context = None
        self._load_token += 1
        self._set_loaded(False)
        self.status_var.set("正在读取字体配置和字体目录...")
        self._reload_async(self._load_token)

    def set_visible(self, visible):
        self._visible = bool(visible)
        if self._visible:
            self._schedule_poll()
        elif self._poll_after_id:
            self.after_cancel(self._poll_after_id)
            self._poll_after_id = None

    def refresh(self):
        if self.game_path and not self._font_apply_active:
            self._load_token += 1
            self._reload_async(self._load_token)

    def begin_apply(self):
        self._font_apply_active = True
        self._load_token += 1
        if self._poll_after_id:
            self.after_cancel(self._poll_after_id)
            self._poll_after_id = None
        self._release_private_fonts()

    def finish_apply(self):
        if not self._font_apply_active:
            return
        self._font_apply_active = False
        self.refresh()
        self._schedule_poll()

    def _release_private_fonts(self):
        for path in list(self._registered_fonts):
            font_coverage.unregister_private_font(path)
        self._registered_fonts.clear()

    def close(self):
        self.set_visible(False)
        self.canvas.unbind_all("<MouseWheel>")
        self._release_private_fonts()

    def _reload_async(self, token):
        if self._font_apply_active or self._loading or not self.game_path:
            return
        self._loading = True
        game_path = self.game_path
        preloaded_context = self._preloaded_context
        self._preloaded_context = None

        def worker():
            try:
                module_root = os.path.join(file_system.get_application_path(), "modules", "WOLF")
                context = preloaded_context or wolf.get_font_revision_context(game_path)
                candidates = font_coverage.discover_font_candidates(module_root, game_path)
                required, from_scripts = wolf.font_revision_required_characters(game_path, self.sample_var.get())
                visible_candidates = font_coverage.visible_font_candidates(candidates, required)
                fingerprint = font_coverage.font_catalog_fingerprint(module_root, game_path)
                error = None
            except Exception as caught:
                context = candidates = visible_candidates = required = fingerprint = None
                from_scripts = False
                error = str(caught)
            self.after(0, lambda: self._finish_reload(
                token,
                context,
                candidates,
                visible_candidates,
                required,
                from_scripts,
                fingerprint,
                error,
            ))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_reload(
        self,
        token,
        context,
        candidates,
        visible_candidates,
        required,
        from_scripts,
        fingerprint,
        error,
    ):
        self._loading = False
        if not self.winfo_exists():
            return
        if token != self._load_token:
            self._reload_async(self._load_token)
            return
        if error:
            self.status_var.set(f"字体配置读取失败：{error}")
            self._set_loaded(False)
            return
        self.context = context
        self.candidates = candidates
        self.visible_candidates = visible_candidates
        self.required_characters = required
        self.required_from_scripts = from_scripts
        self.catalog_fingerprint = fingerprint
        self._register_local_fonts()
        available_families = tkfont.families(self)
        for candidate in self.candidates:
            candidate["preview_family"] = font_coverage.resolve_available_font_family(
                candidate,
                available_families,
            )
        self._populate_choices()
        self._set_loaded(True)
        self.status_var.set(f"已加载 {len(visible_candidates)} 个字体选项")
        self._update_previews()

    def _register_local_fonts(self):
        desired = {
            item["path"]
            for candidate in self.candidates
            if candidate["source"] in ("module", "game")
            for item in candidate["files"]
        }
        for path in self._registered_fonts - desired:
            font_coverage.unregister_private_font(path)
        for path in desired - self._registered_fonts:
            font_coverage.register_private_font(path)
        self._registered_fonts = desired

    def _populate_choices(self):
        previous = [self._selection_descriptor(index) for index in range(4)]
        saved = self.context["selected_slots"]
        self.candidate_by_label = {}
        labels = []
        for candidate in self.visible_candidates:
            label = f"[{SOURCE_NAMES[candidate['source']]}] {candidate['family']}"
            suffix = 2
            base = label
            while label in self.candidate_by_label:
                label = f"{base} ({suffix})"
                suffix += 1
            self.candidate_by_label[label] = candidate
            labels.append(label)

        available = {
            (candidate["source"], candidate["family"])
            for candidate in self.visible_candidates
        }
        saved_descriptors = [
            (item.get("source"), item.get("family"))
            if isinstance(item, dict) else None
            for item in saved
        ]
        for descriptor in (*previous, *saved_descriptors):
            if not descriptor or descriptor in available:
                continue
            source, family = descriptor
            if source in ("current", "empty") or not family:
                continue
            label = f"[不可用] [{SOURCE_NAMES.get(source, source)}] {family}"
            if label in self.candidate_by_label:
                continue
            self.candidate_by_label[label] = {
                "source": source,
                "family": family,
                "files": [],
                "unavailable": True,
            }
            labels.append(label)
        for index, combo in enumerate(self.combos):
            values = [KEEP_CURRENT]
            if index:
                values.append(CLEAR_SLOT)
            values.extend(labels)
            combo.configure(values=values)

        for index, family in enumerate(self.context["original_slots"]):
            self.original_family_vars[index].set(family or "未设置")
        for index in range(4):
            descriptor = previous[index]
            if descriptor and self._set_descriptor(index, descriptor):
                continue
            item = saved[index] if index < len(saved) and isinstance(saved[index], dict) else {}
            if item.get("source") == "current":
                self.selection_vars[index].set(KEEP_CURRENT)
            elif not item.get("family") and index:
                self.selection_vars[index].set(CLEAR_SLOT)
            elif not self._set_descriptor(index, (item.get("source"), item.get("family"))):
                self.selection_vars[index].set(KEEP_CURRENT)

    def _selection_descriptor(self, index):
        value = self.selection_vars[index].get()
        if value == KEEP_CURRENT:
            return ("current", self.context["applied_slots"][index]) if self.context else None
        if value == CLEAR_SLOT:
            return ("empty", "")
        candidate = self.candidate_by_label.get(value)
        return (candidate["source"], candidate["family"]) if candidate else None

    def _set_descriptor(self, index, descriptor):
        source, family = descriptor
        if source == "current" and self.context and family == self.context["applied_slots"][index]:
            self.selection_vars[index].set(KEEP_CURRENT)
            return True
        if not family and index:
            self.selection_vars[index].set(CLEAR_SLOT)
            return True
        for label, candidate in self.candidate_by_label.items():
            if candidate["source"] == source and candidate["family"] == family:
                self.selection_vars[index].set(label)
                return True
        return False

    def _candidate_for_family(self, family):
        for source in ("game", "module", "system"):
            for candidate in self.candidates:
                if candidate["source"] == source and candidate["family"].casefold() == family.casefold():
                    return candidate
        return None

    def _selection(self, index, for_coverage=False):
        value = self.selection_vars[index].get()
        if value == CLEAR_SLOT:
            return {"family": "", "source": "empty", "files": []}
        if value == KEEP_CURRENT:
            family = self.context["applied_slots"][index]
            candidate = self._candidate_for_family(family) if family else None
            files = candidate["files"] if candidate and for_coverage else []
            return {"family": family, "source": "current", "files": files}
        candidate = self.candidate_by_label.get(value)
        if not candidate or candidate.get("unavailable"):
            return None
        return candidate

    def _on_selection(self, _index):
        self._update_previews()

    def _update_previews(self):
        if not self.context:
            return
        try:
            size = max(8, min(48, int(self.size_var.get())))
        except (tk.TclError, ValueError):
            size = 22
        sample = self.sample_var.get()
        self._active_preview_fonts = []
        for index, (original_widget, replacement_widget) in enumerate(self.preview_fonts):
            original_family = self.context["original_slots"][index]
            selection = self._selection(index, for_coverage=True)
            replacement_family = selection["family"] if selection else ""
            original_candidate = self._candidate_for_family(original_family) if original_family else None
            original_preview_family = (
                original_candidate.get("preview_family", original_family)
                if original_candidate else original_family
            )
            replacement_preview_family = (
                selection.get("preview_family", replacement_family)
                if selection else replacement_family
            )
            original_font = tkfont.Font(self, family=original_preview_family or "TkDefaultFont", size=size)
            replacement_font = tkfont.Font(self, family=replacement_preview_family or "TkDefaultFont", size=size)
            self._active_preview_fonts.extend((original_font, replacement_font))
            self._set_preview_text(original_widget, sample if original_family else "未设置", original_font)
            self._set_preview_text(replacement_widget, sample if replacement_family else "未设置", replacement_font)
            self.coverage_vars[index].set(self._coverage_text(selection))
        self._update_apply_state()

    def _set_preview_text(self, widget, text, font):
        widget.configure(text=text, font=font)

    def _coverage_text(self, selection):
        if not self.required_from_scripts:
            return ""
        if not selection or not selection.get("family"):
            return "未设置"
        paths = [item["path"] for item in selection.get("files", []) if os.path.isfile(item.get("path", ""))]
        if not paths:
            return "无法定位字体文件，预览可能使用系统回退"
        try:
            missing = font_coverage.missing_characters_in_files(paths, self.required_characters)
        except (OSError, font_coverage.FontCoverageError) as error:
            return f"覆盖检查失败：{error}"
        if not missing:
            return f"覆盖全部 {len(self.required_characters)} 个检查字符"
        return f"缺少 {len(missing)} 字：{''.join(sorted(missing))[:24]}"

    def _update_apply_state(self):
        valid = bool(self.context)
        for index in range(4):
            selection = self._selection(index)
            if not selection or (index == 0 and not selection.get("family")):
                valid = False
            if selection and selection.get("source") in ("module", "game", "system") and not selection.get("files"):
                valid = False
        self.apply_button.configure(state=tk.NORMAL if valid else tk.DISABLED)

    def _set_loaded(self, loaded):
        state = tk.NORMAL if loaded else tk.DISABLED
        for control in self.controls:
            try:
                if isinstance(control, ttk.Combobox):
                    control.configure(state="readonly" if loaded else tk.DISABLED)
                else:
                    control.configure(state=state)
            except tk.TclError:
                pass

    def restore_original(self):
        if not self.context:
            return
        for index, family in enumerate(self.context["original_slots"]):
            if not family and index:
                self.selection_vars[index].set(CLEAR_SLOT)
                continue
            candidate = self._candidate_for_family(family)
            if candidate and self._set_descriptor(index, (candidate["source"], candidate["family"])):
                continue
            if family == self.context["applied_slots"][index]:
                self.selection_vars[index].set(KEEP_CURRENT)
            else:
                messagebox.showerror("字体不可用", f"无法定位项目最初字体：{family}", parent=self)
                return
        self._update_previews()

    def apply_revision(self):
        try:
            raw = [self._selection(index) for index in range(4)]
            prepared = wolf.prepare_font_revision(raw)
        except Exception as error:
            messagebox.showerror("字体方案无效", str(error), parent=self)
            return

        system_families = sorted({item["family"] for item in prepared if item["source"] == "system"})
        if system_families and not messagebox.askyesno(
            "系统字体授权确认",
            "将把以下系统字体复制到游戏目录：\n"
            + "\n".join(system_families)
            + "\n\n请确认你有权随译版分发这些字体。",
            parent=self,
        ):
            return

        coverage_revision = [self._selection(index, for_coverage=True) for index in range(4)]
        coverage = (
            wolf.font_revision_missing_characters(coverage_revision, self.required_characters)
            if self.required_from_scripts else []
        )
        problems = [
            f"槽位 {item['slot']} {item['family']}：缺少 {len(item['missing'])} 字 "
            f"{''.join(sorted(item['missing']))[:20]}"
            for item in coverage
            if item["missing"]
        ]
        if problems and not messagebox.askyesno(
            "字体仍有缺字",
            "\n".join(problems) + "\n\n仍要应用该字体方案吗？",
            parent=self,
        ):
            return

        revision = {"slots": prepared, "system_font_copy_ack": system_families}
        self.app.start_task("apply_wolf_fonts", mode="font", game_path=self.game_path, task_payload=revision)

    def _schedule_poll(self):
        if self._font_apply_active or not self._visible or self._poll_after_id:
            return
        self._poll_after_id = self.after(2000, self._poll_catalog)

    def _poll_catalog(self):
        self._poll_after_id = None
        if not self._visible or self._loading or not self.game_path:
            self._schedule_poll()
            return
        game_path = self.game_path
        token = self._load_token

        def worker():
            module_root = os.path.join(file_system.get_application_path(), "modules", "WOLF")
            try:
                fingerprint = font_coverage.font_catalog_fingerprint(module_root, game_path)
            except OSError:
                fingerprint = None
            self.after(0, lambda: self._finish_poll(token, fingerprint))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_poll(self, token, fingerprint):
        if token == self._load_token and fingerprint is not None and fingerprint != self.catalog_fingerprint:
            self._load_token += 1
            self._reload_async(self._load_token)
        self._schedule_poll()
