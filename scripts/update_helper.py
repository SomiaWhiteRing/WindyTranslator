"""Standalone PyInstaller onefile updater. No dependency on the installed runtime."""
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.update_install import (InstallError, InstallationLock, process_alive, read_json, read_task,
                                 replace_files, restart, restore, write_json)


def run(workspace, recover=False):
    task, install = read_task(workspace)
    state_path = workspace / "task.json"
    def state(value):
        task.update(state=value, helperPid=os.getpid())
        write_json(state_path, task)

    if process_alive(task.get("helperPid", -1)) and task.get("helperPid") != os.getpid():
        raise InstallError("另一更新进程仍在运行。")
    if recover:
        lock = InstallationLock(install)
        try:
            if (workspace / "journal.json").exists():
                restore(workspace, install)
            state("restored")
        finally:
            lock.close()
        restart(install, workspace)
        return
    child = None
    state("waiting")
    write_json(workspace / "ready.json", {"pid": os.getpid()})
    deadline = time.monotonic() + 60
    while process_alive(task["parentPid"]):
        if (workspace / "cancel").exists() or time.monotonic() >= deadline:
            state("cancelled")
            return
        time.sleep(0.1)
    if (workspace / "cancel").exists():
        state("cancelled")
        return
    lock = InstallationLock(install)
    try:
        state("replacing")
        replace_files(workspace, install)
        state("restarting")
        lock.close()
        child = restart(install, workspace, success=True)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise InstallError("新版未能启动，正在恢复旧版。")
            if (workspace / "started.json").exists():
                started = read_json(workspace / "started.json", 16384)
                if started.get("buildId") == task["buildId"] and process_alive(started.get("pid", -1)):
                    state("complete")
                    # The next normal start removes the workspace after this helper exits.
                    import shutil
                    try:
                        for name in ("backup", "staged"):
                            path = workspace / name
                            if path.exists():
                                shutil.rmtree(path)
                        (workspace / "update.zip").unlink(missing_ok=True)
                    except OSError:
                        (workspace / "cleanup.log").write_text(traceback.format_exc(), encoding="utf-8")
                    return
            time.sleep(0.1)
        raise InstallError("新版启动超时，正在恢复旧版。")
    except Exception:
        state("recovering")
        if child is not None and child.poll() is None:
            subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW, timeout=20, check=True)
            child.wait(timeout=20)
        lock.close()
        lock = InstallationLock(install)
        try:
            if (workspace / "journal.json").exists():
                restore(workspace, install)
            state("restored")
        finally:
            lock.close()
        restart(install, workspace)
        raise
    finally:
        lock.close()


if __name__ == "__main__":
    workspace = Path(sys.argv[1]).absolute()
    try:
        run(workspace, "--recover" in sys.argv[2:])
    except Exception as error:
        (workspace / "error.log").write_text(traceback.format_exc(), encoding="utf-8")
        import ctypes
        try:
            restored = read_json(workspace / "task.json", 16384).get("state") == "restored"
        except Exception:
            restored = False
        message = ("更新未完成，已恢复旧版。请稍后重试，或从网站下载完整安装包。"
                   if restored else "更新未完成。请保留下面目录中的备份和日志，联系维护者协助恢复。")
        ctypes.windll.user32.MessageBoxW(None, f"{message}\n\n备份及日志：{workspace}", "WindyTranslator 更新", 0x10)
        sys.exit(1)
