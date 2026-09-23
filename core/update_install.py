"""Portable Windows update preparation and recoverable file replacement.

Standard library only: shared by the app, build scripts and standalone helper.
No application imports and no UI side effects.
"""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile

MANIFEST = "package-files.json"
PROGRAM = "WindyTranslator.exe"
HELPER = "WindyUpdater.exe"
MAX_UNPACKED = 2 * 1024**3
MAX_FILES = 40000
MAX_MANIFEST = 8 * 1024**2


class InstallError(Exception):
    pass


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def read_json(path, limit=MAX_MANIFEST):
    with Path(path).open("rb") as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise InstallError("更新信息过大，请重新下载安装包。")
    return json.loads(value)


def safe_name(name):
    if not isinstance(name, str) or not name or "\\" in name:
        raise InstallError("安装包包含无效路径。")
    parts = name.split("/")
    if any(not part or part in (".", "..") or part.endswith((" ", "."))
           or re.search(r'[<>:"|?*\x00-\x1f]', part)
           or re.match(r"(?i)^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", part) for part in parts):
        raise InstallError("安装包包含不安全的路径。")
    return name


def managed_name(name):
    safe_name(name)
    if name not in (PROGRAM, HELPER, MANIFEST) and not name.startswith(("_internal/", "tools/")):
        raise InstallError("安装包包含程序范围之外的文件。")
    return name


def contained(root, name):
    root = Path(root).resolve()
    path = root.joinpath(*PurePosixPath(safe_name(name)).parts)
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        if current.exists() or current.is_symlink():
            flags = current.lstat()
            if stat.S_ISLNK(flags.st_mode) or getattr(flags, "st_file_attributes", 0) & 0x400:
                raise InstallError("更新目录包含链接，请移至普通文件夹后重试。")
    if not path.resolve().is_relative_to(root):
        raise InstallError("更新路径超出程序目录。")
    return path


def load_manifest(root):
    data = read_json(contained(root, MANIFEST))
    files = data.get("files")
    if data.get("schemaVersion") != 1 or not isinstance(files, dict) or not 0 < len(files) <= MAX_FILES:
        raise InstallError("当前安装包缺少自动更新信息，请先手动安装完整新版。")
    seen = set()
    total = 0
    for name, item in files.items():
        managed_name(name)
        if name == MANIFEST or name.casefold() in seen or not isinstance(item, dict):
            raise InstallError("安装包文件清单无效。")
        seen.add(name.casefold())
        if type(item.get("size")) is not int or item["size"] < 0 or not re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256", ""))):
            raise InstallError("安装包文件校验信息无效。")
        total += item["size"]
    for name in files:
        parents = PurePosixPath(name).parents
        if any(str(parent).casefold() in seen for parent in parents if str(parent) != "."):
            raise InstallError("安装包文件与目录冲突。")
    if total > MAX_UNPACKED or not {PROGRAM, HELPER, "_internal/build-info.json"}.issubset(files):
        raise InstallError("安装包文件不完整或过大。")
    build = read_json(contained(root, "_internal/build-info.json"), 16384)
    if build.get("autoUpdateProtocol") != 1 or data.get("applicationBuildId") != build.get("applicationBuildId"):
        raise InstallError("此安装包不支持自动更新，请先手动安装完整新版。")
    return data


def verify_files(root, manifest):
    for name, info in manifest["files"].items():
        path = contained(root, name)
        if not path.is_file() or path.stat().st_size != info["size"] or digest(path) != info["sha256"]:
            raise InstallError(f"文件已修改或不完整：{name}。请保留修改后重新安装完整包。")


def preflight(install, staged):
    old, new = load_manifest(install), load_manifest(staged)
    verify_files(install, old)
    verify_files(staged, new)
    old_names = {name.casefold(): name for name in old["files"]}
    for name in new["files"]:
        if name.casefold() in old_names and old_names[name.casefold()] != name:
            raise InstallError("安装包文件路径大小写发生变化，请手动安装。")
        if name not in old["files"] and contained(install, name).exists():
            raise InstallError(f"更新会覆盖额外文件：{name}。请先移走该文件。")
    needed = sum(item["size"] for item in old["files"].values()) + 64 * 1024**2
    if shutil.disk_usage(install).free < needed:
        raise InstallError("磁盘空间不足，请清理空间后重试。")
    return old, new


