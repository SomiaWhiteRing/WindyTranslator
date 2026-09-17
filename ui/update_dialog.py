"""Tk update window; network and hashing work never run on the Tk thread."""

import logging
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
import webbrowser

from core import updates


log = logging.getLogger(__name__)


class UpdateController:
    def __init__(self, app):
        self.app = app
        self.root = app.root
        self.build_info = app.build_info
        settings = app.config.get("updates", {})
        if not isinstance(settings, dict):
            settings = {}
        self.site_url = settings.get("site_url", updates.DEFAULT_SITE_URL)
        if not isinstance(self.site_url, str):
            self.site_url = ""
        self.check_on_startup = settings.get("check_on_startup") is True
        self.dialog = None
        self.result = None
        self.saved_path = None
        self.busy = ""
        self.status = "点击“检查更新”获取网站推荐版本。"
        self.progress = (0, 0)
        self.messages = queue.Queue()
        self.cancel = threading.Event()
        self.closed = False
        self._dismissed = False
        self._poll_id = self.root.after(100, self._poll)
        self._startup_id = None
        if self.check_on_startup and self.site_url:
            self._startup_id = self.root.after(2500, self._startup_check)

    def _startup_check(self):
        self._startup_id = None
        if not self.closed and self.check_on_startup:
            self.check(manual=False)

    def show(self):
        if self.closed:
            return
        if self.dialog is None:
            self.dialog = UpdateDialog(self)
        self.dialog.deiconify()
        self.dialog.lift()
        self.dialog.render()

    def save_settings(self, site_url, check_on_startup):
        try:
            normalized = updates.normalize_site_url(site_url) if site_url.strip() else ""
        except updates.UpdateError as error:
            self.status = str(error)
            self._render()
            return False
        settings = {"site_url": normalized, "check_on_startup": bool(check_on_startup)}
        new_config = dict(self.app.config, updates=settings)
        new_config["selected_mode"] = self.app.main_window.get_current_mode()
        if not self.app.config_manager.save_config(new_config):
            self.status = "更新设置保存失败，请检查配置文件的写入权限。"
            self._render()
            return False
        if normalized != self.site_url:
            self.result = None
            self.saved_path = None
        self.app.config["updates"] = settings
        self.site_url, self.check_on_startup = normalized, bool(check_on_startup)
        self.status = "更新设置已保存。"
        self._render()
        return True

    def _start(self, operation, work, manual=True):
        if self.busy or self.closed:
            return
        self.busy = operation
        self._dismissed = False
        self.cancel = threading.Event()
        cancel = self.cancel
        self._render()

        def worker():
            try:
                result = work(cancel)
                self.messages.put((operation, result, manual))
            except updates.UpdateCancelled as error:
                self.messages.put(("cancelled", str(error), manual))
            except updates.UpdateError as error:
                self.messages.put(("error", str(error), manual))
            except Exception:
                log.exception("更新操作失败")
                self.messages.put(("error", "更新操作失败，请查看日志后重试。", manual))

        threading.Thread(target=worker, name=f"website-update-{operation}", daemon=True).start()

    def check(self, manual=True):
        if self.busy or self.closed:
            return
        self.result = None
        self.saved_path = None
        self.progress = (0, 0)
        self.status = "正在检查网站推荐版本…"
        site_url = self.site_url
        build_id = self.build_info.get("applicationBuildId", "")
        self._start("check", lambda cancel: updates.check_update(site_url, build_id, cancel), manual)

    def download(self):
        if self.busy or not self.result or not self.result.artifact:
            return
        destination = filedialog.asksaveasfilename(
            parent=self.dialog, title="保存温蒂翻译器更新包",
            initialdir=str(Path.home() / "Downloads"), initialfile=self.result.artifact.filename,
            defaultextension=".zip", filetypes=[("ZIP 更新包", "*.zip")], confirmoverwrite=True,
        )
        if not destination:
            return
        info = self.result
        build_id = self.build_info.get("applicationBuildId", "")
        self.saved_path = None
        self.progress = (0, info.artifact.size)
        self.status = "正在确认版本并下载，完成后将校验文件…"

        def work(cancel):
            return updates.download_update(
                info, destination, cancel,
                lambda received, total: self.messages.put(("progress", (received, total), True)),
                build_id=build_id,
            )

        self._start("download", work)

    def _result_status(self):
        if self.result.status == "paused":
            return "网站暂时停止提供此平台的更新。"
        return {
            "newer": "发现网站发布的新版本，可以下载更新包。",
            "current": "当前程序已是网站推荐版本。",
            "older": "当前安装版本比网站推荐版本更新；下载旧包前请确认用途。",
            "unknown": "尚未识别当前程序的本站版本；以下是网站推荐版本，可下载后手动安装。",
        }[self.result.comparison]

    def _poll(self):
        if self.closed:
            return
        try:
            while True:
                kind, value, manual = self.messages.get_nowait()
                if kind == "progress":
                    self.progress = value
                else:
                    self.busy = ""
                    if kind == "check":
                        self.result = value
                        self.status = self._result_status()
                        if not manual and not self._dismissed and value.comparison == "newer":
                            self.show()
                    elif kind == "download":
                        self.saved_path = value
                        self.status = "下载完成，文件大小与 SHA-256 校验通过。请打开文件夹查看更新包。"
                    else:
                        self.status = value
                    if not manual:
                        self.app.log_message(self.status, "warning" if kind == "error" else "normal")
                self._render()
        except queue.Empty:
            pass
        self._poll_id = self.root.after(100, self._poll)

    def _render(self):
        if self.dialog is not None:
            self.dialog.render()

    def close_dialog(self):
        if self.busy == "download" and not messagebox.askyesno(
            "取消下载", "关闭更新窗口将取消当前下载，是否继续？", parent=self.dialog,
        ):
            return
        if self.busy:
            self.cancel.set()
        self._dismissed = True
        if self.dialog is not None:
            self.dialog.destroy()
            self.dialog = None

    def close(self):
        self.closed = True
        self.cancel.set()
        for callback_id in (self._poll_id, self._startup_id):
            if callback_id is not None:
                self.root.after_cancel(callback_id)


