# ui/dict_editor.py
import tkinter as tk
from tkinter import ttk, messagebox
import csv
import os
import logging
from core.utils import file_system, text_processing
# --- 修改：导入基础字典路径常量和表头 ---
from core.utils.dictionary_manager import (
    BASE_CHARACTER_DICT_PATH, BASE_ENTITY_DICT_PATH,
    BASE_CHARACTER_HEADERS, BASE_ENTITY_HEADERS
)
from core.config import DEFAULT_WORLD_DICT_CONFIG

log = logging.getLogger(__name__)

class DictEditorWindow(tk.Toplevel):
    """世界观字典编辑器窗口 (支持人物和事物词典 Tab)。"""

    def __init__(self, parent, app_controller, works_dir, game_path=None, is_base_dict=False): # <--- 修改签名
        """
        初始化字典编辑器窗口。

        Args:
            parent (tk.Widget): 父窗口。
            app_controller (RPGTranslatorApp): 应用控制器实例。
            works_dir (str): Works 目录根路径 (仅当编辑游戏特定字典时相关)。
            game_path (str, optional): 当前游戏路径。如果 is_base_dict=True，则可为 None。
            is_base_dict (bool, optional): 如果为 True，则编辑基础字典。默认为 False。
        """
        super().__init__(parent)
        self.app = app_controller
        self.is_base_dict = is_base_dict
        self.game_path = game_path # 保存游戏路径，供“应用基础字典”按钮使用

        window_title_prefix = "基础字典编辑器" if self.is_base_dict else f"游戏字典编辑器 - {text_processing.sanitize_filename(os.path.basename(game_path or '')) or '未命名游戏'}"
        self.title(window_title_prefix)
        self.geometry("1100x700")
        self.transient(parent)
        # self.grab_set()

        if self.is_base_dict:
            self.character_dict_path = BASE_CHARACTER_DICT_PATH
            self.entity_dict_path = BASE_ENTITY_DICT_PATH
            self.work_game_dir = os.path.dirname(BASE_CHARACTER_DICT_PATH) # 用于确保目录存在
            # 对于基础字典，使用预定义的表头
            self.character_headers_override = BASE_CHARACTER_HEADERS
            self.entity_headers_override = BASE_ENTITY_HEADERS
        else:
            if not game_path:
                messagebox.showerror("错误", "编辑游戏特定字典时必须提供游戏路径。", parent=self)
                self.destroy()
                return
            game_folder_name = text_processing.sanitize_filename(os.path.basename(game_path))
            if not game_folder_name: game_folder_name = "UntitledGame"
            self.work_game_dir = os.path.join(works_dir, game_folder_name)

            world_dict_config = self.app.config.get('world_dict_config', DEFAULT_WORLD_DICT_CONFIG)
            char_filename = world_dict_config.get("character_dict_filename", DEFAULT_WORLD_DICT_CONFIG["character_dict_filename"])
            entity_filename = world_dict_config.get("entity_dict_filename", DEFAULT_WORLD_DICT_CONFIG["entity_dict_filename"])
            self.character_dict_path = os.path.join(self.work_game_dir, char_filename)
            self.entity_dict_path = os.path.join(self.work_game_dir, entity_filename)
            # 对于游戏特定字典，使用原有的表头定义
            self.character_headers_override = ['原文', '译文', '对应原名', '性别', '年龄', '性格', '口吻', '描述']
            self.entity_headers_override = ['原文', '译文', '类别', '描述']


        self.character_columns = ('original', 'translation', 'main_name', 'gender', 'age', 'personality', 'tone', 'description')
        # self.character_headers (使用 override)
        self.entity_columns = ('original', 'translation', 'category', 'description')
        # self.entity_headers (使用 override)
        
        if not file_system.ensure_dir_exists(self.work_game_dir): # work_game_dir 现在可能是基础字典目录
             messagebox.showerror("错误", f"无法创建工作目录: {self.work_game_dir}", parent=self)
             self.destroy()
             return

        self._edit_widget = None
        self._edit_item_id = None
        self._edit_column_id = None
        self._edit_widget_type = None
        self._editing_table = None

        self.create_widgets()
        self._load_data()

    def create_widgets(self):
        main_frame = ttk.Frame(self, padding="5")
        main_frame.pack(fill=tk.BOTH, expand=True)

        self.notebook = ttk.Notebook(main_frame)
        self.notebook.pack(fill=tk.BOTH, expand=True, pady=(0, 5))

        char_tab = ttk.Frame(self.notebook, padding="5")
        self.notebook.add(char_tab, text='人物词典')
        self._create_table_ui(char_tab, 'character')

        entity_tab = ttk.Frame(self.notebook, padding="5")
        self.notebook.add(entity_tab, text='事物词典')
        self._create_table_ui(entity_tab, 'entity')

        button_frame = ttk.Frame(main_frame)
        button_frame.pack(fill=tk.X, pady=5)

        add_button = ttk.Button(button_frame, text="添加行", command=self._add_row)
        add_button.pack(side=tk.LEFT, padx=5)

        self.delete_button = ttk.Button(button_frame, text="删除选中行", command=self._delete_selected_row, state=tk.DISABLED)
        self.delete_button.pack(side=tk.LEFT, padx=5)

        # --- 修改：保存和关闭按钮放右边，应用按钮（如果显示）放它们左边 ---
        cancel_button = ttk.Button(button_frame, text="关闭", command=self.destroy)
        cancel_button.pack(side=tk.RIGHT, padx=5)

        save_button = ttk.Button(button_frame, text="保存全部", command=self._save_data)
        save_button.pack(side=tk.RIGHT, padx=5)

        # --- 新增：“应用基础字典”按钮，仅在非基础字典编辑时显示 ---
        if not self.is_base_dict:
            self.apply_base_dict_button = ttk.Button(
                button_frame,
                text="应用基础字典",
                command=lambda: self.app.start_task('apply_base_dictionary_manual', game_path=self.game_path)
                # 使用新的任务名区分自动和手动，或者在 app.start_task 中判断来源
                # 这里用 game_path=self.game_path 传递当前游戏路径
            )
            self.apply_base_dict_button.pack(side=tk.RIGHT, padx=10) # 放在保存左边，加点间距

        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)
    
    def _create_table_ui(self, parent_frame, table_type): # <--- 修改：使用 self.character_headers_override
        table_frame = ttk.Frame(parent_frame)
        table_frame.pack(fill=tk.BOTH, expand=True)

        if table_type == 'character':
            columns = self.character_columns
            headers = self.character_headers_override # <--- 修改
            table = ttk.Treeview(
                table_frame,
                columns=columns,
                show='headings',
                selectmode='extended'
            )
            self.character_table = table
        elif table_type == 'entity':
            columns = self.entity_columns
            headers = self.entity_headers_override # <--- 修改
            table = ttk.Treeview(
                table_frame,
                columns=columns,
                show='headings',
                selectmode='extended'
            )
            self.entity_table = table
        else:
            return

        for col, header in zip(columns, headers):
            table.heading(col, text=header)
        # ... (列宽设置保持不变) ...
        if table_type == 'character':
            table.column('original', width=150, anchor='w', stretch=True)
            table.column('translation', width=150, anchor='w', stretch=True)
            table.column('main_name', width=120, anchor='w')
            table.column('gender', width=60, anchor='center')
            table.column('age', width=60, anchor='center')
            table.column('personality', width=100, anchor='w')
            table.column('tone', width=100, anchor='w')
            table.column('description', width=300, anchor='w', stretch=True)
        elif table_type == 'entity':
            table.column('original', width=200, anchor='w', stretch=True)
            table.column('translation', width=200, anchor='w', stretch=True)
            table.column('category', width=100, anchor='center')
            table.column('description', width=400, anchor='w', stretch=True)

        scrollbar_y = ttk.Scrollbar(table_frame, orient="vertical", command=table.yview)
        scrollbar_y.pack(side="right", fill="y")
        scrollbar_x = ttk.Scrollbar(table_frame, orient="horizontal", command=table.xview)
        scrollbar_x.pack(side="bottom", fill="x")
        table.configure(yscrollcommand=scrollbar_y.set, xscrollcommand=scrollbar_x.set)
        table.pack(side="left", fill="both", expand=True)

        table.bind('<Double-1>', self._on_cell_double_click)
        table.bind('<Delete>', self._delete_selected_row)
        table.bind('<ButtonRelease-1>', self._on_selection_change)

    def _get_table_config(self, table_widget):
        if table_widget == self.character_table:
            return {
                "columns": self.character_columns,
                "headers": self.character_headers_override, # <--- 修改
                "path": self.character_dict_path,
                "type": "character"
            }
        elif table_widget == self.entity_table:
            return {
                "columns": self.entity_columns,
                "headers": self.entity_headers_override, # <--- 修改
                "path": self.entity_dict_path,
                "type": "entity"
            }
        else:
            return None
    
    # _create_empty_dict_file, _load_data, _load_single_table, _save_data, _save_single_table,
    # _add_row, _delete_selected_row, _update_delete_button_state, _on_tab_changed,
    # _on_selection_change, _on_cell_double_click, _start_edit, _commit_edit,
    # _cancel_edit, _destroy_edit_widget, _reset_edit_state 方法大部分保持不变，
    # 只需要确保它们使用的是 self.character_headers_override 和 self.entity_headers_override
    # 以及正确的 self.character_dict_path, self.entity_dict_path

    # --- 示例：_create_empty_dict_file 修改 ---
    def _create_empty_dict_file(self, file_path, headers): # headers 参数现在来自 self.xxx_headers_override
        """如果字典文件不存在，则创建一个带指定表头的空文件。"""
        try:
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, 'w', encoding='utf-8-sig', newline='') as f:
                writer = csv.writer(f, quoting=csv.QUOTE_ALL)
                writer.writerow(headers) # 使用传入的 headers
            log.info(f"已创建空的字典文件: {file_path}")
            # --- 修改：只有在编辑游戏特定字典时才尝试通知主界面 ---
            if not self.is_base_dict and hasattr(self.app, 'log_message'):
                 self.app.log_message(f"已创建空的字典文件: {os.path.basename(file_path)}", "success")
            return True
        except Exception as e:
            log.exception(f"创建空字典文件失败: {file_path} - {e}")
            messagebox.showerror("错误", f"无法创建字典文件:\n{file_path}\n{e}", parent=self)
            return False

    def _load_single_table(self, table_widget):
         config = self._get_table_config(table_widget)
         if not config: return

         table_path = config["path"]
         headers_to_use = config["headers"] # <--- 获取正确的表头
         num_columns = len(config["columns"])

         for item in table_widget.get_children():
             table_widget.delete(item)

         if not os.path.exists(table_path):
             log.warning(f"字典文件未找到，将创建空文件: {table_path}")
             if not self._create_empty_dict_file(table_path, headers_to_use): # <--- 使用正确的表头创建
                 log.error(f"创建空字典文件 {table_path} 失败，无法加载。")
                 return
         # ... (其余加载逻辑不变，它使用 num_columns 和 config['type'])
         try:
             with open(table_path, 'r', encoding='utf-8-sig', newline='') as f:
                 reader = csv.reader(f, quoting=csv.QUOTE_ALL)
                 try:
                     file_header = next(reader, None)
                     if not file_header or len(file_header) != num_columns:
                         log.warning(f"字典文件 {table_path} 表头无效或列数不为 {num_columns}。将尝试加载。表头: {file_header}")
                 except StopIteration:
                     log.info(f"字典文件 {table_path} 为空或只有表头。")
                     return

                 for i, row in enumerate(reader):
                     if not row: continue
                     row_len = len(row)
                     iid = f"{config['type']}_row_{i}_{os.urandom(4).hex()}"
                     if row_len == num_columns:
                         table_widget.insert('', 'end', values=row, iid=iid)
                     elif row_len > num_columns:
                         log.warning(f"{config['type']} 字典文件中第 {i+1} 行有多于{num_columns}列，将只取前{num_columns}列: {row}")
                         table_widget.insert('', 'end', values=row[:num_columns], iid=iid)
                     else:
                         log.warning(f"{config['type']} 字典文件中第 {i+1} 行少于{num_columns}列，将用空值填充: {row}")
                         padded_row = list(row) + [''] * (num_columns - row_len)
                         table_widget.insert('', 'end', values=padded_row, iid=iid)
             log.info(f"已从 {table_path} 加载数据到 {config['type']} 表格。")
         except FileNotFoundError:
             log.error(f"字典文件未找到 (意外情况): {table_path}")
             messagebox.showerror("加载失败", f"字典文件未找到:\n{table_path}", parent=self)
         except Exception as e:
             log.exception(f"加载字典文件失败: {table_path} - {e}")
             messagebox.showerror("加载失败", f"无法加载字典文件:\n{table_path}\n{e}", parent=self)

    def _save_single_table(self, table_widget):
         config = self._get_table_config(table_widget)
         if not config: return False

         table_path = config["path"]
         headers_to_save = config["headers"] # <--- 获取正确的表头
         num_columns = len(config["columns"])
         data_to_save = [headers_to_save] # <--- 使用正确的表头开始

         for item_id in table_widget.get_children():
             values = table_widget.item(item_id, 'values')
             if isinstance(values, (list, tuple)) and len(values) == num_columns:
                 data_to_save.append(list(values))
             else:
                 log.warning(f"保存 {config['type']} 表格时跳过无效数据行 (ID: {item_id}, Values: {values})")
         try:
             with open(table_path, 'w', encoding='utf-8-sig', newline='') as f:
                 writer = csv.writer(f, quoting=csv.QUOTE_ALL)
                 writer.writerows(data_to_save)
             log.info(f"{config['type']} 字典数据已保存到: {table_path}")
             return True
         except Exception as e:
             log.exception(f"保存 {config['type']} 字典文件失败: {table_path} - {e}")
             messagebox.showerror("保存失败", f"无法保存 {config['type']} 字典文件:\n{table_path}\n{e}", parent=self)
             return False
    # 其他方法基本可以保持不变，它们的操作不直接依赖于表头内容，而是依赖于列的数量和ID。
    def _get_active_table(self):
        """获取当前活动的 Notebook Tab 对应的 Treeview 控件。"""
        try:
            selected_tab_index = self.notebook.index(self.notebook.select())
            if selected_tab_index == 0: # 第一个 Tab 是人物
                return self.character_table
            elif selected_tab_index == 1: # 第二个 Tab 是事物
                return self.entity_table
            else:
                return None
        except tk.TclError: 
            return None

    def _load_data(self):
        """从两个 CSV 文件加载数据并填充到对应的 Treeview 表格中。"""
        self._load_single_table(self.character_table)
        self._load_single_table(self.entity_table)
        self._update_delete_button_state()

    def _save_data(self):
        """将两个 Treeview 表格中的数据分别保存回对应的 CSV 文件。"""
        if self._edit_widget:
            self._commit_edit()
            if self._edit_widget:
                messagebox.showwarning("保存提示", "请先完成或取消当前单元格的编辑。", parent=self)
                return

        char_saved = self._save_single_table(self.character_table)
        entity_saved = self._save_single_table(self.entity_table)

        if char_saved and entity_saved:
            log.info("人物和事物词典均已保存。")
            # --- 修改：只有在编辑游戏特定字典时才尝试通知主界面 ---
            if not self.is_base_dict and hasattr(self.app, 'log_message'):
                self.app.log_message("世界观字典已全部保存。", "success")
        elif char_saved:
            log.warning("仅人物词典已保存，事物词典保存失败。")
            if not self.is_base_dict and hasattr(self.app, 'log_message'):
                 self.app.log_message("人物词典已保存，但事物词典保存失败。", "warning")
        elif entity_saved:
            log.warning("仅事物词典已保存，人物词典保存失败。")
            if not self.is_base_dict and hasattr(self.app, 'log_message'):
                 self.app.log_message("事物词典已保存，但人物词典保存失败。", "warning")
        else:
             log.error("人物和事物词典均保存失败。")

    def _add_row(self):
        """在当前活动的表格末尾添加一个空行。"""
        self._commit_edit()
        active_table = self._get_active_table()
        config = self._get_table_config(active_table)
        if not active_table or not config: return

        num_columns = len(config["columns"])
        empty_values = [''] * num_columns
        new_iid = f"{config['type']}_new_{len(active_table.get_children())}_{os.urandom(4).hex()}"

        new_item_id = active_table.insert('', 'end', values=empty_values, iid=new_iid)
        active_table.selection_set(new_item_id)
        active_table.see(new_item_id)
        self._update_delete_button_state()

    def _delete_selected_row(self, event=None):
        """删除当前活动表格中选中的行。"""
        active_table = self._get_active_table()
        if not active_table: return

        selected_items = active_table.selection()
        if not selected_items:
            return

        if self._editing_table == active_table and self._edit_widget and self._edit_item_id in selected_items:
            self._cancel_edit()

        if messagebox.askyesno("确认删除", f"确定要删除选中的 {len(selected_items)} 行吗？\n(在 '{self.notebook.tab(self.notebook.select(), 'text')}' 表格中)", parent=self):
            for item_id in selected_items:
                try:
                    active_table.delete(item_id)
                except tk.TclError as e:
                    log.warning(f"删除行 {item_id} 失败: {e}")
            self._update_delete_button_state()

    def _update_delete_button_state(self):
        """根据当前活动表格是否有选中项更新删除按钮的状态"""
        active_table = self._get_active_table()
        if active_table and active_table.selection():
            self.delete_button.config(state=tk.NORMAL)
        else:
            self.delete_button.config(state=tk.DISABLED)

    def _on_tab_changed(self, event=None):
        """当 Notebook Tab 切换时调用，主要用于更新按钮状态。"""
        if self._edit_widget:
             log.debug("Tab changed with active edit, attempting to commit.")
             self._commit_edit()
        self._update_delete_button_state()

    def _on_selection_change(self, event=None):
         """当表格选中行改变时，更新删除按钮状态。不提交编辑。"""
         self._update_delete_button_state()

    def _on_cell_double_click(self, event):
        """处理单元格双击事件，启动编辑。"""
        target_table = event.widget
        config = self._get_table_config(target_table)
        if not config: return

        if self._edit_widget:
            self._commit_edit()
            if self._edit_widget:
                 return

        region = target_table.identify_region(event.x, event.y)
        if region != "cell":
            return

        column_id_str = target_table.identify_column(event.x)
        item_id = target_table.identify_row(event.y)

        if not item_id or not column_id_str:
            return

        log.debug(f"Starting edit for item: {item_id}, column: {column_id_str} in {config['type']} table")
        self._start_edit(item_id, column_id_str, target_table)

    def _start_edit(self, item_id, column_id_str, table_widget):
        """在指定的表格、指定的单元格启动编辑。"""
        config = self._get_table_config(table_widget)
        if not config: return

        try:
            if self._edit_widget:
                self._destroy_edit_widget()

            column_index = int(column_id_str.replace('#', '')) - 1
            if not (0 <= column_index < len(config["columns"])):
                log.warning(f"无效的列索引: {column_index}")
                return

            self._edit_column_id = config["columns"][column_index]
            self._edit_item_id = item_id
            self._editing_table = table_widget

            try:
                 bbox = table_widget.bbox(item_id, column=column_id_str)
                 if not bbox:
                     log.warning(f"无法获取单元格边界框: item={item_id}, col={column_id_str}")
                     table_widget.see(item_id)
                     self.update_idletasks()
                     bbox = table_widget.bbox(item_id, column=column_id_str)
                     if not bbox:
                         messagebox.showwarning("提示", "无法编辑当前不可见的单元格。", parent=self)
                         self._reset_edit_state()
                         return
                 x, y, width, height = bbox
            except tk.TclError as e:
                 log.error(f"获取单元格bbox时出错: {e}")
                 self._reset_edit_state()
                 return

            current_value = table_widget.set(item_id, column=self._edit_column_id)

            if self._edit_column_id == 'description':
                self._edit_widget_type = 'text'
                self._edit_widget = tk.Text(
                    table_widget,
                    wrap=tk.WORD, height=3, bd=1, relief=tk.SUNKEN,
                    font=ttk.Style().lookup("TEntry", "font")
                )
                self._edit_widget.insert("1.0", current_value)
                self._edit_widget.bind('<Control-Return>', self._commit_edit)
                self._edit_widget.bind('<Escape>', self._cancel_edit)
                self._edit_widget.bind('<FocusOut>', self._commit_edit)
                actual_height = max(height, self._edit_widget.winfo_reqheight())
                self._edit_widget.place(x=x, y=y, width=width, height=actual_height)

            else:
                self._edit_widget_type = 'entry'
                self._edit_widget = ttk.Entry(table_widget)
                self._edit_widget.insert(0, current_value)
                self._edit_widget.select_range(0, tk.END)
                self._edit_widget.bind('<Return>', self._commit_edit)
                self._edit_widget.bind('<Escape>', self._cancel_edit)
                self._edit_widget.bind('<FocusOut>', self._commit_edit)
                self._edit_widget.place(x=x, y=y, width=width, height=height)

            self._edit_widget.focus_force()
            log.debug(f"Edit widget ({self._edit_widget_type}) placed for table {config['type']}.")

        except Exception as e:
            log.exception(f"启动编辑时发生错误: {e}")
            messagebox.showerror("编辑错误", f"无法开始编辑单元格:\n{e}", parent=self)
            self._reset_edit_state()

    def _commit_edit(self, event=None):
        """将编辑控件中的值写回对应的 Treeview 并销毁编辑控件。"""
        if not self._edit_widget or not self._edit_item_id or not self._edit_column_id or not self._editing_table:
            log.debug("Commit edit called but no active edit widget or state.")
            self._destroy_edit_widget()
            return

        try:
            target_table = self._editing_table
            item_id = self._edit_item_id
            column_id = self._edit_column_id
            widget_type = self._edit_widget_type
            widget = self._edit_widget

            new_value = ""
            if widget_type == 'text':
                new_value = widget.get("1.0", tk.END + "-1c")
            elif widget_type == 'entry':
                new_value = widget.get()
            else:
                 log.warning(f"未知的编辑控件类型: {widget_type}")
                 self._cancel_edit()
                 return "break"

            config = self._get_table_config(target_table)
            log.debug(f"Committing edit to {config['type']}: Item={item_id}, Column={column_id}, Value='{new_value[:50]}...'")

            if not target_table.exists(item_id):
                 log.warning(f"提交编辑失败：行 {item_id} 在 {config['type']} 表格中已不存在。")
                 self._destroy_edit_widget()
                 return "break"

            target_table.set(item_id, column=column_id, value=new_value)

        except tk.TclError as e:
             log.warning(f"提交编辑失败 (可能行已被删除或控件已销毁): {e}")
        except Exception as e:
             log.exception(f"提交编辑时发生未知错误: {e}")
        finally:
            self._destroy_edit_widget()
        return "break"

    def _cancel_edit(self, event=None):
        """销毁编辑控件而不保存更改。"""
        log.debug("Cancelling edit.")
        self._destroy_edit_widget()
        return "break"

    def _destroy_edit_widget(self):
        """安全地销毁编辑控件并重置状态变量。"""
        widget = self._edit_widget
        if widget:
            try:
                try: widget.unbind('<FocusOut>')
                except tk.TclError: pass
                try: widget.unbind('<Return>')
                except tk.TclError: pass
                try: widget.unbind('<Control-Return>')
                except tk.TclError: pass
                try: widget.unbind('<Escape>')
                except tk.TclError: pass
                try: widget.destroy()
                except tk.TclError: pass
            except Exception as e:
                 log.exception(f"销毁编辑控件时发生未知错误: {e}")
        self._reset_edit_state()

    def _reset_edit_state(self):
        """重置编辑相关的状态变量。"""
        self._edit_widget = None
        self._edit_item_id = None
        self._edit_column_id = None
        self._edit_widget_type = None
        self._editing_table = None
        log.debug("Edit state reset.")