def extract_package(package_path, staged, build_id, progress):
    with zipfile.ZipFile(package_path) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_FILES * 2:
            raise InstallError("更新包文件数量过多。")
        seen, files, total = set(), [], 0
        for entry in entries:
            # PowerShell Compress-Archive may use backslashes; normalize before validation.
            name = entry.filename.replace("\\", "/").rstrip("/")
            safe_name(name)
            if not name.startswith("WindyTranslator/") and name != "WindyTranslator":
                raise InstallError("请选择 WindyTranslator 完整发行 ZIP。")
            if name.casefold() in seen:
                raise InstallError("更新包包含重复文件。")
            seen.add(name.casefold())
            mode = entry.external_attr >> 16
            if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR) or entry.flag_bits & 1:
                raise InstallError("更新包包含链接或加密文件。")
            if entry.is_dir() or entry.filename.endswith("\\"):
                continue
            relative = managed_name(name.removeprefix("WindyTranslator/"))
            total += entry.file_size
            if total > MAX_UNPACKED or entry.file_size > MAX_UNPACKED:
                raise InstallError("更新包解压后过大。")
            files.append((entry, relative))
        if shutil.disk_usage(staged.parent).free < total * 2 + 128 * 1024**2:
            raise InstallError("磁盘空间不足，请清理空间后重试。")
        done = 0
        for entry, name in files:
            target = contained(staged, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            with archive.open(entry) as source, target.open("xb") as output:
                while chunk := source.read(256 * 1024):
                    written += len(chunk)
                    if written > entry.file_size:
                        raise InstallError("更新包文件长度不符。")
                    output.write(chunk)
                    done += len(chunk)
                    progress(90 + min(7, 7 * done / max(total, 1)), "正在准备新版…")
            if written != entry.file_size:
                raise InstallError("更新包文件不完整。")
    new = load_manifest(staged)
    if new["applicationBuildId"] != build_id or {name for _, name in files} != set(new["files"]) | {MANIFEST}:
        raise InstallError("更新包与网站发布的版本不一致。")
    verify_files(staged, new)


def prepare_update(install, info, build_id, download, progress):
    install = Path(install).resolve()
    if os.name != "nt" or not getattr(sys, "frozen", False):
        raise InstallError("源码运行不支持替换程序，请使用完整 Windows 安装包。")
    old = load_manifest(install)
    verify_files(install, old)
    workspace = Path(tempfile.mkdtemp(prefix=".windy-update-", dir=install))
    try:
        staged = workspace / "staged"
        staged.mkdir()
        package = workspace / "update.zip"
        download(info, package, progress=lambda received, total: progress(80 * received / total, "正在下载更新…"), build_id=build_id,
                 phase=lambda: progress(85, "正在校验更新…"))
        progress(90, "正在准备新版…")
        extract_package(package, staged, info.artifact.build_id, progress)
        preflight(install, staged)
        progress(98, "正在准备重启…")
        helper = workspace / HELPER
        shutil.copy2(contained(install, HELPER), helper)
        task = {"schemaVersion": 1, "install": str(install), "parentPid": os.getpid(),
                "buildId": info.artifact.build_id, "state": "prepared"}
        write_json(workspace / "task.json", task)
        return workspace
    except Exception:
        shutil.rmtree(workspace)
        raise


def read_task(workspace):
    workspace = Path(workspace).absolute()
    if workspace.is_symlink() or workspace.resolve() != workspace:
        raise InstallError("更新临时目录无效。")
    task = read_json(workspace / "task.json", 16384)
    install = Path(task["install"]).resolve()
    if (task.get("schemaVersion") != 1 or workspace.parent != install
            or not workspace.name.startswith(".windy-update-") or type(task.get("parentPid")) is not int):
        raise InstallError("更新任务路径无效。")
    contained(install, workspace.name)
    return task, install


def start_helper(workspace, recover=False):
    read_task(workspace)
    environment = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")
    return subprocess.Popen([str(Path(workspace) / HELPER), str(workspace), *( ["--recover"] if recover else [])],
                            cwd=workspace, env=environment, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)


def process_alive(pid):
    if type(pid) is not int or pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.OpenProcess(0x00100000, False, pid)
    if not handle:
        return ctypes.get_last_error() != 87  # Access denied is not evidence of exit.
    try:
        return kernel.WaitForSingleObject(handle, 0) != 0
    finally:
        kernel.CloseHandle(handle)


def restore(workspace, install):
    journal = read_json(workspace / "journal.json")
    for name, existed in journal["originals"].items():
        managed_name(name)
        target = contained(install, name)
        if existed:
            backup = contained(workspace / "backup", name)
            if not backup.is_file():
                raise InstallError(f"恢复文件缺失：{name}，备份位于 {workspace}")
            # A failed replacement can leave its locked target untouched. Do not
            # require write access to files which already match the backup.
            if target.is_file() and target.stat().st_size == backup.stat().st_size and digest(target) == digest(backup):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, target)
        elif target.exists():
            target.unlink()


