"""A single user-facing update window; all network and file work is off Tk."""
import logging
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, scrolledtext
import webbrowser

from core import updates
from core import update_install as installer

log = logging.getLogger(__name__)


class UpdateController:
    def __init__(self, app):
        self.app, self.root = app, app.root
        self.build_info = app.build_info
        self.site_url = self.build_info.get("updateSite", updates.DEFAULT_SITE_URL)
        settings = app.config.get("updates", {})
        self.check_on_startup = settings.get("check_on_startup", True) is not False if isinstance(settings, dict) else True
        self.dialog = self.result = None
        self.busy = ""
        self.status = "正在检查更新…"
        self.progress = 0
        self.messages = queue.Queue()
        self.cancel = threading.Event()
        self.closed = False
        self._dismissed = False
        self._workspace = None
        self._font_visible = False
        self._poll_id = self.root.after(100, self._poll)
        self._startup_id = None
        skip = os.environ.pop("WINDY_SKIP_UPDATE_CHECK", "") == "1"
        if self.check_on_startup and not skip:
            self._startup_id = self.root.after(2500, self._startup_check)

    @property
    def updating(self):
        return self.busy in ("prepare", "handoff")

    def _startup_check(self):
        self._startup_id = None
        if not self.closed and self.check_on_startup:
            self.check(manual=False)

    def show(self, check=True):
        if self.closed:
            return
        if self.dialog is None:
            self.dialog = UpdateDialog(self)
        self.dialog.deiconify()
        self.dialog.lift()
        self.dialog.render()
        if check and not self.busy:
            self.check()

    def save_auto_check(self, enabled):
        settings = {"check_on_startup": bool(enabled)}
        config = dict(self.app.config, updates=settings)
        if self.app.config_manager.save_config(config):
            self.app.config["updates"] = settings
            self.check_on_startup = bool(enabled)
        else:
            self.status = "设置保存失败，请检查程序目录是否可写。"
        self._render()

    def _start(self, operation, work, manual=True):
        if self.busy or self.closed:
            return
        self.busy = operation
        self.cancel = threading.Event()
        self._dismissed = False
        self._render()
        def worker():
            try:
                self.messages.put((operation, work(), manual))
            except Exception as error:
                log.exception("WindyTranslator 更新失败")
                message = (str(error) if operation == "prepare" and isinstance(error, (installer.InstallError, updates.UpdateError))
                           else "更新准备失败，请检查网络、磁盘空间及目录权限后重试。" if operation == "prepare"
                           else "暂时无法检查更新，请稍后重试。")
                self.messages.put(("error", message, manual))
        threading.Thread(target=worker, name=f"windy-update-{operation}", daemon=True).start()

    def check(self, manual=True):
        if self.busy:
            return
        # A manually requested check consumes the pending startup check too.
        if self._startup_id is not None:
            self.root.after_cancel(self._startup_id)
            self._startup_id = None
        self.result = None
        self.status = "正在检查更新…"
        self._start("check", lambda: updates.check_update(self.site_url, self.build_info.get("applicationBuildId", ""), self.cancel), manual)

    def _lock(self, locked):
        panel = self.app.main_window.font_panel
        if locked:
            self._font_visible = panel._visible
            panel.set_visible(False)
            self.root.attributes("-disabled", True)
            self.dialog.grab_set()
            self.dialog.focus_set()
        else:
            self.root.attributes("-disabled", False)
            if self.dialog is not None:
                self.dialog.grab_release()
            panel.set_visible(self._font_visible)

    def install(self):
        if self.busy or not self.result or self.result.comparison != "newer":
            return
        reason = self.app.update_blocker(self.dialog)
        if reason:
            self.status = reason
            self._render()
            return
        if not self.app.save_config():
            self.status = "无法保存配置，请检查程序目录是否可写。"
            self._render()
            return
        try:
            self._lock(True)
        except tk.TclError:
            self.root.attributes("-disabled", False)
            self.app.main_window.font_panel.set_visible(self._font_visible)
            self.status = "无法锁定主窗口，请重新启动 WindyTranslator 后重试。"
            self._render()
            return
        self.status, self.progress = "正在下载更新…", 0
        def prepare():
            self._workspace = installer.prepare_update(
                self.app.executable_dir, self.result, self.build_info.get("applicationBuildId", ""),
                updates.download_update,
                lambda value, label: self.messages.put(("progress", (value, label), True)))
            helper = installer.start_helper(self._workspace)
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                if helper.poll() is not None:
                    raise installer.InstallError("更新助手未能启动，程序未被替换。")
                if (self._workspace / "ready.json").exists():
                    return self._workspace
                time.sleep(0.1)
            (self._workspace / "cancel").touch()
            raise installer.InstallError("更新助手启动超时，请稍后重试。")
        self._start("prepare", prepare)

    def _poll(self):
        if self.closed:
            return
        try:
            while True:
                kind, value, manual = self.messages.get_nowait()
                if kind == "progress":
                    self.progress, self.status = value
                elif kind == "prepare":
                    self.busy = "handoff"
                    self.progress, self.status = 100, "准备完成，正在重启更新…"
                    self._render()
                    self.root.after(250, self._exit_for_update)
                else:
                    was_updating = self.updating
                    self.busy = ""
                    if was_updating:
                        if self._workspace is not None:
                            (self._workspace / "cancel").touch()
                        self._lock(False)
                    if kind == "check":
                        self.result = value
                        self.status = ("暂时没有可用更新。" if value.status == "paused" else {
                            "newer": "WindyTranslator 有新版本了",
                            "current": "暂无可用更新。",
                            "older": "暂无可用更新。",
                            "unknown": "无法确认当前版本，可前往网站下载完整新版。",
                        }[value.comparison])
                        if not manual and not self._dismissed and value.comparison == "newer":
                            self.show(check=False)
                    else:
                        self.status = value
                    if not manual:
                        log.info("启动更新检查：%s", self.status)
                self._render()
        except queue.Empty:
            pass
        self._poll_id = self.root.after(100, self._poll)

    def _exit_for_update(self):
        try:
            self.app.exit_for_update()
        except Exception:
            log.exception("退出更新失败")
            if self._workspace:
                (self._workspace / "cancel").touch()
            self.busy = ""
            self._lock(False)
            self.status = "无法完成重启准备，请关闭并重新打开 WindyTranslator 后重试。"
            self._render()

    def _render(self):
        if self.dialog is not None:
            self.dialog.render()

    def close_dialog(self):
        if self.updating:
            return
        self._dismissed = True
        if self.busy == "check":
            self.cancel.set()
        if self.dialog is not None:
            self.dialog.destroy()
            self.dialog = None

    def close(self):
        self.closed = True
        self.cancel.set()
        for callback in (self._poll_id, self._startup_id):
            if callback is not None:
                self.root.after_cancel(callback)


