# app.py
import subprocess
import os
import tkinter as tk
from tkinter import messagebox, filedialog
import queue
import threading
import traceback
import logging
import time
import csv
from concurrent.futures import ThreadPoolExecutor

# 导入 UI 层 (这里先假设 UI 类已定义，后面再实现)
from ui import main_window #, config_dialogs, rtp_dialog, dict_editor # 等

# 导入 Core 层
from core import config as cfg # 重命名避免与 tkinter.config 冲突
from core.tasks import (
    initialize, rename, export, json_creation,
    dict_generation, translate, json_release, import_task,
    easy_mode_flow, apply_base_dictionary
)
from core.utils import (text_processing, dictionary_manager)
from ui.dict_editor import DictEditorWindow
from core.utils.file_system import get_executable_dir  # 导入路径辅助函数
from core.utils.engine_detection import detect_game_engine

log = logging.getLogger(__name__) # 获取 logger 实例

class RPGTranslatorApp:
    """主应用程序类，负责协调 UI 和核心逻辑。"""

    def __init__(self, root):
        """
        初始化应用程序。

        Args:
            root (tk.Tk): Tkinter 的根窗口。
        """
        self.root = root
        self.root.protocol("WM_DELETE_WINDOW", self._on_close) # 绑定关闭事件

        self.executable_dir = get_executable_dir()
        self.works_dir = os.path.join(self.executable_dir, "Works")
        self.config_file_path = os.path.join(self.executable_dir, "app_config.json") # 定义路径
        self.config_manager = cfg.ConfigManager(self.config_file_path) # 实例化管理器
        self.config = self.config_manager.load_config() # 加载配置

        # --- 应用状态 ---
        self.game_path = tk.StringVar()
        self.is_processing = False # 标记是否有后台任务在运行
        self.current_task_thread = None # 引用当前运行的任务线程 (可选)
        self._stop_requested = False # 用于请求停止当前任务 (可选)

        # --- 后台任务处理 ---
        self.message_queue = queue.Queue()
        self.thread_pool = ThreadPoolExecutor(max_workers=1) # 主任务通常只跑一个
        # 新增：用于存储任务ID和对应编辑器实例的映射
        self._editor_callbacks_registry = {}
        self.root.after(100, self._process_messages) # 启动消息循环

        # --- 初始化 UI ---
        # 将 self (App实例) 传递给 MainWindow，以便 UI 调用 App 的方法
        self.main_window = main_window.MainWindow(self.root, self, self.config)
        self._check_and_update_ui_states() # <--- 新增: 初始检查

        # 根据加载的配置设置初始模式和窗口大小
        initial_mode = self.config.get('selected_mode', 'easy')
        self.main_window.switch_to_mode(initial_mode)

        log.info("RPG 翻译助手应用程序已初始化。")
        self.log_message("程序已启动，请选择游戏目录", "normal")

    # --- UI 调用接口 ---

    def browse_game_path(self):
        """弹出目录选择对话框，更新游戏路径。"""
        path = filedialog.askdirectory(title="选择游戏目录", parent=self.root)
        if path:
            detected = detect_game_engine(path)
            if detected:
                self.game_path.set(path)
                self.log_message(f"已选择游戏目录: {path} ({detected.engine})")
                self.update_status("游戏目录已选择，可以开始操作。")
                # 清理旧状态？或者在任务开始时清理
                self._check_and_update_ui_states() # <--- 新增: 路径改变后检查按钮状态
            else:
                messagebox.showerror(
                    "路径无效",
                    "选择的目录不是有效的 RPG Maker 2000/2003 或 RPG Maker VX Ace 游戏目录（未找到 RPG_RT.lmt 或 Data/MapInfos.rvdata2）。",
                    parent=self.root,
                )
                self.log_message("选择了无效的游戏目录。", "error")

    def get_game_path(self):
        """获取当前游戏路径。"""
        return self.game_path.get()

    def start_task(self, task_name, mode='pro', game_path=None, task_id_for_callback=None):
        """
        根据任务名称启动相应的后台任务。

        Args:
            task_name (str): 任务的唯一标识符 (例如 'initialize', 'translate', 'easy_flow')。
            mode (str): 当前的操作模式 ('easy' 或 'pro')，用于更新 UI 状态。
            game_path (str, optional): 从外部（如DictEditor）传递的游戏路径。
                                      如果为 None，则使用 self.get_game_path()。
            task_id_for_callback (str, optional): 用于回调的唯一任务 ID，通常用于编辑器实例。
        """
        if self.is_processing:
            self.log_message("请等待当前操作完成。", "error")
            parent_window_for_msg = self.root
            # 如果此任务是由编辑器发起的，错误提示应相对于编辑器
            if task_id_for_callback and task_id_for_callback in self._editor_callbacks_registry:
                 editor = self._editor_callbacks_registry.get(task_id_for_callback)
                 if editor and editor.winfo_exists():
                     parent_window_for_msg = editor
            messagebox.showwarning("操作繁忙", "请等待当前操作完成后再试。", parent=parent_window_for_msg)
            # 如果是从编辑器调用的，并且操作被阻止，需要通知编辑器（可选，但更好）
            if task_id_for_callback and task_id_for_callback in self._editor_callbacks_registry:
                editor = self._editor_callbacks_registry.pop(task_id_for_callback) # 移除，因为任务未启动
                if editor and editor.winfo_exists():
                    editor.handle_apply_base_dict_result(False, "操作繁忙，任务未启动。")
            return

        # --- 修改：优先使用传入的 game_path ---
        current_game_path = game_path if game_path is not None else self.get_game_path()

        # --- 修改：检查路径时使用 current_game_path ---
        if not current_game_path and task_name not in [
            'configure_gemini', 'configure_deepseek', 'select_rtp',
            # 'open_base_dict_editor' 是直接方法调用，不在这里处理
        ]:
            if not self._check_game_path_set(current_game_path): return # <--- 传递 current_game_path

        # --- 准备任务参数 ---
        task_func = None
        task_args = []
        task_kwargs = {}

        # 从配置中获取所需参数
        pro_config = self.config.get('pro_mode_settings', {}) # 专业模式的独立配置
        rtp_options = pro_config.get('rtp_options', {'2000': True, '2000en': False, '2003': False, '2003steam': False})
        export_encoding = pro_config.get('export_encoding', '932')
        import_encoding = pro_config.get('import_encoding', '936')
        rewrite_rtp_fix = pro_config.get('rewrite_rtp_fix', True)
        world_dict_config = self.config.get('world_dict_config', {})
        translate_config = self.config.get('translate_config', {})

        # 根据 task_name 选择任务函数和参数
        if task_name == 'initialize':
            task_func = initialize.run_initialize
            task_args = [current_game_path, rtp_options, self.message_queue]
        elif task_name == 'rename':
            task_func = rename.run_rename
            task_args = [current_game_path, self.executable_dir, rewrite_rtp_fix, self.message_queue]
        elif task_name == 'export':
            task_func = export.run_export
            task_args = [current_game_path, export_encoding, self.message_queue]
        elif task_name == 'create_json':
            task_func = json_creation.run_create_json
            task_args = [current_game_path, self.works_dir, self.message_queue]
        elif task_name == 'generate_dictionary':
            task_func = dict_generation.run_generate_dictionary
            task_args = [current_game_path, self.works_dir, world_dict_config, self.message_queue]
        elif task_name == 'translate':
            task_func = translate.run_translate
            task_args = [current_game_path, self.works_dir, translate_config, world_dict_config, self.message_queue]
        elif task_name == 'release_json':
            json_files = self._find_translated_json_files(current_game_path) # <--- 传递 current_game_path
            if not json_files:
                 messagebox.showerror("错误", f"在 Works/{self._get_work_subfolder(current_game_path)}/translated 目录下未找到翻译后的 JSON 文件。", parent=self.root)
                 return
            selected_json_path = None
            if len(json_files) == 1:
                selected_json_path = json_files[0]
            else:
                selected_filename = self.main_window.show_file_selection_dialog(
                    "选择翻译文件",
                    "请选择要导入的翻译 JSON 文件:",
                    [os.path.basename(p) for p in json_files]
                )
                if selected_filename:
                    selected_json_path = os.path.join(self._get_translated_dir(current_game_path), selected_filename) # <--- 传递 current_game_path
                else:
                    self.log_message("取消选择翻译文件。", "warning")
                    return
            if selected_json_path:
                task_func = json_release.run_release_json
                task_args = [current_game_path, self.works_dir, selected_json_path, self.message_queue]
            else:
                return
        elif task_name == 'import':
            task_func = import_task.run_import
            task_args = [current_game_path, import_encoding, self.message_queue]
        elif task_name == 'easy_flow':
             # 轻松模式需要检查 API Key 是否配置
             if not world_dict_config.get("api_key") or not translate_config.get("api_key"):
                 messagebox.showerror("配置缺失", "请先在 字典API 和 翻译API 配置中填写 API Key。", parent=self.root)
                 return

             task_func = easy_mode_flow.run_easy_flow
             task_args = [
                 current_game_path, self.executable_dir, self.works_dir,
                 rtp_options, export_encoding, import_encoding,
                 world_dict_config, translate_config, rewrite_rtp_fix,
                 self.message_queue
             ]
        # --- 新增：手动应用基础字典任务 ---
        elif task_name == 'apply_base_dictionary_manual':
            task_func = apply_base_dictionary.run_apply_base_dictionary
            # task_id_for_callback 将通过 kwargs 传递给任务函数
            if task_id_for_callback:
                task_kwargs['task_id_for_callback'] = task_id_for_callback
            task_args = [current_game_path, self.works_dir, world_dict_config.copy(), self.message_queue]
        elif task_name == 'start_game':
             self._start_game() # 直接调用内部方法，不需后台线程
             return
        elif task_name == 'edit_dictionary':
             self._open_dict_editor() # 直接调用内部方法
             return
        
        elif task_name == 'fix_fallback': # 修正回退任务
            fallback_csv_path = self._get_fallback_csv_path() # 获取路径
            translated_json_path = self._get_translated_json_path() # 获取翻译 JSON 路径

            if not fallback_csv_path or not translated_json_path:
                messagebox.showerror("错误", "无法确定修正所需的文件路径 (可能是游戏路径未设置?)。", parent=self.root)
                return

            if self._check_fallback_csv_status(fallback_csv_path): # 检查文件状态
                 from ui.fix_fallback_dialog import FixFallbackDialog # 导入对话框
                 try:
                    FixFallbackDialog(
                        parent=self.root,
                        app_controller=self, # 传递 App 实例
                        fallback_csv_path=fallback_csv_path, # 传递 CSV 路径
                        translated_json_path=translated_json_path # 传递 JSON 路径
                    )
                    self.log_message("修正回退对话框已打开。", "normal")
                 except Exception as e:
                    log.exception("打开修正回退对话框时出错。")
                    messagebox.showerror("错误", f"无法打开修正回退对话框:\n{e}", parent=self.root)
            else:
                 messagebox.showinfo("提示", "没有检测到需要修正的回退项。", parent=self.root)
                 self.log_message("没有需要修正的回退项。", "normal")
            return # 处理完毕，直接返回
        elif task_name == 'configure_gemini':
             self._open_gemini_config() # 直接调用内部方法
             return
        elif task_name == 'configure_deepseek':
             self._open_deepseek_config() # 直接调用内部方法
             return
        elif task_name == 'select_rtp':
             self._open_rtp_selection() # 直接调用内部方法
             return

        else:
            self.log_message(f"未知的任务名称: {task_name}", "error")
            messagebox.showerror("错误", f"无法识别的操作: {task_name}", parent=self.root)
            return

        if task_func:
            self._run_task_in_thread(task_func, task_args, task_kwargs, task_name, mode)

    def save_pro_mode_settings(self, settings):
        """保存专业模式的设置到配置中。由 ProModePanel 调用。"""
        if 'pro_mode_settings' not in self.config:
            self.config['pro_mode_settings'] = {}
        self.config['pro_mode_settings'].update(settings)
        self.save_config() # 保存到文件

    def save_config(self):
        """保存当前应用配置。"""
        try:
            # 更新当前选择的模式
            self.config['selected_mode'] = self.main_window.get_current_mode()
            self.config_manager.save_config(self.config)
            self.log_message("配置已保存。", "success")
        except Exception as e:
            log.exception("保存配置失败。")
            self.log_message(f"保存配置失败: {e}", "error")
            messagebox.showerror("保存失败", f"无法保存配置文件。\n错误: {e}", parent=self.root)

    def log_message(self, message, level="normal"):
        """将消息记录到 UI 日志区域。"""
        if hasattr(self, 'main_window') and self.main_window:
            self.main_window.add_log(message, level)
        else:
            print(f"[{level.upper()}] {message}") # UI 未就绪时的备用方案

    def update_status(self, message):
        """更新 UI 状态栏文本。"""
        if hasattr(self, 'main_window') and self.main_window:
            self.main_window.update_status(message)

    def update_easy_mode_status(self, message):
        """更新轻松模式状态标签。"""
        if hasattr(self, 'main_window') and self.main_window:
            self.main_window.update_easy_status(message)

    def update_easy_mode_progress(self, value):
        """更新轻松模式进度条。"""
        if hasattr(self, 'main_window') and self.main_window:
            self.main_window.update_easy_progress(value)
            
    def set_processing_state(self, processing):
        """设置应用的繁忙状态，并更新 UI 控件的可用性。"""
        self.is_processing = processing
        if hasattr(self, 'main_window') and self.main_window:
             # MainWindow 需要实现 disable_controls/enable_controls 方法
             self.main_window.set_controls_enabled(not processing)

    # --- 内部方法 ---

    def _get_work_subfolder(self, game_path_override=None):
        """获取当前游戏对应的 Works 子目录名。"""
        current_game_path = game_path_override if game_path_override is not None else self.get_game_path()
        if not current_game_path: return None
        game_folder_name = text_processing.sanitize_filename(os.path.basename(current_game_path))
        return game_folder_name or "UntitledGame"

    def _get_translated_dir(self, game_path_override=None):
        """获取当前游戏翻译文件存放目录的完整路径。"""
        subfolder = self._get_work_subfolder(game_path_override)
        if not subfolder: return None
        return os.path.join(self.works_dir, subfolder, "translated")
    
    def _get_fallback_csv_path(self, game_path_override=None):
        """获取当前游戏 fallback_corrections.csv 的完整路径。"""
        translated_dir = self._get_translated_dir(game_path_override)
        if not translated_dir: return None
        return os.path.join(translated_dir, "fallback_corrections.csv")

    def _get_translated_json_path(self, game_path_override=None):
        """获取当前游戏 translation_translated.json 的完整路径。"""
        translated_dir = self._get_translated_dir(game_path_override)
        if not translated_dir: return None
        return os.path.join(translated_dir, "translation_translated.json")

    def _check_fallback_csv_status(self, csv_path): # <--- 新增: 检查 CSV 状态
        """检查回退 CSV 文件是否存在且有数据行（非表头）。"""
        if not csv_path or not os.path.exists(csv_path):
            return False # 文件不存在，按钮禁用
        try:
            with open(csv_path, 'r', encoding='utf-8-sig', newline='') as f:
                reader = csv.reader(f)
                header = next(reader, None) # 读取表头
                if header is None:
                    return False # 空文件，禁用
                # 检查是否存在下一行 (数据行)
                first_data_row = next(reader, None)
                return first_data_row is not None # 如果能读到数据行，则返回 True (启用)
        except StopIteration: # 只有表头，没有数据行
            return False # 只有表头，禁用
        except Exception as e:
            log.error(f"检查回退 CSV 文件状态时出错 ({csv_path}): {e}")
            return False # 读取出错，视为禁用

    def _check_and_update_ui_states(self): # <--- 新增: 统一检查和更新 UI
        """检查依赖文件状态的 UI 元素并更新它们。"""
        # 检查回退按钮状态
        fallback_csv_path = self._get_fallback_csv_path()
        enable_fix_button = self._check_fallback_csv_status(fallback_csv_path)
        if hasattr(self, 'main_window') and self.main_window:
            self.main_window.update_fix_fallback_button_state(enable_fix_button)
        # 未来可以添加其他需要根据文件状态更新的 UI 逻辑

    def _find_translated_json_files(self, game_path_override=None):
        """查找当前游戏已翻译的 JSON 文件列表。"""
        translated_dir = self._get_translated_dir(game_path_override)
        if not translated_dir or not os.path.isdir(translated_dir):
            return []
        try:
            return [
                os.path.join(translated_dir, f)
                for f in os.listdir(translated_dir)
                if f.lower().endswith('.json')
            ]
        except OSError as e:
            log.error(f"无法读取翻译目录 {translated_dir}: {e}")
            return []

    def _check_game_path_set(self, path_to_check=None):
        """检查游戏路径是否已设置，如果未设置则显示错误。"""
        current_path = path_to_check if path_to_check is not None else self.get_game_path()
        if not current_path:
            self.log_message("请先选择有效的游戏目录。", "error")
            messagebox.showerror("错误", "请先选择一个有效的 RPG Maker 游戏目录。", parent=self.root)
            return False
        return True

    def _run_task_in_thread(self, task_func, args, kwargs, task_name="后台任务", mode='pro'):
        """在后台线程中运行给定的任务函数。"""
        self.set_processing_state(True)
        self.update_status(f"正在执行: {task_name}...")
        self._stop_requested = False # 重置停止标志

        # 从 kwargs 中提取 task_id_for_callback，以便在异常时使用
        # 实际的 task_id 会通过 kwargs 传递给 task_func
        task_id_from_kwargs_for_exception_handling = kwargs.get('task_id_for_callback')

        def wrapper():
            task_start_time = time.time()
            try:
                task_func(*args, **kwargs)
                # 注意：任务成功完成的消息由任务自身通过队列发送 ("success", ...)
            except Exception as e:
                # 捕获任务函数本身抛出的未处理异常（理论上不应发生）
                log.exception(f"任务 '{task_name}' 执行期间发生未捕获的严重错误。")
                # 发送错误消息到队列
                self.message_queue.put(("error", f"任务 '{task_name}' 失败: {traceback.format_exc()}")) # 发送完整 traceback
                self.message_queue.put(("status", f"{task_name} 执行失败"))
                # --- 如果任务因异常而终止，并且它有关联的回调ID，则尝试通知编辑器 ---
                if task_id_from_kwargs_for_exception_handling and task_id_from_kwargs_for_exception_handling in self._editor_callbacks_registry:
                    editor_to_notify = self._editor_callbacks_registry.pop(task_id_from_kwargs_for_exception_handling, None)
                    if editor_to_notify and editor_to_notify.winfo_exists():
                        error_message_for_callback = f"任务 '{task_name}' 执行时发生意外错误: {e}"
                        log.info(f"任务 {task_id_from_kwargs_for_exception_handling} (异常结束) 将通知编辑器。消息: {error_message_for_callback}")
                        self.root.after(0, lambda w=editor_to_notify, msg=error_message_for_callback: w.handle_apply_base_dict_result(False, msg))
                    elif editor_to_notify: # 编辑器存在但窗口已关闭
                        log.info(f"任务 {task_id_from_kwargs_for_exception_handling} (异常结束) 的编辑器回调被跳过，因为窗口已关闭。")
            finally:
                elapsed = time.time() - task_start_time
                log.info(f"任务 '{task_name}' (ID: {task_id_from_kwargs_for_exception_handling if task_id_from_kwargs_for_exception_handling else 'N/A'}) 线程执行完毕，耗时: {elapsed:.2f} 秒。")
                # 确保发送 'done' 信号，即使任务内部忘记发送或出错中断
                # 但为了避免重复发送，最好还是依赖任务自身发送
                # self.message_queue.put(("done", None))
                # 在主线程中更新状态
                self.root.after(0, lambda: self.set_processing_state(False))
                self.root.after(10, self._check_and_update_ui_states) # <--- 新增: 任务完成后也检查状态
                # 移除对线程的引用
                self.current_task_thread = None

        # 启动线程
        self.current_task_thread = threading.Thread(target=wrapper, daemon=True)
        self.current_task_thread.start()

    def _process_messages(self):
        """处理来自后台任务的消息队列。"""
        try:
            while True: # 处理队列中的所有当前消息
                message = self.message_queue.get_nowait()
                msg_type, content = message
                
                task_id_from_done_signal = None
                callback_success = False
                callback_message = ""
                
                if msg_type == "done":
                    # 检查 content 是否是 (task_id, (success_bool, message_str)) 的格式
                    if isinstance(content, tuple) and len(content) == 2 and isinstance(content[0], str):
                        potential_task_id = content[0]
                        if isinstance(content[1], tuple) and len(content[1]) == 2 and isinstance(content[1][0], bool):
                            task_id_from_done_signal = potential_task_id
                            callback_success, callback_message = content[1]
                            log.info(f"收到带回调ID '{task_id_from_done_signal}' 的 'done' 信号。成功: {callback_success}, 消息: '{callback_message[:100]}...'")
                        else:
                            log.debug(f"收到普通 'done' 信号（content[0]是字符串但内容格式不符回调）: {content}")
                    else:
                        log.debug(f"收到普通 'done' 信号，内容: {content}")
                
                # 现在 task_id_from_done_signal 要么是 None，要么是从 "done" 消息中赋的值
                if task_id_from_done_signal and task_id_from_done_signal in self._editor_callbacks_registry:
                    editor_to_notify = self._editor_callbacks_registry.pop(task_id_from_done_signal, None)
                    if editor_to_notify and editor_to_notify.winfo_exists():
                        log.info(f"为任务 {task_id_from_done_signal} 执行编辑器回调。")
                        self.root.after(0, lambda w=editor_to_notify, s=callback_success, m=callback_message: w.handle_apply_base_dict_result(s, m))
                    elif editor_to_notify:
                        log.info(f"任务 {task_id_from_done_signal} 的编辑器回调被跳过，因为窗口已关闭。")
                    # 注意：如果 "done" 信号是用于回调的，我们可能不希望它再触发下面的通用 "done" 处理逻辑
                    # 所以，这里在处理完回调后，可以考虑跳过后续的 msg_type == "done" 判断
                    # 或者，确保回调的 "done" 信号不会再被后续的通用 "done" 处理。
                    # 当前的结构，如果 task_id_from_done_signal 被赋值了，后续的 elif msg_type == "done" 不会执行。

                elif msg_type == "log":
                    level, text = content
                    self.log_message(text, level)
                elif msg_type == "status":
                    self.update_status(content)
                elif msg_type == "success":
                    self.log_message(content, "success") # 在日志中也显示成功信息
                    # 可以在这里加一个短暂的成功状态显示，然后恢复默认
                    # self.update_status(content)
                    # self.root.after(3000, lambda: self.update_status("就绪"))
                elif msg_type == "error":
                    self.log_message(content, "error")
                    # 可以在状态栏显示错误提示
                    # self.update_status("操作出错，详情请查看日志")
                elif msg_type == "progress": # 特别为轻松模式
                    self.update_easy_mode_progress(content)
                elif msg_type == "easy_status": # 特别为轻松模式
                    self.update_easy_mode_status(content)
                elif msg_type == "done":
                    # 任务完成信号，由任务内部发送
                    # App 层主要用它来判断是否可以启动新任务
                    # self.set_processing_state(False) # 移到线程 wrapper 的 finally 中处理
                    self.log_message("后台任务处理完成。", "normal")
                    # 如果是轻松模式结束，可以显示最终状态
                    current_mode = self.main_window.get_current_mode()
                    if current_mode == 'easy' and not self.is_processing: # 确保是 easy 模式且真的结束了
                        # 检查最后的状态是否包含错误
                        last_status = self.main_window.get_status() # MainWindow 需要提供方法获取当前状态
                        if "失败" in last_status or "中止" in last_status or "错误" in last_status:
                             self.update_easy_mode_status("轻松模式执行完毕（有错误）。")
                        else:
                             self.update_easy_mode_status("轻松模式执行成功！")

                self.message_queue.task_done() # 标记消息处理完成

        except queue.Empty:
            # 队列为空，是正常情况
            pass
        except Exception as e:
            # 处理消息循环本身发生的错误
            log.exception(f"处理消息队列时出错: {e}")
        finally:
            # 无论如何，100ms 后再次检查队列
            self.root.after(100, self._process_messages)

    def start_task_for_editor_callback(self, task_name, game_path, editor_instance):
        """
        启动一个后台任务，并在任务完成后通过回调通知指定的编辑器实例。
        """
        if self.is_processing:
            if editor_instance and editor_instance.winfo_exists():
                messagebox.showwarning("操作繁忙", "请等待当前操作完成后再试。", parent=editor_instance)
                # 直接调用编辑器的回调，告知操作未执行
                editor_instance.handle_apply_base_dict_result(False, "另一个任务正在运行，操作未执行。")
            else:
                messagebox.showwarning("操作繁忙", "请等待当前操作完成后再试。", parent=self.root)
            return

        # 为这个任务和编辑器实例的组合生成一个唯一的ID
        # 使用时间戳确保ID的唯一性，以防编辑器快速关闭再打开
        unique_task_id_for_callback = f"{task_name}_{id(editor_instance)}_{time.time_ns()}"

        # 注册回调：存储 task_id 和 editor_instance 的映射
        self._editor_callbacks_registry[unique_task_id_for_callback] = editor_instance
        log.info(f"已为任务 '{task_name}' (编辑器: {id(editor_instance)}) 注册回调, ID: {unique_task_id_for_callback}")

        # 调用通用的 start_task 方法，并传递这个 unique_id
        self.start_task(
            task_name=task_name,
            game_path=game_path, # 确保 game_path 被使用
            task_id_for_callback=unique_task_id_for_callback # 传递生成的唯一ID
        )
    
    def _start_game(self, game_path_to_start=None):
        """启动游戏。"""
        current_game_path = game_path_to_start if game_path_to_start is not None else self.get_game_path()
        if not self._check_game_path_set(current_game_path): return

        player_exe = os.path.join(current_game_path, "Player.exe") # EasyRPG Player
        rpg_rt_exe = os.path.join(current_game_path, "RPG_RT.exe") # 原版
        vxace_exe = os.path.join(current_game_path, "Game.exe") # VX Ace

        exe_to_run = None
        if os.path.exists(player_exe):
            exe_to_run = player_exe
        elif os.path.exists(vxace_exe):
            exe_to_run = vxace_exe
        elif os.path.exists(rpg_rt_exe):
            exe_to_run = rpg_rt_exe
        else:
            messagebox.showerror("启动失败", f"未在游戏目录中找到 Player.exe、Game.exe 或 RPG_RT.exe。", parent=self.root)
            self.log_message("无法启动游戏：未找到 Player.exe、Game.exe 或 RPG_RT.exe。", "error")
            return

        self.log_message(f"尝试启动游戏: {exe_to_run}")
        try:
            # 使用 subprocess.Popen 在后台启动，设置工作目录为游戏目录
            subprocess.Popen([exe_to_run], cwd=current_game_path)
            self.log_message("游戏已启动（在单独进程中）。", "success")
        except Exception as e:
            log.exception(f"启动游戏失败: {e}")
            messagebox.showerror("启动失败", f"启动游戏时发生错误：\n{e}", parent=self.root)
            self.log_message(f"启动游戏失败: {e}", "error")

    def _open_dict_editor(self, game_path_to_edit=None):
        """打开世界观字典编辑器。"""
        
        current_game_path = game_path_to_edit if game_path_to_edit is not None else self.get_game_path()
        if not self._check_game_path_set(current_game_path): return

        try:
            DictEditorWindow(
                parent=self.root,
                app_controller=self,
                works_dir=self.works_dir,
                game_path=current_game_path, # <--- 使用 current_game_path
                is_base_dict=False # 明确这是编辑游戏特定字典
            )
            self.log_message("世界观字典编辑器已打开。", "normal")
        except Exception as e:
            log.exception("打开字典编辑器时出错。")
            messagebox.showerror("错误", f"无法打开字典编辑器:\n{e}", parent=self.root)
            self.log_message(f"打开字典编辑器失败: {e}", "error")

    def open_base_dict_editor(self, parent_for_editor=None): # 添加可选参数
        """打开基础字典编辑器。"""
        try:
            # 如果没有显式传递父窗口，则使用主窗口
            actual_parent = parent_for_editor if parent_for_editor else self.root

            editor = DictEditorWindow(
                parent=actual_parent, # 使用实际的父窗口
                app_controller=self,
                works_dir=self.works_dir,
                game_path=None,
                is_base_dict=True
            )
            self.log_message("基础字典编辑器已打开。", "normal")
            return editor # 返回创建的窗口实例
        except Exception as e:
            log.exception("打开基础字典编辑器时出错。")
            messagebox.showerror("错误", f"无法打开基础字典编辑器:\n{e}", parent=actual_parent) # 使用 actual_parent
            self.log_message(f"打开基础字典编辑器失败: {e}", "error")
            return None # 打开失败返回 None

    def _open_gemini_config(self):
        """打开 Gemini 配置窗口。"""
        from ui.config_dialogs import WorldDictConfigWindow # 导入配置窗口类
        WorldDictConfigWindow(self.root, self, self.config['world_dict_config'])

    def _open_deepseek_config(self):
        """打开 DeepSeek 配置窗口。"""
        from ui.config_dialogs import TranslateConfigWindow # 导入配置窗口类
        TranslateConfigWindow(self.root, self, self.config['translate_config'])

    def _open_rtp_selection(self):
        """打开 RTP 选择窗口。"""
        from ui.rtp_dialog import RTPSelectionWindow # 导入 RTP 选择窗口类
        # RTP 配置现在存在 config['pro_mode_settings']['rtp_options'] 中
        pro_settings = self.config.setdefault('pro_mode_settings', {})
        rtp_options = pro_settings.setdefault('rtp_options', {'2000': True, '2000en': False, '2003': False, '2003steam': False})
        # 传递配置给窗口，窗口修改后直接更新这个字典
        RTPSelectionWindow(self.root, self, rtp_options)
        # 更新专业模式面板上的按钮文本 (需要 MainWindow 提供方法)
        self.main_window.update_rtp_button_text()


    def _on_close(self):
        """应用程序关闭时的处理。"""
        log.info("应用程序正在关闭...")
        if self.is_processing:
            if messagebox.askyesno("确认退出", "有后台任务正在运行，确定要强制退出吗？", parent=self.root):
                log.warning("用户强制退出，后台任务可能未完成。")
                # 可以尝试更优雅地停止线程，但 Popen 启动的外部进程无法直接停止
                # self._stop_requested = True # 设置停止标志 (需要任务支持)
                # self.thread_pool.shutdown(wait=False) # 不等待线程结束
                self.root.destroy()
            else:
                return # 用户取消退出
        else:
             # 尝试保存最后的配置
             self.save_config()
             self.thread_pool.shutdown(wait=True) # 等待线程池关闭
             self.root.destroy()
