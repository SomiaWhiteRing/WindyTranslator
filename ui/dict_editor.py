# ui/dict_editor.py
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
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
        self._active_tab_before_disable = None # 新增：记录禁用前的活动tab

        # --- 新增状态标志 ---
        self._is_applying_base_dict = False

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

        self.add_button = ttk.Button(button_frame, text="添加行", command=self._add_row)
        self.add_button.pack(side=tk.LEFT, padx=5)

        self.delete_button = ttk.Button(button_frame, text="删除选中行", command=self._delete_selected_row, state=tk.DISABLED)
        self.delete_button.pack(side=tk.LEFT, padx=5)

        # --- 修改：保存和关闭按钮放右边，应用按钮（如果显示）放它们左边 ---
        self.cancel_button = ttk.Button(button_frame, text="关闭", command=self.destroy)
        self.cancel_button.pack(side=tk.RIGHT, padx=5)

        self.save_button = ttk.Button(button_frame, text="保存全部", command=self._on_apply_base_dictionary_clicked)
        self.save_button.pack(side=tk.RIGHT, padx=5)

        # --- 新增：“应用基础字典”按钮，仅在非基础字典编辑时显示 ---
        if not self.is_base_dict:
            self.apply_base_dict_button = ttk.Button(
                button_frame,
                text="应用基础字典",
                command=self._on_apply_base_dictionary_clicked # 修改命令
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
        """将两个 Treeview 表格中的数据分别保存回对应的 CSV 文件。
        返回 True 如果所有部分都成功保存，否则 False。
        """
        if self._edit_widget:
            self._commit_edit()
            if self._edit_widget: # 检查 commit_edit 是否成功清除了编辑控件
                messagebox.showwarning("保存提示", "请先完成或取消当前单元格的编辑。", parent=self)
                return False # 保存未完成

        char_saved = self._save_single_table(self.character_table)
        entity_saved = self._save_single_table(self.entity_table)

        all_saved = char_saved and entity_saved

        # 日志记录部分保持不变
        if all_saved:
            log.info("人物和事物词典均已保存。")
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
        
        return all_saved # 返回整体保存状态

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
    
    def _on_save_data_clicked(self):
        """当用户点击“保存全部”按钮时调用。"""
        if self._save_data(): # _save_data 现在应返回布尔值
            self.app.log_message("字典数据已手动保存。", "success")
            # 可以选择性地在这里也弹出一个showinfo，如果需要的话
            # messagebox.showinfo("保存成功", "所有字典数据已保存。", parent=self)
        else:
            # _save_data 内部应该已经弹出了错误框
            self.app.log_message("保存字典数据时发生错误或部分失败。", "warning")
    
    def _get_interactive_controls(self):
        """返回编辑器窗口中需要管理的交互控件列表。"""
        controls = [
            self.add_button, self.delete_button,
            self.save_button, self.cancel_button, # 通常不禁用关闭按钮
            self.character_table, self.entity_table,
            self.notebook
        ]
        if hasattr(self, 'apply_base_dict_button') and self.apply_base_dict_button.winfo_exists():
            controls.append(self.apply_base_dict_button)
        return controls
    
    def _set_controls_enabled_for_apply(self, enabled):
        """在应用基础字典期间启用/禁用编辑器控件。"""
        state = tk.NORMAL if enabled else tk.DISABLED
        notebook_tab_state = tk.NORMAL if enabled else 'disabled'

        if not enabled:
            # 禁用前记录当前选中的标签页ID (注意，不是索引)
            try:
                self._active_tab_before_disable = self.notebook.select()
            except tk.TclError:
                self._active_tab_before_disable = None # 如果获取失败

        for control in self._get_interactive_controls():
            if control and hasattr(control, 'winfo_exists') and control.winfo_exists():
                try:
                    if control == self.notebook:
                        for i in range(len(control.tabs())):
                            try:
                                control.tab(i, state=notebook_tab_state)
                            except tk.TclError: pass
                        
                        # --- 新增：重新启用后，如果之前有记录的活动tab，则尝试恢复 ---
                        if enabled and self._active_tab_before_disable:
                            try:
                                # 确保 Notebook 自身是活动的，否则 select 可能无效或行为异常
                                if self.notebook.winfo_exists() and self.notebook.instate(['!disabled']):
                                    self.notebook.select(self._active_tab_before_disable)
                                    log.debug(f"重新选中 Notebook tab: {self._active_tab_before_disable}")
                            except tk.TclError as e:
                                log.warning(f"重新选中 Notebook tab失败: {e}. 当前选中的tab: {self.notebook.select() if self.notebook.winfo_exists() else 'N/A'}")
                                # 如果恢复失败，尝试选中第一个tab作为回退
                                try:
                                    if len(self.notebook.tabs()) > 0:
                                        self.notebook.select(0)
                                except tk.TclError:
                                    pass # 最后的尝试也失败了
                            finally:
                                self._active_tab_before_disable = None # 清理记录
                                
                    elif control == self.character_table or control == self.entity_table:
                        control.configure(takefocus=enabled)
                        # 禁用/启用双击编辑和删除键绑定
                        if enabled:
                            control.bind('<Double-1>', self._on_cell_double_click)
                            control.bind('<Delete>', self._delete_selected_row)
                        else:
                            control.unbind('<Double-1>')
                            control.unbind('<Delete>')
                    else:
                        control.configure(state=state)
                except tk.TclError:
                    pass # 忽略控件可能已销毁的错误
                
    def _on_apply_base_dictionary_clicked(self):
        """处理“应用基础字典”按钮点击事件。"""
        if self.is_base_dict:
            return
        if self._is_applying_base_dict: # 防止重复点击
            messagebox.showinfo("提示", "正在应用基础字典，请稍候。", parent=self)
            return

        if not self._save_data(): # 调用修改后的 _save_data，它现在返回 True/False
            messagebox.showwarning("保存失败", "应用基础字典前未能成功保存当前更改，操作已取消。", parent=self)
            return
        self.app.log_message("应用基础字典前，当前字典已自动保存。", "normal")

        self._is_applying_base_dict = True
        self._set_controls_enabled_for_apply(False) # 禁用控件
        original_title = self.title()
        self.title(f"{original_title} - 正在应用基础字典...") # 修改标题提示

        # 请求 App 启动任务，并传递自身实例用于回调
        self.app.start_task_for_editor_callback( # 调用 App 的新方法
            task_name='apply_base_dictionary_manual',
            game_path=self.game_path,
            editor_instance=self
        )

    def handle_apply_base_dict_result(self, success, message):
        """
        由 App 在 'apply_base_dictionary_manual' 任务完成后调用。
        """
        # 恢复窗口标题
        if self.title().endswith(" - 正在应用基础字典..."):
            self.title(self.title().replace(" - 正在应用基础字典...", ""))

        self._load_data() # 重新加载表格数据以刷新显示

        self._set_controls_enabled_for_apply(True) # 重新启用控件
        self._is_applying_base_dict = False

        # 弹窗提示结果
        if success:
            messagebox.showinfo("操作完成", f"应用基础字典已完成。\n{message}", parent=self)
        else:
            messagebox.showerror("操作失败", f"应用基础字典时发生错误。\n{message}\n请检查主窗口日志获取详细信息。", parent=self)
        
        self.focus_set() # 尝试将焦点带回此窗口
        self.lift()      # 尝试将窗口置于顶层


