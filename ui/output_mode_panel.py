"""共用的项目输出方式入口、首次选择和升级提示。"""
import tkinter as tk
from tkinter import ttk, messagebox

from core.utils.output_mode import project_key


def _legacy_mode_label(encoding):
    name = {"936": "GBK", "950": "Big5", "932": "Shift-JIS", "949": "韩语"}.get(encoding, encoding)
    return f"传统兼容（{name}）"


class OutputModePanel(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=(5, 3))
        self.app = app
        self.path = ""
        self.profile = None
        self.dialog = None
        self._intro_scheduled = False
        self.columnconfigure(2, weight=1)
        self.summary = tk.StringVar()
        ttk.Label(self, text="输出方式").grid(row=0, column=0, padx=(0, 10))
        ttk.Label(self, textvariable=self.summary).grid(row=0, column=1, sticky="w")
        self.change_button = ttk.Button(self, text="更改…", command=self.show_choice)
        self.change_button.grid(row=0, column=3, sticky="e")
        self.notice = ttk.LabelFrame(self, text="新增 UTF-8 输出方式", padding=8)
        self.notice.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(6, 2))
        self.notice.columnconfigure(0, weight=1)
        ttk.Label(
            self.notice,
            text="已保留您的作品设置。新作品默认使用 UTF-8，可保留原文件名和更多符号。",
            wraplength=580,
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        actions = ttk.Frame(self.notice)
        actions.grid(row=1, column=0, columnspan=2, sticky="e", pady=(6, 0))
        self.learn_button = ttk.Button(actions, text="查看新模式…", command=lambda: self.show_choice(recommend_utf8=True))
        self.learn_button.pack(side="left", padx=(0, 6))
        self.dismiss_button = ttk.Button(actions, text="知道了", command=self.dismiss_notice)
        self.dismiss_button.pack(side="left")
        self.grid_remove()

    def get_controls(self):
        return [self.change_button, self.learn_button, self.dismiss_button]

    def refresh(self, path, detected, *, show_intro=True):
        if not detected or detected.engine != "rm200x":
            self.path = ""
            self.profile = None
            self.grid_remove()
            self.app.main_window.pro_panel.set_output_profile(None)
            return
        self.path = path
        self.profile = self.app.ensure_output_profile(path)
        profile = self.profile
        if profile is None:
            self.summary.set("当前路径的编码尚未确认")
            self.notice.grid_remove()
            self.grid()
            self.app.main_window.pro_panel.set_output_profile(None)
            self.set_enabled(False)
            return
        if profile["mode"] == "utf8":
            self.summary.set("UTF-8")
        else:
            self.summary.set(_legacy_mode_label(profile["legacy_encoding"]))
        self.grid()
        if self.app.config["output_settings"]["upgrade_notice_pending"]:
            self.notice.grid()
        else:
            self.notice.grid_remove()
        self.app.main_window.pro_panel.set_output_profile(profile)
        self.set_enabled(not self.app.is_processing)
        if show_intro and not self.app.config["output_settings"]["onboarding_seen"] and not self._intro_scheduled:
            self._intro_scheduled = True
            self.after_idle(lambda selected=path: self._first_choice(selected))

    def _first_choice(self, path):
        self._intro_scheduled = False
        if self.app._closing or self.app.is_processing or not self.path:
            return
        if project_key(path) == project_key(self.app.get_game_path()) and not self.app.config["output_settings"]["onboarding_seen"]:
            self.show_choice(initial=True)

    def set_enabled(self, enabled):
        for control in self.get_controls():
            control.configure(state="normal" if enabled and self.profile else "disabled")

    def dismiss_notice(self):
        self.app.config["output_settings"]["upgrade_notice_pending"] = False
        self.app.save_config()
        self.notice.grid_remove()

    def choose_source_encoding(self, game_path, reason):
        """仅用于已有工程中未登记路径的检测失败/冲突，不修改输出偏好。"""
        if self.dialog is not None and self.dialog.winfo_exists():
            return None
        dialog = self.dialog = tk.Toplevel(self)
        dialog.title("确认当前游戏的数据编码")
        dialog.transient(self.winfo_toplevel())
        dialog.grab_set()
        dialog.resizable(False, False)
        content = ttk.Frame(dialog, padding=18)
        content.pack(fill="both", expand=True)
        ttk.Label(content, text="已有工程尚未记录此路径，请选择这份游戏当前的数据编码。",
                  wraplength=540).pack(anchor="w")
        ttk.Label(content, text=f"{game_path}\n\n{reason}", wraplength=540).pack(anchor="w", pady=10)
        choices = {
            f"{name} - {code}": code
            for name, code in self.app.main_window.pro_panel.encoding_options if code != "auto"
        }
        selected = tk.StringVar(value="")
        combo = ttk.Combobox(content, textvariable=selected, values=list(choices), state="readonly", width=32)
        combo.pack(fill="x")
        accepted = None

        def apply():
            nonlocal accepted
            if project_key(game_path) == project_key(self.app.get_game_path()):
                accepted = choices.get(selected.get())
            dialog.destroy()

        actions = ttk.Frame(content)
        actions.pack(anchor="e", pady=(14, 0))
        ttk.Button(actions, text="取消", command=dialog.destroy).pack(side="left", padx=(0, 8))
        confirm = ttk.Button(actions, text="使用此编码", command=apply, state="disabled")
        confirm.pack(side="left")
        combo.bind("<<ComboboxSelected>>", lambda _event: confirm.configure(state="normal"))
        try:
            self.wait_window(dialog)
        finally:
            self.dialog = None
        return accepted

    def show_choice(self, *, initial=False, recommend_utf8=False):
        if self.app.is_processing or not self.path:
            return False
        if self.dialog is not None and self.dialog.winfo_exists():
            self.dialog.lift()
            return False
        path = self.path
        profile = self.app.get_output_profile(path)
        dialog = self.dialog = tk.Toplevel(self)
        dialog.title("选择本作品的运行方式" if initial else "更改本作品的输出方式")
        dialog.transient(self.winfo_toplevel())
        dialog.grab_set()
        dialog.resizable(False, False)
        background = ttk.Style(dialog).lookup("TFrame", "background")
        if background:
            dialog.configure(background=background)
        content = ttk.Frame(dialog, padding=18)
        content.pack(fill="both", expand=True)
        selected = tk.StringVar(value="utf8" if recommend_utf8 else profile["mode"])
        make_default = tk.BooleanVar(value=initial and not profile["existing_work"])
        descriptions = (
            ("utf8", "UTF-8（推荐）", "仅支持 EasyRPG 或兼容 UTF-8 的专用 RPG_RT。\n\n保留原文件名和 ♪ 等符号。"),
            ("legacy", _legacy_mode_label(profile["legacy_encoding"]), "支持原版 RPG_RT。\n\n文件名会按兼容规则转换。\n导入时会替换部分不支持的符号。"),
        )
        for column, (mode, title, description) in enumerate(descriptions):
            card = ttk.Frame(content, padding=12, relief="groove", borderwidth=1)
            card.grid(row=1, column=column, sticky="nsew", padx=(0, 6) if column == 0 else (6, 0))
            ttk.Radiobutton(card, text=title, variable=selected, value=mode).pack(anchor="w")
            ttk.Label(card, text=description, wraplength=255, justify="left").pack(anchor="w", pady=(10, 0))
        ttk.Checkbutton(content, text="同时设为今后新作品的默认方式", variable=make_default).grid(row=2, column=0, columnspan=2, sticky="w", pady=(14, 8))
        if profile["existing_work"]:
            ttk.Label(
                content,
                text="已经重写的文件名，以及旧版译文中已被替换的符号，不会自动恢复。",
                wraplength=570,
                justify="left",
            ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(0, 10))
        accepted = False

        def apply():
            nonlocal accepted
            if project_key(path) != project_key(self.app.get_game_path()):
                messagebox.showwarning("作品已变化", "请为当前选择的作品重新设置输出方式。", parent=dialog)
                dialog.destroy()
                return
            accepted = self.app.set_output_profile(path, selected.get(), make_default=make_default.get())
            if accepted:
                dialog.destroy()

        actions = ttk.Frame(content)
        actions.grid(row=4, column=0, columnspan=2, sticky="e", pady=(8, 0))
        ttk.Button(actions, text="取消", command=dialog.destroy).pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="使用所选方式" if initial else "应用到当前作品", command=apply).pack(side="left")
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.update_idletasks()
        owner = self.winfo_toplevel()
        x = max(0, owner.winfo_rootx() + (owner.winfo_width() - dialog.winfo_reqwidth()) // 2)
        y = max(0, owner.winfo_rooty() + 50)
        dialog.geometry(f"+{x}+{y}")
        self.wait_window(dialog)
        self.dialog = None
        return accepted
