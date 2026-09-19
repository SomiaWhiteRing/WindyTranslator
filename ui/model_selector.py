"""Editable model selection with asynchronous endpoint discovery."""

import queue
import threading
import tkinter as tk
from tkinter import ttk

from core.api_clients.model_catalog import fetch_openai_models


class ModelSelector(ttk.Frame):
    def __init__(self, parent, api_url_var, api_key_var, model_var,
                 status_callback, provider_var=None, native_models=()):
        super().__init__(parent)
        self.api_url_var = api_url_var
        self.api_key_var = api_key_var
        self.model_var = model_var
        self.provider_var = provider_var
        self.native_models = native_models
        self._status_callback = status_callback
        self._status_updates_enabled = True
        self._provider = self._current_provider()
        self._saved_models = {self._provider: model_var.get()}
        self._generation = 0
        self._closed = False
        self._debounce_id = None
        self._poll_id = None
        self._fetching = False
        self._ready_to_fetch = False
        self._results = queue.Queue()
        self._traces = []

        self.columnconfigure(0, weight=1)
        self.combobox = ttk.Combobox(self, textvariable=model_var, values=(), width=40)
        self.combobox.grid(row=0, column=0, sticky="ew")
        self.refresh_frame = ttk.Frame(self)
        self.refresh_frame.grid(row=0, column=1, padx=(5, 0), sticky="ns")
        # The compact height cannot accommodate the theme's vertical padding;
        # remove it so the centered label fits without being clipped.
        self.refresh_button = ttk.Button(
            self.refresh_frame, text="刷新模型", command=self.refresh, padding=(5, 0),
        )
        # Let the combobox determine the row height, keeping the native styles.
        self.refresh_frame.configure(width=self.refresh_button.winfo_reqwidth())
        self.refresh_button.place(x=0, y=0, relwidth=1, relheight=1)
        self.bind("<Destroy>", self._on_destroy)

        for variable in (api_url_var, api_key_var, provider_var):
            if variable is not None:
                trace_id = variable.trace_add("write", self._schedule_fetch)
                self._traces.append((variable, trace_id))
        # Populate saved configurations silently, preserving the dialog's status.
        self._debounce_id = self.after_idle(lambda: self._schedule_fetch(show_status=False))
        self._poll_id = self.after(100, self._poll_results)

    def _current_provider(self):
        return self.provider_var.get() if self.provider_var is not None else "openai"

    def _set_status(self, message, color):
        if self._status_updates_enabled:
            self._status_callback(message, color)

    def suspend_status_updates(self):
        """Keep late model results from replacing connection-test messages."""
        self._status_updates_enabled = False

    def _schedule_fetch(self, *args, show_status=True):
        if self._closed:
            return
        self._status_updates_enabled = show_status
        self._generation += 1
        self._ready_to_fetch = False
        if self._debounce_id is not None:
            self.after_cancel(self._debounce_id)
            self._debounce_id = None

        provider = self._current_provider()
        if provider != self._provider:
            self._saved_models[self._provider] = self.model_var.get()
            self._provider = provider
            self.model_var.set(self._saved_models.get(provider, ""))

        if provider != "openai":
            self.combobox.configure(values=self.native_models)
            self.refresh_frame.grid_remove()
            return

        self.combobox.configure(values=())
        self.refresh_frame.grid()
        if not self.api_url_var.get().strip() or not self.api_key_var.get().strip():
            self.refresh_button.configure(state=tk.DISABLED)
            self._set_status("等待 API 地址和 Key", "orange")
            return
        self.refresh_button.configure(state=tk.NORMAL)
        self._set_status("等待获取模型列表...", "orange")
        self._debounce_id = self.after(700, self._start_fetch)

    def refresh(self):
        self._schedule_fetch()
        if self._debounce_id is not None:
            self.after_cancel(self._debounce_id)
            self._debounce_id = None
            self._start_fetch()

    def _start_fetch(self):
        self._debounce_id = None
        if self._closed:
            return
        if self._fetching:
            self._ready_to_fetch = True
            return
        self._ready_to_fetch = False
        self._fetching = True
        self.refresh_button.configure(state=tk.DISABLED)
        self._set_status("正在获取模型列表...", "blue")
        threading.Thread(
            target=self._fetch_models,
            args=(self._generation, self.api_url_var.get().strip(), self.api_key_var.get().strip(), self._results),
            daemon=True,
        ).start()

    @staticmethod
    def _fetch_models(generation, api_url, api_key, results):
        try:
            models = fetch_openai_models(api_url, api_key)
            message = ""
        except (ConnectionError, ValueError) as error:
            models, message = [], str(error).replace(api_key, "[REDACTED]")
        except Exception:
            models, message = [], "无法读取接口返回的模型列表。"
        # Workers never call Tk, including after the dialog has closed.
        results.put((generation, models, message))

    def _poll_results(self):
        self._poll_id = None
        if self._closed:
            return
        try:
            generation, models, message = self._results.get_nowait()
        except queue.Empty:
            pass
        else:
            self._fetching = False
            if generation == self._generation:
                self.refresh_button.configure(state=tk.NORMAL)
                self.combobox.configure(values=models)
                if message:
                    self._set_status(message, "red")
                elif models:
                    self._set_status(f"已获取 {len(models)} 个模型", "green")
                else:
                    self._set_status("接口未返回可用模型。", "orange")
            if self._ready_to_fetch:
                self._start_fetch()
        self._poll_id = self.after(100, self._poll_results)

    def _on_destroy(self, event):
        if event.widget is not self:
            return
        self._closed = True
        for after_id in (self._debounce_id, self._poll_id):
            if after_id is not None:
                self.after_cancel(after_id)
        for variable, trace_id in self._traces:
            variable.trace_remove("write", trace_id)
