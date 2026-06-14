import queue
from types import SimpleNamespace

import app
from core.utils import windows_notifications
from ui import main_window


def test_task_message_proxy_marks_problem_on_error_message():
    target = queue.Queue()
    proxy = app.TaskMessageProxy(target)

    proxy.put(("log", ("error", "bad")))
    proxy.put(("status", "继续中"))
    proxy.put(("error", "boom"))

    assert proxy.has_problem is True
    assert target.get() == ("log", ("error", "bad"))
    assert target.get() == ("status", "继续中")
    assert target.get() == ("error", "boom")


def test_task_message_proxy_marks_problem_on_failure_status():
    target = queue.Queue()
    proxy = app.TaskMessageProxy(target)

    proxy.put(("log", ("error", "可恢复的内部错误日志")))
    assert proxy.has_problem is False

    proxy.put(("status", "导入文本失败"))

    assert proxy.has_problem is True


def test_should_emit_completion_notification_skips_release_auto_import():
    instance = SimpleNamespace(
        _is_continuous_task=lambda task_name, mode: True,
        _should_auto_import_after_release=lambda: True,
    )

    assert app.RPGTranslatorApp._should_emit_completion_notification(
        instance, "release_json", "pro", True
    ) is False


def test_should_emit_completion_notification_allows_easy_flow_and_pro_step():
    instance = SimpleNamespace(
        _is_continuous_task=lambda task_name, mode: task_name == "easy_flow" and mode == "easy",
        _should_auto_import_after_release=lambda: False,
    )

    assert app.RPGTranslatorApp._should_emit_completion_notification(
        instance, "easy_flow", "easy", True
    ) is True

    instance2 = SimpleNamespace(
        _is_continuous_task=lambda task_name, mode: task_name == "translate" and mode == "pro",
        _should_auto_import_after_release=lambda: False,
    )

    assert app.RPGTranslatorApp._should_emit_completion_notification(
        instance2, "translate", "pro", False
    ) is True


def test_should_emit_completion_notification_skips_when_auto_import_pends():
    instance = SimpleNamespace(
        _is_continuous_task=lambda task_name, mode: task_name == "release_json" and mode == "pro",
        _should_auto_import_after_release=lambda: True,
    )

    assert app.RPGTranslatorApp._should_emit_completion_notification(
        instance, "release_json", "pro", True
    ) is False


def test_has_notification_identity_requires_matching_app_id(tmp_path, monkeypatch):
    shortcut_path = tmp_path / "WindyTranslator.lnk"
    shortcut_path.write_text("placeholder", encoding="utf-8")

    monkeypatch.setattr(windows_notifications, "_get_shortcut_path", lambda: str(shortcut_path))
    monkeypatch.setattr(windows_notifications, "_get_shortcut_app_id", lambda path: "other.app.id")
    monkeypatch.setattr(windows_notifications, "is_windows_notification_supported", lambda: True)

    assert windows_notifications.has_notification_identity() is False


def test_register_notification_identity_skips_shortcut_write_when_identity_exists(monkeypatch):
    write_calls = []

    monkeypatch.setattr(windows_notifications, "is_windows_notification_supported", lambda: True)
    monkeypatch.setattr(windows_notifications, "has_notification_identity", lambda: True)
    monkeypatch.setattr(windows_notifications, "_ensure_app_user_model_id", lambda: None)
    monkeypatch.setattr(
        windows_notifications,
        "_ensure_start_menu_shortcut",
        lambda: write_calls.append(True) or False,
    )

    assert windows_notifications.register_notification_identity() is True
    assert write_calls == []


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
    show_error_calls = []
    monkeypatch.setattr(main_window.messagebox, "showerror", lambda *args, **kwargs: show_error_calls.append(args))

    main_window.MainWindow._on_completion_notification_change(instance)

    assert state["value"] is False
    assert instance.config["enable_completion_notification"] is False
    assert saved["count"] == 1
    assert show_error_calls


def test_maybe_send_completion_notification_skips_when_window_active(monkeypatch):
    sent = []
    instance = SimpleNamespace(
        completion_notifications_enabled=lambda: True,
        _app_window_is_active=lambda: True,
    )

    monkeypatch.setattr(app.windows_notifications, "send_task_notification", lambda problem=False: sent.append(problem) or True)

    app.RPGTranslatorApp._maybe_send_completion_notification(instance, problem=False)

    assert sent == []


def test_maybe_send_completion_notification_sends_when_window_inactive(monkeypatch):
    sent = []
    instance = SimpleNamespace(
        completion_notifications_enabled=lambda: True,
        _app_window_is_active=lambda: False,
    )

    monkeypatch.setattr(app.windows_notifications, "send_task_notification", lambda problem=False: sent.append(problem) or True)

    app.RPGTranslatorApp._maybe_send_completion_notification(instance, problem=True)

    assert sent == [True]