class UpdateDialog(tk.Toplevel):
    def __init__(self, controller):
        super().__init__(controller.root)
        self.controller = controller
        self.title("温蒂翻译器更新")
        self.transient(controller.root)
        self.geometry("650x580")
        self.minsize(640, 580)
        self.protocol("WM_DELETE_WINDOW", controller.close_dialog)
        frame = ttk.Frame(self, padding=14)
        frame.pack(fill=tk.BOTH, expand=True)
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(6, weight=1)

        ttk.Label(frame, text=f"当前程序：{controller.build_info.get('version', '未知版本')}").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 12))
        ttk.Label(frame, text="更新网站").grid(row=1, column=0, sticky="w", padx=(0, 8))
        self.site_var = tk.StringVar(value=controller.site_url)
        self.site_entry = ttk.Entry(frame, textvariable=self.site_var)
        self.site_entry.grid(row=1, column=1, columnspan=2, sticky="ew")
        self.auto_var = tk.BooleanVar(value=controller.check_on_startup)
        self.auto_checkbox = ttk.Checkbutton(frame, text="启动时检查更新", variable=self.auto_var)
        self.auto_checkbox.grid(row=2, column=0, columnspan=2, sticky="w", pady=10)
        self.save_button = ttk.Button(frame, text="保存设置", command=self._save)
        self.save_button.grid(row=2, column=2, sticky="e")
        self.status_var = tk.StringVar()
        ttk.Label(frame, textvariable=self.status_var, wraplength=590).grid(
            row=3, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        self.version_var = tk.StringVar()
        ttk.Label(frame, textvariable=self.version_var, wraplength=590).grid(row=4, column=0, columnspan=3, sticky="w", pady=(0, 8))
        ttk.Label(frame, text="版本说明").grid(row=5, column=0, columnspan=3, sticky="w")
        self.notes = scrolledtext.ScrolledText(frame, height=9, width=65, wrap=tk.WORD, state=tk.DISABLED)
        self.notes.grid(row=6, column=0, columnspan=3, sticky="nsew", pady=(4, 8))
        self._notes_text = None
        self.progress = ttk.Progressbar(frame, mode="determinate", maximum=100)
        self.progress.grid(row=7, column=0, columnspan=3, sticky="ew")
        self.progress_var = tk.StringVar()
        ttk.Label(frame, textvariable=self.progress_var).grid(row=8, column=0, columnspan=3, sticky="w", pady=4)
        ttk.Label(frame, text="下载后先退出程序，将 ZIP 解压到新目录运行。迁移原有配置、字典、Works 和自装工具前请做好备份。",
                  wraplength=590).grid(row=9, column=0, columnspan=3, sticky="ew", pady=(4, 10))
        buttons = ttk.Frame(frame)
        buttons.grid(row=10, column=0, columnspan=3, sticky="ew")
        self.check_button = ttk.Button(buttons, text="检查更新", command=self._check)
        self.check_button.pack(side=tk.LEFT)
        self.download_button = ttk.Button(buttons, text="下载更新包", command=controller.download)
        self.download_button.pack(side=tk.LEFT, padx=6)
        self.notes_button = ttk.Button(buttons, text="查看说明网页", command=self._open_notes)
        self.notes_button.pack(side=tk.LEFT)
        self.folder_button = ttk.Button(buttons, text="打开文件夹", command=self._open_folder)
        self.folder_button.pack(side=tk.LEFT, padx=6)
        self.cancel_button = ttk.Button(buttons, text="取消", command=self._cancel)
        self.cancel_button.pack(side=tk.RIGHT)
        self.site_var.trace_add("write", lambda *_: self.render())

    def _save(self):
        saved = self.controller.save_settings(self.site_var.get(), self.auto_var.get())
        if saved:
            self.site_var.set(self.controller.site_url)
        return saved

    def _check(self):
        if self._save():
            self.controller.check()

    def _cancel(self):
        self.controller.cancel.set()
        self.controller.status = "正在取消，请稍候…"
        self.render()

    def _open_notes(self):
        if self.controller.result and self.controller.result.notes_url:
            webbrowser.open(self.controller.result.notes_url)

    def _open_folder(self):
        path = self.controller.saved_path
        if path is None:
            return
        try:
            if os.name == "nt":
                os.startfile(str(path.parent))
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path.parent)])
        except OSError as error:
            messagebox.showerror("打开失败", str(error), parent=self)

    def render(self):
        controller = self.controller
        busy = bool(controller.busy)
        info = controller.result
        available = info is not None and info.status == "available"
        unsaved_site = self.site_var.get().strip().rstrip("/") != controller.site_url
        for widget in (self.site_entry, self.auto_checkbox, self.save_button, self.check_button):
            widget.configure(state=tk.DISABLED if busy else tk.NORMAL)
        self.download_button.configure(state=tk.NORMAL if available and not busy and not unsaved_site else tk.DISABLED)
        self.notes_button.configure(state=tk.NORMAL if available and not unsaved_site else tk.DISABLED)
        self.folder_button.configure(state=tk.NORMAL if controller.saved_path else tk.DISABLED)
        self.cancel_button.configure(state=tk.NORMAL if busy else tk.DISABLED)
        self.status_var.set(controller.status)
        notes = "网站上线并发布工具版本后，即可检查和下载更新。"
        version_text = ""
        if available:
            version_text = f"网站推荐：{info.version}　大小：{info.artifact.size / 1_000_000:.2f} MB"
            if info.installed_version:
                version_text += f"\n当前本站版本：{info.installed_version}"
            notes = info.notes or "网站未提供内嵌说明，可点击“查看说明网页”。"
        self.version_var.set(version_text)
        if notes != self._notes_text:
            self.notes.configure(state=tk.NORMAL)
            self.notes.delete("1.0", tk.END)
            self.notes.insert("1.0", notes)
            self.notes.configure(state=tk.DISABLED)
            self._notes_text = notes
        received, total = controller.progress
        self.progress.configure(value=received / total * 100 if total else 0)
        self.progress_var.set(f"{received / 1_000_000:.2f} / {total / 1_000_000:.2f} MB" if total else "")
