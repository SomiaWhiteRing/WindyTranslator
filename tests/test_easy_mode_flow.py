class _TrackingQueue:
    def __init__(self):
        self.has_problem = False
        self.messages = []

    def put(self, message):
        self.messages.append(message)
        if message[0] == "error" or (message[0] == "status" and "失败" in str(message[1])):
            self.has_problem = True


def test_easy_flow_stops_after_first_failed_step(monkeypatch, tmp_path):
    from core.tasks import easy_mode_flow

    calls = []

    def succeed(*_args):
        calls.append("initialize")

    def fail(*args):
        calls.append("export")
        args[-1].put(("status", "导出文本失败"))

    def must_not_run(*_args):
        calls.append("unexpected")

    monkeypatch.setattr(easy_mode_flow.initialize, "run_initialize", succeed)
    monkeypatch.setattr(easy_mode_flow.export, "run_export", fail)
    monkeypatch.setattr(easy_mode_flow.rename, "run_rename", must_not_run)
    queue = _TrackingQueue()

    result = easy_mode_flow.run_easy_flow(
        str(tmp_path),
        str(tmp_path),
        {},
        "932",
        "936",
        {},
        {},
        {},
        queue,
    )

    assert result is False
    assert calls == ["initialize", "export"]
    assert ("status", "轻松模式中止于步骤 2: 导出文本") in queue.messages
