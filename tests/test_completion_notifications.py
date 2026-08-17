from types import SimpleNamespace

import app
from ui import main_window


def test_completion_notification_toggle_rolls_back_when_registration_fails(monkeypatch):
    state = {"value": True}
    saved = {"count": 0}

    class FakeVar:
        def get(self):
            return state["value"]

        def set(self, value):
            state["value"] = value

    instance = SimpleNamespace(
        completion_notification_var=FakeVar(),
        config={},
        root=SimpleNamespace(),
        app=SimpleNamespace(save_config=lambda: saved.__setitem__("count", saved["count"] + 1)),
    )
    monkeypatch.setattr(main_window.windows_notifications, "has_notification_identity", lambda: False)
    monkeypatch.setattr(main_window.windows_notifications, "register_notification_identity", lambda: False)
    monkeypatch.setattr(main_window.messagebox, "askyesno", lambda *args, **kwargs: True)
    errors = []
    monkeypatch.setattr(main_window.messagebox, "showerror", lambda *args, **kwargs: errors.append(args))

    main_window.MainWindow._on_completion_notification_change(instance)

    assert state["value"] is False
    assert instance.config["enable_completion_notification"] is False
    assert saved["count"] == 1
    assert errors
