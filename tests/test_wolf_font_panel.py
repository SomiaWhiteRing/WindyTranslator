import tkinter as tk
from tkinter import ttk

from ui.wolf_font_panel import WolfFontPanel


class _App:
    def start_task(self, _task):
        pass


def test_font_list_scrolls_separately_from_actions():
    root = tk.Tk()
    root.withdraw()
    try:
        container = ttk.Frame(root)
        panel = WolfFontPanel(container, _App())

        assert panel.font_area.master is panel
        assert panel.canvas.master is panel.font_area
        assert panel.actions_frame.master is panel
        assert panel.font_area.grid_info()["row"] == 0
        assert panel.actions_frame.grid_info()["row"] == 1
        assert panel.size_var.get() == 12
        assert all(isinstance(preview, ttk.Label) for pair in panel.preview_fonts for preview in pair)
    finally:
        root.destroy()
