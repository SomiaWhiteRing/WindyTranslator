# app.py
"""应用程序控制器。"""

import subprocess
import os
import queue
import logging
import time
import csv

from PyQt6.QtWidgets import QApplication, QMessageBox, QFileDialog

from core import config as cfg
from core.message_broker import MessageBroker
from core.path_resolver import PathResolver
from core.task_dispatcher import TaskDispatcher
from core.models.enums import TaskName
from core.utils.file_system import get_executable_dir, ensure_dir_exists
from core.utils.engine_detection import detect_game_engine

from ui.signals import TaskSignalBridge, QueuePollerThread
from ui.main_window import MainWindow

log = logging.getLogger(__name__)


class RPGTranslatorApp:
    """应用程序控制器，负责组装组件和协调 UI 与核心逻辑。"""

    def __init__(self) -> None:
        # --- 基础设施 ---
        self.executable_dir = get_executable_dir()
        self.works_dir = os.path.join(self.executable_dir, "Works")
        self.config_file_path = os.path.join(self.executable_dir, "app_config.json")
        ensure_dir_exists(self.works_dir)

        # --- 组件组装 ---
        self.config_manager = cfg.ConfigManager(self.config_file_path)
        self.config = self.config_manager.load_config()
        if not os.path.exists(self.config_file_path):
            try:
                self.config_manager.save_config(self.config)
            except Exception as e:
                log.exception(f"创建默认配置文件失败: {e}")

        self.path_resolver = PathResolver(self.works_dir)

        # --- 应用状态 ---
        self._game_path: str = self.config.get('last_game_path', "")

        # --- 后台任务处理 ---
        self.message_queue: queue.Queue = queue.Queue()
        self.broker = MessageBroker(self.message_queue)
        self.dispatcher = TaskDispatcher(self.broker)
        self.dispatcher.set_on_task_finished(self._on_task_thread_finished)

        # --- Qt 信号桥接 ---
        self._signal_bridge = TaskSignalBridge()
        self._queue_poller = QueuePollerThread(self.message_queue, self._signal_bridge)

        # --- 初始化 UI ---
        self.main_window = MainWindow(self, self.config)
        self._connect_signals()
        self._check_and_update_ui_states()

        initial_mode = self.config.get('selected_mode', 'easy')
        self.main_window.switch_to_mode(initial_mode)

        # --- 启动消息轮询线程 ---
        self._queue_poller.start()

        # --- 恢复上次的游戏路径 ---
        if self._game_path and os.path.isdir(self._game_path):
            detected = detect_game_engine(self._game_path)
            if detected:
                self.main_window.set_game_path_text(self._game_path)
                self._check_and_update_ui_states()
            else:
                self._game_path = ""

        log.info("WindyTranslator 应用程序已初始化。")
        self.log_message("程序已启动，请选择游戏目录", "normal")

    def _connect_signals(self) -> None:
        """将信号桥接器的信号连接到 UI 处理槽。"""
        bridge = self._signal_bridge
        bridge.log_received.connect(self._on_log)
        bridge.status_received.connect(self.main_window.update_status)
        bridge.success_received.connect(lambda text: self.log_message(text, "success"))
        bridge.error_received.connect(lambda text: self.log_message(text, "error"))
        bridge.warning_received.connect(lambda text: self.log_message(text, "warning"))
        bridge.progress_received.connect(self.main_window.update_easy_progress)
        bridge.easy_status_received.connect(self.main_window.update_easy_status)
        bridge.done_received.connect(self._on_done)
        bridge.task_finished.connect(self._on_task_finished_in_main_thread)

    def _on_log(self, level: str, text: str) -> None:
        self.log_message(text, level)

    def _on_done(self, content: object) -> None:
        """处理 done 信号。"""
        # 解析编辑器回调
        task_id_from_done = None
        callback_success = False
        callback_message = ""

        if isinstance(content, tuple) and len(content) == 2 and isinstance(content[0], str):
            potential_task_id = content[0]
            if isinstance(content[1], tuple) and len(content[1]) == 2 and isinstance(content[1][0], bool):
                task_id_from_done = potential_task_id
                callback_success, callback_message = content[1]
                log.info(f"收到带回调ID '{task_id_from_done}' 的 'done' 信号。成功: {callback_success}")

        if task_id_from_done and self.dispatcher.has_editor_callback(task_id_from_done):
            editor = self.dispatcher.pop_editor_callback(task_id_from_done)
            if editor and hasattr(editor, 'handle_apply_base_dict_result'):
                visible = editor.isVisible()
                if visible:
                    log.info(f"为任务 {task_id_from_done} 执行编辑器回调。")
                    editor.handle_apply_base_dict_result(callback_success, callback_message)
        else:
            self.log_message("后台任务处理完成。", "normal")
            current_mode = self.main_window.get_current_mode()
            if current_mode == 'easy' and not self.is_processing:
                last_status = self.main_window.get_status()
                if any(kw in last_status for kw in ("失败", "中止", "错误")):
                    self.main_window.update_easy_status("轻松模式执行完毕（有错误）。")
                else:
                    self.main_window.update_easy_status("轻松模式执行成功！")

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def is_processing(self) -> bool:
        return self.dispatcher.is_processing

    # ------------------------------------------------------------------
    # UI 调用接口
    # ------------------------------------------------------------------

    def browse_game_path(self) -> None:
        """弹出目录选择对话框，更新游戏路径。"""
        dialog = QFileDialog(self.main_window, "选择游戏目录")
        dialog.setFileMode(QFileDialog.FileMode.Directory)
        dialog.setOption(QFileDialog.Option.ShowDirsOnly, True)
        # 设置初始目录为上次选择路径的父目录
        if self._game_path and os.path.isdir(self._game_path):
            dialog.setDirectory(os.path.dirname(self._game_path))
        if not dialog.exec():
            return
        paths = dialog.selectedFiles()
        path = paths[0] if paths else ""
        if path:
            detected = detect_game_engine(path)
            if detected:
                self._game_path = path
                self.main_window.set_game_path_text(path)
                self.log_message(f"已选择游戏目录: {path} ({detected.engine})")
                self.update_status("游戏目录已选择，可以开始操作。")
                self._check_and_update_ui_states()
            else:
                QMessageBox.critical(
                    self.main_window, "路径无效",
                    "选择的目录不是有效的 RPG Maker 2000/2003 或 RPG Maker VX Ace 游戏目录"
                    "（未找到 RPG_RT.lmt 或 Data/MapInfos.rvdata2）。"
                )
                self.log_message("选择了无效的游戏目录。", "error")

    def get_game_path(self) -> str:
        return self._game_path

    def start_task(self, task_name: str, mode: str = 'pro', game_path: str | None = None,
                   task_id_for_callback: str | None = None) -> None:
        """根据任务名称启动相应的后台任务或 UI 动作。"""
        if self.is_processing:
            self._handle_busy_rejection(task_id_for_callback)
            return

        current_game_path = game_path if game_path is not None else self.get_game_path()

        # --- UI 动作 ---
        ui_actions = {
            'start_game': lambda: self._start_game(),
            'edit_dictionary': lambda: self._open_dict_editor(),
            'fix_fallback': lambda: self._handle_fix_fallback(current_game_path),
            'configure_gemini': lambda: self._open_gemini_config(),
            'configure_deepseek': lambda: self._open_deepseek_config(),
            'select_rtp': lambda: self._open_rtp_selection(),
        }
        if task_name in ui_actions:
            if task_name not in ('configure_gemini', 'configure_deepseek', 'select_rtp'):
                if not current_game_path and not self._check_game_path_set(current_game_path):
                    return
            ui_actions[task_name]()
            return

        # --- 后台任务 ---
        if not current_game_path:
            if not self._check_game_path_set(current_game_path):
                return

        task_args, task_kwargs = self._build_task_args(task_name, current_game_path, task_id_for_callback)
        if task_args is None:
            return

        try:
            task_enum = TaskName(task_name)
        except ValueError:
            self.log_message(f"未知的任务名称: {task_name}", "error")
            QMessageBox.critical(self.main_window, "错误", f"无法识别的操作: {task_name}")
            return

        self.set_processing_state(True)
        self.update_status(f"正在执行: {task_name}...")

        success = self.dispatcher.dispatch(task_enum, task_args, task_kwargs)
        if not success:
            self.set_processing_state(False)
            self.log_message(f"任务 '{task_name}' 启动失败。", "error")

    def start_task_for_editor_callback(self, task_name: str, game_path: str | None,
                                       editor_instance: object) -> None:
        """启动后台任务，完成后通过回调通知编辑器实例。"""
        if self.is_processing:
            QMessageBox.warning(self.main_window, "操作繁忙", "请等待当前操作完成后再试。")
            if hasattr(editor_instance, 'handle_apply_base_dict_result'):
                editor_instance.handle_apply_base_dict_result(False, "另一个任务正在运行，操作未执行。")
            return

        unique_task_id = f"{task_name}_{id(editor_instance)}_{time.time_ns()}"
        self.dispatcher.register_editor_callback(unique_task_id, editor_instance)
        log.info(f"已为任务 '{task_name}' 注册回调, ID: {unique_task_id}")

        self.start_task(
            task_name=task_name,
            game_path=game_path,
            task_id_for_callback=unique_task_id,
        )

    def save_pro_mode_settings(self, settings: dict) -> None:
        if 'pro_mode_settings' not in self.config:
            self.config['pro_mode_settings'] = {}
        self.config['pro_mode_settings'].update(settings)
        self.save_config()

    def save_config(self) -> None:
        try:
            self.config['selected_mode'] = self.main_window.get_current_mode()
            self.config['last_game_path'] = self._game_path
            self.config_manager.save_config(self.config)
            self.log_message("配置已保存。", "success")
        except Exception as e:
            log.exception("保存配置失败。")
            self.log_message(f"保存配置失败: {e}", "error")
            QMessageBox.critical(self.main_window, "保存失败", f"无法保存配置文件。\n错误: {e}")

    # ------------------------------------------------------------------
    # UI 更新便捷方法
    # ------------------------------------------------------------------

    def log_message(self, message: str, level: str = "normal") -> None:
        if hasattr(self, 'main_window') and self.main_window:
            self.main_window.add_log(message, level)
        else:
            print(f"[{level.upper()}] {message}")

    def update_status(self, message: str) -> None:
        if hasattr(self, 'main_window') and self.main_window:
            self.main_window.update_status(message)

    def update_easy_mode_status(self, message: str) -> None:
        if hasattr(self, 'main_window') and self.main_window:
            self.main_window.update_easy_status(message)

    def update_easy_mode_progress(self, value: float) -> None:
        if hasattr(self, 'main_window') and self.main_window:
            self.main_window.update_easy_progress(value)

    def set_processing_state(self, processing: bool) -> None:
        if hasattr(self, 'main_window') and self.main_window:
            self.main_window.set_controls_enabled(not processing)

    def _check_and_update_ui_states(self) -> None:
        current_game_path = self.get_game_path()
        if current_game_path:
            fallback_csv_path = self.path_resolver.get_fallback_csv_path(current_game_path)
        else:
            fallback_csv_path = None
        enable_fix = self._check_fallback_csv_status(fallback_csv_path)
        if hasattr(self, 'main_window') and self.main_window:
            self.main_window.update_fix_fallback_button_state(enable_fix)

    # ------------------------------------------------------------------
    # 内部方法：任务参数构建
    # ------------------------------------------------------------------

    def _build_task_args(self, task_name: str, game_path: str,
                         task_id_for_callback: str | None) -> tuple:
        pro_config = self.config.get('pro_mode_settings', {})
        rtp_options = pro_config.get('rtp_options', {'2000': True, '2000en': False, '2003': False, '2003steam': False})
        export_encoding = pro_config.get('export_encoding', '932')
        import_encoding = pro_config.get('import_encoding', '936')
        rewrite_rtp_fix = pro_config.get('rewrite_rtp_fix', True)
        world_dict_config = self.config.get('world_dict_config', {})
        translate_config = self.config.get('translate_config', {})

        args: list = []
        kwargs: dict = {}

        if task_name == 'initialize':
            args = [game_path, rtp_options, self.broker]
        elif task_name == 'rename':
            args = [game_path, self.executable_dir, rewrite_rtp_fix, self.broker]
        elif task_name == 'export':
            args = [game_path, export_encoding, self.broker]
        elif task_name == 'create_json':
            args = [game_path, self.works_dir, self.broker]
        elif task_name == 'generate_dictionary':
            args = [game_path, self.works_dir, world_dict_config, self.broker]
        elif task_name == 'translate':
            args = [game_path, self.works_dir, translate_config, world_dict_config, self.broker]
        elif task_name == 'release_json':
            result = self._resolve_release_json_path(game_path)
            if result is None:
                return None, None
            args = [game_path, self.works_dir, result, self.broker]
        elif task_name == 'import':
            args = [game_path, import_encoding, self.broker]
        elif task_name == 'easy_flow':
            if not world_dict_config.get("api_key") or not translate_config.get("api_key"):
                QMessageBox.critical(
                    self.main_window, "配置缺失",
                    "请先在 字典API 和 翻译API 配置中填写 API Key。"
                )
                return None, None
            args = [
                game_path, self.executable_dir, self.works_dir,
                rtp_options, export_encoding, import_encoding,
                world_dict_config, translate_config, rewrite_rtp_fix,
                self.broker,
            ]
        elif task_name == 'apply_base_dictionary_manual':
            if task_id_for_callback:
                kwargs['task_id_for_callback'] = task_id_for_callback
            args = [game_path, self.works_dir, world_dict_config.copy(), self.broker]
        else:
            return None, None

        return args, kwargs

    def _resolve_release_json_path(self, game_path: str) -> str | None:
        json_files = self.path_resolver.find_translated_json_files(game_path)
        if not json_files:
            subfolder = self.path_resolver.get_work_subfolder(game_path)
            QMessageBox.critical(
                self.main_window, "错误",
                f"在 Works/{subfolder}/translated 目录下未找到翻译后的 JSON 文件。"
            )
            return None

        if len(json_files) == 1:
            return json_files[0]

        selected = self.main_window.show_file_selection_dialog(
            "选择翻译文件",
            "请选择要导入的翻译 JSON 文件:",
            [os.path.basename(p) for p in json_files],
        )
        if selected:
            translated_dir = self.path_resolver.get_translated_dir(game_path)
            return os.path.join(translated_dir, selected)

        self.log_message("取消选择翻译文件。", "warning")
        return None

    # ------------------------------------------------------------------
    # 内部方法：繁忙拒绝
    # ------------------------------------------------------------------

    def _handle_busy_rejection(self, task_id_for_callback: str | None) -> None:
        self.log_message("请等待当前操作完成。", "error")
        if task_id_for_callback and self.dispatcher.has_editor_callback(task_id_for_callback):
            editor = self.dispatcher.pop_editor_callback(task_id_for_callback)
            if editor and hasattr(editor, 'handle_apply_base_dict_result'):
                editor.handle_apply_base_dict_result(False, "操作繁忙，任务未启动。")
                return
        QMessageBox.warning(self.main_window, "操作繁忙", "请等待当前操作完成后再试。")

    # ------------------------------------------------------------------
    # 内部方法：任务线程完成回调
    # ------------------------------------------------------------------

    def _on_task_thread_finished(self) -> None:
        """任务线程结束后的回调（由 TaskDispatcher 在工作线程中调用）。

        通过 TaskSignalBridge.task_finished 信号将 UI 更新调度到主线程。
        Qt 信号的跨线程 emit 是线程安全的，会自动以 QueuedConnection 方式
        投递到接收者所在线程的事件循环。
        """
        self._signal_bridge.task_finished.emit()

    def _on_task_finished_in_main_thread(self) -> None:
        """在主线程中处理任务完成后的 UI 状态恢复。

        由 task_finished 信号触发，保证在 UI 线程中执行。
        """
        self.set_processing_state(False)
        self._check_and_update_ui_states()

    # ------------------------------------------------------------------
    # 内部方法：UI 动作
    # ------------------------------------------------------------------

    def _start_game(self) -> None:
        current_game_path = self.get_game_path()
        if not self._check_game_path_set(current_game_path):
            return

        player_exe = os.path.join(current_game_path, "Player.exe")
        rpg_rt_exe = os.path.join(current_game_path, "RPG_RT.exe")
        vxace_exe = os.path.join(current_game_path, "Game.exe")

        exe_to_run = None
        if os.path.exists(player_exe):
            exe_to_run = player_exe
        elif os.path.exists(vxace_exe):
            exe_to_run = vxace_exe
        elif os.path.exists(rpg_rt_exe):
            exe_to_run = rpg_rt_exe
        else:
            QMessageBox.critical(
                self.main_window, "启动失败",
                "未在游戏目录中找到 Player.exe、Game.exe 或 RPG_RT.exe。"
            )
            self.log_message("无法启动游戏：未找到可执行文件。", "error")
            return

        self.log_message(f"尝试启动游戏: {exe_to_run}")
        try:
            subprocess.Popen([exe_to_run], cwd=current_game_path)
            self.log_message("游戏已启动（在单独进程中）。", "success")
        except Exception as e:
            log.exception(f"启动游戏失败: {e}")
            QMessageBox.critical(self.main_window, "启动失败", f"启动游戏时发生错误：\n{e}")
            self.log_message(f"启动游戏失败: {e}", "error")

    def _open_dict_editor(self) -> None:
        current_game_path = self.get_game_path()
        if not self._check_game_path_set(current_game_path):
            return
        try:
            from ui.dict_editor import DictEditorWindow
            editor = DictEditorWindow(
                parent=self.main_window,
                app_controller=self,
                works_dir=self.works_dir,
                game_path=current_game_path,
                is_base_dict=False,
            )
            editor.show()
            self.log_message("世界观字典编辑器已打开。", "normal")
        except Exception as e:
            log.exception("打开字典编辑器时出错。")
            QMessageBox.critical(self.main_window, "错误", f"无法打开字典编辑器:\n{e}")
            self.log_message(f"打开字典编辑器失败: {e}", "error")

    def open_base_dict_editor(self, parent_for_editor: object = None) -> object | None:
        try:
            from ui.dict_editor import DictEditorWindow
            actual_parent = parent_for_editor if parent_for_editor else self.main_window
            editor = DictEditorWindow(
                parent=actual_parent,
                app_controller=self,
                works_dir=self.works_dir,
                game_path=None,
                is_base_dict=True,
            )
            self.log_message("基础字典编辑器已打开。", "normal")
            return editor
        except Exception as e:
            log.exception("打开基础字典编辑器时出错。")
            QMessageBox.critical(
                parent_for_editor or self.main_window,
                "错误", f"无法打开基础字典编辑器:\n{e}"
            )
            self.log_message(f"打开基础字典编辑器失败: {e}", "error")
            return None

    def _handle_fix_fallback(self, current_game_path: str) -> None:
        if not current_game_path:
            if not self._check_game_path_set(current_game_path):
                return

        fallback_csv_path = self.path_resolver.get_fallback_csv_path(current_game_path)
        translated_json_path = self.path_resolver.get_translated_json_path(current_game_path)

        if not fallback_csv_path or not translated_json_path:
            QMessageBox.critical(self.main_window, "错误", "无法确定修正所需的文件路径。")
            return

        if self._check_fallback_csv_status(fallback_csv_path):
            from ui.fix_fallback_dialog import FixFallbackDialog
            try:
                dialog = FixFallbackDialog(
                    parent=self.main_window,
                    app_controller=self,
                    fallback_csv_path=fallback_csv_path,
                    translated_json_path=translated_json_path,
                )
                dialog.exec()
                self.log_message("修正回退对话框已关闭。", "normal")
            except Exception as e:
                log.exception("打开修正回退对话框时出错。")
                QMessageBox.critical(self.main_window, "错误", f"无法打开修正回退对话框:\n{e}")
        else:
            QMessageBox.information(self.main_window, "提示", "没有检测到需要修正的回退项。")
            self.log_message("没有需要修正的回退项。", "normal")

    def _open_gemini_config(self) -> None:
        from ui.config_dialogs import WorldDictConfigWindow
        dialog = WorldDictConfigWindow(self.main_window, self, self.config['world_dict_config'])
        dialog.exec()

    def _open_deepseek_config(self) -> None:
        from ui.config_dialogs import TranslateConfigWindow
        dialog = TranslateConfigWindow(self.main_window, self, self.config['translate_config'])
        dialog.exec()

    def _open_rtp_selection(self) -> None:
        from ui.rtp_dialog import RTPSelectionWindow
        pro_settings = self.config.setdefault('pro_mode_settings', {})
        rtp_options = pro_settings.setdefault(
            'rtp_options', {'2000': True, '2000en': False, '2003': False, '2003steam': False}
        )
        dialog = RTPSelectionWindow(self.main_window, self, rtp_options)
        dialog.exec()
        self.main_window.update_rtp_button_text()

    # ------------------------------------------------------------------
    # 内部方法：辅助
    # ------------------------------------------------------------------

    def _check_game_path_set(self, path_to_check: str | None = None) -> bool:
        current_path = path_to_check if path_to_check is not None else self.get_game_path()
        if not current_path:
            self.log_message("请先选择有效的游戏目录。", "error")
            QMessageBox.critical(self.main_window, "错误", "请先选择一个有效的 RPG Maker 游戏目录。")
            return False
        return True

    @staticmethod
    def _check_fallback_csv_status(csv_path: str | None) -> bool:
        if not csv_path or not os.path.exists(csv_path):
            return False
        try:
            with open(csv_path, 'r', encoding='utf-8-sig', newline='') as f:
                reader = csv.reader(f)
                header = next(reader, None)
                if header is None:
                    return False
                first_data_row = next(reader, None)
                return first_data_row is not None
        except Exception as e:
            log.error(f"检查回退 CSV 文件状态时出错 ({csv_path}): {e}")
            return False

    def shutdown(self) -> None:
        """应用程序关闭时的清理。"""
        log.info("应用程序正在关闭...")
        if self.is_processing:
            reply = QMessageBox.question(
                self.main_window, "确认退出",
                "有后台任务正在运行，确定要强制退出吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
            log.warning("用户强制退出，后台任务可能未完成。")
        else:
            self.save_config()

        # 停止轮询线程
        self._queue_poller.stop()
        self._queue_poller.wait(2000)
