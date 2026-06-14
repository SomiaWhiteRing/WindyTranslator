import html
import logging
import os
import platform
import sys

log = logging.getLogger(__name__)

APP_ID = "WindyTranslator.RPGRewriterOwnuse"
SHORTCUT_NAME = "WindyTranslator.lnk"


def is_windows_notification_supported():
    return platform.system() == "Windows"


def has_notification_identity():
    if not is_windows_notification_supported():
        return False
    return _get_shortcut_app_id(_get_shortcut_path()) == APP_ID


def register_notification_identity():
    """Register or refresh the desktop notification identity."""
    if not is_windows_notification_supported():
        return False
    try:
        _ensure_app_user_model_id()
        if has_notification_identity():
            return True
        if not _ensure_start_menu_shortcut():
            return False
        return has_notification_identity()
    except Exception as exc:
        log.debug("注册 Windows 任务中心身份失败: %s", exc, exc_info=True)
        return False


def send_task_notification(problem=False):
    """Send a Windows toast notification for a completed task."""
    if not is_windows_notification_supported():
        return False

    title = "当前任务出现问题" if problem else "当前任务已完成"
    try:
        if not register_notification_identity():
            return False
        _show_toast(title)
        return True
    except Exception as exc:
        log.debug("Windows 任务中心提醒发送失败: %s", exc, exc_info=True)
        return False


def _ensure_app_user_model_id():
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        log.debug("设置 AppUserModelID 失败。", exc_info=True)


def _show_toast(title):
    from winsdk.windows.data.xml.dom import XmlDocument
    from winsdk.windows.ui.notifications import ToastNotification, ToastNotificationManager

    escaped_title = html.escape(title)
    xml = XmlDocument()
    xml.load_xml(
        f"""
<toast>
  <visual>
    <binding template="ToastGeneric">
      <text>{escaped_title}</text>
      <text>WindyTranslator</text>
    </binding>
  </visual>
</toast>
""".strip()
    )

    notifier = ToastNotificationManager.create_toast_notifier(APP_ID)
    notifier.show(ToastNotification(xml))


def _ensure_start_menu_shortcut():
    shortcut_path = _get_shortcut_path()
    target_path, arguments = _get_target_path_and_arguments()
    if not target_path or not os.path.exists(target_path):
        return False

    os.makedirs(os.path.dirname(shortcut_path), exist_ok=True)

    import pythoncom
    from win32com.propsys import propsys, pscon
    from win32com.shell import shell

    link = pythoncom.CoCreateInstance(
        shell.CLSID_ShellLink,
        None,
        pythoncom.CLSCTX_INPROC_SERVER,
        shell.IID_IShellLink,
    )
    persist_file = link.QueryInterface(pythoncom.IID_IPersistFile)
    if os.path.exists(shortcut_path):
        persist_file.Load(shortcut_path)

    link.SetPath(target_path)
    link.SetWorkingDirectory(os.path.dirname(target_path))
    if arguments:
        link.SetArguments(arguments)

    property_store = link.QueryInterface(propsys.IID_IPropertyStore)
    property_store.SetValue(pscon.PKEY_AppUserModel_ID, propsys.PROPVARIANTType(APP_ID))
    property_store.Commit()

    persist_file.Save(shortcut_path, 0)
    return os.path.exists(shortcut_path)


def _get_shortcut_app_id(shortcut_path):
    if not os.path.exists(shortcut_path):
        return None

    try:
        import pythoncom
        from win32com.propsys import propsys, pscon
        from win32com.shell import shell

        link = pythoncom.CoCreateInstance(
            shell.CLSID_ShellLink,
            None,
            pythoncom.CLSCTX_INPROC_SERVER,
            shell.IID_IShellLink,
        )
        persist_file = link.QueryInterface(pythoncom.IID_IPersistFile)
        persist_file.Load(shortcut_path)

        property_store = link.QueryInterface(propsys.IID_IPropertyStore)
        value = property_store.GetValue(pscon.PKEY_AppUserModel_ID)
        return value.GetValue()
    except Exception:
        log.debug("读取 Windows 任务中心身份失败。", exc_info=True)
        return None


def _get_shortcut_path():
    start_menu = os.environ.get("APPDATA")
    if not start_menu:
        start_menu = os.path.expanduser("~\\AppData\\Roaming")
    return os.path.join(
        start_menu,
        "Microsoft",
        "Windows",
        "Start Menu",
        "Programs",
        SHORTCUT_NAME,
    )


def _get_target_path_and_arguments():
    if getattr(sys, "frozen", False):
        return sys.executable, ""

    script_path = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if not script_path:
        return sys.executable, ""
    return sys.executable, f'"{script_path}"'
