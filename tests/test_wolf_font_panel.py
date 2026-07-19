import tkinter as tk
from tkinter import ttk

from ui.wolf_font_panel import WolfFontPanel


class _App:
    def start_task(self, _task):
        pass


class _Var:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class _Combo:
    def configure(self, **values):
        self.values = values["values"]


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


def test_font_refresh_preserves_unavailable_pending_selection():
    panel = WolfFontPanel.__new__(WolfFontPanel)
    pending_label = "[内置] Pending Font"
    panel.context = {
        "original_slots": ["Original", "", "", ""],
        "applied_slots": ["Original", "", "", ""],
        "selected_slots": [
            {"source": "current", "family": "Original"},
            {"source": "empty", "family": ""},
            {"source": "empty", "family": ""},
            {"source": "empty", "family": ""},
        ],
    }
    panel.visible_candidates = []
    panel.candidate_by_label = {
        pending_label: {"source": "module", "family": "Pending Font", "files": []},
    }
    panel.selection_vars = [_Var(pending_label), _Var("清空槽位"), _Var("清空槽位"), _Var("清空槽位")]
    panel.original_family_vars = [_Var("") for _ in range(4)]
    panel.combos = [_Combo() for _ in range(4)]

    panel._populate_choices()

    assert panel.selection_vars[0].get() == "[不可用] [内置] Pending Font"
    assert panel._selection_descriptor(0) == ("module", "Pending Font")
    assert panel._selection(0) is None


def test_font_apply_releases_private_fonts_and_resumes_refresh(monkeypatch):
    panel = WolfFontPanel.__new__(WolfFontPanel)
    panel._font_apply_active = False
    panel._load_token = 3
    panel._poll_after_id = "poll-id"
    panel._registered_fonts = {"Data/font.ttf", "modules/font.otf"}
    cancelled = []
    released = []
    resumed = []
    panel.after_cancel = cancelled.append
    panel.refresh = lambda: resumed.append("refresh")
    panel._schedule_poll = lambda: resumed.append("poll")
    monkeypatch.setattr(
        "ui.wolf_font_panel.font_coverage.unregister_private_font",
        lambda path: released.append(path) or True,
    )

    panel.begin_apply()

    assert panel._font_apply_active is True
    assert panel._load_token == 4
    assert panel._poll_after_id is None
    assert cancelled == ["poll-id"]
    assert set(released) == {"Data/font.ttf", "modules/font.otf"}
    assert panel._registered_fonts == set()

    panel.finish_apply()

    assert panel._font_apply_active is False
    assert resumed == ["refresh", "poll"]


def test_font_coverage_text_is_blank_without_translated_scripts():
    panel = WolfFontPanel.__new__(WolfFontPanel)
    panel.required_from_scripts = False
    panel.required_characters = set("示例")

    assert panel._coverage_text({"family": "Test", "files": []}) == ""
