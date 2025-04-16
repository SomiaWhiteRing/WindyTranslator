# ui/dict_editor.py
import tkinter as tk
from tkinter import ttk, messagebox
import csv
import os
import logging
from core.utils import file_system, text_processing # 需要 text_processing 清理游戏名

log = logging.getLogger(__name__)

class DictEditorWindow(tk.Toplevel):
    """世界观字典编辑器窗口。"""

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
        if not game_folder_name: game_folder_name = "UntitledGame"
        work_game_dir = os.path.join(works_dir, game_folder_name)
        self.dict_csv_path = os.path.join(work_game_dir, "world_dictionary.csv")

        # --- 窗口设置 ---
        self.title(f"世界观字典编辑器 - {game_folder_name}")
        self.geometry("1000x600") # 设置较大的默认尺寸
        self.transient(parent)
        # self.grab_set() # 可选：如果希望在关闭编辑器前不能操作主窗口

        # --- 确保目录和文件存在 ---
        if not file_system.ensure_dir_exists(work_game_dir):
             messagebox.showerror("错误", f"无法创建工作目录: {work_game_dir}", parent=self)
             self.destroy()
             return
        if not os.path.exists(self.dict_csv_path):
            self._create_empty_dict_file()

        # --- 创建控件 ---
        self.create_widgets()

        # --- 加载数据 ---
        self._load_data()

    def create_widgets(self):
        """创建窗口中的所有控件。"""
        # --- 主框架 ---
        main_frame = ttk.Frame(self, padding="5")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # --- 表格区域 ---
        table_frame = ttk.Frame(main_frame)
        table_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 5))

        # Treeview 作为表格
        columns = ('original', 'translation', 'category', 'description')
        self.dict_table = ttk.Treeview(
            table_frame,
            columns=columns,
            show='headings', # 只显示列标题，不显示第一列的树状结构
            selectmode='browse' # 'browse' 单选, 'extended' 多选
        )

        # 定义列标题
        self.dict_table.heading('original', text='原文')
        self.dict_table.heading('translation', text='译文')
        self.dict_table.heading('category', text='类别')
        self.dict_table.heading('description', text='描述')

        # 设置列宽和对齐方式
        self.dict_table.column('original', width=200, anchor='w', stretch=True) # 允许拉伸
        self.dict_table.column('translation', width=200, anchor='w', stretch=True)
        self.dict_table.column('category', width=100, anchor='center', stretch=False) # 类别通常较短
        self.dict_table.column('description', width=400, anchor='w', stretch=True)

        # 垂直滚动条
        scrollbar_y = ttk.Scrollbar(table_frame, orient="vertical", command=self.dict_table.yview)
        scrollbar_y.pack(side="right", fill="y")
        # 水平滚动条 (可选，如果描述可能很长)
        scrollbar_x = ttk.Scrollbar(table_frame, orient="horizontal", command=self.dict_table.xview)
        scrollbar_x.pack(side="bottom", fill="x")

        self.dict_table.configure(yscrollcommand=scrollbar_y.set, xscrollcommand=scrollbar_x.set)
        self.dict_table.pack(side="left", fill="both", expand=True)

        # --- 绑定事件 ---
        self.dict_table.bind('<Double-1>', self._on_cell_double_click) # 双击编辑
        self.dict_table.bind('<Delete>', self._delete_selected_row)   # 按 Delete 键删除
        self.dict_table.bind('<ButtonRelease-1>', self._on_selection_change) # 单击释放时取消编辑状态

        # --- 按钮区域 ---
        button_frame = ttk.Frame(main_frame)
        button_frame.pack(fill=tk.X, pady=5)

        # 添加行按钮
        add_button = ttk.Button(button_frame, text="添加行", command=self._add_row)
        add_button.pack(side=tk.LEFT, padx=5)

        # 删除行按钮
        self.delete_button = ttk.Button(button_frame, text="删除选中行", command=self._delete_selected_row, state=tk.DISABLED)
        self.delete_button.pack(side=tk.LEFT, padx=5)

        # 保存按钮
        save_button = ttk.Button(button_frame, text="保存", command=self._save_data)
        save_button.pack(side=tk.RIGHT, padx=5)

        # 取消/关闭按钮
        cancel_button = ttk.Button(button_frame, text="关闭", command=self.destroy)
        cancel_button.pack(side=tk.RIGHT, padx=5)

        # --- 状态变量 ---
        self._edit_entry = None # 当前的编辑框实例
        self._edit_item_id = None # 正在编辑的行 ID
        self._edit_column_id = None # 正在编辑的列 ID ('original', 'translation', etc.)

    def _create_empty_dict_file(self):
        """如果字典文件不存在，则创建一个带表头的空文件。"""
        try:
            with open(self.dict_csv_path, 'w', encoding='utf-8-sig', newline='') as f:
                writer = csv.writer(f, quoting=csv.QUOTE_ALL)
                writer.writerow(['原文', '译文', '类别', '描述']) # 写入表头
            log.info(f"已创建空的字典文件: {self.dict_csv_path}")
            if hasattr(self.app, 'log_message'): # 通知主界面
                 self.app.log_message(f"已创建空的字典文件: {os.path.basename(self.dict_csv_path)}", "success")
        except Exception as e:
            log.exception(f"创建空字典文件失败: {self.dict_csv_path} - {e}")
            messagebox.showerror("错误", f"无法创建字典文件:\n{e}", parent=self)
            self.destroy() # 创建失败则关闭窗口

    def _load_data(self):
        """从 CSV 文件加载数据并填充到 Treeview 表格中。"""
        # 清空现有数据
        for item in self.dict_table.get_children():
            self.dict_table.delete(item)

        try:
            with open(self.dict_csv_path, 'r', encoding='utf-8-sig', newline='') as f:
                reader = csv.reader(f, quoting=csv.QUOTE_ALL)
                header = next(reader, None) # 读取并跳过表头
                if not header or len(header) != 4:
                     log.warning(f"字典文件 {self.dict_csv_path} 表头无效或列数不为 4。")
                     # 可以尝试继续加载，或者报错

                for row in reader:
                    if len(row) == 4: # 确保行数据完整
                        self.dict_table.insert('', 'end', values=row)
                    else:
                        log.warning(f"跳过字典文件中格式错误的行 (期望4列): {row}")
            log.info(f"已从 {self.dict_csv_path} 加载数据。")
        except FileNotFoundError:
             log.warning(f"字典文件未找到，将创建空文件: {self.dict_csv_path}")
             self._create_empty_dict_file() # 文件中途被删除了？尝试重新创建
        except Exception as e:
            log.exception(f"加载字典文件失败: {self.dict_csv_path} - {e}")
            messagebox.showerror("加载失败", f"无法加载字典文件:\n{e}", parent=self)

    def _save_data(self):
        """将 Treeview 表格中的数据保存回 CSV 文件。"""
        # 如果当前有单元格在编辑状态，先强制保存或取消编辑
        self._commit_edit()

        data_to_save = []
        # 添加表头行
        data_to_save.append(['原文', '译文', '类别', '描述'])

        # 获取所有行的数据
        for item_id in self.dict_table.get_children():
            values = self.dict_table.item(item_id, 'values')
            data_to_save.append(list(values)) # 转换为 list

        try:
            with open(self.dict_csv_path, 'w', encoding='utf-8-sig', newline='') as f:
                writer = csv.writer(f, quoting=csv.QUOTE_ALL) # 写入时也用 QUOTE_ALL 保持一致
                writer.writerows(data_to_save)
            log.info(f"字典数据已保存到: {self.dict_csv_path}")
            if hasattr(self.app, 'log_message'):
                self.app.log_message("世界观字典已保存。", "success")
            messagebox.showinfo("保存成功", "世界观字典已成功保存。", parent=self)
        except Exception as e:
            log.exception(f"保存字典文件失败: {self.dict_csv_path} - {e}")
            messagebox.showerror("保存失败", f"无法保存字典文件:\n{e}", parent=self)

    def _add_row(self):
        """在表格末尾添加一个空行。"""
        self._commit_edit() # 完成之前的编辑
        new_item_id = self.dict_table.insert('', 'end', values=['', '', '', ''])
        self.dict_table.selection_set(new_item_id) # 选中新行
        self.dict_table.see(new_item_id) # 滚动到新行
        # 可以选择性地直接进入第一列的编辑状态
        # self._start_edit(new_item_id, '#1')

    def _delete_selected_row(self, event=None):
        """删除当前选中的行。"""
        selected_items = self.dict_table.selection()
        if not selected_items:
            return

        if messagebox.askyesno("确认删除", f"确定要删除选中的 {len(selected_items)} 行吗？", parent=self):
            self._commit_edit() # 完成编辑
            for item_id in selected_items:
                self.dict_table.delete(item_id)
            self.delete_button.config(state=tk.DISABLED) # 删除后禁用按钮

    def _on_selection_change(self, event=None):
         """当表格选中行改变时，更新删除按钮状态并取消编辑状态。"""
         self._commit_edit() # 如果在编辑，先提交
         if self.dict_table.selection():
             self.delete_button.config(state=tk.NORMAL)
         else:
             self.delete_button.config(state=tk.DISABLED)

    def _on_cell_double_click(self, event):
        """处理单元格双击事件，启动编辑。"""
        self._commit_edit() # 先完成之前的编辑

        region = self.dict_table.identify_region(event.x, event.y)
        if region != "cell":
            return # 没有点在单元格上

        column_id_str = self.dict_table.identify_column(event.x) # 例如 '#1', '#2'
        item_id = self.dict_table.identify_row(event.y)      # 行 ID

        if not item_id or not column_id_str:
            return

        self._start_edit(item_id, column_id_str)

    def _start_edit(self, item_id, column_id_str):
        """在指定的单元格启动编辑。"""
        column_index = int(column_id_str.replace('#', '')) - 1
        column_identifier = self.dict_table['columns'][column_index] # 获取列名 ('original', etc.)

        # 获取单元格边界和当前值
        x, y, width, height = self.dict_table.bbox(item_id, column=column_id_str)
        current_value = self.dict_table.set(item_id, column=column_identifier)

        # 创建 Entry 或 Text (如果需要多行编辑，例如描述)
        if column_identifier == 'description': # 假设描述列需要多行编辑
             # 使用 Text 实现多行编辑会更复杂，需要处理滚动和换行保存
             # 简化：仍然使用 Entry，但可以做得宽一些
             self._edit_entry = ttk.Entry(self.dict_table, width=width//7) # 估计宽度
        else:
             self._edit_entry = ttk.Entry(self.dict_table) #, width=width)

        self._edit_item_id = item_id
        self._edit_column_id = column_identifier # 保存列名

        # 填充当前值并获取焦点
        self._edit_entry.insert(0, current_value)
        self._edit_entry.select_range(0, tk.END)
        self._edit_entry.focus_force()

        # 绑定事件以保存或取消编辑
        self._edit_entry.bind('<Return>', self._commit_edit)      # 回车保存
        self._edit_entry.bind('<Escape>', self._cancel_edit)      # Esc 取消
        self._edit_entry.bind('<FocusOut>', self._commit_edit)     # 失去焦点时保存 (或者取消？看需求)
        # self._edit_entry.bind('<Tab>', self._commit_edit_and_move) # Tab 移动到下一个 (需要实现)

        # 放置编辑框覆盖单元格
        self._edit_entry.place(x=x, y=y, width=width, height=height)

    def _commit_edit(self, event=None):
        """将编辑框中的值写回 Treeview 并销毁编辑框。"""
        if self._edit_entry and self._edit_item_id and self._edit_column_id:
            new_value = self._edit_entry.get()
            # 更新 Treeview 中的值
            self.dict_table.set(self._edit_item_id, column=self._edit_column_id, value=new_value)
            # 销毁编辑框
            self._edit_entry.destroy()
            self._edit_entry = None
            self._edit_item_id = None
            self._edit_column_id = None
        return "break" # 阻止事件继续传播 (例如回车换行)

    def _cancel_edit(self, event=None):
        """销毁编辑框而不保存更改。"""
        if self._edit_entry:
            self._edit_entry.destroy()
            self._edit_entry = None
            self._edit_item_id = None
            self._edit_column_id = None
        return "break"