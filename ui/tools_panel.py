from __future__ import annotations

import os
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from core.tools import RunningTool, ToolManager, ToolManifest, ToolSpecError
from core.utils.file_system import get_modules_path


class ToolsPanel(ttk.Frame):
    def __init__(self, parent, app, shortcut_changed=None):
        super().__init__(parent, padding=8)
        self.app = app
        self.shortcut_changed = shortcut_changed
        self.launch_option_ids: dict[str, str] = {}
        self.manager = ToolManager(Path(app.executable_dir) / "tools")
        self.tools: list[ToolManifest] = []
        self.diagnostics: list[tuple[Path, str]] = []
        self.output_queue: queue.Queue[tuple[RunningTool, str, bool]] = queue.Queue()
        self._build_ui()
        self.refresh_tools()
        self.after(150, self._poll_process)

    def _build_ui(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        top = ttk.Frame(self)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text="工具列表").grid(row=0, column=0, sticky="w")
        ttk.Button(top, text="这是什么？", command=self._show_tools_help).grid(row=0, column=1, sticky="w", padx=(6, 0))
        ttk.Button(top, text="刷新", command=self.refresh_tools).grid(row=0, column=2, padx=2)
        ttk.Button(top, text="打开目录", command=self._open_tools_dir).grid(row=0, column=3, padx=2)

        self.content = ttk.Frame(self)
        self.content.grid(row=1, column=0, sticky="nsew")
        self.content.columnconfigure(0, weight=0)
        self.content.columnconfigure(1, weight=1)
        self.content.rowconfigure(0, weight=1)
        list_frame = ttk.Frame(self.content)
        list_frame.grid(row=0, column=0, sticky="ns", padx=(0, 10))
        self.tool_list = tk.Listbox(list_frame, exportselection=False, width=30, height=12)
        list_scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.tool_list.yview)
        self.tool_list.configure(yscrollcommand=list_scroll.set)
        self.tool_list.pack(side=tk.LEFT, fill="y", expand=True)
        list_scroll.pack(side=tk.RIGHT, fill="y")
        self.tool_list.bind("<<ListboxSelect>>", lambda _event: self._show_selected_tool())

        detail = ttk.Frame(self.content)
        detail.grid(row=0, column=1, sticky="nsew")
        detail.columnconfigure(0, weight=1)
        detail.rowconfigure(3, weight=1)
        detail.bind("<Configure>", self._resize_detail)
        self.info_var = tk.StringVar(value="请选择工具。")
        self.info_label = ttk.Label(detail, textvariable=self.info_var, justify="left", wraplength=1)
        self.info_label.grid(row=0, column=0, sticky="new", pady=(0, 4))
        self.status_var = tk.StringVar()
        self.status_label = ttk.Label(detail, textvariable=self.status_var, foreground="#b00020")
        self.status_label.grid(row=1, column=0, sticky="new")
        self.status_label.grid_remove()
        self.options_frame = ttk.Frame(detail)
        self.options_frame.grid(row=2, column=0, sticky="ew", pady=(4, 0))
        self.options_label_var = tk.StringVar(value="启动选项")
        ttk.Label(self.options_frame, textvariable=self.options_label_var).pack(side=tk.LEFT, padx=(0, 6))
        self.option_var = tk.StringVar()
        self.option_name_var = tk.StringVar()
        self.option_combo = ttk.Combobox(
            self.options_frame,
            textvariable=self.option_name_var,
            state="readonly",
            width=18,
        )
        self.option_combo.pack(side=tk.LEFT)
        self.option_combo.bind("<<ComboboxSelected>>", self._select_launch_option)
        self.options_frame.grid_remove()
        self.log_text = tk.Text(detail, width=1, height=7, state=tk.DISABLED, wrap=tk.WORD)
        self.log_text.grid(row=3, column=0, sticky="nsew", pady=(8, 0))
        actions = ttk.Frame(detail)
        actions.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        self.start_button = ttk.Button(actions, text="启动工具", command=self.start_tool)
        self.start_button.pack(side=tk.LEFT)
        self.shortcut_tool_var = tk.StringVar(value=self._shortcut()['tool_id'])
        self.shortcut_radio = ttk.Radiobutton(
            actions, text="设为快捷工具", variable=self.shortcut_tool_var,
            command=self._register_shortcut,
        )

    def _resize_detail(self, event):
        wraplength = max(1, event.width - 12)
        self.info_label.configure(wraplength=wraplength)

    def refresh_tools(self):
        selected = self._selected()
        selected_id = selected.id if selected else self._shortcut()['tool_id']
        self.tools, self.diagnostics = self.manager.discover()
        self.tool_list.delete(0, tk.END)
        for tool in self.tools:
            self.tool_list.insert(tk.END, tool.name)
        if self.tools:
            index = next((index for index, tool in enumerate(self.tools) if tool.id == selected_id), 0)
            self.tool_list.selection_set(index)
            self._show_selected_tool()
        else:
            self.info_var.set("未发现可用工具。请将带 manifest.json 的工具目录放入 tools。")
            self.options_frame.grid_remove()
            self.status_label.grid_remove()
            self.shortcut_radio.pack_forget()
        if self.diagnostics:
            self._write("工具扫描诊断：" + "; ".join(f"{path.name}: {error}" for path, error in self.diagnostics))
        self._update_start_button()

    def _selected(self) -> ToolManifest | None:
        selected = self.tool_list.curselection()
        index = selected[0] if selected else -1
        return self.tools[index] if 0 <= index < len(self.tools) else None

    def _show_selected_tool(self):
        tool = self._selected()
        if not tool:
            return
        self.info_var.set(f"{tool.name}\n版本：{tool.version}\n作者：{tool.author}\n\n{tool.description}")
        self._show_launch_options(tool)
        if tool.pro_shortcut_name:
            self.shortcut_radio.configure(value=tool.id)
            self.shortcut_radio.pack(side=tk.LEFT, padx=(12, 0))
        else:
            self.shortcut_radio.pack_forget()
        if tool.errors:
            self.status_var.set("状态检查未通过")
            self.status_label.grid()
        else:
            self.status_var.set("")
            self.status_label.grid_remove()
        self._update_start_button()

    def _show_launch_options(self, tool):
        if not tool.launch_options:
            self.option_var.set("")
            self.option_name_var.set("")
            self.options_label_var.set("启动选项")
            self.option_combo.configure(values=())
            self.options_frame.grid_remove()
            return
        shortcut = self._shortcut()
        option_id = shortcut['launch_option_id'] if shortcut['tool_id'] == tool.id else self.launch_option_ids.get(tool.id)
        index = next((index for index, option in enumerate(tool.launch_options) if option.id == option_id), 0)
        self.option_var.set(tool.launch_options[index].id)
        self.options_label_var.set(tool.launch_options_label)
        self.option_combo.configure(values=tuple(option.name for option in tool.launch_options))
        self.option_combo.current(index)
        self.options_frame.grid()

    def _select_launch_option(self, _event):
        tool = self._selected()
        if not tool:
            return
        index = self.option_combo.current()
        if 0 <= index < len(tool.launch_options):
            option_id = tool.launch_options[index].id
            self.option_var.set(option_id)
            self.launch_option_ids[tool.id] = option_id
            if self._shortcut()['tool_id'] == tool.id:
                self._save_shortcut(tool, option_id)

    def _shortcut(self):
        return self.app.config['tools']['pro_shortcut']

    def _register_shortcut(self):
        tool = self._selected()
        if tool and tool.pro_shortcut_name and tool.available and not self.app.is_processing:
            self._save_shortcut(tool, self.option_var.get() if tool.launch_options else None)
        else:
            self.shortcut_tool_var.set(self._shortcut()['tool_id'])

    def _save_shortcut(self, tool, option_id):
        previous = self._shortcut()
        shortcut = dict(previous, tool_id=tool.id, launch_option_id=option_id)
        if shortcut != previous:
            self.app.config['tools']['pro_shortcut'] = shortcut
            if not self.app.save_config():
                self.app.config['tools']['pro_shortcut'] = previous
                self._show_launch_options(tool)
        self.shortcut_tool_var.set(self._shortcut()['tool_id'])
        self.refresh_shortcut_state()

    def _is_running(self, tool):
        running = self.manager.running.get(tool.id)
        return running is not None and running.process.poll() is None

    def refresh_shortcut_state(self):
        shortcut = self._shortcut()
        tool = next((tool for tool in self.tools if tool.id == shortcut['tool_id']), None)
        enabled = bool(tool and tool.pro_shortcut_name and tool.available)
        if enabled:
            try:
                tool.arguments_for(shortcut['launch_option_id'])
            except ToolSpecError:
                enabled = False
        if enabled:
            enabled = not self.app.is_processing and not self._is_running(tool)
        if self.shortcut_changed:
            name = tool.pro_shortcut_name if tool and tool.pro_shortcut_name else "工具不可用"
            self.shortcut_changed(name, enabled)

    def start_shortcut(self):
        shortcut = self._shortcut()
        tool = next((tool for tool in self.tools if tool.id == shortcut['tool_id']), None)
        if not tool or not tool.pro_shortcut_name:
            messagebox.showerror("启动失败", "快捷工具不可用，请在小工具 Tab 使用“设为快捷工具”重新选择。", parent=self)
            return
        self.start_tool_by_id(tool.id, shortcut['launch_option_id'], from_shortcut=True)

    def _update_start_button(self):
        tool = self._selected()
        available = tool and tool.available and not self.app.is_processing and not self._is_running(tool)
        state = tk.NORMAL if available else tk.DISABLED
        self.start_button.configure(state=state)
        self.shortcut_radio.configure(state=tk.NORMAL if tool and tool.available and not self.app.is_processing else tk.DISABLED)
        self.refresh_shortcut_state()

    def _open_tools_dir(self):
        self.manager.tools_root.mkdir(parents=True, exist_ok=True)
        os.startfile(str(self.manager.tools_root))

    def _show_tools_help(self):
        messagebox.showinfo(
            "这是什么",
            "该页面展示的是tools文件夹下的翻译辅助工具。\n"
            "如果你有自己在用的RM2K翻译辅助工具，只需要按照tools文件夹下的`小工具文档.md`填写manifest.json后放入tools就可以让其通过WindyTranslator启动~\n"
            "（可以把文档发给AI帮忙写）",
            parent=self.winfo_toplevel(),
        )

    def _values(self, arguments):
        values = {}
        for argument in arguments:
            if argument.type == "game_path" and self.app.get_game_path():
                game_path = Path(self.app.get_game_path())
                values[argument.name] = str(game_path.joinpath(*Path(argument.source).parts)) if argument.source else str(game_path)
            elif argument.type == "current_game_file":
                values[argument.name] = self.app.get_tools_context_path(argument.source)
            elif argument.type == "modules":
                values[argument.name] = str(Path(get_modules_path(), *Path(argument.source).parts))
        return values

    def _host_command(self):
        if getattr(sys, "frozen", False):
            return [sys.executable]
        return [sys.executable, str(Path(sys.argv[0]).resolve())]

    def start_tool(self):
        tool = self._selected()
        if not tool:
            return
        option_id = self.option_var.get() if tool.launch_options else None
        self.start_tool_by_id(tool.id, option_id)

    def start_tool_by_id(self, tool_id, option_id=None, *, from_shortcut=False):
        tool = next((tool for tool in self.tools if tool.id == tool_id), None)
        if not tool:
            return
        if self.app.is_processing:
            messagebox.showerror("启动失败", "请先等待当前任务完成。", parent=self)
            return
        try:
            arguments = tool.arguments_for(option_id)
        except ToolSpecError as exc:
            messagebox.showerror("启动失败", str(exc), parent=self)
            self._write(f"启动失败：{exc}", to_main=from_shortcut)
            return
        needs_game_directory = any(
            argument.required and argument.type in {"game_path", "current_game_file"}
            for argument in arguments
        )
        if needs_game_directory and not os.path.isdir(self.app.get_game_path() or ""):
            messagebox.showerror("启动失败", "请先选择一个有效的游戏目录。", parent=self)
            self._write("启动失败：请先选择一个有效的游戏目录。", to_main=from_shortcut)
            return
        try:
            running = self.manager.start(tool, self._values(arguments), self._host_command(), option_id)
        except (ToolSpecError, OSError) as exc:
            messagebox.showerror("启动失败", str(exc), parent=self)
            self._write(f"启动失败：{exc}", to_main=from_shortcut)
            return
        self._write(f"已启动：{tool.name}", to_main=from_shortcut)
        self._update_start_button()

        def read_output(process_info):
            if running.process.stdout:
                for line in running.process.stdout:
                    self.output_queue.put((process_info, line.rstrip(), from_shortcut))
            running.process.wait()

        threading.Thread(target=read_output, args=(running,), daemon=True).start()

    def _poll_process(self):
        while True:
            try:
                running, message, to_main = self.output_queue.get_nowait()
                self._write(f"[{running.manifest.name}] {message}", to_main=to_main)
            except queue.Empty:
                break
        for running in self.manager.clear_finished():
            if running.process.returncode:
                self._write(f"[{running.manifest.name}] 工具已退出，退出码：{running.process.returncode}", to_main=True)
        self._update_start_button()
        self.after(150, self._poll_process)

    def _write(self, message, *, to_main=False):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, str(message) + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)
        if to_main:
            self.app.log_message(message)
