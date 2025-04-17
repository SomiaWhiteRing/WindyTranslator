import tkinter as tk
from tkinter import ttk, messagebox
import csv
import os
import logging
# 确保导入 text_processing 用于清理游戏名
from core.utils import file_system, text_processing
# 导入默认配置以获取默认文件名
from core.config import DEFAULT_WORLD_DICT_CONFIG

log = logging.getLogger(__name__)

class DictEditorWindow(tk.Toplevel):
    """世界观字典编辑器窗口 (支持人物和事物词典 Tab)。"""

    def __init__(self, parent, app_controller, works_dir, game_path):
        """
        初始化字典编辑器窗口。

        Args:
            parent (tk.Widget): 父窗口。
            app_controller (RPGTranslatorApp): 应用控制器实例 (可能用于日志或状态)。
            works_dir (str): Works 目录根路径。
            game_path (str): 当前游戏路径，用于确定字典文件位置。
        """
        super().__init__(parent)
        self.app = app_controller # 保留引用，可能需要记录日志

        # --- 确定字典文件路径 ---
        game_folder_name = text_processing.sanitize_filename(os.path.basename(game_path))
        if not game_folder_name: game_folder_name = "UntitledGame" # 提供默认名
        self.work_game_dir = os.path.join(works_dir, game_folder_name)

        # 从 app 的配置中获取实际文件名，若无则用默认值
        world_dict_config = self.app.config.get('world_dict_config', DEFAULT_WORLD_DICT_CONFIG)
        char_filename = world_dict_config.get("character_dict_filename", DEFAULT_WORLD_DICT_CONFIG["character_dict_filename"])
        entity_filename = world_dict_config.get("entity_dict_filename", DEFAULT_WORLD_DICT_CONFIG["entity_dict_filename"])

        self.character_dict_path = os.path.join(self.work_game_dir, char_filename)
        self.entity_dict_path = os.path.join(self.work_game_dir, entity_filename)

        # 定义列结构
        self.character_columns = ('original', 'translation', 'main_name', 'gender', 'age', 'personality', 'tone', 'description')
        self.character_headers = ['原文', '译文', '对应原名', '性别', '年龄', '性格', '口吻', '描述']
        self.entity_columns = ('original', 'translation', 'category', 'description')
        self.entity_headers = ['原文', '译文', '类别', '描述']


        # --- 窗口设置 ---
        self.title(f"世界观字典编辑器 - {game_folder_name}")
        self.geometry("1100x700") # 设置更大的默认尺寸以容纳双Tab和多列
        self.transient(parent)
        # self.grab_set() # 可选

        # --- 确保目录存在 (文件存在性在加载时检查) ---
        if not file_system.ensure_dir_exists(self.work_game_dir):
             messagebox.showerror("错误", f"无法创建工作目录: {self.work_game_dir}", parent=self)
             self.destroy()
             return

        # --- 状态变量 (移到控件创建前) ---
        self._edit_widget = None # 当前的编辑控件实例 (Entry 或 Text)
        self._edit_item_id = None # 正在编辑的行 ID
        self._edit_column_id = None # 正在编辑的列标识符 ('original', 'translation', etc.)
        self._edit_widget_type = None # 标记当前编辑控件类型 ('entry' or 'text')
        self._editing_table = None # 标记当前正在编辑哪个 Treeview

        # --- 创建控件 ---
        self.create_widgets()

        # --- 加载数据 ---
        # 确保控件创建完毕后再加载
        self._load_data()


    def create_widgets(self):
        """创建窗口中的所有控件。"""
        # --- 主框架 ---
        main_frame = ttk.Frame(self, padding="5")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # --- 创建 Notebook (Tab 控件) ---
        self.notebook = ttk.Notebook(main_frame)
        self.notebook.pack(fill=tk.BOTH, expand=True, pady=(0, 5))

        # --- 创建人物词典 Tab ---
        char_tab = ttk.Frame(self.notebook, padding="5")
        self.notebook.add(char_tab, text='人物词典')
        self._create_table_ui(char_tab, 'character') # 传入标识符 'character'

        # --- 创建事物词典 Tab ---
        entity_tab = ttk.Frame(self.notebook, padding="5")
        self.notebook.add(entity_tab, text='事物词典')
        self._create_table_ui(entity_tab, 'entity') # 传入标识符 'entity'

        # --- 按钮区域 ---
        button_frame = ttk.Frame(main_frame)
        button_frame.pack(fill=tk.X, pady=5)

        # 添加行按钮 (操作当前活动 Tab)
        add_button = ttk.Button(button_frame, text="添加行", command=self._add_row)
        add_button.pack(side=tk.LEFT, padx=5)

        # 删除行按钮 (操作当前活动 Tab)
        self.delete_button = ttk.Button(button_frame, text="删除选中行", command=self._delete_selected_row, state=tk.DISABLED)
        self.delete_button.pack(side=tk.LEFT, padx=5)

        # 保存按钮 (保存所有 Tab 的数据)
        save_button = ttk.Button(button_frame, text="保存全部", command=self._save_data)
        save_button.pack(side=tk.RIGHT, padx=5)

        # 关闭按钮
        cancel_button = ttk.Button(button_frame, text="关闭", command=self.destroy)
        cancel_button.pack(side=tk.RIGHT, padx=5)

        # 绑定 Notebook 切换事件，以便更新删除按钮状态
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)


    def _create_table_ui(self, parent_frame, table_type):
        """在指定的 Frame 中创建 Treeview 表格及其滚动条。"""
        table_frame = ttk.Frame(parent_frame)
        table_frame.pack(fill=tk.BOTH, expand=True)

        if table_type == 'character':
            columns = self.character_columns
            headers = self.character_headers
            table = ttk.Treeview(
                table_frame,
                columns=columns,
                show='headings',
                selectmode='extended' # 允许多选
            )
            self.character_table = table # 存储引用
        elif table_type == 'entity':
            columns = self.entity_columns
            headers = self.entity_headers
            table = ttk.Treeview(
                table_frame,
                columns=columns,
                show='headings',
                selectmode='extended'
            )
            self.entity_table = table # 存储引用
        else:
            return # 未知类型

        # 定义列标题
        for col, header in zip(columns, headers):
            table.heading(col, text=header)

        # 设置列宽和对齐方式 (可以根据需要调整)
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

        # 滚动条
        scrollbar_y = ttk.Scrollbar(table_frame, orient="vertical", command=table.yview)
        scrollbar_y.pack(side="right", fill="y")
        scrollbar_x = ttk.Scrollbar(table_frame, orient="horizontal", command=table.xview)
        scrollbar_x.pack(side="bottom", fill="x")
        table.configure(yscrollcommand=scrollbar_y.set, xscrollcommand=scrollbar_x.set)
        table.pack(side="left", fill="both", expand=True)

        # --- 绑定事件 ---
        table.bind('<Double-1>', self._on_cell_double_click) # 双击编辑
        table.bind('<Delete>', self._delete_selected_row)   # 按 Delete 键删除
        table.bind('<ButtonRelease-1>', self._on_selection_change) # 单击释放时更新状态

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
        except tk.TclError: # 如果没有 Tab 被选中（理论上不应发生）
            return None

    def _get_table_config(self, table_widget):
        """根据 Treeview 控件返回对应的配置（列、表头、文件路径）。"""
        if table_widget == self.character_table:
            return {
                "columns": self.character_columns,
                "headers": self.character_headers,
                "path": self.character_dict_path,
                "type": "character"
            }
        elif table_widget == self.entity_table:
            return {
                "columns": self.entity_columns,
                "headers": self.entity_headers,
                "path": self.entity_dict_path,
                "type": "entity"
            }
        else:
            return None


    def _create_empty_dict_file(self, file_path, headers):
        """如果字典文件不存在，则创建一个带指定表头的空文件。"""
        try:
            # 确保目录存在
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, 'w', encoding='utf-8-sig', newline='') as f:
                writer = csv.writer(f, quoting=csv.QUOTE_ALL)
                writer.writerow(headers) # 写入指定的表头
            log.info(f"已创建空的字典文件: {file_path}")
            if hasattr(self.app, 'log_message'): # 通知主界面
                 self.app.log_message(f"已创建空的字典文件: {os.path.basename(file_path)}", "success")
            return True
        except Exception as e:
            log.exception(f"创建空字典文件失败: {file_path} - {e}")
            messagebox.showerror("错误", f"无法创建字典文件:\n{file_path}\n{e}", parent=self)
            # 不再自动销毁窗口，让调用者决定如何处理
            return False

    def _load_data(self):
        """从两个 CSV 文件加载数据并填充到对应的 Treeview 表格中。"""
        self._load_single_table(self.character_table)
        self._load_single_table(self.entity_table)
        self._update_delete_button_state() # 初始加载后更新按钮状态

    def _load_single_table(self, table_widget):
        """加载单个表格的数据。"""
        config = self._get_table_config(table_widget)
        if not config: return

        table_path = config["path"]
        headers = config["headers"]
        num_columns = len(config["columns"])

        # 清空现有数据
        for item in table_widget.get_children():
            table_widget.delete(item)

        # 检查文件是否存在，不存在则创建
        if not os.path.exists(table_path):
             log.warning(f"字典文件未找到，将创建空文件: {table_path}")
             if not self._create_empty_dict_file(table_path, headers):
                 log.error(f"创建空字典文件 {table_path} 失败，无法加载。")
                 return # 创建失败，无法加载

        # 加载数据
        try:
            with open(table_path, 'r', encoding='utf-8-sig', newline='') as f:
                reader = csv.reader(f, quoting=csv.QUOTE_ALL)
                try:
                    file_header = next(reader, None) # 读取表头
                    # 简单的表头验证（可选）
                    if not file_header or len(file_header) != num_columns:
                         log.warning(f"字典文件 {table_path} 表头无效或列数不为 {num_columns}。将尝试加载。表头: {file_header}")
                         # 即使表头不对，也尝试读取数据行
                except StopIteration: # 文件为空（只有表头或完全为空）
                     log.info(f"字典文件 {table_path} 为空或只有表头。")
                     return # 文件为空，直接返回

                for i, row in enumerate(reader):
                    if not row: continue # 跳过空行

                    row_len = len(row)
                    # 使用 iid 保证唯一性
                    iid = f"{config['type']}_row_{i}_{os.urandom(4).hex()}"

                    if row_len == num_columns:
                        table_widget.insert('', 'end', values=row, iid=iid)
                    elif row_len > num_columns:
                         log.warning(f"{config['type']} 字典文件中第 {i+1} 行有多于{num_columns}列，将只取前{num_columns}列: {row}")
                         table_widget.insert('', 'end', values=row[:num_columns], iid=iid)
                    else: # 列数不足
                        log.warning(f"{config['type']} 字典文件中第 {i+1} 行少于{num_columns}列，将用空值填充: {row}")
                        padded_row = list(row) + [''] * (num_columns - row_len)
                        table_widget.insert('', 'end', values=padded_row, iid=iid)

            log.info(f"已从 {table_path} 加载数据到 {config['type']} 表格。")
        except FileNotFoundError: # 理论上已被处理，但保留以防万一
             log.error(f"字典文件未找到 (意外情况): {table_path}")
             messagebox.showerror("加载失败", f"字典文件未找到:\n{table_path}", parent=self)
        except Exception as e:
            log.exception(f"加载字典文件失败: {table_path} - {e}")
            messagebox.showerror("加载失败", f"无法加载字典文件:\n{table_path}\n{e}", parent=self)


    def _save_data(self):
        """将两个 Treeview 表格中的数据分别保存回对应的 CSV 文件。"""
        # 如果当前有单元格在编辑状态，先强制保存
        if self._edit_widget:
            self._commit_edit() # 尝试提交当前编辑
            # 如果提交后仍然处于编辑状态（例如出错），则不继续保存
            if self._edit_widget:
                messagebox.showwarning("保存提示", "请先完成或取消当前单元格的编辑。", parent=self)
                return

        # 保存人物词典
        char_saved = self._save_single_table(self.character_table)
        # 保存事物词典
        entity_saved = self._save_single_table(self.entity_table)

        if char_saved and entity_saved:
            log.info("人物和事物词典均已保存。")
            if hasattr(self.app, 'log_message'):
                self.app.log_message("世界观字典已全部保存。", "success")
            # messagebox.showinfo("保存成功", "人物和事物词典已成功保存。", parent=self) # 可选的提示
        elif char_saved:
            log.warning("仅人物词典已保存，事物词典保存失败。")
            if hasattr(self.app, 'log_message'):
                 self.app.log_message("人物词典已保存，但事物词典保存失败。", "warning")
        elif entity_saved:
            log.warning("仅事物词典已保存，人物词典保存失败。")
            if hasattr(self.app, 'log_message'):
                 self.app.log_message("事物词典已保存，但人物词典保存失败。", "warning")
        else:
             log.error("人物和事物词典均保存失败。")
             # 错误信息已在 _save_single_table 中弹出

    def _save_single_table(self, table_widget):
        """保存单个表格的数据到其对应的 CSV 文件。"""
        config = self._get_table_config(table_widget)
        if not config: return False

        table_path = config["path"]
        headers = config["headers"]
        num_columns = len(config["columns"])
        data_to_save = [headers] # 添加表头行

        # 获取所有行的数据
        for item_id in table_widget.get_children():
            values = table_widget.item(item_id, 'values')
            # 确保values是列表或元组，并且包含正确数量的元素
            if isinstance(values, (list, tuple)) and len(values) == num_columns:
                 data_to_save.append(list(values)) # 转换为 list
            else:
                 log.warning(f"保存 {config['type']} 表格时跳过无效数据行 (ID: {item_id}, Values: {values})")

        try:
            with open(table_path, 'w', encoding='utf-8-sig', newline='') as f:
                writer = csv.writer(f, quoting=csv.QUOTE_ALL) # 写入时也用 QUOTE_ALL
                writer.writerows(data_to_save)
            log.info(f"{config['type']} 字典数据已保存到: {table_path}")
            return True
        except Exception as e:
            log.exception(f"保存 {config['type']} 字典文件失败: {table_path} - {e}")
            messagebox.showerror("保存失败", f"无法保存 {config['type']} 字典文件:\n{table_path}\n{e}", parent=self)
            return False

    def _add_row(self):
        """在当前活动的表格末尾添加一个空行。"""
        self._commit_edit() # 完成之前的编辑
        active_table = self._get_active_table()
        config = self._get_table_config(active_table)
        if not active_table or not config: return

        num_columns = len(config["columns"])
        empty_values = [''] * num_columns
        # 生成唯一的 iid
        new_iid = f"{config['type']}_new_{len(active_table.get_children())}_{os.urandom(4).hex()}"

        new_item_id = active_table.insert('', 'end', values=empty_values, iid=new_iid)
        active_table.selection_set(new_item_id) # 选中新行
        active_table.see(new_item_id) # 滚动到新行
        self._update_delete_button_state() # 更新删除按钮状态
        # 可以选择性地直接进入第一列的编辑状态
        # self.after(50, lambda: self._start_edit(new_item_id, '#1', active_table))

    def _delete_selected_row(self, event=None):
        """删除当前活动表格中选中的行。"""
        active_table = self._get_active_table()
        if not active_table: return

        selected_items = active_table.selection()
        if not selected_items:
            return

        # 检查是否有正在编辑的行是被选中的行之一 (并且是当前活动表格)
        if self._editing_table == active_table and self._edit_widget and self._edit_item_id in selected_items:
            self._cancel_edit() # 如果正在编辑要删除的行，先取消编辑

        if messagebox.askyesno("确认删除", f"确定要删除选中的 {len(selected_items)} 行吗？\n(在 '{self.notebook.tab(self.notebook.select(), 'text')}' 表格中)", parent=self):
            for item_id in selected_items:
                try:
                    active_table.delete(item_id)
                except tk.TclError as e:
                    log.warning(f"删除行 {item_id} 失败: {e}")
            self._update_delete_button_state() # 删除后更新按钮状态

    def _update_delete_button_state(self):
        """根据当前活动表格是否有选中项更新删除按钮的状态"""
        active_table = self._get_active_table()
        if active_table and active_table.selection():
            self.delete_button.config(state=tk.NORMAL)
        else:
            self.delete_button.config(state=tk.DISABLED)

    def _on_tab_changed(self, event=None):
        """当 Notebook Tab 切换时调用，主要用于更新按钮状态。"""
        # 如果切换 Tab 时有未保存的编辑，可以选择提交、取消或提示用户
        if self._edit_widget:
             # 简单处理：直接提交
             log.debug("Tab changed with active edit, attempting to commit.")
             self._commit_edit()
             # 如果提交后仍有编辑控件（说明提交失败或被取消），最好阻止Tab切换或再次提示？
             # 为简化，暂时不阻止切换，未保存的编辑会丢失（如果commit失败）

        self._update_delete_button_state()

    def _on_selection_change(self, event=None):
         """当表格选中行改变时，更新删除按钮状态。不提交编辑。"""
         self._update_delete_button_state()
         # 焦点管理由 Tkinter 内部处理

    def _on_cell_double_click(self, event):
        """处理单元格双击事件，启动编辑。"""
        target_table = event.widget # 获取事件发生的 Treeview
        config = self._get_table_config(target_table)
        if not config: return

        # 如果当前已经在编辑，先提交或取消
        if self._edit_widget:
            self._commit_edit()
            if self._edit_widget: # 提交失败或取消，不启动新编辑
                 return

        region = target_table.identify_region(event.x, event.y)
        if region != "cell":
            return

        column_id_str = target_table.identify_column(event.x) # 例如 '#1', '#2'
        item_id = target_table.identify_row(event.y)      # 行 ID (iid)

        if not item_id or not column_id_str:
            return

        log.debug(f"Starting edit for item: {item_id}, column: {column_id_str} in {config['type']} table")
        self._start_edit(item_id, column_id_str, target_table)


    def _start_edit(self, item_id, column_id_str, table_widget):
        """在指定的表格、指定的单元格启动编辑。"""
        config = self._get_table_config(table_widget)
        if not config: return

        try:
            # 先取消任何可能存在的旧编辑控件（以防万一状态没清理干净）
            if self._edit_widget:
                self._destroy_edit_widget()

            column_index = int(column_id_str.replace('#', '')) - 1
            if not (0 <= column_index < len(config["columns"])):
                log.warning(f"无效的列索引: {column_index}")
                return

            self._edit_column_id = config["columns"][column_index] # 获取列名
            self._edit_item_id = item_id
            self._editing_table = table_widget # 记录当前编辑的表格

            # 获取单元格边界和当前值
            try:
                 bbox = table_widget.bbox(item_id, column=column_id_str)
                 if not bbox:
                     log.warning(f"无法获取单元格边界框: item={item_id}, col={column_id_str}")
                     table_widget.see(item_id) # 尝试滚动
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

            # --- 根据列选择使用 Entry 或 Text ---
            # 只有 'description' 列使用 Text
            if self._edit_column_id == 'description':
                self._edit_widget_type = 'text'
                self._edit_widget = tk.Text(
                    table_widget, # 父控件是 Treeview
                    wrap=tk.WORD, height=3, bd=1, relief=tk.SUNKEN,
                    font=ttk.Style().lookup("TEntry", "font")
                )
                self._edit_widget.insert("1.0", current_value)
                self._edit_widget.bind('<Control-Return>', self._commit_edit)
                self._edit_widget.bind('<Escape>', self._cancel_edit)
                self._edit_widget.bind('<FocusOut>', self._commit_edit)
                actual_height = max(height, self._edit_widget.winfo_reqheight())
                self._edit_widget.place(x=x, y=y, width=width, height=actual_height)

            else: # 其他所有列都用 Entry
                self._edit_widget_type = 'entry'
                self._edit_widget = ttk.Entry(table_widget)
                self._edit_widget.insert(0, current_value)
                self._edit_widget.select_range(0, tk.END)
                self._edit_widget.bind('<Return>', self._commit_edit)
                self._edit_widget.bind('<Escape>', self._cancel_edit)
                self._edit_widget.bind('<FocusOut>', self._commit_edit)
                self._edit_widget.place(x=x, y=y, width=width, height=height)

            # --- 通用操作 ---
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
            self._destroy_edit_widget() # 确保清理
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

            # 检查 target_table 和 item_id 是否仍然有效
            if not target_table.exists(item_id):
                 log.warning(f"提交编辑失败：行 {item_id} 在 {config['type']} 表格中已不存在。")
                 self._destroy_edit_widget()
                 return "break"

            # 更新 Treeview 中的值
            target_table.set(item_id, column=column_id, value=new_value)

        except tk.TclError as e:
             log.warning(f"提交编辑失败 (可能行已被删除或控件已销毁): {e}")
        except Exception as e:
             log.exception(f"提交编辑时发生未知错误: {e}")
        finally:
            # 无论成功与否，都销毁编辑控件并重置状态
            # 不要在 finally 中访问可能已销毁的控件变量
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
                # 尝试解除绑定，如果控件已销毁会报错，忽略 TclError
                try: widget.unbind('<FocusOut>')
                except tk.TclError: pass
                try: widget.unbind('<Return>')
                except tk.TclError: pass
                try: widget.unbind('<Control-Return>')
                except tk.TclError: pass
                try: widget.unbind('<Escape>')
                except tk.TclError: pass

                # 销毁控件，如果已销毁会报错，忽略 TclError
                try: widget.destroy()
                except tk.TclError: pass

            except Exception as e:
                 log.exception(f"销毁编辑控件时发生未知错误: {e}")

        # 总是重置状态
        self._reset_edit_state()


    def _reset_edit_state(self):
        """重置编辑相关的状态变量。"""
        self._edit_widget = None
        self._edit_item_id = None
        self._edit_column_id = None
        self._edit_widget_type = None
        self._editing_table = None # 重置正在编辑的表格
        log.debug("Edit state reset.")