class UpdateDialog(tk.Toplevel):
    def __init__(self, controller):
        super().__init__(controller.root)
        self.controller = controller
        self.title("检查更新")
        self.transient(controller.root)
        self.geometry("520x420")
        self.minsize(460, 360)
        self.protocol("WM_DELETE_WINDOW", controller.close_dialog)
        frame = ttk.Frame(self, padding=20)
        frame.pack(fill=tk.BOTH, expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)
        self.status = tk.StringVar()
        ttk.Label(frame, textvariable=self.status, wraplength=470, font=("Microsoft YaHei UI", 12, "bold")).grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self.version = tk.StringVar()
        ttk.Label(frame, textvariable=self.version, wraplength=470).grid(row=1, column=0, sticky="w", pady=(0, 10))
        self.notes = scrolledtext.ScrolledText(frame, height=7, wrap=tk.WORD, state=tk.DISABLED)
        self.notes.grid(row=2, column=0, sticky="nsew", pady=(0, 12))
        self._notes = None
        self.progress = ttk.Progressbar(frame, maximum=100)
        self.progress.grid(row=3, column=0, sticky="ew", pady=(0, 12))
        self.auto = tk.BooleanVar(value=controller.check_on_startup)
        self.auto_button = ttk.Checkbutton(frame, text="启动时自动检查更新", variable=self.auto,
                                          command=lambda: controller.save_auto_check(self.auto.get()))
        self.auto_button.grid(row=4, column=0, sticky="w", pady=(0, 12))
        buttons = ttk.Frame(frame)
        buttons.grid(row=5, column=0, sticky="e")
        self.later = ttk.Button(buttons, text="稍后", command=controller.close_dialog)
        self.later.pack(side=tk.LEFT, padx=6)
        self.primary = ttk.Button(buttons, command=self._primary)
        self.primary.pack(side=tk.LEFT)

    def _primary(self):
        info = self.controller.result
        if info and info.comparison == "newer":
            self.controller.install()
        elif info and info.status == "available" and info.comparison == "unknown":
            webbrowser.open(info.notes_url)
        else:
            self.controller.check()

    def render(self):
        c, info = self.controller, self.controller.result
        self.status.set(c.status)
        current = (info.installed_version if info else "") or c.build_info.get("version", "未知版本")
        newer = info is not None and info.comparison == "newer"
        self.version.set(f"{current} → {info.version} · {info.artifact.size / 1_000_000:.1f} MB" if newer else f"当前版本：{current}")
        notes = (info.notes or "本次更新未填写说明。") if newer else ""
        if notes != self._notes:
            self.notes.configure(state=tk.NORMAL)
            self.notes.delete("1.0", tk.END)
            self.notes.insert("1.0", notes)
            self.notes.configure(state=tk.DISABLED)
            self._notes = notes
        self.progress.configure(value=c.progress)
        if c.updating:
            self.progress.grid()
        else:
            self.progress.grid_remove()
        self.auto.set(c.check_on_startup)
        self.auto_button.configure(state=tk.DISABLED if c.updating else tk.NORMAL)
        self.later.configure(text="稍后" if newer else "关闭", state=tk.DISABLED if c.updating else tk.NORMAL)
        self.primary.configure(text="立即更新" if newer else "前往网站" if info and info.status == "available" and info.comparison == "unknown" else "重新检查",
                               state=tk.DISABLED if c.busy else tk.NORMAL)