def replace_files(workspace, install):
    staged = workspace / "staged"
    old, new = preflight(install, staged)
    originals = {}
    names = sorted(set(old["files"]) | set(new["files"]) | {MANIFEST})
    # Finish every backup before committing the journal or touching installed files.
    for name in names:
        source = contained(install, name)
        originals[name] = source.exists()
        if source.exists():
            target = contained(workspace / "backup", name)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if digest(source) != digest(target):
                raise InstallError("备份校验失败，未替换程序文件。")
    write_json(workspace / "journal.json", {"originals": originals})
    for name in names:
        target = contained(install, name)
        if name in new["files"] or name == MANIFEST:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(contained(staged, name), target)
        elif target.exists():
            target.unlink()
    verify_files(install, new)


def restart(install, workspace, success=False):
    environment = dict(os.environ)
    environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    environment["WINDY_SKIP_UPDATE_CHECK"] = "1"
    if success:
        environment["WINDY_UPDATE_TASK"] = str(workspace)
    else:
        environment.pop("WINDY_UPDATE_TASK", None)
    return subprocess.Popen([str(install / PROGRAM)], cwd=install, env=environment)


def acknowledge_startup(build_id):
    value = os.environ.pop("WINDY_UPDATE_TASK", "")
    if value:
        workspace = Path(value)
        task, install = read_task(workspace)
        if Path(sys.executable).resolve().parent != install or task["buildId"] != build_id:
            raise InstallError("更新重启身份不匹配。")
        write_json(workspace / "started.json", {"buildId": build_id, "pid": os.getpid()})


def pending_update(install):
    """Return a recoverable transaction; never silently boot a mixed installation."""
    for workspace in Path(install).glob(".windy-update-*"):
        if not (workspace / "task.json").exists():
            continue
        task, _ = read_task(workspace)
        if task.get("state") == "complete":
            if not process_alive(task.get("helperPid", task["parentPid"])):
                try:
                    shutil.rmtree(workspace)
                except OSError:
                    pass  # Cleanup must never prevent an otherwise healthy app starting.
        elif task.get("state") not in ("prepared", "cancelled", "restored"):
            return workspace, task
    return None


class InstallationLock:
    """An OS-owned mutex also excludes already-running duplicate installations."""
    def __init__(self, install):
        self.handle = None
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
        self.kernel.CreateMutexW.restype = wintypes.HANDLE
        self.kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        self.kernel.ReleaseMutex.argtypes = (wintypes.HANDLE,)
        self.kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        name = hashlib.sha256(str(Path(install).resolve()).casefold().encode()).hexdigest()
        handle = self.kernel.CreateMutexW(None, False, "Local\\WindyInstall-" + name)
        if not handle:
            raise InstallError("无法锁定程序目录，请稍后重试。")
        if self.kernel.WaitForSingleObject(handle, 0) not in (0, 0x80):
            self.kernel.CloseHandle(handle)
            raise InstallError("该目录中的 WindyTranslator 仍在运行，请先关闭原窗口。")
        self.handle = handle

    def close(self):
        if self.handle:
            self.kernel.ReleaseMutex(self.handle)
            self.kernel.CloseHandle(self.handle)
            self.handle = None
